"""
ghi_inbox.py
GHI INBOX ENGINE (William rulings 2026-07-18, built from the 5693/SO 17118 case).

Two jobs:

1. APPROVAL REPLY DRAFTS (draft-first law). GHI will not process an order until
   a human replies "approved" to their "Please review your order and approve
   for processing" email. When the verifier finishes a GHI sales order, this
   module writes the reply DRAFT in Kathryn's thread:
     clean verify  -> plain approval ("SO X for our PO Y checks out - approved
                      for processing")
     held flags    -> conditional approval ("please confirm item(s) below -
                      with that confirmed, PO Y is approved for processing")
   William reviews and hits SEND on every one — the robot never sends approval
   itself. Body always carries the PO number + supplier door info so the thread
   stays searchable; NEVER customer info. A notification email tells us a draft
   is waiting. One draft per (order, SO doc) — re-verifies don't duplicate.
   NOTE: internal-only flags (PRICE-CHECK = our stale cogs.csv) go in the
   notification to US, never in the draft to GHI.

2. CROSS-THREAD PO CAPTURE. GHI answers questions about one order inside
   another order's thread, or in fresh emails, with no rhyme or reason
   (William). Every email from ghicabinets.com gets scanned for 4-digit numbers
   that match REAL order ids in our DB; the email is filed (order_events
   'ghi_email_mention') against every order it names, regardless of thread.
   A GHI email naming NO known order -> alert to us instead of silent drop.
   Dedupe per message id (ghi_inbox_seen), so each email is handled once.
"""

import base64
import json
import re
import urllib.request
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from db_helpers import get_db


# =============================================================================
# GMAIL WRITE HELPER (drafts.create — read/send helpers live in gmail_sync)
# =============================================================================

def _gmail_post(endpoint: str, payload: Dict) -> Optional[Dict]:
    from gmail_sync import get_gmail_access_token

    token = get_gmail_access_token()
    if not token:
        return None
    req = urllib.request.Request(
        f"https://gmail.googleapis.com/gmail/v1/users/me/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[GHI-INBOX] gmail POST {endpoint} error {e.code}: "
              f"{e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"[GHI-INBOX] gmail POST {endpoint} failed: {e}")
        return None


# =============================================================================
# APPROVAL REPLY DRAFT
# =============================================================================

def _draft_already_created(order_id: str, doc_ref: str) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM order_events
                    WHERE order_id = %s AND event_type = 'ghi_approval_draft'
                      AND event_data LIKE %s
                    LIMIT 1
                """, (order_id, f'%{doc_ref}%'))
                return cur.fetchone() is not None
    except Exception:
        return False


def _door_text(order_id: str) -> str:
    """'(Frontier, FTS)' from the order's GHI line prefix — searchability."""
    try:
        from supplier_orders import SUPPLIER_DOOR_INFO
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT product_sku FROM order_line_items
                               WHERE order_id = %s""", (order_id,))
                for (sku,) in cur.fetchall():
                    pre = (sku or "").split("-")[0].upper()
                    info = SUPPLIER_DOOR_INFO.get(("GHI", pre))
                    if info:
                        return f" ({info['door_name']}, {info['presku']})"
    except Exception:
        pass
    return ""


def _their(sku: str) -> str:
    """GHI-facing token for one of our skus (forward map; falls back to body)."""
    try:
        from estimate_verifier import _their_sku
        return _their_sku(sku, "GHI")
    except Exception:
        return sku.split("-", 1)[1] if "-" in (sku or "") else (sku or "?")


def build_approval_asks(report: Dict) -> List[str]:
    """Plain-language confirm/correct asks from a verify report — supplier
    dialect only, internal PRICE-CHECK flags excluded (those are OUR data)."""
    asks = []
    sub_skus = set()
    for s in report.get("possible_substitutions") or []:
        sub_skus.add(s.get("sent_sku"))
        sub_skus.add(s.get("supplier_sku"))
        asks.append(f"The {_their(s.get('supplier_sku') or '')} line - our PO "
                    f"calls for {_their(s.get('sent_sku') or '')}. Please confirm "
                    f"the correct item is entered, or correct the line.")
    for e in report.get("qty_mismatch") or []:
        asks.append(f"{_their(e.get('sku') or '')}: please change Qty to "
                    f"{e.get('sent_qty')} (the SO shows {e.get('supplier_qty')}).")
    for e in report.get("missing_at_supplier") or []:
        if e.get("sku") in sub_skus:
            continue
        asks.append(f"Please add {e.get('sent_qty')} x {_their(e.get('sku') or '')} "
                    f"- we don't see it on the SO.")
    for e in report.get("unexpected_from_supplier") or []:
        if e.get("sku") in sub_skus:
            continue
        asks.append(f"{_their(e.get('sku') or '')} x{e.get('supplier_qty')} is on "
                    f"the SO but not on our PO - please confirm or remove.")
    for e in report.get("unresolved_supplier_lines") or []:
        asks.append(f"We could not identify this SO line: "
                    f"\"{e.get('desc') or e.get('item') or '?'}\" x{e.get('qty')} "
                    f"- please tell us what item that is.")
    for f in report.get("flags") or []:
        if "PRICE-CHECK" in f:
            continue  # our stale cogs data — internal note, not a GHI question
        if "PRICE-ID" in f:
            asks.append(f"Please double-check this line - the billed price points "
                        f"at a different item than the description: {f}")
        elif f.startswith("BACKORDER"):
            asks.append(f"{f} - please confirm the backorder timing.")
        elif f.startswith("PRICE MATH"):
            asks.append(f"The math on this line doesn't work out - please check: {f}")
    return asks


def create_approval_draft(order_id: str, doc_ref: str, report: Dict,
                          message_id: str, clean: bool) -> Dict:
    """Write the approval reply DRAFT in the GHI thread. Never sends."""
    from config import SUPPLIER_INFO
    from gmail_sync import gmail_api_request
    from supplier_orders import _send_email, INTERNAL_ALERT_EMAIL, supplier_greeting

    doc_ref = doc_ref or "?"
    if _draft_already_created(order_id, doc_ref):
        return {"status": "already_drafted", "order_id": order_id, "doc_ref": doc_ref}

    meta = gmail_api_request(
        f"messages/{message_id}",
        {"format": "metadata", "metadataHeaders": "Subject"})
    meta2 = gmail_api_request(
        f"messages/{message_id}",
        {"format": "metadata", "metadataHeaders": "Message-ID"})
    headers = {}
    for m in (meta, meta2):
        if m:
            for h in (m.get("payload", {}) or {}).get("headers", []):
                headers[h["name"].lower()] = h["value"]
    thread_id = (meta or {}).get("threadId")
    subject = headers.get("subject") or f"PO {order_id}"
    if not subject.upper().startswith("RE"):
        subject = f"RE: {subject}"
    orig_msgid = headers.get("message-id")

    contact_email = (SUPPLIER_INFO.get("GHI") or {}).get("email",
                                                         "kbelfiore@ghicabinets.com")
    greeting = supplier_greeting("GHI").replace("Hey", "Hi")
    door = _door_text(order_id)

    if clean:
        n = len(report.get("matched") or [])
        units = report.get("supplier_line_total")
        body = (f"{greeting}\n\n"
                f"SO {doc_ref} for our PO {order_id}{door} checks out on our end"
                f"{f' - all {n} lines match, {units} pieces' if n else ''}.\n\n"
                f"PO {order_id} is approved for processing.\n\n"
                f"Thank you\nWilliam")
    else:
        asks = build_approval_asks(report)
        if not asks:
            asks = ["Please re-send the SO - our checker flagged it but could "
                    "not compose a specific question (a human is looking too)."]
        numbered = "\n".join(f"{i}. {a}" for i, a in enumerate(asks, 1))
        body = (f"{greeting}\n\n"
                f"SO {doc_ref} for our PO {order_id}{door} mostly checks out - "
                f"please confirm the item(s) below before processing:\n\n"
                f"{numbered}\n\n"
                f"Once that is confirmed/corrected, PO {order_id} is approved "
                f"for processing.\n\n"
                f"Thank you\nWilliam")

    mime = MIMEText(body)
    mime["To"] = contact_email
    mime["Subject"] = subject
    if orig_msgid:
        mime["In-Reply-To"] = orig_msgid
        mime["References"] = orig_msgid
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    payload = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id
    res = _gmail_post("drafts", payload)
    if not res:
        return {"status": "error", "message": "gmail drafts.create failed"}

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'ghi_approval_draft', %s, 'ghi_inbox')
                """, (order_id, json.dumps({
                    "doc_ref": doc_ref, "clean": clean,
                    "draft_id": res.get("id"), "message_id": message_id})))
                conn.commit()
    except Exception as e:
        print(f"[GHI-INBOX] event insert failed: {e}")

    internal_notes = [f for f in (report.get("flags") or []) if "PRICE-CHECK" in f]
    notes_html = ("<p><strong>Internal only (not in the draft):</strong> "
                  + "<br>".join(internal_notes) + "</p>") if internal_notes else ""
    _send_email(
        order_id, INTERNAL_ALERT_EMAIL,
        f"APPROVAL DRAFT READY - PO {order_id} (GHI SO {doc_ref})"
        + ("" if clean else " - has confirm questions"),
        f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
        f"<p>The reply draft for GHI is waiting in Gmail drafts - review and send.</p>"
        f"<pre style='background:#f5f5f5;padding:12px;white-space:pre-wrap;'>"
        f"{body}</pre>{notes_html}</div>",
        triggered_by="ghi_approval_draft")
    return {"status": "drafted", "order_id": order_id, "doc_ref": doc_ref,
            "clean": clean, "draft_id": res.get("id")}


# =============================================================================
# CROSS-THREAD PO CAPTURE
# =============================================================================

def ensure_inbox_seen_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ghi_inbox_seen (
                message_id VARCHAR(120) PRIMARY KEY,
                order_ids TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        conn.commit()


def _inbox_seen(message_id: str) -> bool:
    with get_db() as conn:
        ensure_inbox_seen_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM ghi_inbox_seen WHERE message_id = %s",
                        (message_id,))
            return cur.fetchone() is not None


def _mark_seen(message_id: str, order_ids: List[str]):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ghi_inbox_seen (message_id, order_ids)
                VALUES (%s, %s) ON CONFLICT (message_id) DO NOTHING
            """, (message_id, ",".join(order_ids)))
            conn.commit()


def _fetch_text(message_id: str):
    """(plain text, subject, sender) for one message — text/plain preferred,
    tag-stripped html fallback."""
    from gmail_sync import gmail_api_request

    data = gmail_api_request(f"messages/{message_id}", {"format": "full"})
    if not data:
        return "", "", ""
    payload = data.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain, html = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if body.get("data"):
            try:
                txt = base64.urlsafe_b64decode(body["data"]).decode(
                    "utf-8", errors="ignore")
            except Exception:
                txt = ""
            if mime == "text/plain":
                plain.append(txt)
            elif mime == "text/html":
                html.append(txt)
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    text = "\n".join(plain) if plain else re.sub(r"<[^>]+>", " ", "\n".join(html))
    return text, headers.get("subject", ""), headers.get("from", "")


def _known_order_ids(candidates: List[str]) -> List[str]:
    if not candidates:
        return []
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT order_id FROM orders WHERE order_id = ANY(%s)",
                            (list(candidates),))
                return sorted({str(r[0]) for r in cur.fetchall()})
    except Exception as e:
        print(f"[GHI-INBOX] order lookup failed: {e}")
        return []


def ghi_thread_capture(hours_back: int = 24) -> Dict:
    """Scan ALL ghicabinets.com email; file each message against every real
    order it mentions (any thread); alert us when it mentions none."""
    from gmail_sync import gmail_configured, search_emails
    from supplier_orders import _send_email, INTERNAL_ALERT_EMAIL

    if not gmail_configured():
        return {"status": "skipped", "reason": "gmail_not_configured"}
    out = {"status": "ok", "checked": 0, "filed": 0, "no_po_alerts": 0,
           "errors": []}
    try:
        messages = search_emails(
            f"newer_than:{int(hours_back)}h from:ghicabinets.com", 50)
    except Exception as e:
        return {"status": "error", "errors": [f"search: {e}"]}
    for m in messages:
        try:
            if _inbox_seen(m["id"]):
                continue
            text, subject, sender = _fetch_text(m["id"])
            out["checked"] += 1
            candidates = sorted(set(re.findall(r"\b(\d{4})\b",
                                               f"{subject} {text}")))
            known = _known_order_ids(candidates)
            for oid in known:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO order_events
                                (order_id, event_type, event_data, source)
                            VALUES (%s, 'ghi_email_mention', %s, 'ghi_inbox')
                        """, (oid, json.dumps({
                            "message_id": m["id"], "subject": subject,
                            "from": sender, "snippet": text.strip()[:400]})))
                        conn.commit()
                out["filed"] += 1
            if not known:
                _send_email(
                    "", INTERNAL_ALERT_EMAIL,
                    f"GHI email needs a human - no PO reference: {subject[:60]}",
                    f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                    f"<p>A GHI email mentions no order number the robot knows - "
                    f"someone needs to read it.</p>"
                    f"<p><strong>From:</strong> {sender}<br>"
                    f"<strong>Subject:</strong> {subject}</p>"
                    f"<pre style='background:#f5f5f5;padding:12px;"
                    f"white-space:pre-wrap;'>{text.strip()[:600]}</pre></div>",
                    triggered_by="ghi_inbox_no_po")
                out["no_po_alerts"] += 1
            _mark_seen(m["id"], known)
        except Exception as e:
            out["errors"].append(f"{m.get('id')}: {e}")
    return out
