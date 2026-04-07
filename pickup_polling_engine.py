"""
pickup_polling_engine.py
WS6 — Warehouse Pickup Order Workflow

Separate engine for warehouse pickup orders (shipping_option_id == 2).
These orders skip R+L entirely. The customer comes to the warehouse to pick up.

Flow:
  send_pickup_ready_poll(shipment_id)
      — Admin clicks "Send to Warehouse"
      — Fires when shipment.pickup_type == 'warehouse_pickup'
      — Sends supplier: "When will Order #XXXX be ready for customer pickup?"
      — Form: GET /supplier/{token}/pickup-ready-form

  supplier_set_pickup_ready(token, ready_date_str, ready_time_str)
      — Supplier submits ready date + time
      — Stores pickup_ready_date, pickup_ready_time on order_shipments
      — Fires customer "Your order is ready for pickup!" email with warehouse address

  check_pickup_confirmations()
      — CRON: runs daily after ready date has passed
      — Finds pickup orders where ready date passed but pickup not confirmed
      — Sends supplier: "Has the customer picked up Order #XXXX?"
      — Form: GET /supplier/{token}/pickup-confirm

  supplier_confirm_pickup_yes(token)
      — Supplier says Yes (customer collected)
      — Marks order complete

  supplier_confirm_pickup_no(token)
      — Supplier says No (not yet collected)
      — Sends CFC escalation alert
"""

import os
import json
import secrets
from datetime import datetime, timezone, date as date_type
from typing import Optional

from db_helpers import get_db
from psycopg2.extras import RealDictCursor
from config import SUPPLIER_INFO

CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "https://cfcorderbackend-sandbox.onrender.com").strip()
CFC_INTERNAL_EMAIL = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL", "cabinetsforcontractors@gmail.com").strip()

WAREHOUSE_ADDRESSES = {
    "Cabinetry Distribution":  {"address": "561 Keuka Rd",              "city": "Interlachen",    "state": "FL", "zip": "32148", "phone": "(615) 410-6775"},
    "DL Cabinetry":            {"address": "7825 Parramore Rd",         "city": "Jacksonville",   "state": "FL", "zip": "32256", "phone": "904-886-5000"},
    "ROC Cabinetry":           {"address": "6015 Unity Dr",             "city": "Norcross",       "state": "GA", "zip": "30071", "phone": "770-263-9800"},
    "GHI Cabinets":            {"address": "1402 10th Ave E",           "city": "Palmetto",       "state": "FL", "zip": "34221", "phone": "941-981-9994"},
    "Go Bravura":              {"address": "6910 Fulton St",            "city": "Houston",        "state": "TX", "zip": "77066", "phone": "832-326-7003"},
    "Love-Milestone":          {"address": "10963 Florida Crown Dr STE 100", "city": "Orlando",   "state": "FL", "zip": "32824", "phone": "407-601-7090"},
    "Cabinet & Stone":         {"address": "1760 Stebbins Dr",          "city": "Houston",        "state": "TX", "zip": "77043", "phone": "713-468-8062"},
    "DuraStone":               {"address": "4506 Archie St",            "city": "Houston",        "state": "TX", "zip": "77037", "phone": "281-445-4700"},
    "L&C Cabinetry":           {"address": "2157 Vista Circle",         "city": "Virginia Beach", "state": "VA", "zip": "23454", "phone": "757-425-5544"},
    "Dealer Cabinetry":        {"address": "200 Industrial Blvd",       "city": "Bremen",         "state": "GA", "zip": "30110", "phone": "770-537-4422"},
}


# =============================================================================
# TOKEN / SHIPMENT HELPERS
# =============================================================================

def get_pickup_shipment_by_token(token: str) -> Optional[dict]:
    """
    Fetch shipment by supplier_token. Does NOT require orders table JOIN —
    safe to call even if the order hasn't been synced to orders table yet.
    """
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM order_shipments WHERE supplier_token = %s",
                    (token,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                result = dict(row)
                # Try to enrich with orders data — non-fatal if order not yet synced
                try:
                    cur.execute(
                        "SELECT customer_name, company_name, email, order_total, order_date, is_complete "
                        "FROM orders WHERE order_id = %s",
                        (result["order_id"],),
                    )
                    order_row = cur.fetchone()
                    if order_row:
                        result.update(dict(order_row))
                except Exception:
                    pass
                return result
    except Exception as e:
        print(f"[PICKUP] get_pickup_shipment_by_token error: {e}")
        return None


def _get_supplier_email(warehouse_name: str) -> Optional[str]:
    """
    Look up supplier email by warehouse name OR code.
    SUPPLIER_INFO keys are codes (LI, DL, ROC...) but values have 'name' matching warehouse_name.
    Falls back to fuzzy match.
    """
    # Direct code lookup
    info = SUPPLIER_INFO.get(warehouse_name)
    if info:
        return info.get("email")
    # Match by name field
    for key, val in SUPPLIER_INFO.items():
        if val.get("name", "").lower() == warehouse_name.lower():
            return val.get("email")
    # Fuzzy match — key substring or name substring
    warehouse_lower = warehouse_name.lower()
    for key, val in SUPPLIER_INFO.items():
        if key.lower() in warehouse_lower or warehouse_lower in key.lower():
            return val.get("email")
        if val.get("name", "").lower() in warehouse_lower or warehouse_lower in val.get("name", "").lower():
            return val.get("email")
    return None


def _log_event(order_id: str, event_type: str, data: dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO order_events (order_id, event_type, event_data, source) "
                    "VALUES (%s, %s, %s, 'pickup_polling')",
                    (order_id, event_type, json.dumps(data))
                )
    except Exception as e:
        print(f"[PICKUP] Event log failed: {e}")


def _send_raw_email(to_email: str, subject: str, html_body: str) -> bool:
    try:
        import base64, urllib.request
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from gmail_sync import get_gmail_access_token

        token = get_gmail_access_token()
        if not token:
            print(f"[PICKUP] No Gmail token for {to_email}")
            return False

        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("Please view in an HTML-capable email client.", "plain"))
        msg.attach(MIMEText(html_body, "html"))
        msg["From"] = "william@cabinetsforcontractors.net"
        msg["To"] = to_email
        msg["Subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        payload = json.dumps({"raw": raw}).encode("utf-8")
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=payload, method="POST"
        )
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print(f"[PICKUP] Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        print(f"[PICKUP] Email failed to {to_email}: {e}")
        return False


# =============================================================================
# STEP 1: ADMIN SENDS TO WAREHOUSE → SUPPLIER POLL "WHEN READY?"
# =============================================================================

def send_pickup_ready_poll(shipment_id: str) -> dict:
    """
    Fires when admin clicks 'Send to Warehouse' for a pickup order.
    Asks supplier: 'When will Order #XXXX be ready for customer pickup?'

    Queries order_shipments directly without requiring orders table JOIN —
    safe even if the order hasn't been synced yet.
    """
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Query shipment only — no orders JOIN (order may not be synced yet)
                cur.execute(
                    "SELECT * FROM order_shipments WHERE shipment_id = %s",
                    (shipment_id,),
                )
                row = cur.fetchone()
                if not row:
                    return {"success": False, "error": f"Shipment '{shipment_id}' not found"}
                shipment = dict(row)

                token = shipment.get("supplier_token")
                if not token:
                    token = secrets.token_urlsafe(32)
                    cur.execute(
                        "UPDATE order_shipments SET supplier_token = %s WHERE shipment_id = %s",
                        (token, shipment_id)
                    )

                cur.execute(
                    """UPDATE order_shipments
                       SET supplier_poll_1_sent_at = NOW(),
                           supplier_poll_sent_count = COALESCE(supplier_poll_sent_count, 0) + 1,
                           updated_at = NOW()
                       WHERE shipment_id = %s""",
                    (shipment_id,)
                )
    except Exception as e:
        return {"success": False, "error": f"DB error: {str(e)}"}

    supplier_email = _get_supplier_email(shipment["warehouse"])
    if not supplier_email:
        return {"success": False, "error": f"No supplier email found for warehouse '{shipment['warehouse']}'"}

    form_url = f"{CHECKOUT_BASE_URL}/supplier/{token}/pickup-ready-form"
    order_id  = shipment["order_id"]

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:32px;">
    <h2 style="color:#1a365d;margin-top:0;">Cabinets For Contractors — Order #{order_id}</h2>
    <p>This order has been paid and is ready to be prepared for <strong>customer pickup</strong>.</p>
    <p>Please click below to enter the date and time this order will be ready for the customer to collect.</p>
    <a href="{form_url}" style="display:inline-block;background:#059669;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;font-size:16px;margin:16px 0;">
        Enter Pickup-Ready Date &amp; Time →
    </a>
    <p style="font-size:12px;color:#999;margin-top:24px;">
        Questions? Email <a href="mailto:orders@cabinetsforcontractors.net" style="color:#1a365d;">orders@cabinetsforcontractors.net</a>
    </p>
</div></body></html>"""

    success = _send_raw_email(
        to_email=supplier_email,
        subject=f"📦 Order #{order_id} — When Will This Be Ready for Customer Pickup?",
        html_body=html,
    )

    _log_event(order_id, "pickup_ready_poll_sent", {
        "shipment_id": shipment_id,
        "warehouse": shipment["warehouse"],
        "supplier_email": supplier_email,
        "form_url": form_url,
        "email_success": success,
    })

    return {"success": True, "sent_to": supplier_email, "form_url": form_url, "order_id": order_id}


# =============================================================================
# STEP 2: SUPPLIER SUBMITS READY DATE + TIME
# =============================================================================

def supplier_set_pickup_ready(token: str, ready_date_str: str, ready_time_str: str) -> dict:
    """
    Supplier submits when order will be ready for customer pickup.
    Stores ready date/time, notifies customer.
    """
    shipment = get_pickup_shipment_by_token(token)
    if not shipment:
        return {"success": False, "error": "Invalid or expired link"}

    try:
        ready_date = datetime.strptime(ready_date_str, "%Y-%m-%d").date()
    except ValueError:
        return {"success": False, "error": f"Invalid date: {ready_date_str}"}

    order_id    = shipment["order_id"]
    shipment_id = shipment["shipment_id"]

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """UPDATE order_shipments
                           SET pickup_ready_date = %s,
                               pickup_ready_time = %s,
                               customer_notified_ready_at = NOW(),
                               updated_at = NOW()
                           WHERE shipment_id = %s""",
                        (ready_date, ready_time_str, shipment_id)
                    )
                except Exception as col_e:
                    # pickup_ready_date col may not exist yet — run /add-ws6-pickup-fields migration
                    conn.rollback()
                    print(f"[PICKUP] pickup_ready_date col missing — run migration: {col_e}")
    except Exception as e:
        return {"success": False, "error": f"DB error: {str(e)}"}

    # Notify customer
    customer_email = shipment.get("email") or ""
    customer_name  = shipment.get("customer_name") or ""
    warehouse_name = shipment.get("warehouse", "the warehouse")
    wh_info = WAREHOUSE_ADDRESSES.get(warehouse_name, {})

    if customer_email:
        _send_customer_order_ready_email(
            to_email=customer_email,
            customer_name=customer_name,
            order_id=order_id,
            ready_date_str=ready_date_str,
            ready_time_str=ready_time_str,
            warehouse_name=warehouse_name,
            wh_info=wh_info,
        )

    try:
        date_display = ready_date.strftime("%A, %B %d, %Y")
    except Exception:
        date_display = ready_date_str

    _send_raw_email(
        to_email=CFC_INTERNAL_EMAIL,
        subject=f"✅ Order #{order_id} Ready for Pickup — {date_display}",
        html_body=f"""<p><strong>Order #{order_id}</strong> is ready for customer pickup at <strong>{warehouse_name}</strong>.</p>
<p><strong>Ready:</strong> {date_display} at {ready_time_str}</p>
<p>Customer notified by email. Pickup confirmation poll will fire after the ready date.</p>""",
    )

    _log_event(order_id, "pickup_ready_set", {
        "shipment_id": shipment_id,
        "warehouse": warehouse_name,
        "ready_date": ready_date_str,
        "ready_time": ready_time_str,
        "customer_notified": bool(customer_email),
    })

    return {"success": True, "ready_date": ready_date_str, "ready_time": ready_time_str}


def _send_customer_order_ready_email(
    to_email: str, customer_name: str, order_id: str,
    ready_date_str: str, ready_time_str: str,
    warehouse_name: str, wh_info: dict,
):
    first_name = customer_name.split()[0] if customer_name else "there"
    try:
        date_display = datetime.strptime(ready_date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")
    except Exception:
        date_display = ready_date_str

    addr_line = f"{wh_info.get('address', '')}, {wh_info.get('city', '')}, {wh_info.get('state', '')} {wh_info.get('zip', '')}".strip(", ")
    phone_line = wh_info.get('phone', '')

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:560px;margin:0 auto;background:white;border-radius:8px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
    <div style="background:#1a365d;margin:-32px -32px 24px;padding:24px 32px;border-radius:8px 8px 0 0;">
        <h2 style="color:white;margin:0;font-size:18px;">Cabinets For Contractors</h2>
        <p style="color:#93c5fd;margin:4px 0 0;font-size:12px;">Wholesale RTA Cabinets &bull; (770) 990-4885</p>
    </div>
    <p style="color:#4a5568;">Hi {first_name},</p>
    <p style="color:#4a5568;">Great news! Your cabinets for Order <strong>#{order_id}</strong> are ready for pickup.</p>
    <div style="background:#ecfdf5;border:1px solid #86efac;border-radius:8px;padding:20px;margin:20px 0;">
        <div style="font-size:11px;font-weight:700;color:#166534;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">Ready For Pickup</div>
        <div style="font-size:20px;font-weight:700;color:#166534;margin-bottom:4px;">{date_display}</div>
        <div style="font-size:15px;color:#166534;">Available from {ready_time_str}</div>
    </div>
    <div style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:16px;margin:16px 0;">
        <div style="font-size:11px;font-weight:700;color:#1E40AF;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">Pickup Location</div>
        <div style="color:#1E40AF;font-size:15px;font-weight:600;">{warehouse_name}</div>
        {f'<div style="color:#1E40AF;font-size:14px;margin-top:4px;">{addr_line}</div>' if addr_line.strip(", ") else ''}
        {f'<div style="color:#1E40AF;font-size:14px;margin-top:2px;">&#128222; {phone_line}</div>' if phone_line else ''}
    </div>
    <p style="color:#4a5568;font-size:14px;"><strong>Please bring:</strong> Your order number (<strong>#{order_id}</strong>) and a photo ID.</p>
    <p style="color:#4a5568;">Questions? Reply to this email or call <strong>(770) 990-4885</strong>.</p>
    <p style="color:#4a5568;">Thanks,<br><strong>William Prince</strong><br>Cabinets For Contractors</p>
    <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;">
    <p style="font-size:12px;color:#999;text-align:center;">Cabinets For Contractors &bull; (770) 990-4885 &bull; orders@cabinetsforcontractors.net</p>
</div>
</body></html>"""

    _send_raw_email(
        to_email=to_email,
        subject=f"Your Order #{order_id} Is Ready for Pickup — {date_display}",
        html_body=html,
    )


# =============================================================================
# STEP 3: CRON — AFTER READY DATE, ASK "HAS CUSTOMER PICKED UP?"
# =============================================================================

def check_pickup_confirmations() -> dict:
    """
    CRON: Run daily.
    After pickup_ready_date has passed, poll supplier: "Has the customer picked up?"
    Skips shipments where pickup_confirm_poll_sent_at already set.
    Safe to run multiple times.
    """
    summary = {"checked": 0, "polls_sent": 0, "errors": []}

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.shipment_id, s.order_id, s.warehouse, s.supplier_token,
                           s.pickup_ready_date, s.pickup_ready_time,
                           s.pickup_confirm_poll_sent_at, s.customer_pickup_confirmed
                    FROM order_shipments s
                    WHERE s.pickup_type = 'warehouse_pickup'
                      AND s.pickup_ready_date IS NOT NULL
                      AND s.pickup_ready_date <= CURRENT_DATE
                      AND s.pickup_confirm_poll_sent_at IS NULL
                      AND (s.customer_pickup_confirmed = FALSE OR s.customer_pickup_confirmed IS NULL)
                    ORDER BY s.pickup_ready_date ASC
                """)
                shipments = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        summary["errors"].append({"phase": "query", "error": str(e)})
        return summary

    for shipment in shipments:
        summary["checked"] += 1
        try:
            result = _send_pickup_confirm_poll(shipment)
            if result.get("success"):
                summary["polls_sent"] += 1
            else:
                summary["errors"].append({"shipment_id": shipment["shipment_id"], "error": result.get("error")})
        except Exception as e:
            summary["errors"].append({"shipment_id": shipment.get("shipment_id"), "error": str(e)})

    return summary


def _send_pickup_confirm_poll(shipment: dict) -> dict:
    """Send 'Has customer picked up?' poll to supplier."""
    token = shipment.get("supplier_token")
    if not token:
        return {"success": False, "error": "No supplier token"}

    supplier_email = _get_supplier_email(shipment["warehouse"])
    if not supplier_email:
        return {"success": False, "error": f"No email for warehouse {shipment['warehouse']}"}

    order_id = shipment["order_id"]
    yes_url  = f"{CHECKOUT_BASE_URL}/supplier/{token}/pickup-confirm?response=yes"
    no_url   = f"{CHECKOUT_BASE_URL}/supplier/{token}/pickup-confirm?response=no"

    try:
        ready_display = shipment["pickup_ready_date"].strftime("%A, %B %d") if hasattr(shipment.get("pickup_ready_date"), "strftime") else str(shipment.get("pickup_ready_date", ""))
    except Exception:
        ready_display = str(shipment.get("pickup_ready_date", ""))

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:32px;">
    <h2 style="color:#1a365d;margin-top:0;">Order #{order_id} — Pickup Confirmation</h2>
    <p>Order #{order_id} was scheduled for customer pickup on <strong>{ready_display}</strong>.</p>
    <p style="font-weight:600;color:#1a365d;margin:16px 0;">Has the customer picked up this order?</p>
    <div style="display:flex;gap:12px;margin:20px 0;flex-wrap:wrap;">
        <a href="{yes_url}" style="display:inline-block;background:#059669;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;">
            ✅ Yes — Customer Picked Up
        </a>
        <a href="{no_url}" style="display:inline-block;background:#DC2626;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;">
            ❌ No — Not Yet Collected
        </a>
    </div>
    <p style="font-size:12px;color:#999;">Questions? Email <a href="mailto:orders@cabinetsforcontractors.net" style="color:#1a365d;">orders@cabinetsforcontractors.net</a></p>
</div></body></html>"""

    success = _send_raw_email(
        to_email=supplier_email,
        subject=f"⚠️ Order #{order_id} — Has Customer Picked Up?",
        html_body=html,
    )

    if success:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE order_shipments SET pickup_confirm_poll_sent_at = NOW(), updated_at = NOW() WHERE shipment_id = %s",
                        (shipment["shipment_id"],)
                    )
        except Exception as e:
            print(f"[PICKUP] Failed to mark pickup_confirm_poll_sent_at: {e}")

    _log_event(order_id, "pickup_confirm_poll_sent", {
        "shipment_id": shipment["shipment_id"],
        "warehouse": shipment["warehouse"],
        "supplier_email": supplier_email,
        "email_success": success,
    })

    return {"success": success}


# =============================================================================
# STEP 4: SUPPLIER RESPONDS YES / NO
# =============================================================================

def supplier_confirm_pickup_yes(token: str) -> dict:
    """Customer has picked up. Mark order complete."""
    shipment = get_pickup_shipment_by_token(token)
    if not shipment:
        return {"success": False, "error": "Invalid or expired link"}

    order_id    = shipment["order_id"]
    shipment_id = shipment["shipment_id"]

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "UPDATE order_shipments SET customer_pickup_confirmed = TRUE, updated_at = NOW() WHERE shipment_id = %s",
                        (shipment_id,)
                    )
                except Exception:
                    conn.rollback()
                cur.execute(
                    "UPDATE orders SET is_complete = TRUE, updated_at = NOW() WHERE order_id = %s",
                    (order_id,)
                )
    except Exception as e:
        return {"success": False, "error": str(e)}

    _send_raw_email(
        to_email=CFC_INTERNAL_EMAIL,
        subject=f"✅ Order #{order_id} — Customer Picked Up",
        html_body=f"<p>Order <strong>#{order_id}</strong> from <strong>{shipment.get('warehouse', '')}</strong> has been picked up by the customer. Order marked complete.</p>",
    )

    _log_event(order_id, "customer_pickup_confirmed_yes", {
        "shipment_id": shipment_id,
        "warehouse": shipment.get("warehouse", ""),
    })

    return {"success": True, "order_id": order_id, "status": "complete"}


def supplier_confirm_pickup_no(token: str) -> dict:
    """Customer has NOT picked up. Escalate to CFC."""
    shipment = get_pickup_shipment_by_token(token)
    if not shipment:
        return {"success": False, "error": "Invalid or expired link"}

    order_id    = shipment["order_id"]
    shipment_id = shipment["shipment_id"]
    warehouse   = shipment.get("warehouse", "unknown warehouse")

    _send_raw_email(
        to_email=CFC_INTERNAL_EMAIL,
        subject=f"🚨 Order #{order_id} — Customer Has NOT Picked Up — CALL NEEDED",
        html_body=f"""<p style="color:#DC2626;font-weight:700;">Customer has not collected Order #{order_id} from {warehouse}.</p>
<p><strong>Action required:</strong> Call the customer to reschedule pickup.</p>
<p>Admin: <a href="https://cfcordersfrontend-sandbox.vercel.app">cfcordersfrontend-sandbox.vercel.app</a></p>""",
    )

    _log_event(order_id, "customer_pickup_confirmed_no", {
        "shipment_id": shipment_id,
        "warehouse": warehouse,
    })

    return {"success": True, "order_id": order_id, "status": "not_collected_escalated"}
