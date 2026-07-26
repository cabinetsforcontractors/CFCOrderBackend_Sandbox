"""
email_templates.py
CFC Orders Email Template Engine — Phase 4: Customer Communications

Templates:
  1. payment_link           — Full QB-style invoice with line items, tariff, shipping, Pay Now button
  2. payment_confirmation   — Payment received confirmation
  3. shipping_notification  — Shipped with tracking/carrier/ETA
  4. delivery_confirmation  — Delivered confirmation
  5. trusted_payment_reminder — Gentle reminder for trusted customers
  6. payment_reminder_day6  — Lifecycle: order not paid (day 6)
  7. inactive_notice_day7   — Lifecycle: moved to inactive (day 7)
  8. cancel_warning_day14   — Lifecycle: will be canceled in 7 days (day 14)
  9. cancel_confirmation    — Lifecycle: order canceled (day 21 or manual)
 10. quote_email            — Quote with line items, no payment link -- B2BWave portal CTA
 11. abandoned_cart_nudge   — Nudge for abandoned B2BWave carts
"""

from typing import Dict, List, Optional
from datetime import datetime


TEMPLATE_REGISTRY = {
    "payment_link": {
        "name": "Payment Link",
        "subject": "Invoice INV-{order_id} - your cabinet order - Cabinets For Contractors",
        "description": "Full invoice with line items, tariff, shipping, Pay Now button",
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
    "inactive_notice_day7": {
        "name": "Inactive Notice (Day 7)",
        "subject": "Your Order #{order_id} Has Been Moved to Inactive",
        "description": "Lifecycle: order moved to inactive due to 7 days of no activity",
        "category": "lifecycle",
        "is_lifecycle": True,
    },
    "cancel_warning_day14": {
        "name": "Cancel Warning (Day 14)",
        "subject": "Your Order #{order_id} Will Be Canceled in 7 Days",
        "description": "Lifecycle: 14 days inactive, will be canceled in 7 more days",
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
    "quote_email": {
        "name": "Quote Email",
        "subject": "Your Cabinet Quote for Order #{order_id} -- Cabinets For Contractors",
        "description": "Quote with line items, no payment link -- B2BWave portal CTA",
        "category": "quote",
        "is_lifecycle": False,
    },
    "abandoned_cart_nudge": {
        "name": "Abandoned Cart Nudge",
        "subject": "You left something behind -- Order #{order_id}",
        "description": "Nudge for abandoned B2BWave carts",
        "category": "quote",
        "is_lifecycle": False,
    },
}


def get_template_list() -> List[Dict]:
    return [{"id": tid, **meta} for tid, meta in TEMPLATE_REGISTRY.items()]


def get_template_subject(template_id: str, order_data: Dict) -> str:
    meta = TEMPLATE_REGISTRY.get(template_id)
    if not meta:
        return f"Order #{order_data.get('order_id', '?')}"
    try:
        return meta["subject"].format(**order_data)
    except (KeyError, ValueError):
        return meta["subject"]


def is_lifecycle_template(template_id: str) -> bool:
    meta = TEMPLATE_REGISTRY.get(template_id)
    return meta.get("is_lifecycle", False) if meta else False


# =============================================================================
# SHARED HTML COMPONENTS
# =============================================================================

def _base_style() -> str:
    return """
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; margin: 0; padding: 0; background: #f0f4f8; }
        .container { max-width: 640px; margin: 0 auto; background: #ffffff; border-radius: 4px; overflow: hidden; }
        .header { background: #1a365d; color: #ffffff; padding: 24px 32px; }
        .header-top { display: flex; justify-content: space-between; align-items: flex-start; }
        .header h1 { margin: 0; font-size: 20px; font-weight: 700; }
        .header .invoice-label { font-size: 28px; font-weight: 800; color: #93c5fd; letter-spacing: 2px; }
        .header .subtitle { color: #93c5fd; font-size: 12px; margin-top: 4px; }
        .body { padding: 28px 32px; color: #333; line-height: 1.6; }
        .body p { margin: 0 0 14px 0; }
        .meta-row { display: flex; gap: 16px; margin-bottom: 20px; }
        .meta-box { flex: 1; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 14px; }
        .meta-box .meta-label { font-size: 10px; font-weight: 700; color: #718096; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
        .meta-box .meta-value { font-size: 13px; color: #1a202c; line-height: 1.5; }
        .meta-box .meta-value strong { display: block; }
        .invoice-table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; }
        .invoice-table thead tr { background: #1a365d; color: #fff; }
        .invoice-table thead th { padding: 9px 10px; text-align: left; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
        .invoice-table thead th.num { text-align: right; }
        .invoice-table tbody tr:nth-child(even) { background: #f7fafc; }
        .invoice-table tbody td { padding: 8px 10px; color: #1a202c; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
        .invoice-table tbody td.num { text-align: right; white-space: nowrap; }
        .invoice-table tbody td.sku { font-family: monospace; font-size: 11px; color: #4a5568; }
        .totals-table { width: 100%; border-collapse: collapse; margin-top: 8px; }
        .totals-table td { padding: 5px 10px; font-size: 13px; }
        .totals-table td.label { color: #718096; text-align: right; width: 60%; }
        .totals-table td.amount { text-align: right; color: #1a202c; white-space: nowrap; width: 40%; }
        .totals-table tr.grand td { font-size: 16px; font-weight: 700; color: #1a365d; border-top: 2px solid #1a365d; padding-top: 10px; }
        .cta-wrap { text-align: center; margin: 24px 0 16px; }
        .cta-button { display: inline-block; background: #2563eb; color: #ffffff !important;
                      text-decoration: none; padding: 14px 40px; border-radius: 6px;
                      font-weight: 700; font-size: 16px; letter-spacing: 0.5px; }
        .policy-box { background: #fffbeb; border: 1px solid #fcd34d; border-radius: 6px; padding: 14px 16px; margin: 20px 0 0; font-size: 11px; color: #92400e; line-height: 1.6; }
        .policy-box strong { display: block; margin-bottom: 6px; font-size: 12px; }
        .policy-box ul { margin: 4px 0 0 16px; padding: 0; }
        .policy-box ul li { margin-bottom: 3px; }
        .warning-box { background: #fef2f2; border: 1px solid #fca5a5; border-radius: 8px; padding: 16px; margin: 16px 0; }
        .warning-box p { color: #991b1b; margin: 0; }
        .tracking-box { background: #ecfdf5; border: 1px solid #86efac; border-radius: 8px; padding: 16px; margin: 16px 0; text-align: center; }
        .tracking-number { font-size: 20px; font-weight: 700; color: #166534; letter-spacing: 1px; }
        .footer { background: #1a365d; padding: 20px 32px; font-size: 12px; color: #93c5fd; text-align: center; }
        .footer a { color: #93c5fd; text-decoration: none; }
        .footer strong { color: #fff; }
        .divider { border: none; border-top: 1px solid #e2e8f0; margin: 16px 0; }
    </style>
    """


def _header(title: str, subtitle: str = "", show_invoice_label: bool = False) -> str:
    label = '<span class="invoice-label">INVOICE</span>' if show_invoice_label else f'<span style="color:#93c5fd;font-size:16px;">{subtitle}</span>'
    return f"""
    <div class="header">
        <div class="header-top">
            <div>
                <h1>Cabinets For Contractors</h1>
                <div class="subtitle">Wholesale RTA Cabinets &bull; (770) 990-4885</div>
            </div>
            {label}
        </div>
    </div>
    """


def _footer() -> str:
    return """
    <div class="footer">
        <p><strong>Cabinets For Contractors</strong></p>
        <p>(770) 990-4885 &bull; <a href="mailto:orders@cabinetsforcontractors.com">orders@cabinetsforcontractors.com</a></p>
        <p style="margin-top: 8px; font-size: 11px; color: #7eb3e8;">
            Questions? Just reply — a real person reads every email.
        </p>
    </div>
    """


def _wrap_email(header: str, body_content: str) -> str:
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


def _order_summary_block(order: Dict) -> str:
    order_id = order.get("order_id", "\u2014")
    customer = order.get("customer_name", "\u2014")
    company = order.get("company_name", "")
    total = order.get("order_total", 0)
    order_date = order.get("order_date", "")

    if isinstance(order_date, datetime):
        order_date = order_date.strftime("%B %d, %Y")

    total_fmt = f"${float(total):,.2f}" if total else "\u2014"
    company_line = f'<tr><td style="color:#718096;padding:5px 0;font-size:13px;">Company</td><td style="font-weight:600;font-size:13px;">{company}</td></tr>' if company else ""

    return f"""
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin:20px 0;">
        <div style="font-weight:700;color:#1a365d;font-size:15px;margin-bottom:12px;">Order Summary</div>
        <table style="width:100%;border-collapse:collapse;">
            <tr><td style="color:#718096;padding:5px 0;font-size:13px;">Order #</td><td style="font-weight:600;font-size:13px;">{order_id}</td></tr>
            <tr><td style="color:#718096;padding:5px 0;font-size:13px;">Customer</td><td style="font-weight:600;font-size:13px;">{customer}</td></tr>
            {company_line}
            <tr><td style="color:#718096;padding:5px 0;font-size:13px;">Date</td><td style="font-weight:600;font-size:13px;">{order_date or "\u2014"}</td></tr>
            <tr><td style="color:#718096;padding:5px 0;font-size:13px;">Total</td><td style="font-weight:700;font-size:16px;color:#1a365d;">{total_fmt}</td></tr>
        </table>
    </div>
    """


# =============================================================================
# TEMPLATE RENDERERS
# =============================================================================

def _render_payment_link(order: Dict) -> str:
    """
    Template 1 — THE v4-LOCKED INVOICE EMAIL (William's reference: the sent
    INV-5711 email, re-confirmed as THE template 2026-07-26). Structure:
    greeting -> "Your Order #N" header -> Billing/Shipping blocks ->
    Items|Qty|Item Cost|Item Total table -> Subtotal (in-stock) / Tariff /
    Shipping / Grand Total -> SHIPPING TO verify box -> centered CLICK HERE
    pay sentence -> signature. Expects shipping_result; fetches line items
    itself when not supplied.
    """
    customer = order.get("customer_name") or "Valued Customer"
    first_name = customer.split()[0] if customer.strip() else "there"
    company = order.get("company_name") or ""
    order_id = str(order.get("order_id", order.get("id", "")))
    payment_link = order.get("payment_link", "#")

    od = order.get("order_date")
    try:
        placed = od.strftime("%b %d, %Y") if hasattr(od, "strftime") else str(od or "")[:10]
    except Exception:
        placed = str(od or "")

    items = order.get("line_items") or []
    if not items:
        try:
            from db_helpers import get_order_line_items
            items = get_order_line_items(order_id) or []
        except Exception:
            items = []

    sr = order.get("shipping_result", {})
    total_items = float(sr.get("total_items", order.get("order_total", 0)) or 0)
    tariff_rate = sr.get("tariff_rate", 0.08)
    tariff_amount = float(sr.get("tariff_amount", round(total_items * tariff_rate, 2)))
    total_shipping = float(sr.get("total_shipping", 0) or 0)
    grand_total = float(sr.get("grand_total",
                        round(total_items + tariff_amount + total_shipping, 2)))
    ship_label = (order.get("shipping_label") or "Shipping").strip()

    street = order.get("street") or ""
    street2 = order.get("street2") or ""
    city = order.get("city") or ""
    state = order.get("state") or ""
    zip_code = order.get("zip_code") or ""
    phone = order.get("phone") or ""
    email = order.get("email") or ""
    csz = f"{city}, {state} {zip_code}".replace(" ,", ",").strip(", ")
    ship_line = ", ".join(x for x in [f"{street} {street2}".strip(), csz] if x)

    rows = ""
    for it in items:
        sku = it.get("sku") or ""
        name = it.get("product_name") or ""
        qty = int(float(it.get("quantity") or 0))
        price = float(it.get("price") or 0)
        line_total = float(it.get("line_total") or (qty * price))
        rows += (
            "<tr><td style='padding:8px 6px;border-bottom:1px solid #eee'>"
            f"<strong>{sku}</strong><br>"
            f"<span style='color:#888;font-size:12px'>{name}</span></td>"
            f"<td style='padding:8px 6px;border-bottom:1px solid #eee;text-align:center'>{qty}</td>"
            f"<td style='padding:8px 6px;border-bottom:1px solid #eee;text-align:right'>${price:,.2f}</td>"
            f"<td style='padding:8px 6px;border-bottom:1px solid #eee;text-align:right'>${line_total:,.2f}</td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan='4' style='padding:8px 6px;color:#888'>(line items pending sync)</td></tr>"

    return f"""<div style="font-family:'Open Sans',Helvetica,Arial,sans-serif;font-size:14px;padding:20px">
<div style="max-width:660px;margin:0px auto;padding:28px;border:1px solid rgb(229,229,229)">
<p style="color:rgb(51,51,51)">Hey {first_name},</p>
<p style="color:rgb(51,51,51)">Thank you for your order! Your invoice is below (a PDF copy is attached).</p>
<h1 style="color:rgb(51,51,51);font-weight:300;font-size:26px;border-bottom:1px solid rgb(221,221,221);padding-bottom:8px;margin-bottom:4px">Your Order #{order_id}</h1>
<div style="color:rgb(136,136,136);font-size:13px;margin-bottom:18px">Placed {placed}</div>
<table style="color:rgb(51,51,51);width:100%;margin-bottom:20px"><tbody><tr>
<td style="vertical-align:top;width:50%"><strong>Billing Info</strong><br>{company}<br>{customer}<br>{street}{('<br>' + street2) if street2 else ''}<br>{csz}<br>{phone}<br>{email}</td>
<td style="vertical-align:top;width:50%"><strong>Shipping Info</strong><br>{company}<br>{street}{('<br>' + street2) if street2 else ''}<br>{csz}</td>
</tr></tbody></table>
<table style="color:rgb(51,51,51);width:100%;border-collapse:collapse">
<thead><tr style="border-bottom:2px solid #ddd"><th style="text-align:left;padding:8px 6px">Items</th><th style="text-align:center;padding:8px 6px">Qty</th><th style="text-align:right;padding:8px 6px">Item Cost</th><th style="text-align:right;padding:8px 6px">Item Total</th></tr></thead>
<tbody>{rows}</tbody>
<tfoot>
<tr><td colspan="2"></td><td style="padding:6px;text-align:right">Subtotal (in-stock)</td><td style="padding:6px;text-align:right">${total_items:,.2f}</td></tr>
<tr><td colspan="2"></td><td style="padding:6px;text-align:right">Tariff Surcharge ({int(tariff_rate * 100)}%)</td><td style="padding:6px;text-align:right">${tariff_amount:,.2f}</td></tr>
<tr><td colspan="2"></td><td style="padding:6px;text-align:right">{ship_label}</td><td style="padding:6px;text-align:right">${total_shipping:,.2f}</td></tr>
<tr><td colspan="2"></td><td style="padding:6px;text-align:right;font-weight:700">Grand Total</td><td style="padding:6px;text-align:right;font-weight:700">${grand_total:,.2f}</td></tr>
</tfoot></table>
<div style="color:rgb(51,51,51);border:2px solid rgb(29,201,183);border-radius:6px;padding:14px;margin:18px 0px">
<div style="font-weight:700">&#128666; SHIPPING TO:</div>
<div style="font-weight:700;margin:6px 0">{company}, {ship_line}</div>
<div style="color:#c0392b;font-weight:700">Please verify this address BEFORE paying. If your ship-to address has changed, reply to this email first &mdash; shipping cost may change with a different address.</div>
</div>
<p style="text-align:center"><font color="#6fa8dc"><b><a href="{payment_link}">If everything looks in order (CLICK HERE) to make payment</a></b></font></p>
<p style="color:rgb(51,51,51)">Any questions, just reply or call.</p>
<p style="color:rgb(51,51,51);margin-top:18px">William Prince<br>Cabinets For Contractors<br>www.CabinetsForContractors.net<br>(770) 990-4885</p>
</div></div>"""



def _render_payment_confirmation(order: Dict) -> str:
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))
    amount = order.get("payment_amount") or order.get("order_total", 0)
    amount_fmt = f"${float(amount):,.2f}" if amount else "\u2014"

    body = f"""
    <p>Hi {first_name},</p>
    <p>We've received your payment of <strong>{amount_fmt}</strong> for Order #{order_id}. Thank you!</p>
    {_order_summary_block(order)}
    <p>Your order is now being processed. We'll notify you once your cabinets have shipped.</p>
    <p>Thanks for your business,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Payment Received", f"Order #{order_id}"), body)


def _render_shipping_notification(order: Dict) -> str:
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))
    tracking = order.get("tracking", "")
    pro_number = order.get("pro_number", "")
    carrier = "R+L Carriers" if pro_number else "Carrier"
    display_tracking = pro_number or tracking or "Pending"

    tracking_html = f"""
    <div class="tracking-box">
        <p style="margin: 0 0 4px 0; color: #166534; font-size: 13px;">Carrier: {carrier}</p>
        <p class="tracking-number" style="margin: 0;">{display_tracking}</p>
    </div>
    """
    if pro_number:
        tracking_link = f"https://www2.rlcarriers.com/freight/shipping/shipment-tracing?pro={pro_number}"
        tracking_html += f'<p style="text-align:center;"><a href="{tracking_link}" style="display:inline-block;background:#16a34a;color:#fff;padding:12px 28px;border-radius:6px;text-decoration:none;font-weight:600;">Track Your Shipment</a></p>'

    body = f"""
    <p>Hi {first_name},</p>
    <p>Great news! Your cabinets for Order #{order_id} are on their way.</p>
    {tracking_html}
    {_order_summary_block(order)}
    <p><strong>Delivery tip:</strong> LTL freight requires someone present to receive. The carrier will call ahead to schedule. Inspect all boxes and note any damage on the delivery receipt before signing.</p>
    <p>Thanks,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Your Order Has Shipped!", f"Order #{order_id}"), body)


def _render_delivery_confirmation(order: Dict) -> str:
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))

    body = f"""
    <p>Hi {first_name},</p>
    <p>Your cabinets for Order #{order_id} have been delivered!</p>
    {_order_summary_block(order)}
    <p>If you notice any damage or issues, please let us know within 48 hours.</p>
    <p>Thanks for choosing Cabinets For Contractors,<br><strong>William Prince</strong></p>
    """
    return _wrap_email(_header("Order Delivered", f"Order #{order_id}"), body)


def _render_trusted_payment_reminder(order: Dict) -> str:
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))
    payment_link = order.get("payment_link", "#")
    total = order.get("order_total", 0)
    total_fmt = f"${float(total):,.2f}" if total else "\u2014"

    body = f"""
    <p>Hi {first_name},</p>
    <p>Just a friendly reminder &mdash; your cabinets for Order #{order_id} have shipped and we have an outstanding balance of <strong>{total_fmt}</strong>.</p>
    {_order_summary_block(order)}
    <p style="text-align:center;"><a href="{payment_link}" class="cta-button">Pay Now</a></p>
    <p>Thanks for your business,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Payment Reminder", f"Order #{order_id}"), body)


def _render_payment_reminder_day6(order: Dict) -> str:
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))
    payment_link = order.get("payment_link", "#")

    body = f"""
    <p>Hi {first_name},</p>
    <p>We noticed your Order #{order_id} hasn't been paid yet. Want to make sure nothing fell through the cracks.</p>
    {_order_summary_block(order)}
    <p style="text-align:center;"><a href="{payment_link}" class="cta-button">Pay Now</a></p>
    <p>Questions? Just reply.<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Payment Reminder", f"Order #{order_id}"), body)


def _render_inactive_notice_day7(order: Dict) -> str:
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))

    body = f"""
    <p>Hi {first_name},</p>
    <p>Your Order #{order_id} has been moved to our <strong>inactive folder</strong> due to 7 days of no activity.</p>
    {_order_summary_block(order)}
    <div class="warning-box"><p><strong>Your order will be canceled in 14 days</strong> if we don't hear from you. Reply to this email or call (770) 990-4885 to keep it active.</p></div>
    <p>Thanks,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Order Moved to Inactive", f"Order #{order_id}"), body)


def _render_cancel_warning_day14(order: Dict) -> str:
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))

    body = f"""
    <p>Hi {first_name},</p>
    <p><strong>Action required:</strong> Your Order #{order_id} will be <strong>automatically canceled in 7 days</strong> unless we hear from you.</p>
    {_order_summary_block(order)}
    <div class="warning-box"><p>Reply to this email or call (770) 990-4885 within 7 days to keep this order active.</p></div>
    <p>Thanks,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Order Will Be Canceled in 7 Days", f"Order #{order_id}"), body)


def _render_cancel_confirmation(order: Dict) -> str:
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))
    reason = order.get("cancel_reason", "")

    reason_text = ""
    if reason == "customer_request":
        reason_text = "<p>This cancellation was made per your request.</p>"
    elif reason == "inactivity":
        reason_text = "<p>This order was canceled due to 21 days of inactivity.</p>"
    elif reason:
        reason_text = f"<p>Reason: {reason}</p>"

    body = f"""
    <p>Hi {first_name},</p>
    <p>Your Order #{order_id} has been <strong>canceled</strong>.</p>
    {reason_text}
    {_order_summary_block(order)}
    <p>If this was a mistake or you'd like to reorder, just reply or call (770) 990-4885.</p>
    <p>Thanks,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Order Canceled", f"Order #{order_id}"), body)


def _render_quote_email(order: Dict) -> str:
    """Quote email with line items, totals, no Pay button -- CTA to B2BWave portal or call."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))
    line_items = order.get("line_items", [])
    b2bwave_url = order.get("b2bwave_portal_url", "#")
    quote_url = order.get("payment_link", "#")

    sr = order.get("shipping_result", {})
    total_items = sr.get("total_items", order.get("order_total", 0))
    tariff_rate = sr.get("tariff_rate", 0.08)
    tariff_amount = sr.get("tariff_amount", round(float(total_items) * tariff_rate, 2))
    total_shipping = sr.get("total_shipping", 0)
    grand_total = sr.get("grand_total", round(float(total_items) + tariff_amount + total_shipping, 2))

    if line_items:
        rows = ""
        for item in line_items:
            sku = item.get("sku", "")
            name = item.get("name", "")
            qty = item.get("quantity", 1)
            price = float(item.get("price", 0))
            line_total = float(item.get("line_total", price * qty))
            rows += f"""
            <tr>
                <td class="sku">{sku}</td>
                <td>{name}</td>
                <td class="num">{qty}</td>
                <td class="num">${price:,.2f}</td>
                <td class="num">${line_total:,.2f}</td>
            </tr>"""

        items_html = f"""
        <table class="invoice-table">
            <thead>
                <tr>
                    <th style="width:110px;">SKU</th>
                    <th>Description</th>
                    <th class="num" style="width:40px;">Qty</th>
                    <th class="num" style="width:90px;">Unit Price</th>
                    <th class="num" style="width:90px;">Total</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>"""
    else:
        items_html = '<p style="color:#718096;font-size:13px;">No line items available.</p>'

    tariff_pct = int(tariff_rate * 100)

    body = f"""
    <p>Hi {first_name},</p>
    <p>Here is your cabinet quote for Order <strong>#{order_id}</strong>.</p>

    <div style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:14px 18px;margin:16px 0;text-align:center;">
        <p style="margin:0;color:#1E40AF;font-weight:600;font-size:14px;">This quote is valid for 30 days.</p>
    </div>

    {items_html}

    <table class="totals-table">
        <tr><td class="label">Subtotal</td><td class="amount">${float(total_items):,.2f}</td></tr>
        <tr><td class="label">Tariff ({tariff_pct}%)</td><td class="amount">${tariff_amount:,.2f}</td></tr>
        <tr><td class="label">Shipping</td><td class="amount">${total_shipping:,.2f}</td></tr>
        <tr class="grand"><td class="label">Quote Total</td><td class="amount">${grand_total:,.2f}</td></tr>
    </table>

    <p style="margin-top:20px;font-weight:600;color:#1a365d;">Ready to place your order? You have two options:</p>

    <div class="cta-wrap" style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;">
        <a href="{quote_url}" class="cta-button">View Quote Details &rarr;</a>
        <a href="tel:7709904885" style="display:inline-block;background:#f1f5f9;color:#334155;text-decoration:none;padding:14px 28px;border-radius:6px;font-weight:700;font-size:16px;border:2px solid #e2e8f0;">
            Call to Order: (770) 990-4885
        </a>
    </div>

    <p style="margin-top:16px;font-size:13px;color:#718096;">
        When you're ready, simply view this quote online or call us and we'll take care of it.
    </p>

    <p style="margin-top:20px;font-size:13px;">Questions? Reply to <a href="mailto:orders@cabinetsforcontractors.com">orders@cabinetsforcontractors.com</a> or call <strong>(770) 990-4885</strong>.</p>
    <p style="font-size:13px;">Thanks,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("Your Cabinet Quote", f"Order #{order_id}"), body)


def _render_abandoned_cart_nudge(order: Dict) -> str:
    """Nudge email for abandoned B2BWave carts."""
    customer = order.get("customer_name", "Valued Customer")
    first_name = customer.split()[0] if customer else "there"
    order_id = str(order.get("order_id", order.get("id", "")))
    line_items = order.get("line_items", [])
    b2bwave_url = order.get("b2bwave_portal_url", "#")

    total = order.get("order_total", 0)
    total_fmt = f"${float(total):,.2f}" if total else ""

    if line_items:
        rows = ""
        for item in line_items:
            sku = item.get("sku", "")
            name = item.get("name", "")
            qty = item.get("quantity", 1)
            price = float(item.get("price", 0))
            line_total = float(item.get("line_total", price * qty))
            rows += f"""
            <tr>
                <td class="sku">{sku}</td>
                <td>{name}</td>
                <td class="num">{qty}</td>
                <td class="num">${price:,.2f}</td>
                <td class="num">${line_total:,.2f}</td>
            </tr>"""

        items_html = f"""
        <table class="invoice-table">
            <thead>
                <tr>
                    <th style="width:110px;">SKU</th>
                    <th>Description</th>
                    <th class="num" style="width:40px;">Qty</th>
                    <th class="num" style="width:90px;">Unit Price</th>
                    <th class="num" style="width:90px;">Total</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>"""
    else:
        items_html = ""

    body = f"""
    <p>Hi {first_name},</p>
    <p>We noticed you started an order and didn't finish. Your items are still saved.</p>

    {items_html}
    {f'<p style="font-size:16px;font-weight:700;color:#1a365d;margin-top:16px;">Subtotal: {total_fmt}</p>' if total_fmt else ''}

    <div class="cta-wrap">
        <a href="{b2bwave_url}" class="cta-button">Complete Your Order &rarr;</a>
    </div>

    <p style="font-size:13px;color:#718096;">Shipping will be calculated once your order is submitted.</p>
    <p style="margin-top:16px;font-size:13px;">Questions? Call <strong>(770) 990-4885</strong> or reply to <a href="mailto:orders@cabinetsforcontractors.com">orders@cabinetsforcontractors.com</a>.</p>
    <p style="font-size:13px;">Thanks,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    """
    return _wrap_email(_header("You Left Something Behind", f"Order #{order_id}"), body)


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
    "inactive_notice_day7": _render_inactive_notice_day7,
    "cancel_warning_day14": _render_cancel_warning_day14,
    "cancel_confirmation": _render_cancel_confirmation,
    "quote_email": _render_quote_email,
    "abandoned_cart_nudge": _render_abandoned_cart_nudge,
}


def render_template(template_id: str, order_data: Dict) -> Optional[str]:
    renderer = _RENDERERS.get(template_id)
    if not renderer:
        return None
    return renderer(order_data)


def render_template_preview(template_id: str) -> Optional[str]:
    sample_order = {
        "order_id": "5307",
        "customer_name": "John Smith",
        "company_name": "Smith Remodeling LLC",
        "order_total": 4250.00,
        "order_date": "April 04, 2026",
        "payment_link": "https://square.link/example",
        "confirm_commercial_url": "https://cfcorderbackend-sandbox.onrender.com/checkout/5307/confirm-commercial?token=abc123",
        "payment_amount": 4250.00,
        "tracking": "PRO 123456789",
        "pro_number": "123456789",
        "cancel_reason": "customer_request",
        "line_items": [
            {"sku": "WSP-W3630", "name": "Wall Cabinet 36W x 30H", "quantity": 4, "price": 89.00, "line_total": 356.00},
            {"sku": "WSP-B30", "name": "Base Cabinet 30W", "quantity": 2, "price": 124.00, "line_total": 248.00},
        ],
        "shipping_address": {
            "address": "123 Main St", "address2": "Apt 4B", "city": "Atlanta", "state": "GA", "zip": "30301"
        },
        "shipping_result": {
            "total_items": 604.00, "tariff_rate": 0.08, "tariff_amount": 48.32,
            "total_shipping": 145.00, "grand_total": 797.32
        }
    }
    return render_template(template_id, sample_order)
