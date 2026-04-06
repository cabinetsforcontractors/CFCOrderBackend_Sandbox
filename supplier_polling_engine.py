"""
supplier_polling_engine.py
WS6 Phase 9 — Warehouse Polling Engine

Manages the full warehouse polling lifecycle:
  1. Initial poll: "When will this order be ready to ship?" (fires when admin sends to warehouse)
  2. Escalation polls: +24hr no response → Poll #2, +48hr → Poll #3 CRITICAL + CFC alert
  3. Day-before confirmation: night before pickup_date → "Still on for tomorrow? Yes/No"
     - Yes → warehouse enters time → BOL fires + R+L pickup request scheduled
     - No (Monday push) → store new date, reset poll cycle, no CFC alert
     - No (Tuesday+ push) → store new date, reset poll cycle, CFC alert "call them"

All supplier-facing pages are tokenized HTML served by supplier_routes.py.
This module handles the logic; supplier_routes.py handles the HTTP endpoints.

IMPORTANT: All public-facing functions return dicts, never raise.
           get_db() re-raises exceptions so every DB block is wrapped in try/except.
"""

import os
import json
import secrets
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Dict, List

from db_helpers import get_db
from psycopg2.extras import RealDictCursor
from config import SUPPLIER_INFO

CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "https://cfcorderbackend-sandbox.onrender.com").strip()
CFC_INTERNAL_EMAIL = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL", "cabinetsforcontractors@gmail.com").strip()
RL_QUOTE_API_URL = os.environ.get("RL_QUOTE_API_URL", "https://rl-quote-sandbox.onrender.com").strip()

POLL_2_THRESHOLD_HOURS = 24
POLL_3_THRESHOLD_HOURS = 48


# =============================================================================
# TOKEN MANAGEMENT
# =============================================================================

def generate_supplier_token(shipment_id: str) -> str:
    return secrets.token_urlsafe(32)


def get_shipment_by_token(token: str) -> Optional[dict]:
    """Fetch shipment + order data by supplier token. Returns plain dict."""
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT s.*,
                           o.customer_name, o.company_name,
                           o.street, o.city, o.state, o.zip_code, o.phone,
                           o.order_total, o.order_date,
                           o.payment_received, o.warehouse_confirmed,
                           o.bol_sent
                    FROM order_shipments s
                    JOIN orders o ON s.order_id = o.order_id
                    WHERE s.supplier_token = %s
                    """,
                    (token,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        print(f"[SUPPLIER_POLL] get_shipment_by_token error: {e}")
        return None


# =============================================================================
# INITIAL POLL — fires when admin clicks "Send to Warehouse"
# =============================================================================

def send_initial_poll(shipment_id: str) -> dict:
    """Send Poll #1 to the warehouse. Called from orders_routes.py."""
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT s.*, o.customer_name, o.company_name, o.order_total,
                           o.street, o.city, o.state, o.zip_code, o.order_date
                    FROM order_shipments s
                    JOIN orders o ON s.order_id = o.order_id
                    WHERE s.shipment_id = %s
                    """,
                    (shipment_id,),
                )
                row = cur.fetchone()
                if not row:
                    return {"success": False, "error": f"Shipment {shipment_id} not found"}
                shipment = dict(row)

                token = shipment.get("supplier_token")
                if not token:
                    token = generate_supplier_token(shipment_id)
                    cur.execute(
                        "UPDATE order_shipments SET supplier_token = %s WHERE shipment_id = %s",
                        (token, shipment_id)
                    )

                cur.execute(
                    """
                    UPDATE order_shipments
                    SET supplier_poll_1_sent_at = NOW(), supplier_poll_sent_count = 1, updated_at = NOW()
                    WHERE shipment_id = %s
                    """,
                    (shipment_id,)
                )
    except Exception as e:
        return {"success": False, "error": f"DB error in send_initial_poll: {str(e)}"}

    supplier_email = _get_supplier_email(shipment["warehouse"])
    if not supplier_email:
        return {"success": False, "error": f"No email for warehouse {shipment['warehouse']}"}

    form_url = f"{CHECKOUT_BASE_URL}/supplier/{token}/date-form"
    result = _send_supplier_poll_email(
        to_email=supplier_email,
        warehouse_name=shipment["warehouse"],
        order_id=shipment["order_id"],
        customer_name=shipment.get("company_name") or shipment.get("customer_name") or "",
        order_total=float(shipment.get("order_total") or 0),
        form_url=form_url,
        poll_number=1,
        is_critical=False,
    )

    _log_event(shipment["order_id"], "supplier_poll_1_sent", {
        "shipment_id": shipment_id,
        "warehouse": shipment["warehouse"],
        "supplier_email": supplier_email,
        "form_url": form_url,
        "email_success": result.get("success"),
    })

    return {"success": True, "poll": 1, "sent_to": supplier_email, "form_url": form_url}


# =============================================================================
# ESCALATION POLLS — called by nightly cron
# =============================================================================

def check_all_warehouse_polls() -> dict:
    """Check all active shipments for polling actions. Called nightly by cron."""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    summary = {"polls_escalated": 0, "day_before_sent": 0, "errors": []}

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT s.*, o.order_date, o.customer_name, o.company_name, o.order_total
                    FROM order_shipments s
                    JOIN orders o ON s.order_id = o.order_id
                    WHERE o.sent_to_warehouse = TRUE
                      AND o.bol_sent = FALSE
                      AND (s.pickup_date IS NULL)
                      AND s.supplier_poll_1_sent_at IS NOT NULL
                      AND o.is_complete = FALSE
                    """,
                )
                pending_date_shipments = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        summary["errors"].append({"phase": "escalation_query", "error": str(e)})
        pending_date_shipments = []

    for s in pending_date_shipments:
        try:
            poll_sent_at = s.get("supplier_poll_1_sent_at")
            if not poll_sent_at:
                continue
            if poll_sent_at.tzinfo is None:
                poll_sent_at = poll_sent_at.replace(tzinfo=timezone.utc)
            hours_elapsed = (now - poll_sent_at).total_seconds() / 3600
            poll_count = s.get("supplier_poll_sent_count") or 1

            if hours_elapsed >= POLL_3_THRESHOLD_HOURS and poll_count < 3:
                _send_escalation_poll(s, poll_number=3, is_critical=True)
                _send_cfc_no_response_alert(s, hours=int(hours_elapsed))
                summary["polls_escalated"] += 1
            elif hours_elapsed >= POLL_2_THRESHOLD_HOURS and poll_count < 2:
                _send_escalation_poll(s, poll_number=2, is_critical=False)
                summary["polls_escalated"] += 1
        except Exception as e:
            summary["errors"].append({"shipment_id": s.get("shipment_id"), "error": str(e)})

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT s.*, o.customer_name, o.company_name, o.order_total, o.zip_code,
                           o.street, o.city, o.state
                    FROM order_shipments s
                    JOIN orders o ON s.order_id = o.order_id
                    WHERE s.pickup_date = %s
                      AND s.day_before_poll_sent_at IS NULL
                      AND o.bol_sent = FALSE
                      AND o.is_complete = FALSE
                    """,
                    (tomorrow,),
                )
                day_before_shipments = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        summary["errors"].append({"phase": "day_before_query", "error": str(e)})
        day_before_shipments = []

    for s in day_before_shipments:
        try:
            _send_day_before_poll(s)
            summary["day_before_sent"] += 1
        except Exception as e:
            summary["errors"].append({"shipment_id": s.get("shipment_id"), "error": str(e)})

    return summary


def _send_escalation_poll(shipment: dict, poll_number: int, is_critical: bool):
    token = shipment.get("supplier_token")
    if not token:
        return
    supplier_email = _get_supplier_email(shipment["warehouse"])
    if not supplier_email:
        return
    form_url = f"{CHECKOUT_BASE_URL}/supplier/{token}/date-form"
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                if poll_number == 2:
                    cur.execute(
                        "UPDATE order_shipments SET supplier_poll_2_sent_at = NOW(), "
                        "supplier_poll_sent_count = 2 WHERE shipment_id = %s",
                        (shipment["shipment_id"],)
                    )
                else:
                    cur.execute(
                        "UPDATE order_shipments SET supplier_poll_3_sent_at = NOW(), "
                        "supplier_poll_sent_count = 3 WHERE shipment_id = %s",
                        (shipment["shipment_id"],)
                    )
    except Exception as e:
        print(f"[SUPPLIER_POLL] escalation poll DB error: {e}")
    _send_supplier_poll_email(
        to_email=supplier_email,
        warehouse_name=shipment["warehouse"],
        order_id=shipment["order_id"],
        customer_name=shipment.get("company_name") or shipment.get("customer_name") or "",
        order_total=float(shipment.get("order_total") or 0),
        form_url=form_url,
        poll_number=poll_number,
        is_critical=is_critical,
    )
    _log_event(shipment["order_id"], f"supplier_poll_{poll_number}_sent", {
        "shipment_id": shipment["shipment_id"],
        "is_critical": is_critical,
    })


def _send_day_before_poll(shipment: dict):
    token = shipment.get("supplier_token")
    if not token:
        return
    supplier_email = _get_supplier_email(shipment["warehouse"])
    if not supplier_email:
        return
    pickup_date = shipment.get("pickup_date")
    date_str = pickup_date.strftime("%A, %B %d") if hasattr(pickup_date, "strftime") else str(pickup_date)
    yes_url = f"{CHECKOUT_BASE_URL}/supplier/{token}/confirm-tomorrow"
    no_url = f"{CHECKOUT_BASE_URL}/supplier/{token}/push-date"
    subject = f"⚠️ Order #{shipment['order_id']} — Pickup Confirmation for {date_str}"
    body = _render_day_before_email(
        warehouse_name=shipment["warehouse"],
        order_id=shipment["order_id"],
        customer_name=shipment.get("company_name") or shipment.get("customer_name") or "",
        date_str=date_str,
        yes_url=yes_url,
        no_url=no_url,
    )
    _send_raw_email(to_email=supplier_email, subject=subject, html_body=body)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE order_shipments SET day_before_poll_sent_at = NOW() WHERE shipment_id = %s",
                    (shipment["shipment_id"],)
                )
    except Exception as e:
        print(f"[SUPPLIER_POLL] day_before poll DB error: {e}")
    _log_event(shipment["order_id"], "day_before_poll_sent", {
        "shipment_id": shipment["shipment_id"],
        "pickup_date": str(pickup_date),
        "supplier_email": supplier_email,
    })


# =============================================================================
# WAREHOUSE DATE SET (supplier submits expected date)
# =============================================================================

def warehouse_set_date(token: str, pickup_date_str: str) -> dict:
    """
    Warehouse submitted the date form. Stores pickup_date, notifies CFC.
    Always returns a dict — never raises.
    """
    try:
        shipment = get_shipment_by_token(token)
        if not shipment:
            return {"success": False, "error": "Invalid or expired link"}

        try:
            pickup_date = datetime.strptime(pickup_date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"success": False, "error": f"Invalid date format: {pickup_date_str}"}

        shipment_id = shipment.get("shipment_id")
        order_id = shipment.get("order_id")

        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE order_shipments SET pickup_date = %s, updated_at = NOW() WHERE shipment_id = %s",
                        (pickup_date, shipment_id)
                    )
        except Exception as db_err:
            return {"success": False, "error": f"Database error: {str(db_err)}"}

        _log_event(order_id, "warehouse_set_pickup_date", {
            "shipment_id": shipment_id,
            "warehouse": shipment.get("warehouse"),
            "pickup_date": str(pickup_date),
        })

        # Notify CFC — best effort, don't fail if email errors
        try:
            date_str = pickup_date.strftime("%A, %B %d, %Y")
            _send_cfc_date_confirmed_alert(shipment, date_str)
        except Exception as email_err:
            print(f"[SUPPLIER_POLL] CFC alert email failed: {email_err}")

        return {"success": True, "pickup_date": str(pickup_date)}

    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


# =============================================================================
# DAY-BEFORE RESPONSES
# =============================================================================

def warehouse_confirm_tomorrow(token: str) -> dict:
    """Warehouse clicked YES on day-before poll."""
    try:
        shipment = get_shipment_by_token(token)
        if not shipment:
            return {"success": False, "error": "Invalid or expired link"}
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE order_shipments SET day_before_confirmed = TRUE, updated_at = NOW() "
                        "WHERE shipment_id = %s",
                        (shipment["shipment_id"],)
                    )
        except Exception as db_err:
            return {"success": False, "error": f"Database error: {str(db_err)}"}
        _log_event(shipment["order_id"], "day_before_confirmed_yes", {
            "shipment_id": shipment["shipment_id"],
        })
        return {"success": True, "next": "time_entry"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def warehouse_push_date(token: str, new_date_str: str) -> dict:
    """Warehouse clicked NO on day-before poll and entered a new date."""
    try:
        shipment = get_shipment_by_token(token)
        if not shipment:
            return {"success": False, "error": "Invalid or expired link"}
        try:
            new_date = datetime.strptime(new_date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"success": False, "error": f"Invalid date format: {new_date_str}"}
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE order_shipments
                        SET pickup_date = %s, day_before_poll_sent_at = NULL,
                            day_before_confirmed = FALSE, updated_at = NOW()
                        WHERE shipment_id = %s
                        """,
                        (new_date, shipment["shipment_id"])
                    )
        except Exception as db_err:
            return {"success": False, "error": f"Database error: {str(db_err)}"}
        date_str = new_date.strftime("%A, %B %d, %Y")
        is_monday = (new_date.weekday() == 0)
        _log_event(shipment["order_id"], "warehouse_pushed_date", {
            "shipment_id": shipment["shipment_id"],
            "new_pickup_date": str(new_date),
            "weekday": new_date.strftime("%A"),
            "cfc_alerted": not is_monday,
        })
        if not is_monday:
            try:
                _send_cfc_push_alert(shipment, date_str, weekday_name=new_date.strftime("%A"))
            except Exception:
                pass
            return {"success": True, "pushed_to": str(new_date), "cfc_alerted": True}
        return {"success": True, "pushed_to": str(new_date), "cfc_alerted": False}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def warehouse_set_pickup_time(token: str, pickup_time_str: str) -> dict:
    """Warehouse entered pickup time → fires BOL."""
    try:
        shipment = get_shipment_by_token(token)
        if not shipment:
            return {"success": False, "error": "Invalid or expired link"}
        if not shipment.get("day_before_confirmed"):
            return {"success": False, "error": "Day-before confirmation not received yet"}
        if shipment.get("bol_sent"):
            return {"success": False, "error": "BOL already generated for this shipment"}
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE order_shipments SET pickup_time = %s, updated_at = NOW() WHERE shipment_id = %s",
                        (pickup_time_str, shipment["shipment_id"])
                    )
                    cur.execute(
                        "UPDATE orders SET warehouse_confirmed = TRUE, warehouse_confirmed_at = NOW(), "
                        "updated_at = NOW() WHERE order_id = %s",
                        (shipment["order_id"],)
                    )
        except Exception as db_err:
            return {"success": False, "error": f"Database error: {str(db_err)}"}

        _log_event(shipment["order_id"], "warehouse_confirmed_pickup_time", {
            "shipment_id": shipment["shipment_id"],
            "pickup_time": pickup_time_str,
        })

        pickup_date = shipment.get("pickup_date")
        pickup_date_str = pickup_date.strftime("%m/%d/%Y") if hasattr(pickup_date, "strftime") else None
        bol_result = _fire_bol(shipment, pickup_date_str)

        if bol_result.get("success"):
            pro_number = bol_result.get("pro_number")
            bol_pdf_url = bol_result.get("bol_pdf_url", "")
            supplier_email = _get_supplier_email(shipment["warehouse"])
            if supplier_email:
                _send_bol_to_warehouse(
                    to_email=supplier_email,
                    warehouse_name=shipment["warehouse"],
                    order_id=shipment["order_id"],
                    pro_number=pro_number,
                    pickup_date=pickup_date_str,
                    pickup_time=pickup_time_str,
                    bol_pdf_url=bol_pdf_url,
                )
            _send_cfc_bol_fired_alert(shipment, pro_number, pickup_date_str, pickup_time_str)
            return {
                "success": True,
                "pro_number": pro_number,
                "bol_pdf_url": bol_pdf_url,
                "message": f"BOL created — PRO {pro_number}. Pickup scheduled {pickup_date_str} at {pickup_time_str}.",
            }
        return {"success": False, "error": f"BOL creation failed: {bol_result.get('error')}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


# =============================================================================
# BOL FIRE
# =============================================================================

def _fire_bol(shipment: dict, pickup_date_str: Optional[str]) -> dict:
    try:
        from bol_routes import BOL_SHIPPER_NAMES, WAREHOUSE_ADDRESSES
        from bol_api import create_bol
        import asyncio

        warehouse_name = shipment["warehouse"]
        wh_info = WAREHOUSE_ADDRESSES.get(warehouse_name)
        if not wh_info:
            return {"success": False, "error": f"Unknown warehouse: {warehouse_name}"}

        shipper_name = BOL_SHIPPER_NAMES.get(warehouse_name, "Cabinets For Contractors")
        consignee_name = shipment.get("company_name") or shipment.get("customer_name") or "Customer"
        dest_zip = (shipment.get("zip_code") or "").split("-")[0][:5]
        weight = int(float(shipment.get("weight") or 200))
        is_residential = bool(shipment.get("is_residential", True))

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(create_bol(
            shipper_name=shipper_name,
            shipper_address=wh_info["address"],
            shipper_city=wh_info["city"],
            shipper_state=wh_info["state"],
            shipper_zip=wh_info["zip"],
            shipper_phone=wh_info["phone"],
            consignee_name=consignee_name,
            consignee_address=shipment.get("street") or "",
            consignee_city=shipment.get("city") or "",
            consignee_state=shipment.get("state") or "",
            consignee_zip=dest_zip,
            consignee_phone=shipment.get("phone") or "",
            weight_lbs=weight,
            is_residential=is_residential,
            order_id=shipment["order_id"],
            pickup_date=pickup_date_str,
            special_instructions=f"CFC Order #{shipment['order_id']}",
        ))
        loop.close()

        if result.get("success") and result.get("pro_number"):
            pro_number = result["pro_number"]
            bol_pdf_url = result.get("bol_pdf_url", "")
            try:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE order_shipments
                            SET pro_number = %s, bol_url = %s, bol_sent = TRUE,
                                bol_sent_at = NOW(), status = 'ready_ship', updated_at = NOW()
                            WHERE shipment_id = %s
                            """,
                            (pro_number, bol_pdf_url, shipment["shipment_id"])
                        )
                        cur.execute(
                            """
                            UPDATE orders
                            SET bol_sent = TRUE, bol_sent_at = NOW(),
                                tracking = %s, pro_number = %s, updated_at = NOW()
                            WHERE order_id = %s
                            """,
                            (pro_number, pro_number, shipment["order_id"])
                        )
                        cur.execute(
                            """
                            INSERT INTO order_events (order_id, event_type, event_data, source)
                            VALUES (%s, 'bol_created', %s, 'supplier_polling')
                            """,
                            (shipment["order_id"], json.dumps({
                                "pro_number": pro_number,
                                "pickup_date": pickup_date_str,
                                "triggered_by": "supplier_time_entry",
                            }))
                        )
            except Exception as db_err:
                print(f"[SUPPLIER_POLL] BOL DB write error: {db_err}")
            return {"success": True, "pro_number": pro_number, "bol_pdf_url": bol_pdf_url}
        return {"success": False, "error": result.get("error", "BOL API returned no PRO number")}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# EMAIL HELPERS
# =============================================================================

def _get_supplier_email(warehouse_name: str) -> Optional[str]:
    info = SUPPLIER_INFO.get(warehouse_name)
    if info:
        return info.get("email")
    for key, val in SUPPLIER_INFO.items():
        if key.lower() in warehouse_name.lower() or warehouse_name.lower() in key.lower():
            return val.get("email")
    return None


def _send_raw_email(to_email: str, subject: str, html_body: str) -> bool:
    try:
        import base64
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from gmail_sync import get_gmail_access_token

        token = get_gmail_access_token()
        if not token:
            print(f"[SUPPLIER_POLL] No Gmail token for email to {to_email}")
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = "william@cabinetsforcontractors.net"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText("Please view this email in an HTML email client.", "plain"))
        msg.attach(MIMEText(html_body, "html"))

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
        print(f"[SUPPLIER_POLL] Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        print(f"[SUPPLIER_POLL] Email failed to {to_email}: {e}")
        return False


def _send_supplier_poll_email(
    to_email: str, warehouse_name: str, order_id: str,
    customer_name: str, order_total: float,
    form_url: str, poll_number: int, is_critical: bool
) -> dict:
    if is_critical:
        subject = f"🚨 URGENT — Order #{order_id} Ship Date Required — No Response Received"
        urgency_line = "<p style='color:#DC2626;font-weight:700;font-size:16px;'>⚠️ We have not received a ship date for this order. Please respond immediately or call (770) 990-4885.</p>"
    elif poll_number == 2:
        subject = f"📦 Reminder — Order #{order_id} — When Will This Ship?"
        urgency_line = "<p style='color:#D97706;font-weight:600;'>We have not yet received a ship date for this order. Please enter one at your earliest convenience.</p>"
    else:
        subject = f"📦 Order #{order_id} from Cabinets For Contractors — Ship Date Needed"
        urgency_line = "<p>Please enter the date this order will be ready for pickup by R+L Carriers.</p>"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Ship Date Request</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:560px;margin:0 auto;background:white;border-radius:8px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
    <h2 style="color:#1a365d;margin-top:0;">Cabinets For Contractors — Order #{order_id}</h2>
    {urgency_line}
    <table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;">
        <tr><td style="padding:6px 0;color:#666;width:140px;">Customer:</td><td style="font-weight:600;">{customer_name}</td></tr>
        <tr><td style="padding:6px 0;color:#666;">Order Total:</td><td style="font-weight:600;">${order_total:,.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#666;">Warehouse:</td><td>{warehouse_name}</td></tr>
    </table>
    <p style="font-size:15px;font-weight:600;color:#1a365d;">When will this order be ready for pickup?</p>
    <a href="{form_url}" style="display:inline-block;background:#1a365d;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;font-size:16px;margin:8px 0;">
        Enter Ship Date →
    </a>
    <p style="font-size:12px;color:#999;margin-top:24px;">Questions? Call (770) 990-4885 or reply to this email.</p>
</div>
</body>
</html>"""
    success = _send_raw_email(to_email=to_email, subject=subject, html_body=html)
    return {"success": success}


def _render_day_before_email(
    warehouse_name: str, order_id: str, customer_name: str,
    date_str: str, yes_url: str, no_url: str
) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Pickup Confirmation</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:560px;margin:0 auto;background:white;border-radius:8px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
    <h2 style="color:#1a365d;margin-top:0;">Order #{order_id} — Pickup Tomorrow?</h2>
    <p>Hi {warehouse_name} team,</p>
    <p>Order <strong>#{order_id}</strong> for <strong>{customer_name}</strong> is scheduled for R+L pickup <strong>tomorrow, {date_str}</strong>.</p>
    <p style="font-size:16px;font-weight:600;color:#1a365d;">Is this order still on track for tomorrow?</p>
    <div style="display:flex;gap:12px;margin:20px 0;flex-wrap:wrap;">
        <a href="{yes_url}" style="display:inline-block;background:#059669;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;font-size:16px;">
            ✅ Yes — Enter Pickup Time →
        </a>
        <a href="{no_url}" style="display:inline-block;background:#DC2626;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;font-size:16px;">
            ❌ No — Enter New Date →
        </a>
    </div>
    <p style="font-size:12px;color:#999;">Questions? Call (770) 990-4885.</p>
</div>
</body>
</html>"""


def _send_cfc_date_confirmed_alert(shipment: dict, date_str: str):
    subject = f"✅ Ship Date Confirmed — Order #{shipment['order_id']} — {shipment['warehouse']}"
    html = f"""<p><strong>Order #{shipment['order_id']}</strong> from <strong>{shipment['warehouse']}</strong>
    has confirmed a ship date of <strong>{date_str}</strong>.</p>
    <p>The day-before confirmation poll will fire automatically the night before.</p>"""
    _send_raw_email(CFC_INTERNAL_EMAIL, subject, html)


def _send_cfc_no_response_alert(shipment: dict, hours: int):
    subject = f"🚨 CALL NEEDED — No Ship Date — Order #{shipment['order_id']} — {shipment['warehouse']}"
    html = f"""<p style='color:#DC2626;font-weight:700;font-size:16px;'>No ship date received after {hours} hours.</p>
    <p><strong>Warehouse:</strong> {shipment['warehouse']}<br>
    <strong>Order:</strong> #{shipment['order_id']}<br>
    <strong>Action Required:</strong> Call the warehouse now.</p>"""
    _send_raw_email(CFC_INTERNAL_EMAIL, subject, html)


def _send_cfc_push_alert(shipment: dict, new_date_str: str, weekday_name: str):
    subject = f"⚠️ CALL NEEDED — {shipment['warehouse']} Pushed Order #{shipment['order_id']} to {weekday_name}"
    html = f"""<p style='color:#D97706;font-weight:700;'>The warehouse has pushed the pickup date.</p>
    <p><strong>Warehouse:</strong> {shipment['warehouse']}<br>
    <strong>Order:</strong> #{shipment['order_id']}<br>
    <strong>New Date:</strong> {new_date_str} ({weekday_name})<br>
    <strong>Action Required:</strong> Call them — {weekday_name} is past the acceptable window.</p>"""
    _send_raw_email(CFC_INTERNAL_EMAIL, subject, html)


def _send_bol_to_warehouse(
    to_email: str, warehouse_name: str, order_id: str,
    pro_number: str, pickup_date: str, pickup_time: str, bol_pdf_url: str
):
    subject = f"📄 BOL Created — Order #{order_id} — PRO {pro_number}"
    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:560px;margin:0 auto;background:white;border-radius:8px;padding:32px;">
    <h2 style="color:#1a365d;margin-top:0;">Bill of Lading Created — Order #{order_id}</h2>
    <p>Hi {warehouse_name} team,</p>
    <p>Your pickup confirmation has been received and the Bill of Lading has been generated.</p>
    <table style="width:100%;border-collapse:collapse;font-size:15px;margin:16px 0;">
        <tr><td style="padding:8px 0;color:#666;width:140px;">PRO Number:</td><td style="font-weight:700;font-size:18px;font-family:monospace;color:#059669;">{pro_number}</td></tr>
        <tr><td style="padding:8px 0;color:#666;">Pickup Date:</td><td style="font-weight:600;">{pickup_date}</td></tr>
        <tr><td style="padding:8px 0;color:#666;">Pickup Time:</td><td style="font-weight:600;">{pickup_time}</td></tr>
    </table>
    <p>R+L Carriers will arrive for pickup as scheduled. Please have the shipment ready.</p>
    {f'<a href="{bol_pdf_url}" style="display:inline-block;background:#1a365d;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:600;">Track Shipment →</a>' if bol_pdf_url else ''}
    <p style="font-size:12px;color:#999;margin-top:24px;">Questions? Call (770) 990-4885.</p>
</div>
</body>
</html>"""
    _send_raw_email(to_email=to_email, subject=subject, html_body=html)


def _send_cfc_bol_fired_alert(shipment: dict, pro_number: str, pickup_date: str, pickup_time: str):
    subject = f"✅ BOL Fired — Order #{shipment['order_id']} — PRO {pro_number}"
    html = f"""<p><strong>BOL has been generated automatically via warehouse confirmation.</strong></p>
    <p><strong>Order:</strong> #{shipment['order_id']}<br>
    <strong>Warehouse:</strong> {shipment['warehouse']}<br>
    <strong>PRO Number:</strong> <span style="font-family:monospace;font-size:16px;color:#059669;">{pro_number}</span><br>
    <strong>Pickup Date:</strong> {pickup_date}<br>
    <strong>Pickup Time:</strong> {pickup_time}</p>
    <p>BOL emailed to warehouse. No action needed.</p>"""
    _send_raw_email(CFC_INTERNAL_EMAIL, subject, html)


def _log_event(order_id: str, event_type: str, data: dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO order_events (order_id, event_type, event_data, source) "
                    "VALUES (%s, %s, %s, 'supplier_polling')",
                    (order_id, event_type, json.dumps(data))
                )
    except Exception as e:
        print(f"[SUPPLIER_POLL] Event log failed: {e}")
