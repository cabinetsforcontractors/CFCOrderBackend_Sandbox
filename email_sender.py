"""
email_sender.py
CFC Orders Gmail Send Module — Phase 4: Customer Communications

Sends emails via Gmail API using existing OAuth credentials from gmail_sync.py.
For the payment_link template, also generates and attaches a PDF invoice.

All sends are logged to order_events with proper source tagging.

CRITICAL: System-generated emails (lifecycle templates) are tagged with
source='system_generated' so the lifecycle engine excludes them from
resetting the inactivity clock.
"""

import os
import json
import base64
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timezone
from typing import Dict, Optional

from config import GMAIL_SEND_ENABLED
from gmail_sync import get_gmail_access_token, gmail_configured
from email_templates import (
    render_template,
    get_template_subject,
    is_lifecycle_template,
    TEMPLATE_REGISTRY,
)
from db_helpers import get_db, get_order_by_id


# Outbound identity: EMAIL_FROM_ADDRESS switch (orders@ after Gmail alias
# verification, William 2026-07-19) wins; legacy william@ header is the
# unset default. Same switch as email_identity.py.
CFC_SENDER_NAME = os.environ.get(
    "EMAIL_FROM_NAME", "William Prince — Cabinets For Contractors").strip()
# Fallback re-homed 2026-07-26: ALL @cabinetsforcontractors.NET mailboxes were
# DELETED (GoDaddy M365 cleanup, marketing-lane note) — mail to them bounces.
CFC_SENDER_EMAIL = (os.environ.get("EMAIL_FROM_ADDRESS", "").strip()
                    or "orders@cabinetsforcontractors.com")


def send_order_email(
    order_id: str,
    template_id: str,
    to_email: str,
    order_data: Dict = None,
    custom_subject: str = None,
    triggered_by: str = "manual",
) -> Dict:
    """
    Send an email for an order using a template.

    For the 'payment_link' template, also generates and attaches a PDF invoice
    if 'shipping_result' is present in order_data.

    Args:
        order_id: The order ID
        template_id: Template to use (from TEMPLATE_REGISTRY)
        to_email: Customer email address
        order_data: Order dict (if None, fetched from DB)
        custom_subject: Override the template's default subject
        triggered_by: Who triggered this send
    """
    if template_id not in TEMPLATE_REGISTRY:
        return {
            "success": False,
            "error": f"Unknown template: {template_id}",
            "available_templates": list(TEMPLATE_REGISTRY.keys()),
        }

    if not GMAIL_SEND_ENABLED:
        return {
            "success": False,
            "error": "Email sending is disabled (GMAIL_SEND_ENABLED=false)",
            "dry_run": True,
        }

    if not gmail_configured():
        return {"success": False, "error": "Gmail OAuth not configured"}

    if not to_email or "@" not in to_email:
        return {"success": False, "error": f"Invalid email address: {to_email}"}

    # Sandbox safety: EMAIL_ALLOWLIST gate
    # If EMAIL_ALLOWLIST is non-empty, non-listed recipients are redirected
    # to INTERNAL_SAFETY_EMAIL (if set) or blocked outright.
    # Default (env unset) = full backward compatibility, no change.
    _email_allowlist = os.environ.get("EMAIL_ALLOWLIST", "").strip()
    if _email_allowlist:
        allowed = {e.strip().lower() for e in _email_allowlist.split(",") if e.strip()}
        if to_email.lower() not in allowed:
            redirect = os.environ.get("INTERNAL_SAFETY_EMAIL", "").strip()
            if redirect:
                print(f"[EMAIL-GUARD] redirected to={to_email} -> {redirect} order={order_id}")
                to_email = redirect
            else:
                print(f"[EMAIL-GUARD] blocked to={to_email} order={order_id} reason=not_in_allowlist")
                return {"success": False, "error": "recipient not in EMAIL_ALLOWLIST", "dry_run": True, "original_to": to_email}

    if order_data is None:
        order_data = get_order_by_id(order_id)
        if not order_data:
            return {"success": False, "error": f"Order {order_id} not found"}

    order_data["order_id"] = order_id

    html_body = render_template(template_id, order_data)
    if not html_body:
        return {"success": False, "error": f"Failed to render template: {template_id}"}

    subject = custom_subject or get_template_subject(template_id, order_data)

    # Generate PDF attachments for payment_link template (invoice + pick list,
    # William 2026-07-28: the pick list rides every invoice email)
    pdf_bytes = None
    extra_attachments = None
    if template_id == "payment_link":
        shipping_result = order_data.get("shipping_result")
        if shipping_result:
            try:
                from invoice_pdf import generate_invoice_pdf
                pdf_bytes = generate_invoice_pdf(order_data, shipping_result)
                if pdf_bytes:
                    print(f"[EMAIL] PDF invoice generated for order {order_id} ({len(pdf_bytes)} bytes)")
                else:
                    print(f"[EMAIL] PDF generation returned None for order {order_id}")
            except Exception as e:
                print(f"[EMAIL] PDF generation failed for order {order_id}: {e}")
        try:
            from picklist_pdf import generate_picklist_pdf
            pk = generate_picklist_pdf(order_data)
            if pk:
                extra_attachments = [{"filename": f"CFC-Picklist-{order_id}.pdf",
                                      "content": pk, "mime": "application/pdf"}]
        except Exception as e:
            print(f"[EMAIL] picklist generation failed for order {order_id}: {e}")

    try:
        message_id = _gmail_send(to_email, subject, html_body, pdf_bytes=pdf_bytes, order_id=order_id,
                                 extra_attachments=extra_attachments)

        if message_id:
            is_lifecycle = is_lifecycle_template(template_id)
            event_source = "system_generated" if is_lifecycle else "email_send"

            _log_email_event(
                order_id=order_id,
                template_id=template_id,
                to_email=to_email,
                subject=subject,
                message_id=message_id,
                triggered_by=triggered_by,
                source=event_source,
            )

            return {
                "success": True,
                "message_id": message_id,
                "template": template_id,
                "to": to_email,
                "subject": subject,
                "pdf_attached": pdf_bytes is not None,
                "is_lifecycle": is_lifecycle,
                "source_tag": event_source,
            }
        else:
            return {"success": False, "error": "Gmail API returned no message ID"}

    except Exception as e:
        _log_email_event(
            order_id=order_id,
            template_id=template_id,
            to_email=to_email,
            subject=subject,
            message_id=None,
            triggered_by=triggered_by,
            source="email_send_failed",
            error=str(e),
        )
        return {"success": False, "error": f"Gmail send failed: {str(e)}"}


def send_email_dry_run(
    order_id: str,
    template_id: str,
    to_email: str,
    order_data: Dict = None,
) -> Dict:
    """Preview what an email would look like without sending."""
    if template_id not in TEMPLATE_REGISTRY:
        return {"success": False, "error": f"Unknown template: {template_id}"}

    if order_data is None:
        order_data = get_order_by_id(order_id)
        if not order_data:
            return {"success": False, "error": f"Order {order_id} not found"}

    order_data["order_id"] = order_id

    html_body = render_template(template_id, order_data)
    subject = get_template_subject(template_id, order_data)

    return {
        "success": True,
        "dry_run": True,
        "to": to_email,
        "subject": subject,
        "html": html_body,
        "template": template_id,
        "is_lifecycle": is_lifecycle_template(template_id),
    }


# =============================================================================
# GMAIL API SEND
# =============================================================================

def _gmail_send(
    to_email: str,
    subject: str,
    html_body: str,
    pdf_bytes: Optional[bytes] = None,
    order_id: str = "",
    extra_attachments: Optional[list] = None,
) -> Optional[str]:
    """
    Send an email via Gmail API, optionally with a PDF attachment plus any
    extra attachments ([{filename, content(bytes), mime}]).
    Returns the Gmail message ID on success, None on failure.
    """
    token = get_gmail_access_token()
    if not token:
        raise Exception("Failed to get Gmail access token")

    msg = MIMEMultipart("mixed")
    msg["From"] = f"{CFC_SENDER_NAME} <{CFC_SENDER_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    # HTML body
    alt = MIMEMultipart("alternative")
    plain_text = "View this email in an HTML-capable email client.\n\nOrder-related email from Cabinets For Contractors.\nCall (770) 990-4885 or reply for help."
    alt.attach(MIMEText(plain_text, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    # PDF attachment
    if pdf_bytes:
        filename = f"CFC-Invoice-{order_id}.pdf" if order_id else "CFC-Invoice.pdf"
        part = MIMEBase("application", "pdf")
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    for att in (extra_attachments or []):
        try:
            maintype, _, subtype = (att.get("mime") or "application/octet-stream").partition("/")
            part = MIMEBase(maintype, subtype or "octet-stream")
            part.set_payload(att["content"])
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment",
                            filename=att.get("filename") or "attachment")
            msg.attach(part)
        except Exception as e:
            print(f"[EMAIL] extra attachment failed ({att.get('filename')}): {e}")

    raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    payload = json.dumps({"raw": raw_message}).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            message_id = data.get("id")
            print(f"[EMAIL] Sent to {to_email}: {subject} (msg_id={message_id}, pdf={pdf_bytes is not None})")
            return message_id
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()[:500]
        print(f"[EMAIL] Gmail API error {e.code}: {error_body}")
        raise Exception(f"Gmail API {e.code}: {error_body}")
    except Exception as e:
        print(f"[EMAIL] Send error: {e}")
        raise


def create_invoice_draft(
    order_id: str,
    to_email: str = "",
    shipping_amount: float = 0.0,
    square_link: str = "",
    tariff_rate: float = 0.08,
    auto_link: bool = True,
    note: str = "",
    residential: str = "",
) -> dict:
    """BEAT 3 (William 2026-07-26): render the v4 invoice (template + PDF)
    from the order row and land it as a GMAIL DRAFT with the PDF attached —
    the repo makes the draft, nobody hand-builds invoices. DRAFT-FIRST: this
    never sends; William reviews and sends.

    PAYMENT LINK (William 2026-07-27, "we need to build a Payment Links
    API for square"): when square_link is empty and auto_link is true, the
    robot CREATES the Square link itself (amount = grand total, name =
    INV-{order_id} so square_sync matches exactly) — the draft lands
    COMPLETE. Manual square_link still wins when passed. If creation fails
    the pay line falls back to '#' and the failure rides the response."""
    token = get_gmail_access_token()
    if not token:
        return {"success": False, "error": "Gmail OAuth not configured"}

    order_data = get_order_by_id(order_id)
    if not order_data:
        return {"success": False, "error": f"Order {order_id} not found"}
    order_data["order_id"] = order_id
    # pay-by-check accounts (William 2026-07-29, Nationwide): no Square link
    # on their invoices — check notice replaces the pay button.
    from config import pays_by_check
    if pays_by_check(order_data):
        auto_link = False
        order_data["pay_by_check"] = True
    if (note or "").strip():
        # William 2026-07-28 (the 5731 B09->B09-FH case): a per-invoice note
        # rides the email body AND the PDF when passed.
        order_data["invoice_note"] = note.strip()
    # classification box mirrors the actual quote (William 2026-07-28):
    # residential="true"/"false" declares how the shipping number was quoted;
    # empty = unknown -> the historical residential box.
    if (residential or "").strip().lower() in ("true", "false"):
        order_data["quoted_residential"] = residential.strip().lower() == "true"

    subtotal = float(order_data.get("order_total") or 0)
    tariff = round(subtotal * tariff_rate, 2)
    shipping = round(float(shipping_amount or 0), 2)
    grand = round(subtotal + tariff + shipping, 2)
    order_data["shipping_result"] = {
        "total_items": subtotal,
        "tariff_amount": tariff,
        "tariff_rate": tariff_rate,
        "total_shipping": shipping,
        "grand_total": grand,
    }
    link_info = None
    link_error = None
    if not (square_link or "").strip() and auto_link:
        try:
            from square_links import create_payment_link
            link_info = create_payment_link(order_id, grand)
            square_link = link_info["url"]
            # event = the kill-switch registry: cancel deletes every link
            # recorded here (William 2026-07-27)
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO order_events
                                   (order_id, event_type, event_data, source)
                                   VALUES (%s, 'payment_link_created', %s,
                                           'invoice_draft')""",
                                (order_id, json.dumps(
                                    {"link_id": link_info["id"],
                                     "url": link_info["url"],
                                     "amount": grand, "auto": True})))
                    conn.commit()
        except Exception as e:
            link_error = str(e)
            print(f"[EMAIL] draft-invoice auto-link failed for {order_id}: {e}")
    order_data["payment_link"] = (square_link or "").strip() or "#"

    # Fold-in ruling 2026-07-27: the residential box rides every invoice; its
    # red button needs a live confirm-commercial link. Monthly token — a daily
    # one would die at midnight while the invoice sits unpaid.
    try:
        from checkout import generate_checkout_token
        base = os.environ.get("CHECKOUT_BASE_URL", "").strip() or "https://cfcorderbackend-sandbox.onrender.com"
        ctok = generate_checkout_token(str(order_id), long_lived=True)
        order_data["confirm_commercial_url"] = f"{base}/checkout/{order_id}/confirm-commercial?token={ctok}"
    except Exception as e:
        print(f"[EMAIL] draft-invoice confirm-commercial url failed for {order_id}: {e}")

    html_body = render_template("payment_link", order_data)
    if not html_body:
        return {"success": False, "error": "template render failed"}
    subject = get_template_subject("payment_link", order_data)

    pdf_bytes = None
    try:
        from invoice_pdf import generate_invoice_pdf
        pdf_bytes = generate_invoice_pdf(order_data, order_data["shipping_result"])
    except Exception as e:
        print(f"[EMAIL] draft-invoice PDF failed for {order_id}: {e}")

    picklist_bytes = None
    try:
        from picklist_pdf import generate_picklist_pdf
        picklist_bytes = generate_picklist_pdf(order_data)
    except Exception as e:
        print(f"[EMAIL] draft-invoice picklist failed for {order_id}: {e}")

    to_email = (to_email or "").strip() or (order_data.get("email") or "").strip()
    if not to_email:
        return {"success": False, "error": "no recipient (order has no email)"}

    msg = MIMEMultipart("mixed")
    from email_identity import apply_from
    apply_from(msg)
    msg["To"] = to_email
    msg["Subject"] = subject
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("View this email in an HTML-capable client.", "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)
    if pdf_bytes:
        part = MIMEBase("application", "pdf")
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=f"CFC-Invoice-{order_id}.pdf")
        msg.attach(part)
    if picklist_bytes:
        part = MIMEBase("application", "pdf")
        part.set_payload(picklist_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=f"CFC-Picklist-{order_id}.pdf")
        msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
        data=json.dumps({"message": {"raw": raw}}).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            draft_id = json.loads(response.read().decode()).get("id")
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"Gmail API {e.code}: {e.read().decode()[:300]}"}

    _log_email_event(order_id=order_id, template_id="payment_link",
                     to_email=to_email, subject=subject, message_id=draft_id,
                     triggered_by="draft_invoice", source="invoice_draft")
    print(f"[EMAIL] invoice DRAFT created for {order_id} -> {to_email} "
          f"(draft={draft_id}, pdf={pdf_bytes is not None})")
    out = {"success": True, "draft_id": draft_id, "to": to_email,
           "subject": subject, "pdf_attached": pdf_bytes is not None,
           "picklist_attached": picklist_bytes is not None,
           "note_included": bool((note or "").strip()),
           "totals": order_data["shipping_result"],
           "payment_link": order_data["payment_link"]
           if order_data["payment_link"] != "#" else None}
    if link_info:
        out["payment_link_id"] = link_info.get("id")
        out["payment_link_auto"] = True
    if link_error:
        out["payment_link_error"] = link_error
    return out


def create_gmail_draft(to_email: str, subject: str, html_body: str,
                       attachments: list = None) -> dict:
    """Land an arbitrary email as a Gmail DRAFT with attachments — nothing
    sends. attachments: [{filename, content(bytes), mime}]. Built for the
    draft-ghi-sheet door (William 2026-07-28: supplier order drafts must land
    COMPLETE, sheet attached)."""
    token = get_gmail_access_token()
    if not token:
        return {"success": False, "error": "Gmail OAuth not configured"}
    msg = MIMEMultipart("mixed")
    from email_identity import apply_from
    apply_from(msg)
    msg["To"] = to_email
    msg["Subject"] = subject
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("View this email in an HTML-capable client.", "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)
    for att in (attachments or []):
        maintype, _, subtype = (att.get("mime") or "application/octet-stream").partition("/")
        part = MIMEBase(maintype, subtype or "octet-stream")
        part.set_payload(att["content"])
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=att.get("filename") or "attachment")
        msg.attach(part)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
        data=json.dumps({"message": {"raw": raw}}).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            draft_id = json.loads(response.read().decode()).get("id")
    except urllib.error.HTTPError as e:
        return {"success": False,
                "error": f"Gmail API {e.code}: {e.read().decode()[:300]}"}
    return {"success": True, "draft_id": draft_id, "to": to_email,
            "subject": subject,
            "attachments": [a.get("filename") for a in (attachments or [])]}


# =============================================================================
# EVENT LOGGING
# =============================================================================

def _log_email_event(
    order_id: str,
    template_id: str,
    to_email: str,
    subject: str,
    message_id: Optional[str],
    triggered_by: str,
    source: str,
    error: str = None,
):
    event_type = "email_sent" if message_id else "email_send_failed"
    event_data = {
        "template_id": template_id,
        "template_name": TEMPLATE_REGISTRY.get(template_id, {}).get("name", template_id),
        "to_email": to_email,
        "subject": subject,
        "triggered_by": triggered_by,
        "gmail_message_id": message_id,
        "is_lifecycle": is_lifecycle_template(template_id),
    }
    if error:
        event_data["error"] = error

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (order_id, event_type, json.dumps(event_data), source),
                )
    except Exception as e:
        print(f"[EMAIL] Failed to log event for order {order_id}: {e}")


def get_email_history(order_id: str) -> list:
    """Get email send history for an order from order_events."""
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=__import__('psycopg2').extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT event_id AS id, order_id, event_type, event_data, source, created_at
                    FROM order_events
                    WHERE order_id = %s
                    AND event_type IN ('email_sent', 'email_send_failed')
                    ORDER BY created_at DESC
                    """,
                    (order_id,),
                )
                rows = cur.fetchall()
                results = []
                for row in rows:
                    entry = dict(row)
                    if isinstance(entry.get("event_data"), str):
                        try:
                            entry["event_data"] = json.loads(entry["event_data"])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    results.append(entry)
                return results
    except Exception as e:
        print(f"[EMAIL] Failed to get email history for {order_id}: {e}")
        return []
