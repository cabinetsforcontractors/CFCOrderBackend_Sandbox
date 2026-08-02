"""
gmail_mirror.py — Wave 5 (William 2026-08-02): THE GMAIL MIRROR.

His rules, verbatim: "if it's deleted in the account it needs to be
deleted on the board" and "parse promotional vs important so promotional
READ is marked handled". The board stops ignoring Gmail — but only in
the two directions he ruled:

  DELETED in Gmail          -> card settles "[robot settled: deleted in
                               Gmail]" (all card types, incl NEEDS REPLY)
  READ + PROMOTIONAL        -> settles "[robot settled: promotional,
                               read in Gmail]"
  READ + IMPORTANT          -> STAYS (sticky law unchanged)

THE PROMOTIONAL TEST — every check must pass:
  1. Gmail's own category label (PROMOTIONS/SOCIAL/FORUMS), or a
     List-Unsubscribe header, or a noise-list sender
  2. no order linked to the card or thread
  3. sender is not a supplier / known customer / cast box
  4. subject carries no money and no order-shaped number

DELETE is judged by TWO consecutive missing checks at least an hour
apart — gmail_api_request returns None on ANY error, and a transient
API blip must never mass-settle the board as "deleted".
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, Optional

from db_helpers import get_db

# state-derived card types stay OWNED by their state sweeps — the mirror
# touches email-born cards only (sticky-law taxonomy, 8/1)
_STATE_TYPES = ("unpaid-order", "supplier-action", "shipment-watch",
                "draft-waiting", "dismissal")

_MONEYISH_RE = re.compile(r"\$\s?\d|\b5\d{3}\b")
_SUPPLIERISH_RE = re.compile(
    r"milestonecabinetry|ghicabinets|roccabinetry|dlcabinetry|"
    r"cabinetstonellc|durastoneusa|gobravura|dealercabinetry|"
    r"cabinetrydistribution|lnccabinetry|cfcinvoices42|rlcarriers|dylt",
    re.I)
_CAST_RE = re.compile(
    r"orders@cabinetsforcontractors\.com|cabinetsforcontractors@gmail\.com|"
    r"wpjob1@gmail\.com|4wprince@gmail\.com|homesupplyplus@gmail\.com", re.I)


def ensure_mirror_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gmail_mirror_checks (
                thread_id VARCHAR(120) PRIMARY KEY,
                checked_at TIMESTAMPTZ DEFAULT NOW(),
                missing_since TIMESTAMPTZ,
                verdict VARCHAR(30)
            )""")
        conn.commit()


def _thread_state(thread_id: str) -> Dict:
    """One metadata fetch: {found, all_read, all_trashed, labels,
    has_unsub, from_addrs, subjects}."""
    from gmail_sync import gmail_api_request
    resp = gmail_api_request(
        f"threads/{thread_id}",
        {"format": "metadata", "metadataHeaders": "List-Unsubscribe"})
    if not resp or not resp.get("messages"):
        return {"found": False}
    labels, unsub = set(), False
    unread = trashed_all = True
    trashed_all = True
    unread_any = False
    for m in resp["messages"]:
        ls = set(m.get("labelIds") or [])
        labels |= ls
        if "UNREAD" in ls:
            unread_any = True
        if "TRASH" not in ls:
            trashed_all = False
        for h in (m.get("payload", {}) or {}).get("headers", []):
            if h.get("name", "").lower() == "list-unsubscribe":
                unsub = True
    return {"found": True, "all_read": not unread_any,
            "all_trashed": trashed_all, "labels": labels,
            "has_unsub": unsub}


def _is_promotional(state: Dict, from_addr: str, subject: str,
                    order_linked: bool) -> bool:
    if order_linked:
        return False
    f = from_addr or ""
    if _SUPPLIERISH_RE.search(f) or _CAST_RE.search(f):
        return False
    try:
        from queue_api import _NOISE_SENDER_RE
        noise = bool(_NOISE_SENDER_RE.search(f))
    except Exception:
        noise = False
    bulk_marked = bool(
        state.get("has_unsub") or noise or
        {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL",
         "CATEGORY_FORUMS"} & (state.get("labels") or set()))
    if not bulk_marked:
        return False
    if _MONEYISH_RE.search(subject or ""):
        return False
    return True


def _settle_card(conn, task_key: str, note: str):
    with conn.cursor() as cur:
        cur.execute("""UPDATE task_board_items
                       SET status = 'handled', note = %s, note_at = NOW(),
                           updated_at = NOW()
                       WHERE task_key = %s AND status = 'open'""",
                    (note, task_key))
        conn.commit()


def _ledger_context(conn, thread_id: str):
    """(from_addr, subject, order_linked) from the ledger for a thread."""
    with conn.cursor() as cur:
        cur.execute("""SELECT from_addr, subject, order_ids FROM email_ledger
                       WHERE thread_id = %s AND folder = 'inbox'
                       ORDER BY email_date DESC LIMIT 1""", (thread_id,))
        row = cur.fetchone()
    if not row:
        return "", "", False
    return row[0] or "", row[1] or "", bool((row[2] or "").strip())


def process_gmail_mirror(limit: int = 60) -> Dict:
    """Rides the ledger cycle (companion cadence). Per-thread recheck
    throttle: 4 hours."""
    out = {"checked": 0, "settled_deleted": 0, "settled_promo": 0,
           "pending_delete": 0, "errors": []}
    with get_db() as conn:
        ensure_mirror_table(conn)
        # -------- board cards (email-born, open, with a thread) ----------
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.task_key, t.thread_id, t.order_id, t.title
                FROM task_board_items t
                LEFT JOIN gmail_mirror_checks c ON c.thread_id = t.thread_id
                WHERE t.status = 'open'
                  AND t.thread_id IS NOT NULL AND t.thread_id != ''
                  AND t.type NOT IN %s
                  AND (c.checked_at IS NULL
                       OR c.checked_at < NOW() - INTERVAL '4 hours')
                LIMIT %s
            """, (_STATE_TYPES, limit))
            cards = cur.fetchall()
        for task_key, tid, order_id, title in cards:
            out["checked"] += 1
            try:
                state = _thread_state(tid)
                from_addr, subject, ledger_linked = _ledger_context(conn, tid)
                order_linked = bool(order_id) or ledger_linked
                if not state.get("found") or state.get("all_trashed"):
                    # two-strike rule before calling it deleted
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO gmail_mirror_checks
                                (thread_id, checked_at, missing_since, verdict)
                            VALUES (%s, NOW(), NOW(), 'missing')
                            ON CONFLICT (thread_id) DO UPDATE SET
                                checked_at = NOW(),
                                missing_since = COALESCE(
                                    gmail_mirror_checks.missing_since, NOW()),
                                verdict = 'missing'
                            RETURNING missing_since
                        """, (tid,))
                        missing_since = cur.fetchone()[0]
                        conn.commit()
                    age = (datetime.now(timezone.utc)
                           - missing_since).total_seconds()
                    if age >= 3600:
                        _settle_card(conn, task_key,
                                     "[robot settled: deleted in Gmail]")
                        out["settled_deleted"] += 1
                    else:
                        out["pending_delete"] += 1
                    continue
                # thread exists — clear any missing strike
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO gmail_mirror_checks
                            (thread_id, checked_at, missing_since, verdict)
                        VALUES (%s, NOW(), NULL, 'present')
                        ON CONFLICT (thread_id) DO UPDATE SET
                            checked_at = NOW(), missing_since = NULL,
                            verdict = 'present'
                    """, (tid,))
                    conn.commit()
                if state.get("all_read") and _is_promotional(
                        state, from_addr, subject or title, order_linked):
                    _settle_card(conn, task_key,
                                 "[robot settled: promotional, read in Gmail]")
                    out["settled_promo"] += 1
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"{task_key}: {e}")

        # -------- NEEDS REPLY pseudo-cards (ledger-derived) --------------
        try:
            from queue_api import _handled_note, awaiting_reply
            res = awaiting_reply(days=60) or {}
            rows = (res.get("cards") or res.get("threads")
                    or res.get("items") or [])
            for r in rows[:limit]:
                tid = r.get("thread_id") or r.get("threadId")
                if not tid:
                    continue
                with conn.cursor() as cur:
                    cur.execute("""SELECT checked_at FROM gmail_mirror_checks
                                   WHERE thread_id = %s
                                     AND checked_at > NOW() - INTERVAL '4 hours'
                                     AND verdict LIKE 'nr-%%'""", (tid,))
                    if cur.fetchone():
                        continue
                out["checked"] += 1
                state = _thread_state(tid)
                from_addr, subject, order_linked = _ledger_context(conn, tid)
                verdict = "nr-present"
                if not state.get("found") or state.get("all_trashed"):
                    # same two-strike rule as cards — a transient API blip
                    # must never silently dismiss a NEEDS REPLY
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO gmail_mirror_checks
                                (thread_id, checked_at, missing_since, verdict)
                            VALUES (%s, NOW(), NOW(), 'nr-missing')
                            ON CONFLICT (thread_id) DO UPDATE SET
                                checked_at = NOW(),
                                missing_since = COALESCE(
                                    gmail_mirror_checks.missing_since, NOW()),
                                verdict = 'nr-missing'
                            RETURNING missing_since
                        """, (tid,))
                        missing_since = cur.fetchone()[0]
                        conn.commit()
                    age = (datetime.now(timezone.utc)
                           - missing_since).total_seconds()
                    if age >= 3600:
                        _handled_note(f"needsreply:{tid}", "",
                                      "[robot settled: deleted in Gmail]")
                        out["settled_deleted"] += 1
                        verdict = "nr-deleted"
                    else:
                        out["pending_delete"] += 1
                        continue
                elif state.get("all_read") and _is_promotional(
                        state, from_addr, subject, order_linked):
                    _handled_note(f"needsreply:{tid}", "",
                                  "[robot settled: promotional, read in Gmail]")
                    out["settled_promo"] += 1
                    verdict = "nr-promo"
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO gmail_mirror_checks
                            (thread_id, checked_at, verdict)
                        VALUES (%s, NOW(), %s)
                        ON CONFLICT (thread_id) DO UPDATE SET
                            checked_at = NOW(), verdict = EXCLUDED.verdict
                    """, (tid, verdict))
                    conn.commit()
        except Exception as e:
            out["errors"].append(f"needs-reply pass: {e}")
    return out
