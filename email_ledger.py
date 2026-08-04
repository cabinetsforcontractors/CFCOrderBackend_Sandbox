"""
email_ledger.py
THE CLEAN DATA LAYER (William architecture ruling 2026-07-18).
CUTOVER LIVE 2026-07-27 (Beat 5, William's word): no longer shadow — the
ledger cycle (ingest -> rebuild facts -> apply) rides every gmail-sync run,
and the applier is THE only email-derived tracking writer (gmail_sync's raw
re-scanner is retired). Rails: only-if-empty stamps, provenance message id
on every write, hand-sent indicator kills duplicate tracking drafts.

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
    # general mail (Beat 5 cutover): the board + per-order timeline read the
    # ledger, so ALL recent mail gets ledgered. Specific streams above run
    # first so their kinds win; these two catch the rest. Each message is
    # still fetched from Gmail exactly once, ever.
    ('in:inbox -in:draft', "inbox_mail"),
    ('in:sent -in:draft', "sent_mail"),
)

AUTOMATION_SUBJECT_PREFIXES = (
    "PROGRESS DRAFT READY",
    "APPROVAL DRAFT READY",
    "DISCREPANCY",
    "ALERT!!",
    "GHI EMAIL NEEDS A HUMAN",
    "DELIVERED DRAFT READY",
    "R+L DELIVERED NEEDS A HUMAN",
    "AUTO-INVOICE NEEDS A HUMAN",
    "PAYMENT NEEDS A HUMAN",
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
    only count when they match a REAL order id in the DB.
    BILL2 LAW (William 2026-08-02, the #5589 mis-attribution): R+L report
    emails carry CSVs full of order-shaped numbers — those never attribute
    from the body, subject only."""
    subj_hits = _SUBJECT_ORDER_RE.findall(subject or "")
    if subj_hits:
        return _known_order_ids(conn, subj_hits) or sorted(set(subj_hits))
    if "BILL2" in (subject or "").upper():
        return []
    # URL-DIGIT GUARD (William 8/3, the E FUEL lesson: Whitewater's portal
    # link mcleodhosted.com:5696 tagged their dunning email as order 5696
    # — a PORT NUMBER). URLs are stripped before body numbers count.
    text = re.sub(r"https?://\S+", " ", f"{subject} {body}"[:6000])
    return _known_order_ids(conn, _BODY_ORDER_RE.findall(text))


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
# CUTOVER APPLIER (Beat 5, 2026-07-27 — facts write orders, with rails)
# =============================================================================

def apply_facts_to_orders(dry_run: bool = False) -> Dict:
    """Stamp orders.tracking/pro_number from order_facts. THE RAILS:
    only-if-empty (a stamped order is never overwritten — the fake-PRO class
    dies here), provenance message id logged in the order event, and when the
    facts indicator says the tracking email was hand-sent, the promise row's
    tracking stage completes too so the robot never drafts a duplicate
    (mirrors progress_emails.stamp_manual_tracking)."""
    out = {"status": "ok", "stamped": [], "skipped_already": 0, "errors": []}
    with get_db() as conn:
        ensure_ledger_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.order_id, f.tracking_value, f.pro_value,
                       f.tracking_msg, f.tracking_email_sent_at,
                       o.tracking, o.pro_number
                FROM order_facts f
                JOIN orders o ON o.order_id = f.order_id
                WHERE f.tracking_value IS NOT NULL
            """)
            rows = cur.fetchall()
        for oid, f_trk, f_pro, f_msg, f_sent, o_trk, o_pro in rows:
            if (o_trk or "").strip() or (o_pro or "").strip():
                out["skipped_already"] += 1
                continue
            item = {"order_id": oid, "tracking": f_trk, "pro": f_pro,
                    "hand_sent": bool(f_sent)}
            if dry_run:
                out["stamped"].append(item)
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE orders SET tracking = %s,
                                   updated_at = NOW() WHERE order_id = %s""",
                                (f_trk, oid))
                    if f_pro:
                        cur.execute("""UPDATE orders SET pro_number = %s,
                                       updated_at = NOW() WHERE order_id = %s""",
                                    (f_pro, oid))
                    if f_sent:
                        cur.execute("""
                            INSERT INTO progress_promises
                                (order_id, suppliers, post_payment_at,
                                 tracking_at)
                            VALUES (%s, 'ledger-tracking', NOW(), NOW())
                            ON CONFLICT (order_id) DO UPDATE
                            SET tracking_at =
                                COALESCE(progress_promises.tracking_at, NOW())
                        """, (oid,))
                    cur.execute("""
                        INSERT INTO order_events
                            (order_id, event_type, event_data, source)
                        VALUES (%s, 'tracking_stamped', %s, 'email_ledger')
                    """, (oid, json.dumps({"message_id": f_msg,
                                           "tracking": f_trk, "pro": f_pro,
                                           "hand_sent": bool(f_sent)})))
                    conn.commit()
                out["stamped"].append(item)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"{oid}: {e}")
    return out


# =============================================================================
# THE LEDGER CYCLE (rides every gmail-sync run: ingest -> rebuild -> apply)
# =============================================================================

def stamp_hand_sent_invoices(days_back: int = 3) -> Dict:
    """HAND-SEND STAMP (William 8/4): a sent-folder email whose subject is
    the robot's own invoice subject stamps payment_link_sent on its
    order. Born from the 8/4 sweep: 5698/5755 invoices went out by his
    hand and the board still shouted 'send the invoice'."""
    import re as _re
    out = {"stamped": []}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT subject FROM email_ledger
                WHERE folder = 'sent'
                  AND email_date > NOW() - (%s || ' days')::interval
                  AND subject ILIKE 'Invoice For Your order #%%'
            """, (int(days_back),))
            subjects = [r[0] for r in cur.fetchall()]
        for subj in subjects:
            m = _re.search(r"#(\d{3,5})", subj or "")
            if not m:
                continue
            oid = m.group(1)
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE orders
                    SET payment_link_sent = TRUE, updated_at = NOW()
                    WHERE order_id = %s
                      AND COALESCE(payment_link_sent, FALSE) = FALSE
                      AND COALESCE(payment_received, FALSE) = FALSE
                """, (oid,))
                if cur.rowcount:
                    cur.execute("""
                        INSERT INTO order_events
                            (order_id, event_type, event_data, source)
                        VALUES (%s, 'invoice_sent_detected', %s,
                                'hand_send_stamp')
                    """, (oid, json.dumps({"subject": (subj or "")[:140]})))
                    out["stamped"].append(oid)
            conn.commit()
    return out


def run_ledger_cycle(hours_back: int = 24) -> Dict:
    ing = ingest_new_messages(hours_back=hours_back)
    if ing.get("new_rows"):
        reb = rebuild_order_facts()
        app = apply_facts_to_orders()
    else:
        reb, app = {"orders": 0}, {"stamped": [], "errors": []}
    # HAND-SEND STAMP (8/4 lesson, the 5698/5755 case): William sends
    # invoice DRAFTS from Gmail by his own hand -- the robot must notice
    # its own invoice subject in the SENT ledger and stamp
    # payment_link_sent (trigger-silent, no polls fire on this
    # checkpoint). Idempotent: only unstamped unpaid orders.
    hand_stamp = {}
    try:
        hand_stamp = stamp_hand_sent_invoices()
    except Exception as e:
        hand_stamp = {"errors": [str(e)]}
    # R+L delivered notices -> customer delivered-email DRAFTS (William
    # 2026-07-28 "yes" ruling; idempotent, safe to run every cycle)
    rl = {}
    try:
        from rl_delivered import process_rl_delivered
        rl = process_rl_delivered(hours_back=hours_back)
    except Exception as e:
        rl = {"errors": [str(e)]}
    # FREIGHT-BILL AUDITOR (Phase 2, William 2026-07-29): unprocessed BILL2
    # reports audit themselves each cycle (idempotent per message + per
    # (pro, report) stamp)
    bill = {}
    try:
        from rl_bill_audit import process_bill2_reports
        bill = process_bill2_reports(hours_back=hours_back)
    except Exception as e:
        bill = {"errors": [str(e)]}
    # OUT-OF-STOCK DETECTOR (Wave 2 build D, William 2026-08-02):
    # detect-only, idempotent per message — one bell per OOS reply
    oos = {}
    try:
        from oos_detect import process_oos_scan
        oos = process_oos_scan(hours_back=hours_back)
    except Exception as e:
        oos = {"errors": [str(e)]}
    # CUSTOMER-PO READER (Wave 3 build K): UFP/Nationwide PO PDFs get
    # parsed + one bell; review-only, never creates an order
    try:
        from customer_po import process_customer_po_scan
        cpo = process_customer_po_scan(hours_back=hours_back)
        if cpo.get("errors"):
            print(f"[LEDGER] customer-po errors: {cpo['errors']}")
    except Exception as e:
        print(f"[LEDGER] customer-po scan failed: {e}")
    # CARRIER CLAIM CLOCKS (Wave 3 build J): deadline-30 and quiet-30
    # alarms, each fires exactly once per condition
    try:
        from carrier_claims import check_claim_clocks
        check_claim_clocks()
    except Exception as e:
        print(f"[LEDGER] claim clocks failed: {e}")
    # STOCK-CHECK CLOCK (William 2026-08-02): 4/8 business-hour nudges on
    # unanswered stock questions; heals on any reply in the thread
    try:
        from oos_detect import check_stock_check_clocks
        check_stock_check_clocks()
    except Exception as e:
        print(f"[LEDGER] stock-check clocks failed: {e}")
    # BOUNCE WATCHER (William 2026-08-03, the weborders@ lesson): a
    # bounced send never dies silently again — one bell per bounce
    try:
        from bounce_watch import process_bounces
        b = process_bounces(hours_back=hours_back)
        if b.get("alerted"):
            print(f"[LEDGER] bounces alerted: {b}")
    except Exception as e:
        print(f"[LEDGER] bounce watch failed: {e}")
    # GMAIL MIRROR (William 2026-08-02): deleted-in-Gmail kills the card;
    # promotional+read auto-settles; important+read stays
    try:
        from gmail_mirror import process_gmail_mirror
        mir = process_gmail_mirror()
        if mir.get("settled_deleted") or mir.get("settled_promo"):
            print(f"[LEDGER] gmail mirror: {mir}")
    except Exception as e:
        print(f"[LEDGER] gmail mirror failed: {e}")
    # FREIGHT MONTHLY (William 2026-08-02): first cycle of a new month
    # emails the charged-vs-billed roll-up (idempotent per month)
    try:
        from freight_monthly import run_monthly_rollup
        run_monthly_rollup()
    except Exception as e:
        print(f"[LEDGER] freight monthly failed: {e}")
    # THE WALK LIST (William 2026-08-03): 8/10/noon/3 ET sweeps, once per
    # slot per day, weekends quiet; unworked items roll by themselves
    try:
        from walk_list import run_walk_list_schedule
        run_walk_list_schedule()
    except Exception as e:
        print(f"[LEDGER] walk list failed: {e}")
    # NEW-CUSTOMER NOTIFICATION GUARD (at most once per NOTIF_GUARD_HOURS,
    # one API list call — the guard itself decides whether it's due)
    try:
        from b2bwave_notif_guard import run_notif_guard
        run_notif_guard(dry_run=False)
    except Exception as e:
        print(f"[LEDGER] notif guard failed: {e}")
    return {"ingested": ing.get("new_rows", 0), "seen": ing.get("seen", 0),
            "facts_orders": reb.get("orders", 0),
            "stamped": app.get("stamped", []),
            "rl_delivered": {k: rl.get(k) for k in
                             ("drafted", "mismatched", "unmatched")
                             if rl.get(k)},
            "bill2": {k: bill.get(k) for k in ("audited", "already")
                      if bill.get(k)},
            "oos": {k: oos.get(k) for k in ("alerted", "already")
                    if oos.get(k)},
            "errors": (ing.get("errors") or []) + (app.get("errors") or [])
                      + (rl.get("errors") or []) + (bill.get("errors") or [])
                      + (oos.get("errors") or [])}


# pre-cutover name, kept so old callers/notes stay valid
run_ledger_shadow = run_ledger_cycle


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


@ledger_router.post("/ledger/rl-delivered")
def ledger_rl_delivered(hours_back: int = 48, dry_run: bool = True,
                        _: bool = Depends(require_admin)):
    """Run the R+L delivered-notice handler on demand [admin]. dry_run=true
    (default) reports the decisions without stamping, drafting, or alerting."""
    from rl_delivered import process_rl_delivered
    return process_rl_delivered(hours_back=hours_back, dry_run=dry_run)


@ledger_router.post("/ledger/oos-scan")
def ledger_oos_scan(hours_back: int = 48, dry_run: bool = True,
                    _: bool = Depends(require_admin)):
    """Run the out-of-stock detector on demand [admin]. dry_run=true
    (default) lists the hits without recording or alerting."""
    from oos_detect import process_oos_scan
    return process_oos_scan(hours_back=hours_back, dry_run=dry_run)


@ledger_router.get("/walk-list")
def walk_list_now(_: bool = Depends(require_admin)):
    """THE WALK LIST in one call [admin] — the chat session's opening move
    (William 2026-08-03: 'show me a list and we work through it one by
    one'). Needs-you w/ subjects, NEW vs ROLLED, due, deferred, supplier
    legs, money, robot receipts."""
    from walk_list import build_walk_list
    return build_walk_list()


@ledger_router.post("/walk-list/send")
def walk_list_send(slot: str = "manual", _: bool = Depends(require_admin)):
    """Compose + email the walk list to the bell [admin] (drill door; the
    scheduled slots ride the ledger cycle)."""
    from walk_list import send_walk_list
    return send_walk_list(slot=slot)


@ledger_router.post("/ledger/gmail-mirror")
def ledger_gmail_mirror(limit: int = 60, _: bool = Depends(require_admin)):
    """Run the Gmail mirror on demand [admin]: deleted-in-Gmail settles the
    card (two-strike rule), promotional+read auto-settles, important stays."""
    from gmail_mirror import process_gmail_mirror
    return process_gmail_mirror(limit=limit)


@ledger_router.post("/b2bwave/notif-guard")
def notif_guard_now(dry_run: bool = True, force: bool = True,
                    _: bool = Depends(require_admin)):
    """Run the new-customer notification guard on demand [admin].
    dry_run=true (default) lists strays without patching."""
    from b2bwave_notif_guard import run_notif_guard
    return run_notif_guard(dry_run=dry_run, force=force)


@ledger_router.post("/ledger/rl-bill-audit/{message_id}")
def ledger_rl_bill_audit(message_id: str, dry_run: bool = True,
                         _: bool = Depends(require_admin)):
    """Run the freight-bill auditor on ONE BILL2 email [admin]. dry_run=true
    (default) parses + matches + compares with ZERO writes and no email."""
    from rl_bill_audit import audit_bill2_message
    return audit_bill2_message(message_id, dry_run=dry_run)


@ledger_router.post("/ledger/apply")
def ledger_apply(dry_run: bool = True, _: bool = Depends(require_admin)):
    """CUTOVER APPLIER door — dry_run=true (default) previews the stamps;
    dry_run=false writes them (only-if-empty rails apply either way)."""
    return apply_facts_to_orders(dry_run=dry_run)


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
