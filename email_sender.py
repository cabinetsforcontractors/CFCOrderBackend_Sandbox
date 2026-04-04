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


CFC_SENDER_NAME = "William Prince — Cabinets For Contractors"
CFC_SENDER_EMAIL = "william@cabinetsforcontractors.net"


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

    if order_data is None:
        order_data = get_order_by_id(order_id)
        if not order_data:
            return {"success": False, "error": f"Order {order_id} not found"}

    order_data["order_id"] = order_id

    html_body = render_template(template_id, order_data)
    if not html_body:
        return {"success": False, "error": f"Failed to render template: {template_id}"}

    subject = custom_subject or get_template_subject(template_id, order_data)

    # Generate PDF attachment for payment_link template
    pdf_bytes = None
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
        message_id = _gmail_send(to_email, subject, html_body, pdf_bytes=pdf_bytes, order_id=order_id)

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
) -> Optional[str]:
    """
    Send an email via Gmail API, optionally with a PDF attachment.
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
                    SELECT id, order_id, event_type, event_data, source, created_at
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
