"""
queue_api.py — QUEUE BACKEND, Phase A+B (William-ruled 2026-07-30/31).

AUTO-SETTLE (ruling 4: leave a "robot settled this because X" trace for
now; silent mode later "as it learns"): flags DIE when their cause dies —
  - alert emails for TEST-REGISTRY / tombstoned orders -> settled
    ("order is a registered test / deleted")
  - [ACTION] UPLOAD NEEDED for an order whose ROC leg is already
    sent/confirmed -> settled ("ROC row confirmed")
  - CONFIRM DISPATCH for an order whose supplier legs already exist
    beyond pending -> settled ("already dispatched")
Settling = the alert thread is marked read (the board derives from unread)
+ a handled note lands in task_board_items: "[robot settled: X]".

MONEY STRIP (ruling 5): GET /queue/money-strip
DONE EVENTS (7/30): honest robot activity, redirects confessed.
AWAITING REPLY (7/30): read is not replied. STICKY (8/1): 60-day window —
unanswered threads stop dropping off before William acts.
ORDER ACTIONS (7/31): six checkpoints + CANCEL (notify-or-quiet).
HANDLED (7/31): the loop is closed; a NEW email brings it back.
THREAD ACTIONS (7/31): Read/Archive/Delete for ledger-born NEEDS REPLY
cards — "I need delete to parse out the spam newsletters and the like".
SUPPLIER-NUMBER LINKING (8/1): orderless cards resolve supplier numbers
(ROC order/SO numbers, R+L PRO numbers) back to OUR order — "the robot
needs to review all ROC's SO's and find out order number to link".

Doors [admin]:
  POST /auto-settle/run?dry_run=true
  GET  /queue/money-strip
  GET  /queue/done-events
  GET  /queue/awaiting-reply
  POST /queue/awaiting-reply/dismiss {thread_id, order_id?, note?}
  POST /queue/order-action {action, order_id?, task_key?, notify?}
  POST /queue/handled {task_key?, thread_id?, order_id?}
  POST /queue/thread-action {thread_id, action: read|archive|trash}
"""

import json
import os
import re
import urllib.request
from typing import Dict, List

from fastapi import APIRouter, Body, Depends

from auth import require_admin
from db_helpers import get_db

queue_router = APIRouter(tags=["queue"])

_OID_RE = re.compile(r"\b(5\d{3})\b")

ALERT_SUBJECTS = ('subject:"AUTO-INVOICE NEEDS A HUMAN" OR '
                  'subject:"PAY PAGE NEEDS A HUMAN" OR '
                  'subject:"CONFIRM DISPATCH" OR '
                  'subject:"UPLOAD NEEDED" OR '
                  'subject:"REPLACEMENT REQUEST"')

# Senders that never deserve a reply — receipts, newsletters, robots,
# carrier tracking, automated supplier confirmations (the verifier eats
# those), and marketing blasts (they recur every campaign, so filtering is
# the only cure — a dismissal would just resurface). Expanded 7/31 across
# two live sweeps.
_NOISE_SENDER_RE = re.compile(
    r"no-?reply|noreply|notifications?@|mailer-daemon|do-?not-?reply|"
    r"@welcome\.|billtrust\.com|pirateship\.com|americanexpress\.com|"
    r"linkedin\.com|squareup\.com|@square\.com|messaging\.squareup|"
    r"@bottomline\.com|calendly\.com|@vercel\.com|@render\.com|"
    r"@github\.com|b2bemailservice|@dylt\.|@rlc\.com|rlcarriers\.com|"
    r"@ups\.com|@fedex\.com|weborders@|whitewaterfreight\.com|billing@|"
    r"receipts?@|invoice@intuit|@close\.com|success@email\.|"
    r"marketing@|marketing\.|@alignable\.com|membersuccess@|"
    r"ifttt\.com|cloudhq\.net|randstadusa\.com|@eq\.intuit\.com|"
    r"emails\.dlcabinetry\.com", re.I)

# OUR boxes — an inbox row from ourselves (robot alerts) is not someone
# waiting on a reply. 4wprince@gmail.com is NOT ours (William 2026-08-02:
# his personal address, cast as the test customer — customer mail must
# make NEEDS REPLY cards).
_OUR_ADDR_RE = re.compile(
    r"orders@cabinetsforcontractors\.com|cabinetsforcontractors@gmail\.com|"
    r"wpjob1@gmail\.com|contact@allprocabinetsandflooring\.com", re.I)

# Supplier-reference shapes: bare numbers (ROC order 41258, SO 139967,
# doc refs like 000041258) and carrier PRO numbers (IAH3136257).
_SUPREF_TOKEN_RE = re.compile(r"\b(?:SO\s?#?\s?)?#?(\d{5,9})\b", re.I)
_PRO_RE = re.compile(r"\b([A-Z]{2,4}\d{6,9})\b")

# ORDER ACTIONS — the full dropdown (William 7/31). Checkpoints ride
# orders_routes.update_checkpoint; cancel rides lifecycle_engine (the
# proven path, B2BWave status 7) with an optional customer notification.
ORDER_ACTIONS = {
    "payment_link_sent":   "Invoice / payment link sent",
    "payment_received":    "Payment received",
    "sent_to_warehouse":   "Sent to warehouse",
    "warehouse_confirmed": "Warehouse confirmed",
    "bol_sent":            "BOL sent",
    "is_complete":         "Picked up / delivered / complete",
    "cancel_order":        "CANCEL order",
}


def _gmail_thread_call(path: str, body: Dict = None) -> bool:
    try:
        from gmail_sync import get_gmail_access_token
        token = get_gmail_access_token()
        if not token:
            return False
        req = urllib.request.Request(
            f"https://gmail.googleapis.com/gmail/v1/users/me/{path}",
            data=json.dumps(body or {}).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except Exception as e:
        print(f"[QUEUE] gmail thread call {path} failed: {e}")
        return False


def _mark_thread_read(thread_id: str) -> bool:
    return _gmail_thread_call(f"threads/{thread_id}/modify",
                              {"removeLabelIds": ["UNREAD"]})


def _handled_note(task_key: str, order_id: str, note: str) -> bool:
    """Upsert a handled row — the trace the board shows. Ledger-born cards
    (needsreply:*) have no pre-existing row, so the INSERT must carry
    every NOT NULL column of task_board_items (board/type/title)."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO task_board_items
                        (task_key, board, type, title, order_id, status,
                         note, note_at, updated_at)
                    VALUES (%s, 'other', 'dismissal', %s, %s, 'handled',
                            %s, NOW(), NOW())
                    ON CONFLICT (task_key) DO UPDATE
                    SET status = 'handled',
                        note = EXCLUDED.note,
                        note_at = NOW(), updated_at = NOW()
                """, (task_key, note, order_id or None, note))
            conn.commit()
        return True
    except Exception as e:
        print(f"[QUEUE] note failed {task_key}: {e}")
        return False


def resolve_supplier_ref(text: str):
    """Map supplier numbers in a subject/body back to OUR order id.
    Tiers: supplier_orders.supplier_doc_ref (leading zeros ignored) ->
    PRO numbers on order_shipments/orders -> the order event history ->
    SIBLING EMAILS (another ledger message carrying the same supplier
    number already attributed to an order). Returns the order id ONLY
    when exactly one order matches — never guesses."""
    text = text or ""
    m = _OID_RE.search(text)
    if m:
        return m.group(1)
    tokens = {t.lstrip("0") for t in _SUPREF_TOKEN_RE.findall(text)}
    tokens = {t for t in tokens if len(t) >= 4}
    pros = set(_PRO_RE.findall(text.upper()))
    if not tokens and not pros:
        return None
    hits = set()
    like = [f"%{t}%" for t in (pros | tokens) if len(t) >= 5]
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                if tokens:
                    cur.execute("""
                        SELECT DISTINCT order_id FROM supplier_orders
                        WHERE TRIM(LEADING '0' FROM
                                   COALESCE(supplier_doc_ref, '')) = ANY(%s)
                    """, (list(tokens),))
                    hits.update(str(r[0]) for r in cur.fetchall())
                probe = list(pros | tokens)
                if probe:
                    cur.execute("""
                        SELECT DISTINCT order_id FROM order_shipments
                        WHERE pro_number = ANY(%s)
                    """, (probe,))
                    hits.update(str(r[0]) for r in cur.fetchall())
                    cur.execute("""
                        SELECT DISTINCT order_id FROM orders
                        WHERE pro_number = ANY(%s) OR tracking = ANY(%s)
                    """, (probe, probe))
                    hits.update(str(r[0]) for r in cur.fetchall())
                if not hits and like:
                    # the event history — supplier docs record their numbers
                    # even when no supplier_orders row carries them
                    clauses = " OR ".join(
                        ["event_data::text ILIKE %s"] * len(like))
                    cur.execute(f"""
                        SELECT DISTINCT order_id FROM order_events
                        WHERE created_at > NOW() - INTERVAL '120 days'
                          AND ({clauses})
                        LIMIT 5
                    """, like)
                    hits.update(str(r[0]) for r in cur.fetchall())
                if not hits and like:
                    # sibling emails: another ledger message with this same
                    # supplier number was already attributed to an order
                    clauses = " OR ".join(["subject ILIKE %s"] * len(like))
                    cur.execute(f"""
                        SELECT DISTINCT order_ids FROM email_ledger
                        WHERE order_ids IS NOT NULL AND order_ids <> ''
                          AND ({clauses})
                        LIMIT 10
                    """, like)
                    for (oids,) in cur.fetchall():
                        m2 = _OID_RE.search(str(oids))
                        if m2:
                            hits.add(m2.group(1))
    except Exception as e:
        print(f"[QUEUE] supplier-ref resolve failed: {e}")
        return None
    if len(hits) == 1:
        return hits.pop()
    return None


def _order_is_dead(conn, order_id: str) -> str:
    """'' if alive; else the reason the order no longer needs its flags."""
    try:
        from test_registry import test_order_ids
        if str(order_id) in test_order_ids():
            return "registered test order"
    except Exception:
        pass
    with conn.cursor() as cur:
        cur.execute("""SELECT lifecycle_status FROM orders
                       WHERE order_id = %s""", (order_id,))
        row = cur.fetchone()
        if row and (row[0] or "") == "deleted":
            return "order deleted/tombstoned"
    return ""


def _supplier_leg_state(conn, order_id: str, warehouse: str = None):
    with conn.cursor() as cur:
        if warehouse:
            cur.execute("""SELECT status FROM supplier_orders
                           WHERE order_id = %s AND warehouse = %s
                           ORDER BY updated_at DESC LIMIT 1""",
                        (order_id, warehouse))
        else:
            cur.execute("""SELECT status FROM supplier_orders
                           WHERE order_id = %s
                           ORDER BY updated_at DESC LIMIT 1""",
                        (order_id,))
        row = cur.fetchone()
        return row[0] if row else None


def run_auto_settle(dry_run: bool = True) -> Dict:
    """The cause-dies-flag-dies sweep."""
    out = {"status": "ok", "dry_run": dry_run, "settled": [],
           "kept": [], "errors": []}
    try:
        from gmail_sync import search_emails, gmail_configured
        if not gmail_configured():
            out["status"] = "skipped"
            return out
        msgs = search_emails(f"in:inbox is:unread ({ALERT_SUBJECTS})", 50)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    with get_db() as conn:
        for m in msgs:
            try:
                mid = m.get("id")
                tid = m.get("threadId") or mid
                subject = m.get("subject") or ""
                if not subject:
                    # search_emails may not carry subjects — fetch headers
                    try:
                        from reply_composer import _gmail_get, _header
                        full = _gmail_get(
                            f"messages/{mid}?format=metadata"
                            f"&metadataHeaders=Subject")
                        subject = _header(full or {}, "Subject")
                    except Exception:
                        subject = ""
                om = _OID_RE.search(subject)
                oid = om.group(1) if om else None
                if not oid:
                    out["kept"].append({"subject": subject[:60],
                                        "why": "no order id"})
                    continue

                reason = ""
                dead = _order_is_dead(conn, oid)
                if dead:
                    reason = dead
                elif "UPLOAD NEEDED" in subject.upper():
                    st = _supplier_leg_state(conn, oid, "ROC")
                    if st in ("sent", "confirmed", "scheduled",
                              "picked_up", "delivered", "invoice_verified"):
                        reason = f"ROC leg already {st}"
                elif "CONFIRM DISPATCH" in subject.upper():
                    st = _supplier_leg_state(conn, oid)
                    if st and st not in ("pending",):
                        reason = f"already dispatched (leg {st})"

                if not reason:
                    out["kept"].append({"subject": subject[:60],
                                        "order_id": oid,
                                        "why": "cause still live"})
                    continue

                item = {"subject": subject[:70], "order_id": oid,
                        "reason": reason}
                if not dry_run:
                    _mark_thread_read(tid)
                    _handled_note(f"thread:{tid}", oid,
                                  f"[robot settled: {reason}]")
                out["settled"].append(item)
            except Exception as e:
                out["errors"].append(f"{m.get('id')}: {e}")
    return out


# =============================================================================
# MONEY STRIP
# =============================================================================

def money_strip() -> Dict:
    landed = 0.0
    landed_n = 0
    awaiting = 0.0
    awaiting_n = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    SELECT event_data FROM order_events
                    WHERE event_type = 'payment_received'
                      AND created_at > NOW() - INTERVAL '24 hours'
                """)
                for (data,) in cur.fetchall():
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except Exception:
                            continue
                    amt = (data or {}).get("payment_amount")
                    if amt:
                        landed += float(amt)
                        landed_n += 1
            except Exception:
                conn.rollback()
            # TEST EXCLUSION (William 2026-08-03 phase-1: the strip must
            # never count play money as receivables)
            try:
                from test_registry import test_order_ids
                tids = list(test_order_ids()) or [""]
            except Exception:
                tids = [""]
            cur.execute("""
                SELECT COALESCE(SUM(order_total), 0), COUNT(*)
                FROM orders
                WHERE payment_link_sent = TRUE
                  AND payment_received = FALSE
                  AND NOT is_complete
                  AND COALESCE(lifecycle_status, 'active') != 'deleted'
                  AND NOT (order_id = ANY(%s))
            """, (tids,))
            row = cur.fetchone()
            awaiting = float(row[0] or 0)
            awaiting_n = int(row[1] or 0)

    freight = {}
    try:
        from rl_bill_audit import rolling_ledger
        freight = rolling_ledger()
    except Exception as e:
        freight = {"error": str(e)}

    return {"status": "ok",
            "landed_today": round(landed, 2), "landed_count": landed_n,
            "awaiting_total": round(awaiting, 2),
            "awaiting_count": awaiting_n,
            "freight_90d_net": freight.get("net_margin"),
            "freight_90d_avg": freight.get("avg_margin"),
            "freight_90d_shipments": freight.get("shipments"),
            "line": (f"Landed today ${landed:,.2f} ({landed_n}) · "
                     f"Awaiting ${awaiting:,.2f} across {awaiting_n} orders · "
                     f"90-day freight net "
                     f"${(freight.get('net_margin') or 0):,.2f}")}


# =============================================================================
# DONE EVENTS — real robot activity, noise excluded, honest details
# =============================================================================

def _event_detail(data) -> str:
    """One honest line per event. William's law: a redirected send must
    SAY so — 'reply_sent' alone reads like the email reached the supplier."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return ""
    if not isinstance(data, dict):
        return ""
    if data.get("redirected"):
        meant = data.get("original_to") or "?"
        return f"⚠ SAFETY REDIRECT — landed in {data.get('to') or '?'}, MEANT FOR {meant}"
    to = data.get("to") or data.get("to_email")
    if to:
        return f"to {to}"
    amt = data.get("payment_amount")
    if amt:
        try:
            return f"${float(amt):,.2f}"
        except Exception:
            pass
    fields = data.get("fields")
    if isinstance(fields, dict) and fields:
        parts = []
        for k, v in list(fields.items())[:3]:
            if isinstance(v, dict) and "was" in v:
                parts.append(f"{k}: {v.get('was')} → {v.get('now')}")
            else:
                parts.append(str(k))
        return ", ".join(parts)
    return ""


def done_events(days: int = 3, limit: int = 60) -> Dict:
    try:
        from fire_log import NOISE_EVENT_TYPES
        noise = list(NOISE_EVENT_TYPES)
    except Exception:
        noise = ["b2bwave_sync"]
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_id, event_type, source, created_at, event_data
                FROM order_events
                WHERE created_at > NOW() - make_interval(days => %s)
                  AND NOT (event_type = ANY(%s))
                ORDER BY created_at DESC LIMIT %s
            """, (days, noise, limit))
            events = [{"order_id": str(a), "event_type": b,
                       "source": c or "",
                       "at": d.isoformat() if d else "",
                       "detail": _event_detail(e)}
                      for a, b, c, d, e in cur.fetchall()]
    return {"status": "ok", "noise_excluded": noise, "events": events}


# =============================================================================
# AWAITING REPLY — read is not replied (William's law 7/30)
# =============================================================================

def awaiting_reply(days: int = 60) -> Dict:
    """Threads where the last OUTSIDE word has no answer from us — from the
    email ledger, so read/forwarded state changes nothing. Self-healing:
    a real reply (ledger sent-row after the inbound) drops the card; a
    dismissal only covers inbounds that existed when it was made.
    STICKY (8/1): 60-day window — a surfaced thread stays until action."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH t AS (
                    SELECT thread_id,
                           MAX(email_date) FILTER (WHERE folder = 'sent')  AS last_sent,
                           MAX(email_date) FILTER (WHERE folder = 'inbox') AS last_in
                    FROM email_ledger
                    WHERE thread_id IS NOT NULL
                    GROUP BY thread_id
                ),
                waiting AS (
                    SELECT thread_id, last_in, last_sent FROM t
                    WHERE last_in IS NOT NULL
                      AND last_in > NOW() - make_interval(days => %s)
                      AND (last_sent IS NULL OR last_in > last_sent)
                )
                SELECT w.thread_id, w.last_in, w.last_sent,
                       l.message_id, l.from_addr, l.subject, l.order_ids
                FROM waiting w
                JOIN LATERAL (
                    SELECT message_id, from_addr, subject, order_ids
                    FROM email_ledger
                    WHERE thread_id = w.thread_id AND folder = 'inbox'
                    ORDER BY email_date DESC LIMIT 1
                ) l ON TRUE
                LEFT JOIN task_board_items d
                  ON d.task_key = 'needsreply:' || w.thread_id
                 AND d.status = 'handled'
                 AND d.note_at > w.last_in
                WHERE d.task_key IS NULL
                ORDER BY w.last_in ASC
                LIMIT 100
            """, (days,))
            rows = cur.fetchall()

    cards = []
    for tid, last_in, last_sent, mid, frm, subj, oids in rows:
        f = frm or ""
        if _NOISE_SENDER_RE.search(f) or _OUR_ADDR_RE.search(f):
            continue
        oid = None
        if oids:
            m = _OID_RE.search(str(oids))
            oid = m.group(1) if m else None
        if not oid:
            # supplier-number linking (William 8/1): ROC SO/order numbers,
            # PRO numbers in the subject point back to OUR order
            oid = resolve_supplier_ref(subj or "")
        cards.append({
            "thread_id": tid,
            "message_id": mid,
            "from": f,
            "subject": subj or "(no subject)",
            "order_id": oid,
            "last_inbound": last_in.isoformat() if last_in else "",
            "ever_answered": last_sent is not None,
        })
    return {"status": "ok", "count": len(cards), "cards": cards}


# =============================================================================
# DOORS
# =============================================================================

@queue_router.post("/auto-settle/run")
def auto_settle_run(dry_run: bool = True, _: bool = Depends(require_admin)):
    """Flags die when their cause dies [admin]. dry_run=true reports what
    WOULD settle; dry_run=false settles (mark read + robot-settled trace)."""
    return run_auto_settle(dry_run=dry_run)


@queue_router.get("/queue/awaiting-orders")
def awaiting_orders(_: bool = Depends(require_admin)):
    """PHASE 1 (William 2026-08-03): the money strip's drill-down — every
    order behind the Awaiting number, with a TEST badge, ready for the
    one-click Mark-Paid (the trigger-silent checkpoint door)."""
    try:
        from test_registry import test_order_ids
        tids = test_order_ids()
    except Exception:
        tids = set()
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT order_id, customer_name, company_name, email,
                       order_total, payment_link_sent_at, order_date
                FROM orders
                WHERE payment_link_sent = TRUE
                  AND payment_received = FALSE
                  AND NOT is_complete
                  AND COALESCE(lifecycle_status, 'active') != 'deleted'
                ORDER BY order_date DESC NULLS LAST
            """)
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["is_test"] = str(r["order_id"]) in tids
    return {"status": "ok", "count": len(rows), "orders": rows}


@queue_router.get("/orders/{order_id}/threads")
def order_threads(order_id: str, _: bool = Depends(require_admin)):
    """PHASE 1 (William 2026-08-03, the 5750/5696 wrong-thread lesson):
    every email thread linked to an order — exact SUBJECT, who spoke last,
    direction, the newest inbound message id (the composer's anchor), and
    an is_alert flag for robot-alert threads (replying there does NOT join
    the order's real conversation)."""
    from email_ledger import AUTOMATION_SUBJECT_PREFIXES
    alert_pfx = tuple(p.upper() for p in AUTOMATION_SUBJECT_PREFIXES) + (
        "CONFIRM DISPATCH", "NEW ORDER #", "PAYMENT NEEDS A HUMAN",
        "OUT OF STOCK -", "NEEDS-A-HUMAN", "[ACTION]", "[CONFIRM+SEND]")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT thread_id,
                       MAX(email_date) AS last_at,
                       (ARRAY_AGG(subject ORDER BY email_date DESC))[1] AS subject,
                       (ARRAY_AGG(folder ORDER BY email_date DESC))[1] AS last_folder,
                       (ARRAY_AGG(from_addr ORDER BY email_date DESC))[1] AS last_from,
                       COUNT(*) AS messages,
                       (ARRAY_AGG(message_id ORDER BY email_date DESC)
                        FILTER (WHERE folder = 'inbox'))[1] AS newest_inbound_id
                FROM email_ledger
                WHERE thread_id IS NOT NULL AND thread_id != ''
                  AND (',' || COALESCE(order_ids, '') || ',') LIKE %s
                GROUP BY thread_id
                ORDER BY MAX(email_date) DESC
                LIMIT 30
            """, (f"%,{order_id},%",))
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        subj = (r.get("subject") or "").upper()
        r["is_alert"] = any(subj.startswith(p) or subj.startswith("RE: " + p)
                            for p in alert_pfx)
        r["who_spoke_last"] = ("us" if r.get("last_folder") == "sent"
                               else "them")
        if r.get("last_at"):
            r["last_at"] = str(r["last_at"])
    return {"status": "ok", "order_id": order_id, "count": len(rows),
            "threads": rows}


@queue_router.get("/queue/money-strip")
def get_money_strip(_: bool = Depends(require_admin)):
    """The one line that stays above the queue (ruling 5)."""
    return money_strip()


@queue_router.get("/queue/done-events")
def get_done_events(days: int = 3, limit: int = 60,
                    _: bool = Depends(require_admin)):
    """DONE RECENTLY feed [admin]: order_events minus the sync heartbeat
    noise — what the robot actually DID, not what it polled. Rows carry a
    detail line; redirected sends say so in plain words."""
    return done_events(days=days, limit=limit)


@queue_router.get("/queue/awaiting-reply")
def get_awaiting_reply(days: int = 60, _: bool = Depends(require_admin)):
    """Conversations waiting on OUR word [admin] — read is not replied.
    STICKY (8/1): 60-day window by default."""
    return awaiting_reply(days=days)


@queue_router.post("/queue/awaiting-reply/dismiss")
def dismiss_awaiting_reply(payload: Dict = Body(...),
                           _: bool = Depends(require_admin)):
    """No reply needed / HANDLED for a ledger card [admin]. Covers only
    what has arrived so far — a NEW inbound resurfaces the card."""
    tid = (payload or {}).get("thread_id", "").strip()
    if not tid:
        return {"status": "error", "message": "thread_id required"}
    note = (payload or {}).get("note", "").strip() or "no reply needed"
    oid = (payload or {}).get("order_id")
    if not _handled_note(f"needsreply:{tid}", oid, f"[William: {note}]"):
        return {"status": "error",
                "message": "settle write failed — the card will stay; "
                           "check server logs"}
    # ONE SETTLE, EVERY TWIN (William 2026-08-03): the same email's
    # thread:/noreply: board cards settle with the dismissal — a card he
    # already ruled on must never keep standing under another key.
    for twin in (f"thread:{tid}", f"noreply:{tid}"):
        _handled_note(twin, oid, f"[William: {note}]")
    return {"status": "ok", "thread_id": tid, "note": note}


@queue_router.post("/queue/handled")
def queue_handled(payload: Dict = Body(...),
                  _: bool = Depends(require_admin)):
    """The HANDLED button [admin] (William 7/31: "we get a response that is
    the end of what we think needs to be done"). Marks the task handled,
    marks the email thread read, and stamps the comeback clock — a NEW
    email on the thread RETURNS it to the queue as a NEEDS REPLY card,
    exactly like 'No reply needed'."""
    task_key = (payload or {}).get("task_key", "").strip()
    thread_id = (payload or {}).get("thread_id", "").strip()
    order_id = (payload or {}).get("order_id") or None
    if not task_key and not thread_id:
        return {"status": "error",
                "message": "task_key or thread_id required"}
    # ONE SETTLE, EVERY TWIN (William 2026-08-03, the QUEUE-resurface
    # lesson): one email can spawn a thread: card, a noreply: card AND a
    # needs-reply row — settling one through the wrong door left the twins
    # standing on his board. Resolve the thread id from whichever key came
    # in and settle ALL of them together.
    if not thread_id and task_key.split(":", 1)[0] in ("thread", "noreply"):
        thread_id = task_key.split(":", 1)[1]
    ok = True
    if task_key:
        ok = _handled_note(task_key, order_id, "[William: HANDLED]") and ok
    if thread_id:
        _mark_thread_read(thread_id)
        for twin in (f"thread:{thread_id}", f"noreply:{thread_id}",
                     f"needsreply:{thread_id}"):
            if twin != task_key:
                ok = _handled_note(twin, order_id,
                                   "[William: HANDLED]") and ok
    if not ok:
        return {"status": "error",
                "message": "settle write failed — the card will stay; "
                           "check server logs"}
    return {"status": "ok", "task_key": task_key, "thread_id": thread_id,
            "comeback": "a new email on this thread returns it as "
                        "NEEDS REPLY"}


@queue_router.post("/queue/thread-action")
def queue_thread_action(payload: Dict = Body(...),
                        _: bool = Depends(require_admin)):
    """Read / Archive / Delete straight on a Gmail thread [admin] — for
    the ledger-born NEEDS REPLY cards (William 7/31: "I need delete to
    parse out the spam newsletters and the like"). trash = Gmail Trash
    (30-day recovery). Archive/trash also stamp the card settled so it
    leaves the queue; a brand-new email later still resurfaces it."""
    tid = (payload or {}).get("thread_id", "").strip()
    action = (payload or {}).get("action", "").strip()
    order_id = (payload or {}).get("order_id") or None
    if not tid or action not in ("read", "archive", "trash"):
        return {"status": "error",
                "message": "thread_id + action (read|archive|trash) required"}
    if action == "trash":
        ok = _gmail_thread_call(f"threads/{tid}/trash")
    elif action == "archive":
        ok = _gmail_thread_call(f"threads/{tid}/modify",
                                {"removeLabelIds": ["UNREAD", "INBOX"]})
    else:
        ok = _mark_thread_read(tid)
    if not ok:
        return {"status": "error", "message": f"gmail {action} failed"}
    if action in ("archive", "trash"):
        if not _handled_note(f"needsreply:{tid}", order_id,
                             f"[William: {action}]"):
            return {"status": "error",
                    "message": f"gmail {action} done but the card settle "
                               "write failed — check server logs"}
    return {"status": "ok", "thread_id": tid, "action": action}


@queue_router.post("/queue/order-action")
def queue_order_action(payload: Dict = Body(...),
                       _: bool = Depends(require_admin)):
    """The full dropdown [admin] (William 7/31 'add all options including
    cancel' + 'when we do CANCEL we need a choose, notify or not notify'):
    any of the six checkpoints, or a lifecycle CANCEL — quiet by default,
    notify=true sends B2BWave's status-change email to the customer.
    Explicit pick only; typed words never fire anything."""
    action = (payload or {}).get("action", "").strip()
    if action not in ORDER_ACTIONS:
        return {"status": "error",
                "message": f"action must be one of {sorted(ORDER_ACTIONS)}"}
    task_key = (payload or {}).get("task_key", "").strip()
    order_id = str((payload or {}).get("order_id") or "").strip()
    notify = bool((payload or {}).get("notify"))
    if not order_id and task_key:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT order_id FROM task_board_items "
                            "WHERE task_key = %s", (task_key,))
                row = cur.fetchone()
                if row and row[0]:
                    order_id = str(row[0])
    if not order_id:
        return {"status": "error", "message": "no order id"}

    try:
        if action == "cancel_order":
            if notify:
                # customer notification: one change_status PATCH with the
                # notify flag BEFORE the quiet lifecycle path runs (its own
                # PATCH re-sets the same status silently — harmless)
                try:
                    import lifecycle_engine as le
                    import requests as _rq
                    canceled_id = int(os.environ.get(
                        "B2BWAVE_CANCELED_STATUS_ID", "7"))
                    nres = _rq.patch(
                        f"{le.B2BWAVE_URL}/api/orders/{order_id}/change_status",
                        json={"status_order_id": canceled_id,
                              "notify_customer": True},
                        headers={"Content-Type": "application/json",
                                 "Accept": "application/json"},
                        auth=(le.B2BWAVE_USERNAME, le.B2BWAVE_API_KEY),
                        timeout=30)
                    print(f"[QUEUE] notify-cancel PATCH {order_id}: "
                          f"{nres.status_code}")
                except Exception as ne:
                    print(f"[QUEUE] notify-cancel patch failed: {ne}")
            from lifecycle_engine import cancel_order
            result = cancel_order(
                order_id,
                reason=f"queue dropdown (William"
                       f"{', customer notified' if notify else ', quiet'})")
        else:
            from orders_routes import update_checkpoint, CheckpointUpdate
            update_checkpoint(order_id,
                              CheckpointUpdate(checkpoint=action,
                                               source="queue_dropdown"), True)
            result = {"checkpoint": action}
    except Exception as e:
        return {"status": "error", "order_id": order_id,
                "message": f"{action} failed: {e}"}

    if task_key:
        label = ORDER_ACTIONS[action]
        if action == "cancel_order":
            label += " — customer notified" if notify else " — quiet"
        _handled_note(task_key, order_id, f"[{label}] via queue dropdown")
    return {"status": "ok", "order_id": order_id, "fired": action,
            "notify": notify if action == "cancel_order" else None,
            "label": ORDER_ACTIONS[action], "result": result}
