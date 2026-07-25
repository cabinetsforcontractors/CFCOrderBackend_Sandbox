"""
task_board.py — Gmail-sweep task board (William's word 2026-07-25).

One screen: everything that needs a human ("NEEDS YOU") and a summary of
what the robots already did ("DONE RECENTLY"), with a note box per task so
William can record dispositions like "for S118998 I sent an email asking
if they saw the last one". A saved note moves the task to HANDLED.

Sources swept (each wrapped so one failure never kills the board):
  1. Unread inbox mail (customer / supplier / website classified)
  2. Robot action flags (sent copies to wpjob1@gmail.com)
  3. Gmail drafts waiting for review — EXCLUDING v6/CCAI handoff drafts
     (William 2026-07-25: handoff drafts are fine as-is, never poll them)
  4. Unpaid open orders (test rows excluded)
  5. supplier_orders rows needing action (pending/prepared/blocked/discrepancy)
  6. Active Daylight shipments (watch items)
Done side: order_events (last 3 days) + William's saved notes.

Endpoints [admin]:
  GET  /tasks            — the board
  POST /tasks/note       — {"task_key": "...", "note": "..."} upsert; empty note clears
READ-ONLY against Gmail — never sends, never drafts, never marks read.
"""

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Body

from auth import require_admin
from db_helpers import get_db
from gmail_sync import search_emails, get_email_content, gmail_api_request, extract_order_id

task_router = APIRouter(tags=["tasks"])

# Drafts whose subject matches this are lane handoffs — NEVER polled
# (William 2026-07-25: "the v6 handoff drafts are ok as is, remove polling them").
HANDOFF_RE = re.compile(r"CCAI|WS21|WS-CCAI|HANDOFF|Handoff|V6 Handoff", re.I)

# Robot action-flag subjects (the flag emails the engines send to wpjob1).
FLAG_RE = re.compile(
    r"PROGRESS DRAFT READY|APPROVAL DRAFT READY|DISCREPANCY|needs a human"
    r"|ALERT|ACTION|SEND CHECK|CONFIRM", re.I)

SUPPLIER_DOMAINS = {
    "ghicabinets.com": "GHI",
    "roccabinetry.com": "ROC",
    "roccabinetrytampa.com": "ROC Tampa",
    "cabinetstonellc.com": "Cabinet & Stone",
    "durastoneusa.com": "DuraStone",
    "milestonecabinetry.com": "Love-Milestone",
    "rlcarriers.com": "R+L Carriers",
    "dylt.com": "Daylight",
}
SUPPLIER_ADDRESSES = {
    "cabinetrydistribution@gmail.com": "Cabinetry Distribution (Li)",
}

# Test/pollution rows come from the ONE registry (test_registry.py, Beat 1);
# it falls back to its own seed list if the table can't be read.
from test_registry import test_order_ids as _registry_ids

OWN_ADDRESS = "cabinetsforcontractors@gmail.com"
FLAG_INBOX = "wpjob1@gmail.com"

_table_ready = False


def _ensure_table():
    global _table_ready
    if _table_ready:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS task_board_notes (
                    task_key   VARCHAR(200) PRIMARY KEY,
                    note       TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
        conn.commit()
    _table_ready = True


def _valid_oid(oid, known_ids):
    """Only surface an extracted order id when it is a REAL order —
    extract_order_id happily grabs years and reservation fragments."""
    oid = str(oid) if oid else ""
    return oid if oid in known_ids else None


def _sender_address(from_header: str) -> str:
    m = re.search(r"<([^>]+)>", from_header or "")
    return (m.group(1) if m else (from_header or "")).strip().lower()


def _classify_sender(addr: str, order_emails: dict):
    """-> (type, who) — customer / supplier / website / other."""
    if not addr:
        return "other", ""
    domain = addr.split("@")[-1]
    if addr in SUPPLIER_ADDRESSES:
        return "supplier", SUPPLIER_ADDRESSES[addr]
    if domain in SUPPLIER_DOMAINS:
        return "supplier", SUPPLIER_DOMAINS[domain]
    if "b2bemailservice" in domain:
        return "website", "B2BWave"
    if "squareup.com" in domain:
        return "payment", "Square"
    if addr in order_emails:
        oid, company = order_emails[addr]
        return "customer", f"{company} (order #{oid})"
    return "other", addr


def _order_email_map(cur) -> dict:
    """email -> (latest open order_id, company)."""
    cur.execute("""
        SELECT LOWER(email), order_id, COALESCE(company_name, customer_name, '')
        FROM orders
        WHERE is_complete = false AND email IS NOT NULL AND email <> ''
        ORDER BY order_date ASC
    """)
    out = {}
    for email, oid, company in cur.fetchall():
        out[email] = (oid, company)          # later (newer) rows overwrite
    return out


def _sweep_unread(order_emails: dict, days: int, known_ids: set):
    tasks = []
    for m in search_emails(f"is:unread in:inbox newer_than:{days}d", 40)[:30]:
        c = get_email_content(m["id"])
        if not c:
            continue
        addr = _sender_address(c.get("from", ""))
        kind, who = _classify_sender(addr, order_emails)
        oid = _valid_oid(extract_order_id(
            (c.get("subject") or "") + " " + (c.get("body") or "")[:500]), known_ids)
        tasks.append({
            "task_key": f"email:{m['id']}",
            "type": f"unread-{kind}",
            "title": c.get("subject") or "(no subject)",
            "detail": f"from {who or addr}" + (f" — order #{oid}" if oid else ""),
            "order_id": oid,
            "date": c.get("date", ""),
        })
    return tasks


def _sweep_robot_flags(known_ids: set):
    tasks = []
    for m in search_emails(f"in:sent to:{FLAG_INBOX} newer_than:3d", 20):
        c = get_email_content(m["id"])
        if not c:
            continue
        subject = c.get("subject") or ""
        if not FLAG_RE.search(subject):
            continue
        tasks.append({
            "task_key": f"flag:{m['id']}",
            "type": "robot-flag",
            "title": subject,
            "detail": "robot flagged this for a human",
            "order_id": _valid_oid(extract_order_id(subject), known_ids),
            "date": c.get("date", ""),
        })
    return tasks


def _sweep_drafts(known_ids: set):
    """Drafts waiting for review — handoff drafts excluded, never polled."""
    tasks = []
    data = gmail_api_request("drafts", {"maxResults": 50}) or {}
    for d in (data.get("drafts") or [])[:25]:
        msg_id = (d.get("message") or {}).get("id")
        if not msg_id:
            continue
        meta = gmail_api_request(f"messages/{msg_id}", {"format": "metadata"})
        if not meta:
            continue
        headers = {h["name"].lower(): h["value"]
                   for h in (meta.get("payload") or {}).get("headers", [])}
        subject = headers.get("subject", "")
        to = (headers.get("to") or "").lower()
        if HANDOFF_RE.search(subject):
            continue                      # lane handoffs — off the board, always
        if OWN_ADDRESS in to and FLAG_INBOX not in to:
            continue                      # self-notes, not actionable
        tasks.append({
            "task_key": f"draft:{d.get('id')}",
            "type": "draft-waiting",
            "title": subject or "(no subject)",
            "detail": f"draft to {to or '(no recipient)'} — review and send",
            "order_id": _valid_oid(extract_order_id(subject), known_ids),
            "date": headers.get("date", ""),
        })
    return tasks


def _sweep_unpaid(cur):
    cur.execute("""
        SELECT order_id, COALESCE(company_name, customer_name, ''), order_total,
               order_date, EXTRACT(DAY FROM NOW() - order_date)::int AS days_open
        FROM orders
        WHERE payment_received = false AND is_complete = false
          AND COALESCE(lifecycle_status, 'active') = 'active'
          AND COALESCE(order_total, 0) > 0
        ORDER BY order_date ASC
    """)
    tasks = []
    test_ids = _registry_ids()
    for oid, company, total, odate, days_open in cur.fetchall():
        if str(oid) in test_ids:
            continue
        tasks.append({
            "task_key": f"unpaid:{oid}",
            "type": "unpaid-order",
            "title": f"Order #{oid} unpaid — {company}",
            "detail": f"${float(total):,.2f}, ordered {odate:%m/%d}, open {days_open} days",
            "order_id": str(oid),
            "date": odate.isoformat() if odate else "",
        })
    return tasks


def _sweep_supplier_orders(cur):
    cur.execute("""
        SELECT id, order_id, warehouse, status
        FROM supplier_orders
        WHERE status IN ('pending', 'prepared', 'blocked', 'discrepancy')
        ORDER BY id DESC
    """)
    tasks = []
    test_ids = _registry_ids()
    for row_id, oid, warehouse, status in cur.fetchall():
        if str(oid) in test_ids:
            continue
        tasks.append({
            "task_key": f"supplier:{row_id}",
            "type": "supplier-action",
            "title": f"Supplier order — #{oid} @ {warehouse}: {status.upper()}",
            "detail": {"pending": "waiting on dispatch",
                       "prepared": "portal upload needed",
                       "blocked": "blocked — see dispatch note",
                       "discrepancy": "supplier doc disagrees — review"}.get(status, status),
            "order_id": str(oid),
            "date": "",
        })
    return tasks


def _sweep_daylight(cur):
    cur.execute("""
        SELECT probill, order_id, status FROM daylight_shipments WHERE active = true
    """)
    tasks = []
    for probill, oid, status in cur.fetchall():
        first = (status or "").split("|")[0].strip()
        tasks.append({
            "task_key": f"daylight:{probill}",
            "type": "shipment-watch",
            "title": f"Daylight PRO {probill} — order #{oid}",
            "detail": f"latest: {first or 'no scan yet'} (poller rides the sweep)",
            "order_id": str(oid),
            "date": "",
        })
    return tasks


def _recent_events(cur):
    cur.execute("""
        SELECT order_id, event_type, source, created_at
        FROM order_events
        WHERE created_at > NOW() - INTERVAL '3 days'
        ORDER BY created_at DESC
        LIMIT 60
    """)
    return [{
        "order_id": str(oid),
        "event_type": etype,
        "source": source or "",
        "at": created.isoformat() if created else "",
    } for oid, etype, source, created in cur.fetchall()]


@task_router.get("/tasks")
def get_tasks(days: int = 7, _: bool = Depends(require_admin)):
    _ensure_table()
    errors = {}
    todo = []

    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                order_emails = _order_email_map(cur)
            except Exception as e:
                order_emails, errors["order_map"] = {}, str(e)
            try:
                cur.execute("SELECT order_id FROM orders")
                known_ids = {str(r[0]) for r in cur.fetchall()}
            except Exception as e:
                known_ids, errors["known_ids"] = set(), str(e)
                conn.rollback()
            for name, fn in [("unpaid", _sweep_unpaid),
                             ("supplier_orders", _sweep_supplier_orders),
                             ("daylight", _sweep_daylight)]:
                try:
                    todo.extend(fn(cur))
                except Exception as e:
                    errors[name] = str(e)
                    conn.rollback()
            try:
                done_events = _recent_events(cur)
            except Exception as e:
                done_events, errors["events"] = [], str(e)
                conn.rollback()
            try:
                cur.execute("SELECT task_key, note, updated_at FROM task_board_notes")
                notes = {k: {"note": n, "at": u.isoformat() if u else ""}
                         for k, n, u in cur.fetchall()}
            except Exception as e:
                notes, errors["notes"] = {}, str(e)
                conn.rollback()

    for name, fn, args in [("unread", _sweep_unread, (order_emails, days, known_ids)),
                           ("robot_flags", _sweep_robot_flags, (known_ids,)),
                           ("drafts", _sweep_drafts, (known_ids,))]:
        try:
            todo.extend(fn(*args))
        except Exception as e:
            errors[name] = str(e)

    handled = []
    open_tasks = []
    for t in todo:
        n = notes.get(t["task_key"])
        if n:
            t["note"] = n["note"]
            t["note_at"] = n["at"]
            handled.append(t)
        else:
            open_tasks.append(t)

    return {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "todo": open_tasks,
        "handled": handled,
        "done_events": done_events,
        "errors": errors or None,
    }


@task_router.post("/tasks/note")
def save_note(payload: dict = Body(...), _: bool = Depends(require_admin)):
    _ensure_table()
    task_key = (payload.get("task_key") or "").strip()
    note = (payload.get("note") or "").strip()
    if not task_key:
        return {"status": "error", "message": "task_key required"}
    with get_db() as conn:
        with conn.cursor() as cur:
            if note:
                cur.execute("""
                    INSERT INTO task_board_notes (task_key, note)
                    VALUES (%s, %s)
                    ON CONFLICT (task_key)
                    DO UPDATE SET note = EXCLUDED.note, updated_at = NOW()
                """, (task_key, note))
                action = "saved"
            else:
                cur.execute("DELETE FROM task_board_notes WHERE task_key = %s", (task_key,))
                action = "cleared"
        conn.commit()
    return {"status": "ok", "task_key": task_key, "action": action}
