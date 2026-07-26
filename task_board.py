"""
task_board.py — TASK BOARD v2 (William's "v2 build", 2026-07-26).

v1 swept Gmail live on every page load. v2 MATERIALIZES the board:
a sweep (riding the gmail-sync cycle + POST /tasks/sweep) writes tasks into
task_board_items; GET /tasks reads the table instantly. Tasks keep stable
lifecycle: open -> handled (note saved) -> reopened, or -> gone (source
vanished). Layout ruling: ONE Tasks tab, TWO boards stacked — ORDER TASKS
(anything order-flavored) then OTHER TASKS (everything else + Add-a-task).

Task sources:
  ORDER board: unpaid orders, supplier-order actions, Daylight watches,
    unread customer/supplier mail, robot flags, drafts awaiting review,
    website + payment notifications.
  OTHER board: other unread mail, NO-REPLY WATCH (our outbound threads
    unanswered >= 2 BUSINESS days — William's law), MANUAL tasks
    ("I called Eddie…", optional follow-up date), PLAUD recorder summaries.

SMART NOTES (v2): saving a note on an order-linked task parses safe intents:
  "invoice sent"/"payment link sent"/"link sent" -> payment_link_sent
  "payment received"/"customer paid"/"paid"      -> payment_received
  "picked up"/"delivered"/"complete(d)"          -> is_complete
  (sent_to_warehouse is DELIBERATELY not an intent — that checkpoint fires
   supplier emails; too heavy for a text note.)

EMAIL ACTIONS (v2, William-ruled incl. hard delete w/ double-confirm in UI):
  POST /tasks/email-action {task_key, action: read|archive|trash}
  trash = Gmail Trash (30-day recovery = the failsafe).

Endpoints [admin]: GET /tasks · POST /tasks/sweep · POST /tasks/note ·
  POST /tasks/manual · POST /tasks/plaud · GET /tasks/plaud/{id} ·
  POST /tasks/email-action
"""

import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Body

from auth import require_admin
from db_helpers import get_db
from gmail_sync import (search_emails, get_email_content, gmail_api_request,
                        extract_order_id)

task_router = APIRouter(tags=["tasks"])

from test_registry import test_order_ids as _registry_ids

HANDOFF_RE = re.compile(r"CCAI|WS21|WS-CCAI|HANDOFF|Handoff|V6 Handoff", re.I)
FLAG_RE = re.compile(
    r"PROGRESS DRAFT READY|APPROVAL DRAFT READY|DISCREPANCY|needs a human"
    r"|ALERT|ACTION|SEND CHECK|CONFIRM", re.I)

SUPPLIER_DOMAINS = {
    "ghicabinets.com": "GHI", "roccabinetry.com": "ROC",
    "roccabinetrytampa.com": "ROC Tampa", "cabinetstonellc.com": "Cabinet & Stone",
    "durastoneusa.com": "DuraStone", "milestonecabinetry.com": "Love-Milestone",
    "rlcarriers.com": "R+L Carriers", "dylt.com": "Daylight",
}
SUPPLIER_ADDRESSES = {"cabinetrydistribution@gmail.com": "Cabinetry Distribution (Li)"}

OWN_ADDRESSES = {a.strip().lower() for a in os.environ.get(
    "OWN_EMAIL_ADDRESSES",
    "orders@cabinetsforcontractors.com,cabinetsforcontractors@gmail.com"
).split(",") if a.strip()}
FLAG_INBOX = os.environ.get("FLAG_INBOX_EMAIL", "wpjob1@gmail.com").strip()

NO_REPLY_BUSINESS_DAYS = 2
ORDER_TYPES = {"unpaid-order", "supplier-action", "shipment-watch",
               "unread-customer", "unread-supplier", "unread-website",
               "unread-payment", "robot-flag", "draft-waiting"}

_table_ready = False


# =============================================================================
# TABLES
# =============================================================================

def _ensure_tables(conn):
    global _table_ready
    if _table_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS task_board_items (
                task_key   VARCHAR(200) PRIMARY KEY,
                board      VARCHAR(10) NOT NULL,
                type       VARCHAR(30) NOT NULL,
                title      TEXT NOT NULL,
                detail     TEXT DEFAULT '',
                order_id   VARCHAR(50),
                gmail_id   VARCHAR(120),
                thread_id  VARCHAR(120),
                date_str   VARCHAR(80) DEFAULT '',
                due_date   DATE,
                status     VARCHAR(12) NOT NULL DEFAULT 'open',
                note       TEXT,
                note_at    TIMESTAMP WITH TIME ZONE,
                first_seen TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_seen  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS plaud_notes (
                id SERIAL PRIMARY KEY,
                title TEXT DEFAULT '',
                body  TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        # legacy v1 notes table stays for one-time migration
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


# =============================================================================
# SOURCE SWEEPS (each returns task dicts; board set per type)
# =============================================================================

def _board_for(ttype: str) -> str:
    return "order" if ttype in ORDER_TYPES else "other"


def _sender_address(from_header: str) -> str:
    m = re.search(r"<([^>]+)>", from_header or "")
    return (m.group(1) if m else (from_header or "")).strip().lower()


def _classify_sender(addr: str, order_emails: dict):
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
    cur.execute("""
        SELECT LOWER(email), order_id, COALESCE(company_name, customer_name, '')
        FROM orders WHERE is_complete = false AND email IS NOT NULL AND email <> ''
        ORDER BY order_date ASC
    """)
    out = {}
    for email, oid, company in cur.fetchall():
        out[email] = (oid, company)
    return out


def _valid_oid(oid, known_ids):
    oid = str(oid) if oid else ""
    return oid if oid in known_ids else None


def _sweep_unread(order_emails, known_ids):
    tasks = []
    for m in search_emails("is:unread in:inbox newer_than:7d", 40)[:30]:
        c = get_email_content(m["id"])
        if not c:
            continue
        addr = _sender_address(c.get("from", ""))
        kind, who = _classify_sender(addr, order_emails)
        oid = _valid_oid(extract_order_id(
            (c.get("subject") or "") + " " + (c.get("body") or "")[:500]), known_ids)
        tasks.append({
            "task_key": f"email:{m['id']}", "type": f"unread-{kind}",
            "title": c.get("subject") or "(no subject)",
            "detail": f"from {who or addr}" + (f" — order #{oid}" if oid else ""),
            "order_id": oid, "gmail_id": m["id"], "thread_id": m.get("threadId"),
            "date_str": c.get("date", ""),
        })
    return tasks


def _sweep_robot_flags(known_ids):
    tasks = []
    for m in search_emails(f"in:sent to:{FLAG_INBOX} newer_than:3d", 20):
        c = get_email_content(m["id"])
        if not c:
            continue
        subject = c.get("subject") or ""
        if not FLAG_RE.search(subject):
            continue
        tasks.append({
            "task_key": f"flag:{m['id']}", "type": "robot-flag", "title": subject,
            "detail": "robot flagged this for a human",
            "order_id": _valid_oid(extract_order_id(subject), known_ids),
            "gmail_id": m["id"], "thread_id": m.get("threadId"),
            "date_str": c.get("date", ""),
        })
    return tasks


def _sweep_drafts(known_ids):
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
            continue
        if any(own in to for own in OWN_ADDRESSES) and FLAG_INBOX not in to:
            continue
        tasks.append({
            "task_key": f"draft:{d.get('id')}", "type": "draft-waiting",
            "title": subject or "(no subject)",
            "detail": f"draft to {to or '(no recipient)'} — review and send",
            "order_id": _valid_oid(extract_order_id(subject), known_ids),
            "gmail_id": msg_id, "date_str": headers.get("date", ""),
        })
    return tasks


def _sweep_unpaid(cur):
    cur.execute("""
        SELECT order_id, COALESCE(company_name, customer_name, ''), order_total,
               order_date, EXTRACT(DAY FROM NOW() - order_date)::int
        FROM orders
        WHERE payment_received = false AND is_complete = false
          AND COALESCE(lifecycle_status, 'active') = 'active'
          AND COALESCE(order_total, 0) > 0
        ORDER BY order_date ASC
    """)
    test_ids = _registry_ids()
    tasks = []
    for oid, company, total, odate, days_open in cur.fetchall():
        if str(oid) in test_ids:
            continue
        tasks.append({
            "task_key": f"unpaid:{oid}", "type": "unpaid-order",
            "title": f"Order #{oid} unpaid — {company}",
            "detail": f"${float(total):,.2f}, ordered {odate:%m/%d}, open {days_open} days",
            "order_id": str(oid), "date_str": odate.isoformat() if odate else "",
        })
    return tasks


def _sweep_supplier_orders(cur):
    cur.execute("""
        SELECT id, order_id, warehouse, status FROM supplier_orders
        WHERE status IN ('pending', 'prepared', 'blocked', 'discrepancy')
        ORDER BY id DESC
    """)
    test_ids = _registry_ids()
    tasks = []
    for row_id, oid, warehouse, status in cur.fetchall():
        if str(oid) in test_ids:
            continue
        tasks.append({
            "task_key": f"supplier:{row_id}", "type": "supplier-action",
            "title": f"Supplier order — #{oid} @ {warehouse}: {status.upper()}",
            "detail": {"pending": "waiting on dispatch",
                       "prepared": "portal upload needed",
                       "blocked": "blocked — see dispatch note",
                       "discrepancy": "supplier doc disagrees — review"}.get(status, status),
            "order_id": str(oid), "date_str": "",
        })
    return tasks


def _sweep_daylight(cur):
    cur.execute("SELECT probill, order_id, status FROM daylight_shipments WHERE active = true")
    tasks = []
    for probill, oid, status in cur.fetchall():
        first = (status or "").split("|")[0].strip()
        tasks.append({
            "task_key": f"daylight:{probill}", "type": "shipment-watch",
            "title": f"Daylight PRO {probill} — order #{oid}",
            "detail": f"latest: {first or 'no scan yet'}",
            "order_id": str(oid), "date_str": "",
        })
    return tasks


def _sweep_no_reply(known_ids):
    """Our outbound threads with no answer for >= NO_REPLY_BUSINESS_DAYS."""
    from business_days import business_days_since
    tasks = []
    seen_threads = set()
    for m in search_emails("in:sent newer_than:10d", 40):
        tid = m.get("threadId")
        if not tid or tid in seen_threads:
            continue
        seen_threads.add(tid)
        if len(seen_threads) > 25:
            break
        th = gmail_api_request(f"threads/{tid}", {"format": "metadata"})
        if not th or not th.get("messages"):
            continue
        msgs = th["messages"]
        last = msgs[-1]
        h = {x["name"].lower(): x["value"]
             for x in (last.get("payload") or {}).get("headers", [])}
        frm = _sender_address(h.get("from", ""))
        to = (h.get("to") or "").lower()
        subject = h.get("subject", "") or "(no subject)"
        # only when the LAST word in the thread is OURS
        if frm not in OWN_ADDRESSES:
            continue
        # skip robot flags, notes-to-self, handoffs
        if FLAG_INBOX in to or HANDOFF_RE.search(subject):
            continue
        if any(own in to for own in OWN_ADDRESSES):
            continue
        try:
            sent_at = datetime.fromtimestamp(int(last.get("internalDate", "0")) / 1000,
                                             tz=timezone.utc)
        except (TypeError, ValueError):
            continue
        age = business_days_since(sent_at)
        if age < NO_REPLY_BUSINESS_DAYS:
            continue
        tasks.append({
            "task_key": f"noreply:{tid}", "type": "no-reply",
            "title": f"No answer yet: {subject}",
            "detail": f"we wrote to {to.split(',')[0].strip()} — {age} business days, no reply",
            "order_id": _valid_oid(extract_order_id(subject), known_ids),
            "gmail_id": last.get("id"), "thread_id": tid,
            "date_str": sent_at.isoformat(),
        })
    return tasks


# =============================================================================
# THE SWEEP (materializer)
# =============================================================================

def run_task_sweep(conn) -> dict:
    _ensure_tables(conn)
    sweep_start = datetime.now(timezone.utc)
    errors = {}
    tasks = []
    with conn.cursor() as cur:
        try:
            order_emails = _order_email_map(cur)
        except Exception as e:
            order_emails, errors["order_map"] = {}, str(e)
            conn.rollback()
        try:
            cur.execute("SELECT order_id FROM orders")
            known_ids = {str(r[0]) for r in cur.fetchall()}
        except Exception as e:
            known_ids, errors["known_ids"] = set(), str(e)
            conn.rollback()
        for name, fn in [("unpaid", _sweep_unpaid),
                         ("supplier", _sweep_supplier_orders),
                         ("daylight", _sweep_daylight)]:
            try:
                tasks.extend(fn(cur))
            except Exception as e:
                errors[name] = str(e)
                conn.rollback()
    for name, fn, args in [("unread", _sweep_unread, (order_emails, known_ids)),
                           ("flags", _sweep_robot_flags, (known_ids,)),
                           ("drafts", _sweep_drafts, (known_ids,)),
                           ("noreply", _sweep_no_reply, (known_ids,))]:
        try:
            tasks.extend(fn(*args))
        except Exception as e:
            errors[name] = str(e)

    upserted = 0
    with conn.cursor() as cur:
        for t in tasks:
            cur.execute("""
                INSERT INTO task_board_items
                    (task_key, board, type, title, detail, order_id, gmail_id,
                     thread_id, date_str, status, last_seen, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',NOW(),NOW())
                ON CONFLICT (task_key) DO UPDATE SET
                    title = EXCLUDED.title, detail = EXCLUDED.detail,
                    order_id = COALESCE(EXCLUDED.order_id, task_board_items.order_id),
                    gmail_id = COALESCE(EXCLUDED.gmail_id, task_board_items.gmail_id),
                    thread_id = COALESCE(EXCLUDED.thread_id, task_board_items.thread_id),
                    date_str = EXCLUDED.date_str,
                    last_seen = NOW(), updated_at = NOW(),
                    status = CASE WHEN task_board_items.status = 'gone'
                                  THEN 'open' ELSE task_board_items.status END
            """, (t["task_key"], _board_for(t["type"]), t["type"], t["title"],
                  t.get("detail", ""), t.get("order_id"), t.get("gmail_id"),
                  t.get("thread_id"), t.get("date_str", "")))
            upserted += 1
        # one-time migration of v1 notes
        cur.execute("""
            UPDATE task_board_items i
            SET note = n.note, note_at = n.updated_at, status = 'handled'
            FROM task_board_notes n
            WHERE i.task_key = n.task_key AND i.note IS NULL
        """)
        # source-derived tasks that vanished -> gone (manual/plaud never swept away)
        cur.execute("""
            UPDATE task_board_items
            SET status = 'gone', updated_at = NOW()
            WHERE type NOT IN ('manual', 'plaud') AND status = 'open'
              AND last_seen < %s
        """, (sweep_start,))
        gone = cur.rowcount
    conn.commit()
    return {"status": "ok", "swept": upserted, "gone": gone,
            "errors": errors or None,
            "at": sweep_start.isoformat()}


# =============================================================================
# SMART NOTES
# =============================================================================

_INTENTS = [
    (re.compile(r"invoice sent|payment link sent|link sent", re.I), "payment_link_sent"),
    (re.compile(r"payment received|customer paid|\bpaid\b", re.I), "payment_received"),
    (re.compile(r"picked up|delivered|\bcomplete(d)?\b", re.I), "is_complete"),
]


def _apply_smart_note(order_id: str, note: str) -> list:
    actions = []
    for rx, checkpoint in _INTENTS:
        if rx.search(note):
            try:
                from orders_routes import update_checkpoint, CheckpointUpdate
                update_checkpoint(order_id,
                                  CheckpointUpdate(checkpoint=checkpoint,
                                                   source="smart_note"), True)
                actions.append(checkpoint)
            except Exception as e:
                actions.append(f"{checkpoint} FAILED: {e}")
            break        # first matching intent only — no chain reactions
    return actions


# =============================================================================
# ENDPOINTS
# =============================================================================

@task_router.get("/tasks")
def get_tasks(_: bool = Depends(require_admin)):
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT task_key, board, type, title, detail, order_id, gmail_id,
                       thread_id, date_str, due_date, status, note, note_at,
                       last_seen
                FROM task_board_items
                WHERE status IN ('open', 'handled')
                ORDER BY (status = 'open') DESC, last_seen DESC
            """)
            cols = ["task_key", "board", "type", "title", "detail", "order_id",
                    "gmail_id", "thread_id", "date_str", "due_date", "status",
                    "note", "note_at", "last_seen"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("""
                SELECT order_id, event_type, source, created_at FROM order_events
                WHERE created_at > NOW() - INTERVAL '3 days'
                ORDER BY created_at DESC LIMIT 60
            """)
            events = [{"order_id": str(a), "event_type": b, "source": c or "",
                       "at": d.isoformat() if d else ""} for a, b, c, d in cur.fetchall()]
            cur.execute("SELECT MAX(last_seen) FROM task_board_items WHERE type NOT IN ('manual','plaud')")
            last_sweep = cur.fetchone()[0]

    for r in rows:
        for k in ("due_date", "note_at", "last_seen"):
            if r.get(k) is not None and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    open_rows = [r for r in rows if r["status"] == "open"]
    handled = [r for r in rows if r["status"] == "handled"][:25]
    return {
        "status": "ok",
        "last_sweep": last_sweep.isoformat() if last_sweep else None,
        "order_tasks": [r for r in open_rows if r["board"] == "order"],
        "other_tasks": [r for r in open_rows if r["board"] == "other"],
        "handled": handled,
        "done_events": events,
        "counts": {"order": sum(1 for r in open_rows if r["board"] == "order"),
                   "other": sum(1 for r in open_rows if r["board"] == "other")},
    }


@task_router.post("/tasks/sweep")
def sweep_now(_: bool = Depends(require_admin)):
    with get_db() as conn:
        return run_task_sweep(conn)


@task_router.post("/tasks/note")
def save_note(payload: dict = Body(...), _: bool = Depends(require_admin)):
    task_key = (payload.get("task_key") or "").strip()
    note = (payload.get("note") or "").strip()
    if not task_key:
        return {"status": "error", "message": "task_key required"}
    actions = []
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT order_id FROM task_board_items WHERE task_key = %s",
                        (task_key,))
            row = cur.fetchone()
            if not row:
                return {"status": "error", "message": f"unknown task {task_key}"}
            order_id = row[0]
            if note:
                cur.execute("""
                    UPDATE task_board_items
                    SET note = %s, note_at = NOW(), status = 'handled', updated_at = NOW()
                    WHERE task_key = %s
                """, (note, task_key))
            else:
                cur.execute("""
                    UPDATE task_board_items
                    SET note = NULL, note_at = NULL, status = 'open', updated_at = NOW()
                    WHERE task_key = %s
                """, (task_key,))
        conn.commit()
    if note and order_id:
        actions = _apply_smart_note(str(order_id), note)
    return {"status": "ok", "task_key": task_key,
            "action": "saved" if note else "reopened",
            "smart_actions": actions}


@task_router.post("/tasks/manual")
def add_manual(payload: dict = Body(...), _: bool = Depends(require_admin)):
    text = (payload.get("text") or "").strip()
    due = (payload.get("due_date") or "").strip() or None
    if not text:
        return {"status": "error", "message": "text required"}
    key = f"manual:{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO task_board_items
                    (task_key, board, type, title, detail, due_date, date_str, status)
                VALUES (%s, 'other', 'manual', %s, %s, %s, %s, 'open')
            """, (key, text, f"added by William" + (f", follow up {due}" if due else ""),
                  due, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    return {"status": "ok", "task_key": key}


@task_router.post("/tasks/plaud")
def add_plaud(payload: dict = Body(...), _: bool = Depends(require_admin)):
    body = (payload.get("text") or "").strip()
    title = (payload.get("title") or "").strip() or "Recorder summary"
    if not body:
        return {"status": "error", "message": "text required"}
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO plaud_notes (title, body) VALUES (%s, %s) RETURNING id",
                        (title, body))
            pid = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO task_board_items
                    (task_key, board, type, title, detail, date_str, status)
                VALUES (%s, 'other', 'plaud', %s, %s, %s, 'open')
            """, (f"plaud:{pid}", f"Recorder summary: {title}",
                  f"{len(body):,} chars — open it, spin off tasks as needed",
                  datetime.now(timezone.utc).isoformat()))
        conn.commit()
    return {"status": "ok", "plaud_id": pid, "task_key": f"plaud:{pid}"}


@task_router.get("/tasks/plaud/{pid}")
def get_plaud(pid: int, _: bool = Depends(require_admin)):
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT title, body, created_at FROM plaud_notes WHERE id = %s", (pid,))
            row = cur.fetchone()
    if not row:
        return {"status": "error", "message": "not found"}
    return {"status": "ok", "title": row[0], "body": row[1],
            "created_at": row[2].isoformat() if row[2] else ""}


@task_router.post("/tasks/email-action")
def email_action(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """read = clear UNREAD · archive = clear UNREAD + leave inbox ·
    trash = Gmail Trash (30-day recovery). UI double-confirms trash."""
    task_key = (payload.get("task_key") or "").strip()
    action = (payload.get("action") or "").strip().lower()
    if action not in ("read", "archive", "trash"):
        return {"status": "error", "message": "action must be read|archive|trash"}
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT gmail_id FROM task_board_items WHERE task_key = %s",
                        (task_key,))
            row = cur.fetchone()
    if not row or not row[0]:
        return {"status": "error", "message": "task has no gmail message"}
    gmail_id = row[0]
    from ghi_inbox import _gmail_post
    if action == "read":
        res = _gmail_post(f"messages/{gmail_id}/modify", {"removeLabelIds": ["UNREAD"]})
    elif action == "archive":
        res = _gmail_post(f"messages/{gmail_id}/modify",
                          {"removeLabelIds": ["UNREAD", "INBOX"]})
    else:
        res = _gmail_post(f"messages/{gmail_id}/trash", {})
    ok = bool(res)
    if ok:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE task_board_items
                    SET status = 'handled', note = %s, note_at = NOW(), updated_at = NOW()
                    WHERE task_key = %s
                """, (f"[{action}] by William", task_key))
            conn.commit()
    return {"status": "ok" if ok else "error", "action": action, "gmail_id": gmail_id}
