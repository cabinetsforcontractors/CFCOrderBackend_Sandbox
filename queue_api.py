"""
queue_api.py — QUEUE BACKEND, Phase A+B (William-ruled 2026-07-30).

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

MONEY STRIP (ruling 5: the one line that stays above the queue):
  GET /queue/money-strip

DONE EVENTS (7/30): order_events minus the sync heartbeat noise, each row
with a `detail` line — a redirected send SAYS it was redirected.

AWAITING REPLY (William's law 7/30: "read is not replied" — he forwards an
email to a customer for a look, the thread goes read, but the sender is
still waiting on an answer): threads from the email ledger where the LAST
outside message has NO reply from us after it — read state irrelevant.
  - settles ITSELF when a real reply lands in the thread (ledger sent-row
    newer than the last inbound)
  - explicit dismiss door for "no reply needed" — but a NEW inbound after
    the dismissal RESURFACES the thread (dismissals only cover what had
    arrived by then)
  - noise senders (receipts, newsletters, robots) filtered out

Doors [admin]:
  POST /auto-settle/run?dry_run=true
  GET  /queue/money-strip
  GET  /queue/done-events
  GET  /queue/awaiting-reply
  POST /queue/awaiting-reply/dismiss {thread_id, order_id?, note?}
"""

import json
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

# Senders that never deserve a reply — receipts, newsletters, robots.
_NOISE_SENDER_RE = re.compile(
    r"no-?reply|noreply|notifications?@|mailer-daemon|do-?not-?reply|"
    r"@welcome\.|billtrust\.com|pirateship\.com|americanexpress\.com|"
    r"linkedin\.com|squareup\.com|@square\.com|messaging\.squareup|"
    r"@bottomline\.com|calendly\.com|@vercel\.com|@render\.com|"
    r"@github\.com|b2bemailservice|@dylt\.|@rlc\.com|billing@|"
    r"receipts?@|invoice@intuit|@close\.com", re.I)

# OUR boxes — an inbox row from ourselves (robot alerts) is not someone
# waiting on a reply.
_OUR_ADDR_RE = re.compile(
    r"orders@cabinetsforcontractors\.com|cabinetsforcontractors@gmail\.com|"
    r"wpjob1@gmail\.com|contact@allprocabinetsandflooring\.com", re.I)


def _mark_thread_read(thread_id: str) -> bool:
    try:
        from gmail_sync import get_gmail_access_token
        token = get_gmail_access_token()
        if not token:
            return False
        req = urllib.request.Request(
            f"https://gmail.googleapis.com/gmail/v1/users/me/threads/"
            f"{thread_id}/modify",
            data=json.dumps({"removeLabelIds": ["UNREAD"]}).encode(),
            method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except Exception as e:
        print(f"[AUTO-SETTLE] mark read failed {thread_id}: {e}")
        return False


def _handled_note(task_key: str, order_id: str, note: str):
    """Upsert a handled row — the trace the board shows."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO task_board_items
                        (task_key, order_id, status, note, note_at,
                         updated_at)
                    VALUES (%s, %s, 'handled', %s, NOW(), NOW())
                    ON CONFLICT (task_key) DO UPDATE
                    SET status = 'handled',
                        note = EXCLUDED.note,
                        note_at = NOW(), updated_at = NOW()
                """, (task_key, order_id or None, note))
            conn.commit()
    except Exception as e:
        print(f"[QUEUE] note failed {task_key}: {e}")


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
            cur.execute("""
                SELECT COALESCE(SUM(order_total), 0), COUNT(*)
                FROM orders
                WHERE payment_link_sent = TRUE
                  AND payment_received = FALSE
                  AND NOT is_complete
                  AND COALESCE(lifecycle_status, 'active') != 'deleted'
            """)
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

def awaiting_reply(days: int = 14) -> Dict:
    """Threads where the last OUTSIDE word has no answer from us — from the
    email ledger, so read/forwarded state changes nothing. Self-healing:
    a real reply (ledger sent-row after the inbound) drops the card; a
    dismissal only covers inbounds that existed when it was made."""
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
def get_awaiting_reply(days: int = 14, _: bool = Depends(require_admin)):
    """Conversations waiting on OUR word [admin] — read is not replied."""
    return awaiting_reply(days=days)


@queue_router.post("/queue/awaiting-reply/dismiss")
def dismiss_awaiting_reply(payload: Dict = Body(...),
                           _: bool = Depends(require_admin)):
    """No reply needed [admin]. Covers only what has arrived so far — a
    NEW inbound on the thread resurfaces the card."""
    tid = (payload or {}).get("thread_id", "").strip()
    if not tid:
        return {"status": "error", "message": "thread_id required"}
    note = (payload or {}).get("note", "").strip() or "no reply needed"
    _handled_note(f"needsreply:{tid}", (payload or {}).get("order_id"),
                  f"[William: {note}]")
    return {"status": "ok", "thread_id": tid, "note": note}
