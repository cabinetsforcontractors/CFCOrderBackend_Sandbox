"""
walk_list.py — THE WALK LIST (William 2026-08-03: "show me a list and we
work through it one by one until the issue is solved or there is a plan
of action, even if the action is to wait or defer").

This is the machinery that makes the 8/3 Monday-clearing PROCESS the
standing process:

  GET  /walk-list            — the whole list in ONE call, pre-organized:
                               needs-you (subjects + who spoke last),
                               NEW vs ROLLED, due today, deferred,
                               supplier legs needing a human, money,
                               robot receipts since the last sweep.
                               The chat session's opening move.
  POST /walk-list/send       — compose + email the list to the bell
                               (manual drill; slot rides the ledger).

SWEEP SCHEDULE (rides the ledger cycle): 8 AM, 10 AM, noon, 3 PM
Eastern, once per slot per day (event-stamped). Anything unworked simply
appears again — a state-derived list rolls forward by itself; the sweep
only MARKS what's new since the last one. ⚖️ Graduation clause (his
ruling): when a week of sweeps carries nothing but robot receipts, the
10 AM and 3 PM sweeps retire; morning + noon stay.

Every email reference carries its exact SUBJECT (the 8/3 law).
Stamps use order_id NULL (the FK law).
"""

import json
import os
from datetime import date, datetime, timezone
from typing import Dict, List

from db_helpers import get_db

INTERNAL_ALERT_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "orders@cabinetsforcontractors.com").strip()

SWEEP_HOURS_ET = (8, 10, 12, 15)


def _last_stamp(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT MAX(created_at) FROM order_events
                       WHERE event_type = 'walk_list_sent'""")
        row = cur.fetchone()
        return row[0] if row else None


def build_walk_list() -> Dict:
    out = {"status": "ok", "generated_at": str(datetime.now(timezone.utc)),
           "needs_you": [], "rolled": [], "due_today": [], "deferred": [],
           "supplier_legs": [], "money": {}, "receipts": [], "errors": []}
    with get_db() as conn:
        last = _last_stamp(conn)
        out["last_sweep"] = str(last) if last else None

        # --- board cards (open, everything except settles) ---------------
        # PRESENTATION LAWS (William 8/3): every item carries FROM +
        # SUBJECT + DATE · >14-day non-order items age off · cost
        # cross-checks (Pirate Ship / Daylight docs / carrier-vs-charged)
        # are AUDIT inputs, their own section · deferred items are NEVER
        # shown before their day ("you are my memory")
        import re as _re
        _XCHECK_RE = _re.compile(
            r"pirate ship|daylight|adjustment notice|"
            r"roc cabinetry (order|invoice)|received your payment", _re.I)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.task_key, t.type, t.title, t.order_id,
                           t.thread_id, t.first_seen, t.due_date, t.date_str,
                           (SELECT l.from_addr FROM email_ledger l
                            WHERE l.thread_id = t.thread_id
                              AND l.folder = 'inbox'
                            ORDER BY l.email_date DESC LIMIT 1) AS from_addr
                    FROM task_board_items t
                    WHERE t.status = 'open' AND t.type != 'dismissal'
                    ORDER BY t.first_seen
                """)
                for (key, typ, title, oid, tid, seen, due, dstr,
                     frm) in cur.fetchall():
                    item = {"task_key": key, "type": typ, "title": title,
                            "subject": title, "from": frm or "",
                            "order_id": oid, "thread_id": tid,
                            "date": str(dstr or seen or ""),
                            "since": str(seen) if seen else None,
                            "due_date": str(due) if due else None}
                    age = None
                    try:
                        if seen:
                            age = (datetime.now(timezone.utc)
                                   - seen).days
                    except Exception:
                        age = None
                    if due and due <= date.today():
                        out["due_today"].append(item)
                    elif due:
                        out["deferred"].append(item)   # held; never emailed
                    elif age is not None and age > 14 and not oid:
                        item["aged"] = age
                        out.setdefault("aged_off", []).append(item)
                    elif _XCHECK_RE.search(title or ""):
                        out.setdefault("cross_checks", []).append(item)
                    elif last and seen and seen > last:
                        item["badge"] = "NEW"
                        out["needs_you"].append(item)
                    else:
                        item["badge"] = "ROLLED"
                        out["rolled"].append(item)
        except Exception as e:
            out["errors"].append(f"cards: {e}")

        # --- NEEDS REPLY (ledger-truth; subjects mandatory) ---------------
        try:
            from queue_api import awaiting_reply
            res = awaiting_reply(days=60) or {}
            rows = (res.get("cards") or res.get("threads")
                    or res.get("items") or [])
            for r in rows:
                item = {"kind": "needs-reply",
                        "subject": r.get("subject") or "(no subject)",
                        "from": r.get("from_addr") or r.get("from") or "",
                        "thread_id": r.get("thread_id"),
                        "order_id": r.get("order_id"),
                        "last_in": str(r.get("last_in") or r.get("date") or "")}
                out["needs_you"].append(item)
        except Exception as e:
            out["errors"].append(f"needs-reply: {e}")

        # --- supplier legs needing a human --------------------------------
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, order_id, warehouse, status, note
                    FROM supplier_orders
                    WHERE status IN ('discrepancy', 'blocked')
                    ORDER BY updated_at DESC LIMIT 20
                """)
                out["supplier_legs"] = [
                    {"row_id": r[0], "order_id": r[1], "warehouse": r[2],
                     "status": r[3], "note": (r[4] or "")[:160]}
                    for r in cur.fetchall()]
        except Exception as e:
            out["errors"].append(f"legs: {e}")

        # --- receipts: what the robot handled since the last sweep --------
        try:
            from fire_log import NOISE_EVENT_TYPES
            noise = list(NOISE_EVENT_TYPES) + [
                "walk_list_sent", "transit_watch", "sweep"]
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT order_id, event_type, created_at
                    FROM order_events
                    WHERE created_at > COALESCE(%s, NOW() - INTERVAL '4 hours')
                      AND NOT (event_type = ANY(%s))
                    ORDER BY created_at DESC LIMIT 25
                """, (last, noise))
                out["receipts"] = [
                    {"order_id": r[0], "event": r[1], "at": str(r[2])}
                    for r in cur.fetchall()]
        except Exception as e:
            out["errors"].append(f"receipts: {e}")

    # --- money -----------------------------------------------------------
    try:
        from queue_api import money_strip
        strip = money_strip()
        out["money"] = {"line": strip.get("line"),
                        "awaiting_total": strip.get("awaiting_total"),
                        "awaiting_count": strip.get("awaiting_count")}
    except Exception as e:
        out["errors"].append(f"money: {e}")

    out["counts"] = {"needs_you": len(out["needs_you"]),
                     "rolled": len(out["rolled"]),
                     "due_today": len(out["due_today"]),
                     "deferred": len(out["deferred"]),
                     "supplier_legs": len(out["supplier_legs"])}
    return out


def _fmt_item(i: Dict) -> str:
    """PRESENTATION LAW (8/3): task · from-address · subject · order # ·
    date — everything he needs to search and age it himself."""
    bits = []
    if i.get("order_id"):
        bits.append(f"#{i['order_id']}")
    if i.get("subject"):
        bits.append(f"&ldquo;{i['subject']}&rdquo;")
    elif i.get("title"):
        bits.append(i["title"])
    if i.get("from"):
        bits.append(f"from {i['from'][:45]}")
    if i.get("date"):
        bits.append(str(i["date"])[:10])
    if i.get("due_date"):
        bits.append(f"due {i['due_date']}")
    return " &middot; ".join(bits)


def send_walk_list(slot: str = "manual") -> Dict:
    wl = build_walk_list()
    c = wl["counts"]

    def section(title, items, color="#1D4ED8"):
        if not items:
            return ""
        rows = "".join(f"<div style='padding:2px 0'>&bull; {_fmt_item(i)}</div>"
                       for i in items[:20])
        return (f"<p style='margin:10px 0 2px;font-weight:700;color:{color}'>"
                f"{title}</p>{rows}")

    # AUTO AGE-OUT (14-day law): the sweep settles what it aged off
    if wl.get("aged_off"):
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    for i in wl["aged_off"]:
                        cur.execute("""
                            UPDATE task_board_items
                            SET status = 'handled',
                                note = %s, note_at = NOW(), updated_at = NOW()
                            WHERE task_key = %s AND status = 'open'
                        """, (f"[aged off: {i.get('aged')}d old, not "
                              f"order-related - the 14-day law]",
                              i["task_key"]))
                    conn.commit()
        except Exception as e:
            print(f"[WALK] age-out failed: {e}")

    # EMAIL LAWS (William 8/3): no already-handled section, no deferred
    # (only on their day), every line = from + subject + order + date
    quiet = not any([wl["needs_you"], wl["due_today"],
                     wl["supplier_legs"], wl.get("cross_checks")])
    body = (
        f"<div style='font-family:Arial,sans-serif;font-size:13px;color:#333'>"
        + (f"<p>Nothing new &middot; {c['rolled']} rolled.</p>" if quiet else "")
        + section("🔴 NEEDS YOU", wl["needs_you"], "#DC2626")
        + section("⏳ ROLLED (still waiting)", wl["rolled"], "#B45309")
        + section("⏰ DUE TODAY", wl["due_today"], "#7C3AED")
        + section("💵 COST CROSS-CHECKS (charged vs paid — dispute candidates)",
                  wl.get("cross_checks") or [], "#0E7490")
        + section("🏭 SUPPLIER LEGS NEEDING A HUMAN", wl["supplier_legs"], "#B45309")
        + (f"<p style='margin:10px 0 2px;font-weight:700;color:#059669'>💰 MONEY</p>"
           f"<div>{(wl['money'] or {}).get('line', '')}</div>")
        + f"<p style='color:#888;font-size:12px;margin-top:12px'>Work it in "
          f"the app, or sit down with Claude and say &ldquo;walk it&rdquo;. "
          f"Anything unworked rolls into the next sweep by itself.</p></div>")

    subject = (f"WALK LIST - {date.today().strftime('%a %m/%d')} {slot} - "
               f"{c['needs_you']} new · {c['rolled']} rolled · "
               f"{c['due_today']} due")

    from supplier_orders import _send_email
    res = _send_email("", INTERNAL_ALERT_EMAIL, subject, body,
                      triggered_by="walk_list")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (NULL, 'walk_list_sent', %s, 'walk_list')
            """, (json.dumps({"slot": slot, "date": str(date.today()),
                              "counts": c, "sent": res.get("success"),
                              "subject": subject}),))
            conn.commit()
    return {"status": "ok", "subject": subject, "counts": c,
            "sent": res.get("success"), "to": res.get("to")}


def run_walk_list_schedule() -> Dict:
    """Rides the ledger cycle: fire the slot email once per slot per day,
    Eastern clock (William's own timezone — this is HIS list)."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.now(timezone.utc)
    if now_et.weekday() >= 5:          # weekends stay quiet
        return {"status": "weekend"}
    due_slot = None
    for h in SWEEP_HOURS_ET:
        if now_et.hour >= h:
            due_slot = h
    if due_slot is None:
        return {"status": "before_first_slot"}
    slot_name = {8: "8 AM", 10: "10 AM", 12: "NOON", 15: "3 PM"}[due_slot]
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM order_events
                           WHERE event_type = 'walk_list_sent'
                             AND event_data::text LIKE %s LIMIT 1""",
                        (f'%"slot": "{slot_name}", "date": "{date.today()}"%',))
            if cur.fetchone():
                return {"status": "already_sent", "slot": slot_name}
    return send_walk_list(slot=slot_name)
