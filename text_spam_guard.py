"""
text_spam_guard.py — THE FUNDING-TEXT FILTER (William 8/4: his phone
texts now forward to email via IFTTT — a great record channel with one
disease: funding-spam texts. His negative-keyword ruling keeps them OFF
the board).

SCOPE LAW: the rules below apply ONLY to the IFTTT forward stream
(sender contains 'ifttt'). They must NEVER touch real order mail —
"terms" and "options" are everyday words on invoices.

His rules — a message is funding spam when ANY of these hit:
  - it addresses him by any name besides William
  - the words: funding, fund, find, terms, term, unsecured, approval,
    option, options, amount, STOP
  - a number with a K at the end (300K class)

A spam hit settles every twin key (thread:/noreply:/needsreply:) with a
robot note and marks the thread read — no surface ever shows it.
Idempotent per message via text_spam_settled events (NULL order id,
the FK law).
"""

import json
import re
from typing import Dict

from db_helpers import get_db

_GREETING_RE = re.compile(
    r"^\s*(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening))\s+"
    r"([a-z]+)", re.I | re.M)
_KEYWORD_RE = re.compile(
    r"\b(funding|funds?|find|terms?|unsecured|approvals?|options?|amount)\b"
    r"|\bSTOP\b"
    r"|\b\d{1,4}\s*[Kk]\b", re.I)
_OK_NAMES = {"william", "will"}


def is_funding_spam(subject: str, body: str) -> bool:
    text = f"{subject or ''}\n{body or ''}"
    m = _GREETING_RE.search(text)
    if m and m.group(1).lower() not in _OK_NAMES:
        return True
    return bool(_KEYWORD_RE.search(text))


def run_text_spam_guard(hours_back: int = 48) -> Dict:
    out = {"scanned": 0, "settled": [], "errors": []}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT message_id, thread_id, subject FROM email_ledger
                WHERE folder = 'inbox'
                  AND from_addr ILIKE '%%ifttt%%'
                  AND email_date > NOW() - (%s || ' hours')::interval
                LIMIT 40
            """, (int(hours_back),))
            rows = cur.fetchall()
    for mid, tid, subject in rows:
        out["scanned"] += 1
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT 1 FROM order_events
                                   WHERE event_type = 'text_spam_settled'
                                     AND event_data::text ILIKE %s
                                   LIMIT 1""", (f"%{mid}%",))
                    if cur.fetchone():
                        continue
            from email_ledger import _fetch_message
            email = _fetch_message(mid) or {}
            body = email.get("body") or ""
            if not is_funding_spam(subject or "", body):
                continue
            from queue_api import _handled_note, _mark_thread_read
            if tid:
                for key in (f"thread:{tid}", f"noreply:{tid}",
                            f"needsreply:{tid}"):
                    _handled_note(key, None,
                                  "[robot settled: funding-text spam "
                                  "(IFTTT stream, William's 8/4 rules)]")
                try:
                    _mark_thread_read(tid)
                except Exception:
                    pass
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO order_events
                            (order_id, event_type, event_data, source)
                        VALUES (NULL, 'text_spam_settled', %s,
                                'text_spam_guard')
                    """, (json.dumps({"message_id": mid,
                                      "subject": (subject or "")[:120]}),))
                    conn.commit()
            out["settled"].append((subject or "")[:60])
        except Exception as e:
            out["errors"].append(f"{mid}: {e}")
    return out
