"""
email_sender.py
CFC Orders Gmail Send Module — Phase 4: Customer Communications

Sends emails via Gmail API using existing OAuth credentials from gmail_sync.py.
All sends are logged to order_events with proper source tagging.

CRITICAL: System-generated emails (lifecycle templates) are tagged with
source='system_generated' so the lifecycle engine excludes them from
resetting the inactivity clock.

Usage:
    from email_sender import send_order_email
    
    result = send_order_email(
        order_id="5307",
        template_id="payment_link",
        to_email="customer@example.com",
        order_data={...}
    )

Requires:
    - GMAIL_SEND_ENABLED=true in environment
    - Valid Gmail OAuth credentials (same as gmail_sync.py)
"""

import os
import json
import base64
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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


# CFC sender identity
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
    
    Args:
        order_id: The order ID
        template_id: Template to use (from TEMPLATE_REGISTRY)
        to_email: Customer email address
        order_data: Order dict (if None, fetched from DB)
        custom_subject: Override the template's default subject
        triggered_by: Who triggered this send — "manual", "lifecycle_engine", "status_change"
    
    Returns:
        Dict with success status, message_id, and details
    """
    # Validate template exists
    if template_id not in TEMPLATE_REGISTRY:
        return {
            "success": False,
            "error": f"Unknown template: {template_id}",
            "available_templates": list(TEMPLATE_REGISTRY.keys()),
        }
    
    # Check if sending is enabled
    if not GMAIL_SEND_ENABLED:
        return {
            "success": False,
            "error": "Email sending is disabled (GMAIL_SEND_ENABLED=false)",
            "dry_run": True,
        }
    
    # Check Gmail credentials
    if not gmail_configured():
        return {
            "success": False,
            "error": "Gmail OAuth not configured",
        }
    
    # Validate email
    if not to_email or "@" not in to_email:
        return {
            "success": False,
            "error": f"Invalid email address: {to_email}",
        }
    
    # Get order data if not provided
    if order_data is None:
        order_data = get_order_by_id(order_id)
        if not order_data:
            return {
                "success": False,
                "error": f"Order {order_id} not found",
            }
    
    # Ensure order_id is in the data
    order_data["order_id"] = order_id
    
    # Render the template
    html_body = render_template(template_id, order_data)
    if not html_body:
        return {
            "success": False,
            "error": f"Failed to render template: {template_id}",
        }
    
    # Get subject
    subject = custom_subject or get_template_subject(template_id, order_data)
    
    # Build and send the email
    try:
        message_id = _gmail_send(to_email, subject, html_body)
        
        if message_id:
            # Determine source tag for event logging
            # Lifecycle emails get 'system_generated' so they don't reset the clock
            is_lifecycle = is_lifecycle_template(template_id)
            event_source = "system_generated" if is_lifecycle else "email_send"
            
            # Log to order_events
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
                "is_lifecycle": is_lifecycle,
                "source_tag": event_source,
            }
        else:
            return {
                "success": False,
                "error": "Gmail API returned no message ID",
            }
    
    except Exception as e:
        # Log the failure too
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
        return {
            "success": False,
            "error": f"Gmail send failed: {str(e)}",
        }


def send_email_dry_run(
    order_id: str,
    template_id: str,
    to_email: str,
    order_data: Dict = None,
) -> Dict:
    """
    Preview what an email would look like without sending.
    Returns the rendered HTML and subject.
    """
    if template_id not in TEMPLATE_REGISTRY:
        return {
            "success": False,
            "error": f"Unknown template: {template_id}",
        }
    
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

def _gmail_send(to_email: str, subject: str, html_body: str) -> Optional[str]:
    """
    Send an email via Gmail API.
    
    Returns the Gmail message ID on success, None on failure.
    """
    token = get_gmail_access_token()
    if not token:
        raise Exception("Failed to get Gmail access token")
    
    # Build MIME message
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{CFC_SENDER_NAME} <{CFC_SENDER_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    
    # Plain text fallback
    plain_text = f"View this email in an HTML-capable email client.\n\nOrder-related email from Cabinets For Contractors.\nCall (770) 990-4885 or reply to this email for help."
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    
    # Encode for Gmail API
    raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    
    # Send via Gmail API
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    payload = json.dumps({"raw": raw_message}).encode("utf-8")
    
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            message_id = data.get("id")
            print(f"[EMAIL] Sent to {to_email}: {subject} (msg_id={message_id})")
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
    """
    Log email send to order_events table.
    
    CRITICAL: source='system_generated' for lifecycle emails ensures
    they don't reset the lifecycle inactivity clock.
    """
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
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, %s, %s, %s)
                """, (order_id, event_type, json.dumps(event_data), source))
    except Exception as e:
        print(f"[EMAIL] Failed to log event for order {order_id}: {e}")


def get_email_history(order_id: str) -> list:
    """
    Get email send history for an order from order_events.
    Returns list of email events sorted newest first.
    """
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=__import__('psycopg2').extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, order_id, event_type, event_data, source, created_at
                    FROM order_events
                    WHERE order_id = %s
                    AND event_type IN ('email_sent', 'email_send_failed')
                    ORDER BY created_at DESC
                """, (order_id,))
                rows = cur.fetchall()
                
                results = []
                for row in rows:
                    entry = dict(row)
                    # Parse event_data JSON if it's a string
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
