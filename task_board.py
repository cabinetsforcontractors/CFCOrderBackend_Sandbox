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

# SENDER IDENTIFIER (William ruling 2026-07-27): the vanity DOMAIN names the
# supplier no matter which person sends; suppliers on plain gmail (L&C, LI)
# are named by their exact address.
SUPPLIER_DOMAINS = {
    "ghicabinets.com": "GHI", "roccabinetry.com": "ROC",
    "roccabinetrytampa.com": "ROC Tampa", "cabinetstonellc.com": "Cabinet & Stone",
    "durastoneusa.com": "DuraStone", "milestonecabinetry.com": "Love-Milestone",
    "dlcabinetry.com": "DL Cabinetry", "lnccabinetry.com": "L&C Cabinetry",
    "rlcarriers.com": "R+L Carriers", "dylt.com": "Daylight",
}
SUPPLIER_ADDRESSES = {
    "cabinetrydistribution@gmail.com": "Cabinetry Distribution (Li)",
    "lnccabinetryvab@gmail.com": "L&C Cabinetry",
}

OWN_ADDRESSES = {a.strip().lower() for a in os.environ.get(
    "OWN_EMAIL_ADDRESSES",
    "orders@cabinetsforcontractors.com,cabinetsforcontractors@gmail.com"
).split(",") if a.strip()}
FLAG_INBOX = os.environ.get("FLAG_INBOX_EMAIL", "wpjob1@gmail.com").strip()

NO_REPLY_BUSINESS_DAYS = 2
ORDER_TYPES = {"unpaid-order", "supplier-action", "shipment-watch",
               "unread-customer", "unread-supplier", "unread-website",
               "unread-payment", "robot-flag", "draft-waiting", "info"}

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
        # v3 (William approved 2026-07-29): keyword-learning rules — the
        # matcher reads this table so tasks get smarter over time without a
        # deploy; William + the robot add rows as lessons occur.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS task_keywords (
                id SERIAL PRIMARY KEY,
                pattern TEXT NOT NULL,
                label   TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        cur.execute("SELECT COUNT(*) FROM task_keywords")
        if cur.fetchone()[0] == 0:
            for pat, label in [
                ("out of stock", "stock issue"),
                ("backorder", "stock issue"),
                ("ready to ship", "ready to ship"),
                ("ready for pickup", "ready to ship"),
                ("has been delivered", "delivery notice"),
                ("out for delivery", "delivery notice"),
                ("damage", "damage mentioned"),
                ("missing", "shortage claim"),
                ("cancel", "cancel mentioned"),
                ("PAYMENT LINK", "old-era invoice email"),
                ("password reset", "password reset"),
            ]:
                cur.execute("""INSERT INTO task_keywords (pattern, label)
                               VALUES (%s, %s)""", (pat, label))
    conn.commit()
    _table_ready = True


def _keyword_rules(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT pattern, label FROM task_keywords
                       WHERE enabled = TRUE""")
        return cur.fetchall()


def _keyword_tags(text: str, rules) -> str:
    """' ⚡label1 ⚡label2' for every enabled rule whose pattern appears in
    text (case-insensitive substring)."""
    hits = []
    low = (text or "").lower()
    for pattern, label in rules:
        if pattern.lower() in low and label not in hits:
            hits.append(label)
    return "".join(f" ⚡{h}" for h in hits)


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
    # INFO EMAILS (William 2026-07-29, the "Text from nationwide" lesson):
    # mail from OUR OWN addresses into the inbox is William feeding the
    # board information — it becomes an INFO task (order board when
    # linkable), its body gets keyword-read, and it must NEVER be
    # auto-settled by reply-awareness (the last word is ours by definition).
    if addr in OWN_ADDRESSES or addr == FLAG_INBOX.lower():
        return "info", "William (info)"
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


def _sweep_unread(order_emails, known_ids, kw_rules=()):
    """Beat 5 (2026-07-27): ONE task per THREAD (the Nationwide collapse —
    a long conversation is one board item, '3 unread in thread', not three
    rows) and LEDGER-FED metadata: messages already in the email ledger cost
    zero Gmail fetches; only unledgered ids fall back to a live fetch.
    Gmail's unread search returns newest first, so the first message seen
    per thread titles the task."""
    msgs = search_emails("is:unread in:inbox newer_than:7d", 40)[:30]
    ids = [m["id"] for m in msgs if m.get("id")]
    meta = {}
    if ids:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT message_id, thread_id, from_addr, subject,
                               email_date, order_ids
                        FROM email_ledger WHERE message_id = ANY(%s)
                    """, (ids,))
                    for mid, tid, frm, subj, edate, oids in cur.fetchall():
                        meta[mid] = {
                            "thread": tid, "from": frm or "",
                            "subject": subj or "",
                            "date": edate.isoformat() if edate else "",
                            "order_ids": oids or ""}
        except Exception:
            pass
    threads = {}
    for m in msgs:
        mid = m.get("id")
        if not mid:
            continue
        mm = meta.get(mid)
        if not mm:
            c = get_email_content(mid)
            if not c:
                continue
            mm = {"thread": m.get("threadId"), "from": c.get("from", ""),
                  "subject": c.get("subject") or "",
                  "date": c.get("date", ""),
                  "order_ids": extract_order_id(
                      (c.get("subject") or "") + " "
                      + (c.get("body") or "")[:500]) or ""}
        tid = mm["thread"] or mid
        if tid in threads:
            threads[tid]["count"] += 1     # newest already holds the title
        else:
            threads[tid] = {"count": 1, "latest": mm, "latest_id": mid}
    tasks = []
    for tid, t in threads.items():
        mm = t["latest"]
        addr = _sender_address(mm["from"])
        kind, who = _classify_sender(addr, order_emails)
        oid = None
        for cand in str(mm.get("order_ids") or "").split(","):
            oid = _valid_oid(cand.strip(), known_ids)
            if oid:
                break
        if not oid:
            oid = _valid_oid(extract_order_id(mm["subject"]), known_ids)
        extra = f" ({t['count']} unread in thread)" if t["count"] > 1 else ""
        # info emails: keyword-read the BODY too (they carry instructions —
        # "order 5560 needs attention" — not just subjects)
        kw_text = mm["subject"]
        if kind == "info":
            try:
                c = get_email_content(t["latest_id"])
                body = (c or {}).get("body") or ""
                kw_text = f"{mm['subject']} {body[:1500]}"
                if not oid:
                    oid = _valid_oid(extract_order_id(body[:1500]), known_ids)
            except Exception:
                pass
        tags = _keyword_tags(kw_text, kw_rules)
        tasks.append({
            "task_key": f"thread:{tid}",
            "type": "info" if kind == "info" else f"unread-{kind}",
            "title": mm["subject"] or "(no subject)",
            "detail": f"from {who or addr}{extra}"
                      + (f" — order #{oid}" if oid else "") + tags,
            "order_id": oid, "gmail_id": t["latest_id"], "thread_id": tid,
            "date_str": mm["date"],
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
    try:
        kw_rules = _keyword_rules(conn)
    except Exception as e:
        kw_rules, errors["keywords"] = (), str(e)
        conn.rollback()
    for name, fn, args in [("unread", _sweep_unread,
                            (order_emails, known_ids, kw_rules)),
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
                    type = EXCLUDED.type, board = EXCLUDED.board,
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
            WHERE type NOT IN ('manual', 'plaud', 'follow-up') AND status = 'open'
              AND last_seen < %s
        """, (sweep_start,))
        gone = cur.rowcount
        # REPLY-AWARENESS (William approved 2026-07-29, the 5696 case):
        # when the LAST word on a thread is OURS (ledger sent-folder newer
        # than the last inbound), the thread task settles itself with a
        # [replied] note — his answers get recorded instead of the task
        # sitting stale.
        replied = 0
        try:
            cur.execute("""
                WITH latest AS (
                    SELECT thread_id,
                           MAX(email_date) FILTER (WHERE folder = 'sent') AS last_sent,
                           MAX(email_date) FILTER (WHERE folder = 'inbox') AS last_in
                    FROM email_ledger
                    WHERE thread_id IS NOT NULL
                    GROUP BY thread_id
                )
                UPDATE task_board_items i
                SET status = 'handled',
                    note = COALESCE(i.note || ' · ', '')
                           || '[replied ' || to_char(l.last_sent, 'MM/DD HH24:MI') || ']',
                    note_at = NOW(), updated_at = NOW()
                FROM latest l
                WHERE i.task_key = 'thread:' || l.thread_id
                  AND i.status = 'open'
                  AND i.type <> 'info'
                  AND l.last_sent IS NOT NULL
                  AND (l.last_in IS NULL OR l.last_sent > l.last_in)
                  AND COALESCE(i.note, '') NOT LIKE '%[replied%'
            """)
            replied = cur.rowcount
        except Exception as e:
            errors["replied"] = str(e)
            conn.rollback()
        # ARCHIVE PURGE (William approved 2026-07-29): completed tasks are
        # kept 3 months in the Archive, then deleted.
        purged = 0
        try:
            cur.execute("""
                DELETE FROM task_board_items
                WHERE status IN ('handled', 'gone')
                  AND updated_at < NOW() - INTERVAL '90 days'
            """)
            purged = cur.rowcount
        except Exception as e:
            errors["purge"] = str(e)
            conn.rollback()
    conn.commit()
    return {"status": "ok", "swept": upserted, "gone": gone,
            "replied_settled": replied, "purged": purged,
            "errors": errors or None,
            "at": sweep_start.isoformat()}


# =============================================================================
# DROPDOWN ACTIONS (William's failsafe ruling 2026-07-26: typed words NEVER
# fire anything — an order action fires only from an explicit dropdown pick)
# =============================================================================

DROPDOWN_ACTIONS = {
    "payment_link_sent": "Invoice / payment link sent",
    "payment_received":  "Payment received",
    "is_complete":       "Picked up / delivered / complete",
}


def _fire_checkpoint(order_id: str, checkpoint: str) -> str:
    from orders_routes import update_checkpoint, CheckpointUpdate
    update_checkpoint(order_id,
                      CheckpointUpdate(checkpoint=checkpoint,
                                       source="task_board_dropdown"), True)
    return checkpoint


def _parse_due(s):
    """ISO date, or the words today/tomorrow. None when blank/unparseable."""
    from datetime import date, timedelta
    s = (s or "").strip().lower()
    if not s:
        return None
    if s == "today":
        return date.today().isoformat()
    if s == "tomorrow":
        return (date.today() + timedelta(days=1)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return None


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


@task_router.get("/tasks/archive")
def get_archive(limit: int = 200, _: bool = Depends(require_admin)):
    """The Archive (William approved 2026-07-29): every completed/vanished
    task, kept 3 months then purged by the sweep."""
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT task_key, board, type, title, detail, order_id,
                       status, note, note_at, updated_at
                FROM task_board_items
                WHERE status IN ('handled', 'gone')
                ORDER BY updated_at DESC LIMIT %s
            """, (min(int(limit), 500),))
            cols = ["task_key", "board", "type", "title", "detail", "order_id",
                    "status", "note", "note_at", "updated_at"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        for k in ("note_at", "updated_at"):
            if r.get(k) is not None and hasattr(r[k], "isoformat"):
                r[k] = r[k].isoformat()
    return {"status": "ok", "count": len(rows), "archive": rows}


@task_router.get("/tasks/rundown")
def get_rundown(_: bool = Depends(require_admin)):
    """THE MORNING RUNDOWN as a list (William approved 2026-07-29 — 'this
    rundown will be delivered via the app'): what landed, what waits on a
    human, what's due — assembled live from the same tables the board uses."""
    test_ids = _registry_ids()
    out = {"status": "ok", "at": datetime.now(timezone.utc).isoformat()}
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            # the orders.payment_received check keeps UNSTAMPED phantom
            # events out of the rundown (the 5707 Gerald case)
            cur.execute("""
                SELECT e.order_id, e.event_data, e.created_at,
                       COALESCE(o.company_name, o.customer_name, '')
                FROM order_events e LEFT JOIN orders o ON o.order_id = e.order_id
                WHERE e.event_type = 'payment_received'
                  AND e.created_at > NOW() - INTERVAL '24 hours'
                  AND COALESCE(o.payment_received, TRUE) = TRUE
                ORDER BY e.created_at DESC
            """)
            out["payments_last24h"] = [
                {"order_id": str(a), "detail": str(b)[:200],
                 "at": c.isoformat(), "customer": d}
                for a, b, c, d in cur.fetchall() if str(a) not in test_ids]
            cur.execute("""
                SELECT order_id, event_data, created_at FROM order_events
                WHERE event_type IN ('rl_delivered', 'delivery_photo_uploaded',
                                     'replacement_request_created')
                  AND created_at > NOW() - INTERVAL '24 hours'
                ORDER BY created_at DESC
            """)
            out["deliveries_and_claims_last24h"] = [
                {"order_id": str(a), "detail": str(b)[:200], "at": c.isoformat()}
                for a, b, c in cur.fetchall() if str(a) not in test_ids]
            cur.execute("""
                SELECT task_key, type, title, detail, order_id, due_date
                FROM task_board_items
                WHERE status = 'open' AND (
                    type IN ('robot-flag', 'draft-waiting', 'supplier-action')
                    OR due_date <= CURRENT_DATE)
                ORDER BY (due_date IS NULL), due_date, last_seen DESC
                LIMIT 60
            """)
            cols = ["task_key", "type", "title", "detail", "order_id", "due_date"]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for r in rows:
                if r.get("due_date") is not None and hasattr(r["due_date"], "isoformat"):
                    r["due_date"] = r["due_date"].isoformat()
            out["needs_you"] = [r for r in rows if r["type"] == "robot-flag"]
            out["drafts_waiting"] = [r for r in rows if r["type"] == "draft-waiting"]
            out["supplier_actions"] = [r for r in rows if r["type"] == "supplier-action"]
            out["due_today"] = [r for r in rows
                                if r.get("due_date")
                                and r["type"] not in ("robot-flag", "draft-waiting",
                                                      "supplier-action")]
            cur.execute("""
                SELECT order_id, COALESCE(company_name, customer_name, ''),
                       order_total, EXTRACT(DAY FROM NOW() - order_date)::int
                FROM orders
                WHERE payment_received = false AND is_complete = false
                  AND payment_link_sent = true
                  AND COALESCE(lifecycle_status, 'active') = 'active'
                  AND COALESCE(order_total, 0) > 0
                ORDER BY order_date ASC LIMIT 15
            """)
            out["awaiting_payment"] = [
                {"order_id": str(a), "customer": b,
                 "total": float(c or 0), "days_open": d}
                for a, b, c, d in cur.fetchall() if str(a) not in test_ids]
    return out


@task_router.get("/tasks/keywords")
def list_keywords(_: bool = Depends(require_admin)):
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, pattern, label, enabled, created_at
                           FROM task_keywords ORDER BY id""")
            rows = [{"id": a, "pattern": b, "label": c, "enabled": d,
                     "created_at": e.isoformat() if e else ""}
                    for a, b, c, d, e in cur.fetchall()]
    return {"status": "ok", "keywords": rows}


@task_router.post("/tasks/keywords")
def add_keyword(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """Teach the board a keyword (William approved 2026-07-29: 'the logic
    will be ongoing to look for key words so tasks can become smarter over
    time'). pattern = case-insensitive substring; label = the ⚡tag shown."""
    pattern = (payload.get("pattern") or "").strip()
    label = (payload.get("label") or "").strip()
    if not pattern or not label:
        return {"status": "error", "message": "pattern and label required"}
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO task_keywords (pattern, label)
                           VALUES (%s, %s) RETURNING id""", (pattern, label))
            kid = cur.fetchone()[0]
        conn.commit()
    return {"status": "ok", "id": kid, "pattern": pattern, "label": label}


@task_router.post("/tasks/keywords/{kid}/toggle")
def toggle_keyword(kid: int, _: bool = Depends(require_admin)):
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""UPDATE task_keywords SET enabled = NOT enabled
                           WHERE id = %s RETURNING enabled""", (kid,))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return {"status": "error", "message": "not found"}
    return {"status": "ok", "id": kid, "enabled": row[0]}


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
    # NOTES ARE PURE (failsafe ruling): typed words never fire actions.
    return {"status": "ok", "task_key": task_key,
            "action": "saved" if note else "reopened",
            "smart_actions": actions}


@task_router.post("/tasks/action")
def dropdown_action(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """Fire an order checkpoint from the EXPLICIT dropdown pick. The only
    door through which a task can change an order."""
    task_key = (payload.get("task_key") or "").strip()
    action = (payload.get("action") or "").strip()
    if action not in DROPDOWN_ACTIONS:
        return {"status": "error",
                "message": f"action must be one of {sorted(DROPDOWN_ACTIONS)}"}
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT order_id FROM task_board_items WHERE task_key = %s",
                        (task_key,))
            row = cur.fetchone()
    if not row or not row[0]:
        return {"status": "error", "message": "task is not linked to an order"}
    order_id = str(row[0])
    try:
        _fire_checkpoint(order_id, action)
    except Exception as e:
        return {"status": "error", "message": f"checkpoint failed: {e}"}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE task_board_items
                SET status = 'handled', note = %s, note_at = NOW(), updated_at = NOW()
                WHERE task_key = %s
            """, (f"[{DROPDOWN_ACTIONS[action]}] via dropdown", task_key))
        conn.commit()
    return {"status": "ok", "order_id": order_id, "fired": action,
            "label": DROPDOWN_ACTIONS[action]}


@task_router.post("/tasks/done")
def task_done(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """Done button — the task leaves the board (kept in HANDLED history)."""
    task_key = (payload.get("task_key") or "").strip()
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE task_board_items
                SET status = 'handled',
                    note = COALESCE(note, '[done]'), note_at = NOW(), updated_at = NOW()
                WHERE task_key = %s
            """, (task_key,))
            hit = cur.rowcount
        conn.commit()
    return {"status": "ok" if hit else "error", "task_key": task_key}


@task_router.post("/tasks/due")
def change_due(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """Change a task's follow-up date ('2026-07-28', 'tomorrow', 'today')."""
    task_key = (payload.get("task_key") or "").strip()
    due = _parse_due(payload.get("due_date"))
    if not due:
        return {"status": "error", "message": "due_date must be YYYY-MM-DD, today, or tomorrow"}
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE task_board_items
                SET due_date = %s, updated_at = NOW() WHERE task_key = %s
            """, (due, task_key))
            hit = cur.rowcount
        conn.commit()
    return {"status": "ok" if hit else "error", "task_key": task_key, "due_date": due}


@task_router.post("/tasks/manual")
def add_manual(payload: dict = Body(...), _: bool = Depends(require_admin)):
    """Add a task. With order_id -> a FOLLOW-UP on the ORDER board
    ("call them tomorrow" on 5695); without -> a plain task on OTHER.
    due_date accepts YYYY-MM-DD, 'today', or 'tomorrow'."""
    text = (payload.get("text") or "").strip()
    due = _parse_due(payload.get("due_date"))
    order_id = str(payload.get("order_id") or "").strip() or None
    if not text:
        return {"status": "error", "message": "text required"}
    if order_id:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM orders WHERE order_id = %s", (order_id,))
                if not cur.fetchone():
                    return {"status": "error", "message": f"order {order_id} not found"}
    board = "order" if order_id else "other"
    ttype = "follow-up" if order_id else "manual"
    key = f"{ttype}:{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    with get_db() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO task_board_items
                    (task_key, board, type, title, detail, order_id, due_date,
                     date_str, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open')
            """, (key, board, ttype, text,
                  "added by William" + (f", follow up {due}" if due else ""),
                  order_id, due, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    return {"status": "ok", "task_key": key, "due_date": due, "board": board}


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
    # Beat 5: thread-grouped tasks (task_key thread:{tid}) act on the WHOLE
    # thread — read/archive/trash one board row = the whole conversation.
    target = (f"threads/{task_key.split(':', 1)[1]}"
              if task_key.startswith("thread:") else f"messages/{gmail_id}")
    if action == "read":
        res = _gmail_post(f"{target}/modify", {"removeLabelIds": ["UNREAD"]})
    elif action == "archive":
        res = _gmail_post(f"{target}/modify",
                          {"removeLabelIds": ["UNREAD", "INBOX"]})
    else:
        res = _gmail_post(f"{target}/trash", {})
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
