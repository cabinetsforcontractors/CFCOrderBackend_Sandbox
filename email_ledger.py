"""
email_ledger.py
THE CLEAN DATA LAYER (William architecture ruling 2026-07-18) — SHADOW MODE.

Root problem this replaces: five scanners re-read raw Gmail every cycle and
re-derive state each time, writing straight onto orders — which produced
re-stamping loops, event spam, and the fake-PRO self-reinfection incident.

New shape, exactly as William drew it:

  1. EMAIL LEDGER — every Gmail message is read EXACTLY ONCE (message_id is
     the primary key; second sight = skip). Drafts are never fetched
     (-in:draft at every query); our own automation notifications are
     ledgered as kind='automation' and never extracted. Each row keeps the
     message's facts: kind, order number(s), subject, date, extracted
     tracking/PRO/amount — with the message id as provenance, so every fact
     is traceable to its source email forever.

  2. ORDER FACTS — the "spreadsheet": one row per order, built from the
     ledger (rebuildable from scratch at any time — the ledger is the truth).
     Columns per William: order number | related email subjects/dates |
     tracking number | and the INDICATOR: tracking_email_sent_at.
         tracking empty                      -> not shipped yet
         tracking filled, indicator empty    -> fire the tracking email, once
         tracking filled, indicator set      -> already handled, move on

  SHADOW MODE: this module NEVER writes to the orders table and NEVER sends
  or drafts anything. It ingests, builds facts, and exposes a COMPARE report
  against the live orders table. Divergences prove the design before cutover:
    - facts have tracking, orders don't  -> the 5699-class gap (hand-sent
      tracking the old scanners missed)
    - orders have tracking, facts don't  -> a suspicious stamp (this is
      exactly how the fake-PRO incident would have been caught in minutes)

  Cutover (separate beat, William's word): appliers replace the scanner
  writes; progress engine reads order_facts.

  BODY READING: uses the RECURSIVE part-walker (ghi_inbox._fetch_text) — the
  shallow reader misses bodies nested in multipart/alternative, which the
  first shadow compare caught within minutes of going live (2026-07-18).
"""

import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import PlainTextResponse

from auth import require_admin
from db_helpers import get_db

ledger_router = APIRouter(tags=["email-ledger"])

_PRO_RE = re.compile(r"PRO\s*(?:#|Number)?[:\s]*([A-Z]{0,2}\d{8,10}(?:-\d)?)",
                     re.IGNORECASE)
_UPS_RE = re.compile(r"\b(1Z[0-9A-Z]{10,16})\b")
_AMOUNT_RE = re.compile(r"\$([\d,]+\.?\d*)")
_SUBJECT_ORDER_RE = re.compile(r"#\s?(\d{4,5})\b")
_BODY_ORDER_RE = re.compile(r"\b(\d{4,5})\b")

# ingestion streams: (gmail query WITHOUT time filter, default kind)
# -in:draft on EVERY stream: a draft is an unsent working copy — it must
# never become data (fake-PRO incident, 2026-07-18).
STREAMS = (
    ('in:sent subject:"TRACKING INFO" -in:draft', "tracking_sent"),
    ('in:sent square.link -in:draft', "payment_link"),
    ('from:noreply@messaging.squareup.com subject:"payment received" -in:draft',
     "payment_received"),
    ('from:ghicabinets.com -in:draft', "ghi_email"),
    ('(from:roccabinetry.com OR from:roccabinetrytampa.com OR '
     'from:sent-via.netsuite.com) -in:draft', "supplier_doc"),
    ('(PRO OR tracking OR "has shipped") -in:draft', "tracking_mention"),
)

AUTOMATION_SUBJECT_PREFIXES = (
    "PROGRESS DRAFT READY",
    "APPROVAL DRAFT READY",
    "DISCREPANCY",
    "ALERT!!",
    "GHI EMAIL NEEDS A HUMAN",
)


# =============================================================================
# TABLES
# =============================================================================

def ensure_ledger_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_ledger (
                message_id VARCHAR(120) PRIMARY KEY,
                thread_id VARCHAR(120),
                folder VARCHAR(10),
                from_addr TEXT,
                to_addr TEXT,
                subject TEXT,
                email_date TIMESTAMP WITH TIME ZONE,
                kind VARCHAR(30),
                order_ids TEXT,
                pros TEXT,
                ups TEXT,
                amounts TEXT,
                ignored_reason TEXT,
                processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_facts (
                order_id VARCHAR(20) PRIMARY KEY,
                payment_link_date TIMESTAMP WITH TIME ZONE,
                payment_link_msg VARCHAR(120),
                tracking_value TEXT,
                pro_value VARCHAR(40),
                tracking_msg VARCHAR(120),
                tracking_date TIMESTAMP WITH TIME ZONE,
                tracking_email_sent_at TIMESTAMP WITH TIME ZONE,
                related_subjects TEXT,
                rebuilt_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_ledger_kind
                       ON email_ledger(kind)""")
        conn.commit()


# =============================================================================
# INGESTION (each message exactly once — message_id PK is the law)
# =============================================================================

def _known_order_ids(conn, candidates: List[str]) -> List[str]:
    if not candidates:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT order_id FROM orders WHERE order_id = ANY(%s)",
                    (list(set(candidates)),))
        return sorted({str(r[0]) for r in cur.fetchall()})


def _classify_orders(conn, subject: str, body: str) -> List[str]:
    """Order attribution: subject '#5699' patterns are trusted; body numbers
    only count when they match a REAL order id in the DB."""
    subj_hits = _SUBJECT_ORDER_RE.findall(subject or "")
    if subj_hits:
        return _known_order_ids(conn, subj_hits) or sorted(set(subj_hits))
    return _known_order_ids(conn, _BODY_ORDER_RE.findall(
        f"{subject} {body}"[:6000]))


def _parse_email_date(s: str):
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s) if s else None
    except Exception:
        return None


def _fetch_message(mid: str):
    """Headers via gmail_sync + RECURSIVE body text via ghi_inbox._fetch_text
    (the shallow single-level reader misses multipart-nested bodies)."""
    from gmail_sync import get_email_content
    from ghi_inbox import _fetch_text

    email = get_email_content(mid)
    if not email:
        return None
    try:
        deep_body, _s, _f = _fetch_text(mid)
        if deep_body and len(deep_body) > len(email.get("body") or ""):
            email["body"] = deep_body
    except Exception:
        pass
    return email


def ingest_new_messages(hours_back: int = 24) -> Dict:
    """Pull each stream, insert UNSEEN messages into the ledger with their
    extracted facts. Idempotent: a message_id already in the ledger is never
    fetched again. SHADOW: touches only ledger tables."""
    from gmail_sync import gmail_configured, search_emails

    out = {"status": "ok", "new_rows": 0, "seen": 0, "by_kind": {},
           "errors": []}
    if not gmail_configured():
        out["status"] = "skipped"
        return out
    with get_db() as conn:
        ensure_ledger_tables(conn)
        seen_batch = set()
        for query, kind in STREAMS:
            try:
                msgs = search_emails(f"newer_than:{int(hours_back)}h {query}", 50)
            except Exception as e:
                out["errors"].append(f"search {kind}: {e}")
                continue
            for m in msgs:
                mid = m.get("id")
                if not mid or mid in seen_batch:
                    continue
                seen_batch.add(mid)
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1 FROM email_ledger WHERE message_id = %s",
                                    (mid,))
                        if cur.fetchone():
                            out["seen"] += 1
                            continue
                    email = _fetch_message(mid)
                    if not email:
                        continue
                    subject = email.get("subject") or ""
                    body = email.get("body") or ""
                    from_addr = email.get("from") or ""
                    ignored = None
                    row_kind = kind
                    if subject.upper().startswith(AUTOMATION_SUBJECT_PREFIXES):
                        row_kind, ignored = "automation", "own automation notification"
                    orders = [] if ignored else _classify_orders(conn, subject, body)
                    text = f"{subject} {body}"
                    pros = [] if ignored else _PRO_RE.findall(text)
                    ups = [] if ignored else _UPS_RE.findall(text.upper())
                    amounts = [] if ignored else _AMOUNT_RE.findall(subject)
                    folder = "sent" if "cabinetsforcontractors" in from_addr.lower() \
                        else "inbox"
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO email_ledger
                                (message_id, thread_id, folder, from_addr,
                                 to_addr, subject, email_date, kind, order_ids,
                                 pros, ups, amounts, ignored_reason)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, %s)
                            ON CONFLICT (message_id) DO NOTHING
                        """, (mid, m.get("threadId"), folder, from_addr[:300],
                              (email.get("to") or "")[:300], subject[:400],
                              _parse_email_date(email.get("date")), row_kind,
                              ",".join(orders), ",".join(pros),
                              ",".join(ups), ",".join(amounts), ignored))
                        conn.commit()
                    out["new_rows"] += 1
                    out["by_kind"][row_kind] = out["by_kind"].get(row_kind, 0) + 1
                except Exception as e:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    out["errors"].append(f"{mid}: {e}")
    return out


# =============================================================================
# ORDER FACTS (the spreadsheet — rebuilt deterministically from the ledger)
# =============================================================================

def rebuild_order_facts() -> Dict:
    """Replay the ledger into order_facts. Fully deterministic: safe to run
    any time, the ledger is the single source of truth.
    INDICATOR RULE (William): a 'tracking_sent' ledger row means the customer
    ALREADY received tracking by hand -> tracking_email_sent_at = that email's
    date. Tracking captured any other way leaves the indicator empty ->
    (post-cutover) the robot drafts the tracking email once, then stamps it."""
    from psycopg2.extras import RealDictCursor

    out = {"status": "ok", "orders": 0}
    with get_db() as conn:
        ensure_ledger_tables(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM email_ledger
                WHERE ignored_reason IS NULL AND order_ids <> ''
                ORDER BY email_date NULLS LAST
            """)
            rows = cur.fetchall()
        facts: Dict[str, Dict] = {}
        for r in rows:
            for oid in (r["order_ids"] or "").split(","):
                oid = oid.strip()
                if not oid:
                    continue
                f = facts.setdefault(oid, {
                    "payment_link_date": None, "payment_link_msg": None,
                    "tracking_value": None, "pro_value": None,
                    "tracking_msg": None, "tracking_date": None,
                    "tracking_email_sent_at": None, "subjects": []})
                subj_entry = f"{str(r['email_date'])[:10]} | {r['subject']}"
                if subj_entry not in f["subjects"]:
                    f["subjects"].append(subj_entry)
                if r["kind"] == "payment_link" and not f["payment_link_date"]:
                    f["payment_link_date"] = r["email_date"]
                    f["payment_link_msg"] = r["message_id"]
                if r["kind"] == "tracking_sent" and (r["pros"] or r["ups"]):
                    # first tracking email wins; later corrections append value
                    val_parts = []
                    if r["ups"]:
                        val_parts.append(r["ups"].replace(",", " "))
                    if r["pros"]:
                        val_parts.append("R+L PRO " + r["pros"].split(",")[0])
                    val = " ".join(val_parts)
                    if not f["tracking_value"]:
                        f["tracking_value"] = val
                        f["pro_value"] = (r["pros"].split(",")[0]
                                          if r["pros"] else None)
                        f["tracking_msg"] = r["message_id"]
                        f["tracking_date"] = r["email_date"]
                        # hand-sent tracking email = indicator SET (customer
                        # already told; never re-send)
                        f["tracking_email_sent_at"] = r["email_date"]
        with get_db() as conn2:
            with conn2.cursor() as cur:
                for oid, f in facts.items():
                    cur.execute("""
                        INSERT INTO order_facts
                            (order_id, payment_link_date, payment_link_msg,
                             tracking_value, pro_value, tracking_msg,
                             tracking_date, tracking_email_sent_at,
                             related_subjects, rebuilt_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (order_id) DO UPDATE SET
                            payment_link_date = EXCLUDED.payment_link_date,
                            payment_link_msg = EXCLUDED.payment_link_msg,
                            tracking_value = EXCLUDED.tracking_value,
                            pro_value = EXCLUDED.pro_value,
                            tracking_msg = EXCLUDED.tracking_msg,
                            tracking_date = EXCLUDED.tracking_date,
                            tracking_email_sent_at =
                                EXCLUDED.tracking_email_sent_at,
                            related_subjects = EXCLUDED.related_subjects,
                            rebuilt_at = NOW()
                    """, (oid, f["payment_link_date"], f["payment_link_msg"],
                          f["tracking_value"], f["pro_value"],
                          f["tracking_msg"], f["tracking_date"],
                          f["tracking_email_sent_at"],
                          "\n".join(f["subjects"][-3:])))
                conn2.commit()
        out["orders"] = len(facts)
    return out


# =============================================================================
# SHADOW COMPARE (the proof artifact before any cutover)
# =============================================================================

def compare_facts_vs_orders() -> Dict:
    """Facts (ledger truth) vs live orders table. Mismatch classes:
    'orders_missing_tracking' = old scanners missed a hand-sent tracking email
    (the 5699 class); 'orders_has_unexplained_tracking' = a stamp with NO
    source email in the ledger (the fake-PRO class — suspicious by definition,
    though for ORDERS OLDER THAN THE LEDGER WINDOW it just means the source
    email predates the ledger backfill)."""
    from psycopg2.extras import RealDictCursor

    out = {"status": "ok", "match": 0, "orders_missing_tracking": [],
           "orders_has_unexplained_tracking": [], "value_mismatch": []}
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT o.order_id, o.tracking AS o_trk, o.pro_number AS o_pro,
                       f.tracking_value AS f_trk, f.pro_value AS f_pro
                FROM orders o
                FULL OUTER JOIN order_facts f ON f.order_id = o.order_id
                WHERE (o.tracking IS NOT NULL AND o.tracking <> '')
                   OR (o.pro_number IS NOT NULL AND o.pro_number <> '')
                   OR f.tracking_value IS NOT NULL
            """)
            rows = cur.fetchall()
    for r in rows:
        o_has = bool((r["o_trk"] or "").strip() or (r["o_pro"] or "").strip())
        f_has = bool(r["f_trk"])
        if f_has and not o_has:
            out["orders_missing_tracking"].append(r["order_id"])
        elif o_has and not f_has:
            out["orders_has_unexplained_tracking"].append(r["order_id"])
        else:
            o_pro = (r["o_pro"] or "").strip()
            f_pro = (r["f_pro"] or "").strip()
            if o_pro and f_pro and o_pro != f_pro:
                out["value_mismatch"].append(
                    {"order_id": r["order_id"], "orders_pro": o_pro,
                     "facts_pro": f_pro})
            else:
                out["match"] += 1
    return out


# =============================================================================
# SHADOW SWEEP HOOK (rides the sync cycle: ingest + rebuild, nothing else)
# =============================================================================

def run_ledger_shadow(hours_back: int = 24) -> Dict:
    ing = ingest_new_messages(hours_back=hours_back)
    reb = rebuild_order_facts() if ing.get("new_rows") else {"orders": 0}
    return {"ingested": ing.get("new_rows", 0), "seen": ing.get("seen", 0),
            "facts_orders": reb.get("orders", 0),
            "errors": ing.get("errors", [])}


# =============================================================================
# ENDPOINTS
# =============================================================================

@ledger_router.post("/ledger/ingest")
def ledger_ingest(hours_back: int = 24, _: bool = Depends(require_admin)):
    return ingest_new_messages(hours_back=hours_back)


@ledger_router.post("/ledger/rebuild")
def ledger_rebuild(_: bool = Depends(require_admin)):
    return rebuild_order_facts()


@ledger_router.post("/ledger/reset")
def ledger_reset(_: bool = Depends(require_admin),
                 x_allow_destructive: Optional[str] =
                 Header(None, alias="X-Allow-Destructive")):
    """SHADOW-PHASE ONLY: truncate ledger + facts so a fixed extractor can
    re-ingest from scratch. Requires X-Allow-Destructive: yes. Shadow tables
    hold derived data only — nothing of record lives here yet."""
    if (x_allow_destructive or "").strip().lower() != "yes":
        return {"status": "error",
                "message": "X-Allow-Destructive: yes header required"}
    with get_db() as conn:
        ensure_ledger_tables(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE email_ledger")
            cur.execute("TRUNCATE order_facts")
            conn.commit()
    return {"status": "ok", "message": "ledger + facts truncated (shadow)"}


@ledger_router.get("/ledger/compare")
def ledger_compare(_: bool = Depends(require_admin)):
    return compare_facts_vs_orders()


@ledger_router.get("/ledger")
def ledger_list(order_id: Optional[str] = None, limit: int = 50,
                _: bool = Depends(require_admin)):
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_ledger_tables(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if order_id:
                cur.execute("""SELECT * FROM email_ledger
                               WHERE order_ids LIKE %s
                               ORDER BY email_date DESC LIMIT %s""",
                            (f"%{order_id}%", limit))
            else:
                cur.execute("""SELECT * FROM email_ledger
                               ORDER BY email_date DESC LIMIT %s""", (limit,))
            rows = cur.fetchall()
    return {"status": "ok", "rows": [dict(r) for r in rows]}


@ledger_router.get("/ledger/facts")
def ledger_facts(_: bool = Depends(require_admin)):
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_ledger_tables(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT * FROM order_facts
                           ORDER BY order_id DESC LIMIT 200""")
            rows = cur.fetchall()
    return {"status": "ok", "facts": [dict(r) for r in rows]}


@ledger_router.get("/ledger/facts.csv", response_class=PlainTextResponse)
def ledger_facts_csv(_: bool = Depends(require_admin)):
    """William's spreadsheet view: open in Excel/Sheets any time."""
    import csv
    import io
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_ledger_tables(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT order_id, payment_link_date, tracking_value,
                                  pro_value, tracking_date,
                                  tracking_email_sent_at, related_subjects
                           FROM order_facts ORDER BY order_id DESC""")
            rows = cur.fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["order", "payment_link_date", "tracking", "pro",
                "tracking_date", "tracking_email_sent (indicator)",
                "related_subjects"])
    for r in rows:
        w.writerow([r["order_id"], r["payment_link_date"] or "",
                    r["tracking_value"] or "", r["pro_value"] or "",
                    r["tracking_date"] or "",
                    r["tracking_email_sent_at"] or "",
                    (r["related_subjects"] or "").replace("\n", " || ")])
    return buf.getvalue()
