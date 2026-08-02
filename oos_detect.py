"""
oos_detect.py — Wave 2 build D (William 2026-08-02): OUT-OF-STOCK DETECTOR.

A warehouse reply saying "out of stock" used to be just another unread
card. Now the ledger cycle runs this detector over fresh INBOUND ledger
rows: a strict phrase match on a reply linked to an order fires ONE
alert to the humans with the exact next moves prefilled (the stock-check
door, then the substitution proposal with oos_message_id attached).

DETECT-ONLY LAW: this module never proposes, never emails a customer or
supplier, never touches B2BWave. Humans own the judgment (William
2026-07-16); the robot spots it, records it, and rings the bell once.

Self-trigger guard: every outbound PO carries "Please check for any
out-of-stock items" — replies quote it back. Matching runs on the FRESH
text only (quoted history and '>'-lines stripped), and plain negations
("no items are out of stock") are skipped.

Dedupe: order_events 'oos_detected' carries the Gmail message id — a
message already recorded never alerts again.
"""

import json
import os
import re
from typing import Dict, List

from db_helpers import get_db

INTERNAL_ALERT_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "orders@cabinetsforcontractors.com").strip()

# strict OOS phrases — a bare "stock" or "order delayed" never matches
OOS_RE = re.compile(
    r"\b(out of stock|out-of-stock|backorder(?:ed)?|back-?order(?:ed)?|"
    r"no longer (?:available|carr(?:y|ied))|discontinued|"
    r"currently (?:missing|unavailable)|sold out|"
    r"(?:don'?t|do not) have (?:it|this|that|them|these) in stock)\b", re.I)

# negation right before the phrase = the GOOD answer, not a hit
NEG_RE = re.compile(
    r"\b(?:no|none|nothing|not|aren'?t|isn'?t)\s+(?:\w+\s+){0,3}"
    r"(?:out of stock|out-of-stock|backordered|back-?ordered|discontinued|"
    r"sold out)\b", re.I)

# any line ending in "wrote:" is a quote header — AOL's runs 150+ chars on
# one line (the Bill Rhoads self-quote leak, caught by the 8/2 dry run)
_QUOTE_CUT_RE = re.compile(
    r"^[^\n]{0,300}\bwrote:\s*$|^-{2,}\s*Original Message|"
    r"^-{2,}\s*Forwarded message", re.I | re.M)

_PO_CODE_RE = re.compile(r"\bPO\s+\d{4,5}-([A-Z]{2,4})\b", re.I)


def _fresh_text(body: str) -> str:
    """The reply's own words: cut at the first quoted-history marker and
    drop '>'-quoted lines."""
    body = body or ""
    m = _QUOTE_CUT_RE.search(body)
    if m:
        body = body[:m.start()]
    return "\n".join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith(">"))


def _warehouse_guess(conn, subject: str, order_id: str) -> str:
    """Subject code first ('PO 5750-LM'); else the order's single
    supplier_orders row; else ''."""
    from supplier_orders import SUPPLIER_SUBJECT_CODE
    m = _PO_CODE_RE.search(subject or "")
    if m:
        code = m.group(1).upper()
        for wh, c in SUPPLIER_SUBJECT_CODE.items():
            if c == code:
                return wh
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT warehouse FROM supplier_orders
                           WHERE order_id = %s""", (order_id,))
            rows = [r[0] for r in cur.fetchall()]
        if len(rows) == 1:
            return rows[0]
    except Exception:
        pass
    return ""


def _already_recorded(conn, message_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM order_events
                       WHERE event_type = 'oos_detected'
                         AND event_data::text ILIKE %s
                       LIMIT 1""", (f"%{message_id}%",))
        return cur.fetchone() is not None


def process_oos_scan(hours_back: int = 48, dry_run: bool = False) -> Dict:
    """Ride the ledger cycle (or the on-demand door). Idempotent per
    Gmail message id."""
    out = {"scanned": 0, "hits": [], "alerted": 0, "already": 0,
           "dry_run": dry_run, "errors": []}
    our_re = re.compile(
        r"orders@cabinetsforcontractors\.com|cabinetsforcontractors@gmail\.com|"
        r"no-?reply|noreply|mailer-daemon", re.I)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT message_id, subject, from_addr, order_ids
                FROM email_ledger
                WHERE folder = 'inbox'
                  AND ignored_reason IS NULL
                  AND COALESCE(order_ids, '') <> ''
                  AND email_date > NOW() - (%s || ' hours')::interval
                ORDER BY email_date DESC
                LIMIT 200
            """, (int(hours_back),))
            rows = cur.fetchall()
        for mid, subject, from_addr, order_ids in rows:
            if our_re.search(from_addr or ""):
                continue
            out["scanned"] += 1
            try:
                if _already_recorded(conn, mid):
                    out["already"] += 1
                    continue
                from email_ledger import _fetch_message
                email = _fetch_message(mid) or {}
                fresh = _fresh_text(email.get("body") or "")
                hits = OOS_RE.findall(fresh)
                if not hits:
                    continue
                if len(NEG_RE.findall(fresh)) >= len(hits):
                    continue  # "no items are out of stock" — the good answer
                phrase = hits[0] if isinstance(hits[0], str) else hits[0][0]
                pos = fresh.lower().find(phrase.lower())
                context = fresh[max(0, pos - 120):pos + 160].strip()
                for oid in [o for o in (order_ids or "").split(",") if o]:
                    wh = _warehouse_guess(conn, subject, oid)
                    hit = {"order_id": oid, "warehouse": wh, "message_id": mid,
                           "from": from_addr, "subject": subject,
                           "phrase": phrase, "context": context[:400]}
                    out["hits"].append(hit)
                    if dry_run:
                        continue
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO order_events
                                (order_id, event_type, event_data, source)
                            VALUES (%s, 'oos_detected', %s, 'oos_detect')
                        """, (oid, json.dumps(hit, default=str)))
                        conn.commit()
                    _alert(oid, wh, hit)
                    out["alerted"] += 1
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"{mid}: {e}")
    return out


def _alert(order_id: str, warehouse: str, hit: Dict):
    """One bell to the humans with the next moves prefilled."""
    from supplier_orders import _send_email
    wh_txt = f" ({warehouse})" if warehouse else ""
    html = (
        f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
        f"<p><strong>The warehouse says something is OUT OF STOCK on order "
        f"#{order_id}{wh_txt}.</strong></p>"
        f"<p>Matched phrase: <strong>{hit.get('phrase')}</strong></p>"
        f"<pre style='background:#f5f5f5;padding:10px;white-space:pre-wrap;'>"
        f"{hit.get('context')}</pre>"
        f"<p>From: {hit.get('from')}<br>Subject: {hit.get('subject')}<br>"
        f"Gmail message id: {hit.get('message_id')}</p>"
        f"<p><strong>Next moves (nothing fires until you do it):</strong></p>"
        f"<ol>"
        f"<li>Ask the warehouse first (William's 7/16 ruling): "
        f"<code>POST /substitutions/stock-check</code> with "
        f"{{order_id: {order_id}, original_sku, substitute_sku, "
        f"oos_message_id: {hit.get('message_id')}}}</li>"
        f"<li>When they confirm stock: <code>POST /substitutions/propose</code> "
        f"with the same fields — the customer gets the Approve/No email.</li>"
        f"</ol></div>")
    _send_email(order_id, INTERNAL_ALERT_EMAIL,
                f"OUT OF STOCK - order #{order_id}{wh_txt} - warehouse reply",
                html, triggered_by="oos_detect")
