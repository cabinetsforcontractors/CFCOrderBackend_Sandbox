"""
email_templates.py
CFC Orders Email Template Engine — Phase 4: Customer Communications

9 HTML email templates with order data injection:
  1. payment_link           — Payment link + order summary
  2. payment_confirmation   — Payment received confirmation
  3. shipping_notification  — Shipped with tracking/carrier/ETA
  4. delivery_confirmation  — Delivered confirmation
  5. trusted_payment_reminder — Gentle reminder for trusted customers
  6. payment_reminder_day6  — Lifecycle: order not paid (day 6)
  7. inactive_notice_day29  — Lifecycle: marked inactive (day 29)
  8. deletion_warning_day44 — Lifecycle: will be canceled tomorrow (day 44)
  9. cancel_confirmation    — Lifecycle: order canceled

Usage:
    from email_templates import render_template, get_template_list
    
    html = render_template("payment_link", order_data)
    templates = get_template_list()
"""

from typing import Dict, List, Optional
from datetime import datetime


# =============================================================================
# TEMPLATE REGISTRY
# =============================================================================

TEMPLATE_REGISTRY = {
    "payment_link": {
        "name": "Payment Link",
        "subject": "Invoice for Order #{order_id} — Cabinets For Contractors",
        "description": "Sends payment link with order summary",
        "category": "manual",
        "is_lifecycle": False,
    },
    "payment_confirmation": {
        "name": "Payment Confirmation",
        "subject": "Payment Received — Order #{order_id}",
        "description": "Confirms payment was received",
        "category": "manual",
        "is_lifecycle": False,
    },
    "shipping_notification": {
        "name": "Shipping Notification",
        "subject": "Your Order #{order_id} Has Shipped!",
        "description": "Notifies customer of shipment with tracking info",
        "category": "manual",
        "is_lifecycle": False,
    },
    "delivery_confirmation": {
        "name": "Delivery Confirmation",
        "subject": "Your Order #{order_id} Has Been Delivered",
        "description": "Confirms delivery of the order",
        "category": "manual",
        "is_lifecycle": False,
    },
    "trusted_payment_reminder": {
        "name": "Trusted Customer Payment Reminder",
        "subject": "Payment Reminder — Order #{order_id}",
        "description": "Gentle payment reminder for trusted customers who shipped before paying",
        "category": "manual",
        "is_lifecycle": False,
    },
    "payment_reminder_day6": {
        "name": "Payment Reminder (Day 6)",
        "subject": "Your Order #{order_id} Hasn't Been Paid",
        "description": "Lifecycle: auto-reminder that order hasn't been paid yet",
        "category": "lifecycle",
        "is_lifecycle": True,
    },
    "inactive_notice_day29": {
        "name": "Inactive Notice (Day 29)",
        "subject": "Your Order #{order_id} Has Been Marked Inactive",
        "description": "Lifecycle: order moved to inactive due to no activity",
        "category": "lifecycle",
        "is_lifecycle": True,
    },
    "deletion_warning_day44": {
        "name": "Deletion Warning (Day 44)",
        "subject": "Your Order #{order_id} Will Be Canceled Tomorrow",
        "description": "Lifecycle: final warning before auto-cancellation",
        "category": "lifecycle",
        "is_lifecycle": True,
    },
    "cancel_confirmation": {
        "name": "Cancellation Confirmation",
        "subject": "Your Order #{order_id} Has Been Canceled",
        "description": "Lifecycle: confirms order was canceled",
        "category": "lifecycle",
        "is_lifecycle": True,
    },
}


def get_template_list() -> List[Dict]:
    """Return list of available templates with metadata."""
    return [
        {"id": tid, **meta}
        for tid, meta in TEMPLATE_REGISTRY.items()
    ]


def get_template_subject(template_id: str, order_data: Dict) -> str:
    """Get the rendered subject line for a template."""
    meta = TEMPLATE_REGISTRY.get(template_id)
    if not meta:
        return f"Order #{order_data.get('order_id', '?')}"
    return meta["subject"].format(**order_data)


def is_lifecycle_template(template_id: str) -> bool:
    """Check if a template is lifecycle-related (system-generated)."""
    meta = TEMPLATE_REGISTRY.get(template_id)
    return meta.get("is_lifecycle", False) if meta else False


# =============================================================================
# SHARED HTML COMPONENTS
# =============================================================================

def _base_style() -> str:
    """Base CSS for all email templates."""
    return """
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; margin: 0; padding: 0; background: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; background: #ffffff; }
        .header { background: #1a365d; color: #ffffff; padding: 24px 32px; }
        .header h1 { margin: 0; font-size: 22px; font-weight: 600; }
        .header .subtitle { color: #93c5fd; font-size: 14px; margin-top: 4px; }
        .body { padding: 32px; color: #333333; line-height: 1.6; }
        .body p { margin: 0 0 16px 0; }
        .body a { color: #2563eb; }
        .cta-button { display: inline-block; background: #2563eb; color: #ffffff !important; 
                      text-decoration: none; padding: 14px 32px; border-radius: 6px; 
                      font-weight: 600; font-size: 16px; margin: 16px 0; }
        .cta-button:hover { background: #1d4ed8; }
        .cta-urgent { background: #dc2626; }
        .cta-urgent:hover { background: #b91c1c; }
        .order-summary { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; 
                         padding: 20px; margin: 20px 0; }
        .order-summary h3 { margin: 0 0 12px 0; color: #1a365d; font-size: 16px; }
        .detail-row { display: flex; justify-content: space-between; padding: 6px 0; 
                      border-bottom: 1px solid #f1f5f9; font-size: 14px; }
        .detail-label { color: #64748b; }
        .detail-value { font-weight: 600; color: #1e293b; }
        .tracking-box { background: #ecfdf5; border: 1px solid #86efac; border-radius: 8px; 
                        padding: 16px; margin: 16px 0; text-align: center; }
        .tracking-number { font-size: 20px; font-weight: 700; color: #166534; letter-spacing: 1px; }
        .warning-box { background: #fef2f2; border: 1px solid #fca5a5; border-radius: 8px; 
                       padding: 16px; margin: 16px 0; }
        .warning-box p { color: #991b1b; margin: 0; }
        .footer { background: #f8fafc; padding: 24px 32px; border-top: 1px solid #e2e8f0; 
                  font-size: 13px; color: #64748b; text-align: center; }
        .footer a { color: #2563eb; text-decoration: none; }
    </style>
    """


def _header(title: str, subtitle: str = "") -> str:
    """Email header with CFC branding."""
    sub = f'<div class="subtitle">{subtitle}</div>' if subtitle else ""
    return f"""
    <div class="header">
        <h1>{title}</h1>
        {sub}
    </div>
    """


def _footer() -> str:
    """Email footer with CFC contact info."""
    return """
    <div class="footer">
        <p><strong>Cabinets For Contractors</strong></p>
        <p>Wholesale RTA Cabinets &bull; Serving 1,050+ Contractors</p>
        <p>(770) 990-4885 &bull; <a href="mailto:william@cabinetsforcontractors.net">william@cabinetsforcontractors.net</a></p>
        <p style="margin-top: 12px; font-size: 11px; color: #94a3b8;">
            Questions about this email? Just reply — a real person reads every one.
        </p>
    </div>
    """


def _order_summary_block(order: Dict) -> str:
    """Reusable order summary card."""
    order_id = order.get("order_id", "—")
    customer = order.get("customer_name", "—")
    company = order.get("company_name", "")
    total = order.get("order_total", 0)
    order_date = order.get("order_date", "")
    
    if isinstance(order_date, datetime):
        order_date = order_date.strftime("%B %d, %Y")
    elif order_date:
        try:
            order_date = datetime.fromisoformat(str(order_date).replace('Z', '+00:00')).strftime("%B %d, %Y")
        except (ValueError, TypeError):
            order_date = str(order_date)
    
    total_fmt = f"${float(total):,.2f}" if total else "—"
    company_line = f'<div class="detail-row"><span class="detail-label">Company</span><span class="detail-value">{company}</span></div>' if company else ""
    
    return f"""
    <div class="order-summary">
        <h3>Order Summary</h3>
        <div class="detail-row"><span class="detail-label">Order #</span><span class="detail-value">{order_id}</span></div>
        <div class="detail-row"><span class="detail-label">Customer</span><span class="detail-value">{customer}</span></div>
        {company_line}
        <div class="detail-row"><span class="detail-label">Order Date</span><span class="detail-value">{order_date}</span></div>
        <div class="detail-row" style="border-bottom: none;"><span class="detail-label">Total</span><span class="detail-value" style="font-size: 18px; color: #1a365d;">{total_fmt}</span></div>
    </div>
    """


def _wrap_email(header: str, body_content: str) -> str:
    """Wrap content in full email HTML shell."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {_base_style()}
</head>
<body>
    <div class="container">
        {header}
        <div class="body">
            {body_content}
        </div>
        {_footer()}
    </div>
</body>
</html>"""


# =============================================================================
# TEMPLATE RENDERERS
# =============================================================================

def _render_payment_link(order: Dict) -> str:
    """Template 1: Payment link with order summary."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    payment_link = order.get("payment_link", "#")
    order_id = order.get("order_id", "")
    
    body = f"""
    <p>Hi {first_name},</p>
    <p>Thank you for your order! Here's your invoice for Order #{order_id}.</p>
    {_order_summary_block(order)}
    <p>Click below to complete your payment securely through Square:</p>
    <p style="text-align: center;">
        <a href="{payment_link}" class="cta-button">Pay Now</a>
    </p>
    <p>If you have any questions about your order, just reply to this email or give us a call.</p>
    <p>Thanks,<br>William Prince<br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Invoice Ready", f"Order #{order_id}"), body)


def _render_payment_confirmation(order: Dict) -> str:
    """Template 2: Payment received confirmation."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = order.get("order_id", "")
    amount = order.get("payment_amount") or order.get("order_total", 0)
    amount_fmt = f"${float(amount):,.2f}" if amount else "—"
    
    body = f"""
    <p>Hi {first_name},</p>
    <p>We've received your payment of <strong>{amount_fmt}</strong> for Order #{order_id}. Thank you!</p>
    {_order_summary_block(order)}
    <p>Your order is now being processed. We'll send your cabinets to the warehouse and 
    notify you once they're ready to ship.</p>
    <p>Thanks for your business,<br>William Prince<br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Payment Received", f"Order #{order_id}"), body)


def _render_shipping_notification(order: Dict) -> str:
    """Template 3: Shipping notification with tracking."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = order.get("order_id", "")
    tracking = order.get("tracking", "")
    pro_number = order.get("pro_number", "")
    carrier = "R+L Carriers" if pro_number else "Shippo"
    
    display_tracking = pro_number or tracking or "Pending"
    
    # Build tracking URL if available
    tracking_link = ""
    if pro_number:
        tracking_link = f"https://www2.rlcarriers.com/freight/shipping/shipment-tracing?pro={pro_number}"
    
    tracking_html = f"""
    <div class="tracking-box">
        <p style="margin: 0 0 4px 0; color: #166534; font-size: 13px;">Carrier: {carrier}</p>
        <p class="tracking-number" style="margin: 0;">{display_tracking}</p>
    </div>
    """
    
    if tracking_link:
        tracking_html += f"""
        <p style="text-align: center;">
            <a href="{tracking_link}" class="cta-button" style="background: #16a34a;">Track Your Shipment</a>
        </p>
        """
    
    body = f"""
    <p>Hi {first_name},</p>
    <p>Great news! Your cabinets for Order #{order_id} are on their way.</p>
    {tracking_html}
    {_order_summary_block(order)}
    <p><strong>Delivery tip:</strong> LTL freight shipments require someone to be present to receive. 
    The carrier will call ahead to schedule delivery. Please inspect all boxes upon arrival and 
    note any damage on the delivery receipt before signing.</p>
    <p>Thanks,<br>William Prince<br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Your Order Has Shipped!", f"Order #{order_id}"), body)


def _render_delivery_confirmation(order: Dict) -> str:
    """Template 4: Delivery confirmation."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = order.get("order_id", "")
    
    body = f"""
    <p>Hi {first_name},</p>
    <p>Your cabinets for Order #{order_id} have been delivered! We hope everything looks great.</p>
    {_order_summary_block(order)}
    <p>If you notice any damage or issues with your order, please let us know within 48 hours 
    so we can get it resolved quickly.</p>
    <p>If everything looks good, we'd love to work with you again on your next project.</p>
    <p>Thanks for choosing Cabinets For Contractors,<br>William Prince</p>
    """
    return _wrap_email(_header("Order Delivered", f"Order #{order_id}"), body)


def _render_trusted_payment_reminder(order: Dict) -> str:
    """Template 5: Gentle reminder for trusted customers."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = order.get("order_id", "")
    payment_link = order.get("payment_link", "#")
    total = order.get("order_total", 0)
    total_fmt = f"${float(total):,.2f}" if total else "—"
    
    body = f"""
    <p>Hi {first_name},</p>
    <p>Just a friendly reminder — your cabinets for Order #{order_id} have already shipped, 
    and we still have an outstanding balance of <strong>{total_fmt}</strong>.</p>
    {_order_summary_block(order)}
    <p>You can pay at your convenience using the link below:</p>
    <p style="text-align: center;">
        <a href="{payment_link}" class="cta-button">Pay Now</a>
    </p>
    <p>As always, thanks for your business. We appreciate you!</p>
    <p>William Prince<br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Payment Reminder", f"Order #{order_id}"), body)


# =============================================================================
# LIFECYCLE TEMPLATE RENDERERS
# =============================================================================

def _render_payment_reminder_day6(order: Dict) -> str:
    """Template 6: Lifecycle — order hasn't been paid (day 6)."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = order.get("order_id", "")
    payment_link = order.get("payment_link", "#")
    
    body = f"""
    <p>Hi {first_name},</p>
    <p>We noticed your Order #{order_id} hasn't been paid yet. We want to make sure 
    nothing fell through the cracks.</p>
    {_order_summary_block(order)}
    <p>If you're ready to move forward, you can pay here:</p>
    <p style="text-align: center;">
        <a href="{payment_link}" class="cta-button">Pay Now</a>
    </p>
    <p>If you have questions or need to make changes, just reply to this email.</p>
    <p>Thanks,<br>William Prince<br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Payment Reminder", f"Order #{order_id}"), body)


def _render_inactive_notice_day29(order: Dict) -> str:
    """Template 7: Lifecycle — order marked inactive (day 29)."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = order.get("order_id", "")
    
    body = f"""
    <p>Hi {first_name},</p>
    <p>Your Order #{order_id} has been marked <strong>inactive</strong> due to no recent activity.</p>
    {_order_summary_block(order)}
    <div class="warning-box">
        <p><strong>What this means:</strong> Your order is still in our system but will be 
        automatically canceled if we don't hear from you within 15 days.</p>
    </div>
    <p>If you'd like to keep this order active, just reply to this email or give us a call. 
    We're happy to help with any questions.</p>
    <p>Thanks,<br>William Prince<br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Order Marked Inactive", f"Order #{order_id}"), body)


def _render_deletion_warning_day44(order: Dict) -> str:
    """Template 8: Lifecycle — order will be canceled tomorrow (day 44)."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = order.get("order_id", "")
    
    body = f"""
    <p>Hi {first_name},</p>
    <p><strong>This is a final notice.</strong></p>
    <p>Your Order #{order_id} will be <strong>automatically canceled tomorrow</strong> 
    due to extended inactivity.</p>
    {_order_summary_block(order)}
    <div class="warning-box">
        <p><strong>Action required:</strong> If you want to keep this order, 
        please reply to this email or call us today at (770) 990-4885.</p>
    </div>
    <p>If you no longer need this order, no action is needed — it will be 
    canceled automatically.</p>
    <p>Thanks,<br>William Prince<br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Order Will Be Canceled Tomorrow", f"Order #{order_id}"), body)


def _render_cancel_confirmation(order: Dict) -> str:
    """Template 9: Lifecycle — order has been canceled."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = order.get("order_id", "")
    reason = order.get("cancel_reason", "")
    
    reason_text = ""
    if reason == "customer_request":
        reason_text = "<p>This cancellation was made per your request.</p>"
    elif reason == "inactivity":
        reason_text = "<p>This order was canceled due to extended inactivity (45+ days with no response).</p>"
    else:
        reason_text = f"<p>Reason: {reason}</p>" if reason else ""
    
    body = f"""
    <p>Hi {first_name},</p>
    <p>Your Order #{order_id} has been <strong>canceled</strong>.</p>
    {reason_text}
    {_order_summary_block(order)}
    <p>If this was a mistake or you'd like to place a new order, just reply to this email 
    or give us a call. We're always here to help.</p>
    <p>Thanks,<br>William Prince<br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Order Canceled", f"Order #{order_id}"), body)


# =============================================================================
# MAIN RENDER FUNCTION
# =============================================================================

_RENDERERS = {
    "payment_link": _render_payment_link,
    "payment_confirmation": _render_payment_confirmation,
    "shipping_notification": _render_shipping_notification,
    "delivery_confirmation": _render_delivery_confirmation,
    "trusted_payment_reminder": _render_trusted_payment_reminder,
    "payment_reminder_day6": _render_payment_reminder_day6,
    "inactive_notice_day29": _render_inactive_notice_day29,
    "deletion_warning_day44": _render_deletion_warning_day44,
    "cancel_confirmation": _render_cancel_confirmation,
}


def render_template(template_id: str, order_data: Dict) -> Optional[str]:
    """
    Render an email template with order data.
    
    Args:
        template_id: One of the 9 template IDs
        order_data: Dict with order fields (order_id, customer_name, etc.)
    
    Returns:
        Rendered HTML string, or None if template not found
    """
    renderer = _RENDERERS.get(template_id)
    if not renderer:
        return None
    return renderer(order_data)


def render_template_preview(template_id: str) -> Optional[str]:
    """Render a template with sample data for preview purposes."""
    sample_order = {
        "order_id": "5307",
        "customer_name": "John Smith",
        "company_name": "Smith Remodeling LLC",
        "order_total": 4250.00,
        "order_date": "2026-02-15",
        "payment_link": "https://square.link/example",
        "payment_amount": 4250.00,
        "tracking": "PRO 123456789",
        "pro_number": "123456789",
        "cancel_reason": "customer_request",
    }
    return render_template(template_id, sample_order)
