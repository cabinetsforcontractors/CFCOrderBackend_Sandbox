"""
bounce_watch.py — THE BOUNCE WATCHER (William 2026-08-03, the weborders@
lesson: his ship-together email to weborders@roccabinetry.com BOUNCED —
he only knew because he sent it BY HAND and saw the mailer-daemon reply.
A robot send that bounces would have died silently: mailer-daemon is
noise-filtered off every board surface).

Rides the ledger cycle: fresh INBOX rows from mailer-daemon / postmaster
/ "Mail Delivery Subsystem" → pull the failed recipient + the original
SUBJECT out of the bounce body → ONE alert to the bell per bounce
(idempotent per message). Detect-and-tell only.
"""

import json
import os
import re
from typing import Dict

from db_helpers import get_db

INTERNAL_ALERT_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "orders@cabinetsforcontractors.com").strip()

_BOUNCE_FROM_RE = re.compile(
    r"mailer-daemon|postmaster@|mail delivery (subsystem|system)", re.I)
_FAILED_ADDR_RE = re.compile(
    r"(?:wasn't delivered to|couldn't be delivered to|delivery to these "
    r"recipients or groups failed|failed[^\n]{0,40}?:?\s*)"
    r"[<\s]*([\w.+-]+@[\w.-]+\.\w+)", re.I)
_ANY_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_SUBJ_RE = re.compile(r"Subject:\s*(.+)", re.I)

_OUR_RE = re.compile(
    r"cabinetsforcontractors|wpjob1@|4wprince@|homesupplyplus@|"
    r"mailer-daemon|postmaster", re.I)


def process_bounces(hours_back: int = 24) -> Dict:
    out = {"scanned": 0, "alerted": 0, "already": 0, "errors": []}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT message_id, subject, from_addr FROM email_ledger
                WHERE folder = 'inbox'
                  AND from_addr ~* 'mailer-daemon|postmaster|mail delivery'
                  AND email_date > NOW() - (%s || ' hours')::interval
                LIMIT 25
            """, (int(hours_back),))
            rows = cur.fetchall()
        for mid, subject, from_addr in rows:
            out["scanned"] += 1
            try:
                with conn.cursor() as cur:
                    cur.execute("""SELECT 1 FROM order_events
                                   WHERE event_type = 'bounce_alerted'
                                     AND event_data::text ILIKE %s LIMIT 1""",
                                (f"%{mid}%",))
                    if cur.fetchone():
                        out["already"] += 1
                        continue
                from email_ledger import _fetch_message
                email = _fetch_message(mid) or {}
                body = email.get("body") or ""
                m = _FAILED_ADDR_RE.search(body)
                failed_to = m.group(1) if m else next(
                    (a for a in _ANY_ADDR_RE.findall(body)
                     if not _OUR_RE.search(a)), "(address not found in body)")
                sm = _SUBJ_RE.search(body)
                orig_subject = sm.group(1).strip()[:120] if sm else ""
                if not orig_subject:
                    # FIRST-LIVE-PROOF LESSON (8/3, the weborders@ bounce):
                    # Gmail bounce bodies don't always quote the headers —
                    # but the bounce rides the SAME thread as the original
                    # send, so the ledger already knows its subject.
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT subject FROM email_ledger
                            WHERE thread_id = (SELECT thread_id
                                               FROM email_ledger
                                               WHERE message_id = %s)
                              AND folder = 'sent'
                            ORDER BY email_date DESC LIMIT 1
                        """, (mid,))
                        row = cur.fetchone()
                    orig_subject = ((row[0] or "").strip()[:120] if row
                                    else "(original subject not found)")
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO order_events
                            (order_id, event_type, event_data, source)
                        VALUES (NULL, 'bounce_alerted', %s, 'bounce_watch')
                    """, (json.dumps({"message_id": mid,
                                      "failed_to": failed_to,
                                      "orig_subject": orig_subject}),))
                    conn.commit()
                from supplier_orders import _send_email
                _send_email(
                    "", INTERNAL_ALERT_EMAIL,
                    f"EMAIL BOUNCED - to {failed_to}",
                    f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                    f"<p><strong>An email did NOT get delivered.</strong></p>"
                    f"<p>Failed recipient: <strong>{failed_to}</strong><br>"
                    f"Original subject: <strong>{orig_subject}</strong></p>"
                    f"<p>Resend it to a working address — the recipient never "
                    f"saw it. (Bounce notice: &ldquo;{(subject or '')[:100]}"
                    f"&rdquo;)</p></div>",
                    triggered_by="bounce_watch")
                out["alerted"] += 1
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"{mid}: {e}")
    return out
