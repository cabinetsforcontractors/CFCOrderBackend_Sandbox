"""
supplier_polling_engine.py
WS6 Phase 9 — Warehouse Polling Engine

Flow:
  - send_initial_poll()       — fires when admin clicks "Sent to Warehouse"
  - warehouse_set_date()      — stores pickup date
  - process_bol_and_pickup()  — BOL + Pickup Request fired; returns data for background email task
  - _delayed_bol_email()      — background task: tries PDF at 2/5/7 min, then reportlab fallback
  - warehouse_set_pickup_time() — day-before flow
  - check_all_warehouse_polls() — nightly cron

Pickup Request: POST /PickupRequest/FromBOL — only needs PRO + date + ready + close time.
R+L pulls shipper info from the BOL automatically.
Time format: "hh:mm tt" e.g. "09:00 AM" — matches form output directly, no conversion needed.
Poll email: minimal — order number and form link only, no customer data.
"""

import os
import json
import time
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

WAREHOUSE_CODE_TO_FULL = {
    'LI':    'Cabinetry Distribution',
    'DL':    'DL Cabinetry',
    'ROC':   'ROC Cabinetry',
    'GHI':   'GHI Cabinets',
    'GB':    'Go Bravura',
    'LOVE':  'Love-Milestone',
    'CS':    'Cabinet & Stone',
    'DS':    'DuraStone',
    'LC':    'L&C Cabinetry',
    'Linda': 'Dealer Cabinetry',
}


# =============================================================================
# TOKEN MANAGEMENT
# =============================================================================

def generate_supplier_token(shipment_id: str) -> str:
    return secrets.token_urlsafe(32)


def get_shipment_by_token(token: str) -> Optional[dict]:
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
# INITIAL POLL
# =============================================================================

def send_initial_poll(shipment_id: str) -> dict:
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
        return {"success": False, "error": f"DB error: {str(e)}"}

    supplier_email = _get_supplier_email(shipment["warehouse"])
    if not supplier_email:
        return {"success": False, "error": f"No email for warehouse {shipment['warehouse']}"}

    form_url = f"{CHECKOUT_BASE_URL}/supplier/{token}/date-form"
    result = _send_supplier_poll_email(
        to_email=supplier_email,
        order_id=shipment["order_id"],
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
# DATE SET
# =============================================================================

def warehouse_set_date(token: str, pickup_date_str: str) -> dict:
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

        return {"success": True, "pickup_date": str(pickup_date)}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


# =============================================================================
# PROCESS BOL + PICKUP REQUEST
# =============================================================================

def process_bol_and_pickup(token: str, pickup_time_str: str, close_time_str: str) -> dict:
    """
    1. Set warehouse_confirmed + store pickup_time/close_time
    2. Fire BOL via rl-quote /bol/create → get PRO number
    3. Fire Pickup Request via rl-quote /pickup/create (FromBOL)
    4. Return data dict — caller schedules _delayed_bol_email as background task
    Returns pickup_error in dict so route can surface R+L error message.
    """
    try:
        shipment = get_shipment_by_token(token)
        if not shipment:
            return {"success": False, "error": "Invalid or expired link"}

        shipment_id = shipment["shipment_id"]
        order_id = shipment["order_id"]

        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE order_shipments
                        SET day_before_confirmed = TRUE,
                            pickup_time = %s,
                            close_time = %s,
                            updated_at = NOW()
                        WHERE shipment_id = %s
                        """,
                        (pickup_time_str, close_time_str, shipment_id)
                    )
                    cur.execute(
                        "UPDATE orders SET warehouse_confirmed = TRUE, warehouse_confirmed_at = NOW(), updated_at = NOW() WHERE order_id = %s",
                        (order_id,)
                    )
        except Exception:
            # close_time column may not exist yet — try without it
            try:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE order_shipments SET day_before_confirmed = TRUE, pickup_time = %s, updated_at = NOW() WHERE shipment_id = %s",
                            (pickup_time_str, shipment_id)
                        )
                        cur.execute(
                            "UPDATE orders SET warehouse_confirmed = TRUE, warehouse_confirmed_at = NOW(), updated_at = NOW() WHERE order_id = %s",
                            (order_id,)
                        )
            except Exception as db_err2:
                return {"success": False, "error": f"Database error: {str(db_err2)}"}

        _log_event(order_id, "warehouse_confirmed_with_time", {
            "shipment_id": shipment_id,
            "pickup_time": pickup_time_str,
            "close_time": close_time_str,
        })

        shipment = get_shipment_by_token(token)
        if not shipment:
            return {"success": False, "error": "Could not re-fetch shipment"}

        pickup_date = shipment.get("pickup_date")
        pickup_date_fmt = pickup_date.strftime("%m/%d/%Y") if hasattr(pickup_date, "strftime") else None

        # Fire BOL
        bol_result = _fire_bol(shipment, pickup_date_fmt)
        if not bol_result.get("success"):
            return {"success": False, "error": f"BOL failed: {bol_result.get('error')}"}

        pro_number = bol_result["pro_number"]
        bol_pdf_url = bol_result.get("bol_pdf_url", "")

        # Fire Pickup Request — log error but always continue (non-blocking)
        pickup_result = _fire_pickup_request(
            pro_number=pro_number,
            pickup_date=pickup_date_fmt or "",
            ready_time=pickup_time_str,
            close_time=close_time_str,
            order_id=order_id,
        )
        pickup_error = None
        if not pickup_result.get("success"):
            pickup_error = pickup_result.get("error", "unknown")
            print(f"[SUPPLIER_POLL] Pickup request FAILED for PRO {pro_number}: {pickup_error}")

        try:
            _send_cfc_bol_fired_alert(
                shipment, pro_number, pickup_date_fmt or "", pickup_time_str,
                close_time=close_time_str,
                pickup_confirmation=pickup_result.get("confirmation_number"),
                pickup_error=pickup_error,
            )
        except Exception:
            pass

        return {
            "success": True,
            "pro_number": pro_number,
            "bol_pdf_url": bol_pdf_url,
            "pickup_date": pickup_date_fmt,
            "pickup_time": pickup_time_str,
            "close_time": close_time_str,
            "pickup_confirmation": pickup_result.get("confirmation_number"),
            "pickup_error": pickup_error,       # surfaced on success page for debug
            "supplier_email": _get_supplier_email(shipment["warehouse"]),
            "warehouse_name": shipment["warehouse"],
            "order_id": order_id,
            "shipment": dict(shipment),
        }

    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


# =============================================================================
# DELAYED BOL EMAIL — background task
# t+2min, t+5min, t+7min retry then reportlab fallback
# =============================================================================

def _delayed_bol_email(
    to_email: str, warehouse_name: str, order_id: str,
    pro_number: str, pickup_date: str, pickup_time: str,
    bol_pdf_url: str, shipment: dict,
):
    retry_delays = [120, 180, 120]  # 2min, then +3min, then +2min = at 2/5/7 min
    pdf_bytes = None
    pdf_source = None

    for i, delay in enumerate(retry_delays):
        print(f"[SUPPLIER_POLL] BOL email attempt {i+1}/3 for PRO {pro_number} — sleeping {delay}s")
        time.sleep(delay)
        pdf_bytes = _fetch_bol_pdf_bytes(pro_number)
        if pdf_bytes:
            pdf_source = f"rl_doc_retrieval_attempt_{i+1}"
            break

    if not pdf_bytes:
        print(f"[SUPPLIER_POLL] All PDF fetch attempts failed for PRO {pro_number} — using fallback")
        pdf_bytes = _generate_fallback_bol_pdf(
            pro_number=pro_number, order_id=order_id,
            shipper_name=warehouse_name,
            shipper_address=_get_shipper_address_str(shipment),
            consignee_name=shipment.get("company_name") or shipment.get("customer_name") or "Customer",
            consignee_address=_get_consignee_address_str(shipment),
            pickup_date=pickup_date, pickup_time=pickup_time,
            weight=int(float(shipment.get("weight") or 200)),
        )
        pdf_source = "fallback_reportlab"

    html = _bol_email_html(warehouse_name, order_id, pro_number, pickup_date, pickup_time, bol_pdf_url)
    _send_raw_email(
        to_email=to_email,
        subject=f"📄 BOL — Order #{order_id} — PRO {pro_number}",
        html_body=html,
        pdf_bytes=pdf_bytes,
        pdf_filename=f"BOL-{pro_number}.pdf",
    )
    _log_event(order_id, "bol_email_sent", {
        "pro_number": pro_number, "to": to_email,
        "pdf_source": pdf_source,
        "pdf_bytes": len(pdf_bytes) if pdf_bytes else 0,
    })


# =============================================================================
# DAY-BEFORE RESPONSES
# =============================================================================

def warehouse_confirm_tomorrow(token: str) -> dict:
    try:
        shipment = get_shipment_by_token(token)
        if not shipment:
            return {"success": False, "error": "Invalid or expired link"}
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE order_shipments SET day_before_confirmed = TRUE, updated_at = NOW() WHERE shipment_id = %s",
                        (shipment["shipment_id"],)
                    )
        except Exception as db_err:
            return {"success": False, "error": f"Database error: {str(db_err)}"}
        _log_event(shipment["order_id"], "day_before_confirmed_yes", {"shipment_id": shipment["shipment_id"]})
        return {"success": True, "next": "time_entry"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def warehouse_push_date(token: str, new_date_str: str) -> dict:
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


def warehouse_set_pickup_time(token: str, pickup_time_str: str, close_time_str: str = "5:00 PM") -> dict:
    """Day-before flow: used by set-time route via process_bol_and_pickup."""
    return process_bol_and_pickup(token, pickup_time_str, close_time_str)


# =============================================================================
# ESCALATION POLLS
# =============================================================================

def check_all_warehouse_polls() -> dict:
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
                    WHERE o.sent_to_warehouse = TRUE AND o.bol_sent = FALSE
                      AND s.pickup_date IS NULL AND s.supplier_poll_1_sent_at IS NOT NULL
                      AND o.is_complete = FALSE
                    """,
                )
                pending = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        summary["errors"].append({"phase": "escalation_query", "error": str(e)})
        pending = []

    for s in pending:
        try:
            poll_sent_at = s.get("supplier_poll_1_sent_at")
            if not poll_sent_at:
                continue
            if poll_sent_at.tzinfo is None:
                poll_sent_at = poll_sent_at.replace(tzinfo=timezone.utc)
            hours = (now - poll_sent_at).total_seconds() / 3600
            count = s.get("supplier_poll_sent_count") or 1
            if hours >= POLL_3_THRESHOLD_HOURS and count < 3:
                _send_escalation_poll(s, poll_number=3, is_critical=True)
                _send_cfc_no_response_alert(s, hours=int(hours))
                summary["polls_escalated"] += 1
            elif hours >= POLL_2_THRESHOLD_HOURS and count < 2:
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
                    WHERE s.pickup_date = %s AND s.day_before_poll_sent_at IS NULL
                      AND s.bol_sent = FALSE AND o.is_complete = FALSE
                    """,
                    (tomorrow,),
                )
                day_before = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        summary["errors"].append({"phase": "day_before_query", "error": str(e)})
        day_before = []

    for s in day_before:
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
                col = "supplier_poll_2_sent_at" if poll_number == 2 else "supplier_poll_3_sent_at"
                cnt = 2 if poll_number == 2 else 3
                cur.execute(
                    f"UPDATE order_shipments SET {col} = NOW(), supplier_poll_sent_count = %s WHERE shipment_id = %s",
                    (cnt, shipment["shipment_id"])
                )
    except Exception as e:
        print(f"[SUPPLIER_POLL] escalation DB error: {e}")
    _send_supplier_poll_email(
        to_email=supplier_email, order_id=shipment["order_id"],
        form_url=form_url, poll_number=poll_number, is_critical=is_critical,
    )
    _log_event(shipment["order_id"], f"supplier_poll_{poll_number}_sent", {
        "shipment_id": shipment["shipment_id"], "is_critical": is_critical,
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
        order_id=shipment["order_id"],
        date_str=date_str, yes_url=yes_url, no_url=no_url,
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
        print(f"[SUPPLIER_POLL] day_before DB error: {e}")
    _log_event(shipment["order_id"], "day_before_poll_sent", {
        "shipment_id": shipment["shipment_id"], "pickup_date": str(pickup_date),
    })


# =============================================================================
# BOL FIRE
# =============================================================================

def _fire_bol(shipment: dict, pickup_date_str: Optional[str]) -> dict:
    try:
        from bol_routes import BOL_SHIPPER_NAMES, WAREHOUSE_ADDRESSES

        warehouse_name = WAREHOUSE_CODE_TO_FULL.get(shipment["warehouse"], shipment["warehouse"])
        wh_info = WAREHOUSE_ADDRESSES.get(warehouse_name)
        if not wh_info:
            return {"success": False, "error": f"Unknown warehouse: {warehouse_name}"}

        shipper_name = BOL_SHIPPER_NAMES.get(warehouse_name, "Cabinets For Contractors")
        consignee_name = shipment.get("company_name") or shipment.get("customer_name") or "Customer"
        dest_zip = (shipment.get("zip_code") or "").split("-")[0][:5]
        weight = int(float(shipment.get("weight") or 200))
        is_residential = bool(shipment.get("is_residential", True))

        payload = {
            "shipper_name": shipper_name,
            "shipper_address": wh_info["address"],
            "shipper_city": wh_info["city"],
            "shipper_state": wh_info["state"],
            "shipper_zip": wh_info["zip"],
            "shipper_phone": wh_info["phone"],
            "consignee_name": consignee_name,
            "consignee_address": shipment.get("street") or "",
            "consignee_city": shipment.get("city") or "",
            "consignee_state": shipment.get("state") or "",
            "consignee_zip": dest_zip,
            "consignee_phone": shipment.get("phone") or "",
            "weight_lbs": weight,
            "is_residential": is_residential,
            "order_id": shipment["order_id"],
            "pieces": 1,
            "description": "RTA Cabinetry",
            "pickup_date": pickup_date_str,
            "special_instructions": f"CFC Order #{shipment['order_id']}",
        }

        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"{RL_QUOTE_API_URL}/bol/create", data=data, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.HTTPError as http_err:
            return {"success": False, "error": f"rl-quote HTTP {http_err.code}: {http_err.read().decode()[:200]}"}

        if not result.get("success") or not result.get("pro_number"):
            return {"success": False, "error": result.get("error", "No PRO number returned")}

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
                        "INSERT INTO order_events (order_id, event_type, event_data, source) "
                        "VALUES (%s, 'bol_created', %s, 'supplier_polling')",
                        (shipment["order_id"], json.dumps({
                            "pro_number": pro_number, "pickup_date": pickup_date_str,
                        }))
                    )
        except Exception as db_err:
            print(f"[SUPPLIER_POLL] BOL DB write error (BOL was created): {db_err}")

        return {"success": True, "pro_number": pro_number, "bol_pdf_url": bol_pdf_url}

    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# PICKUP REQUEST FIRE — POST /PickupRequest/FromBOL
# =============================================================================

def _fire_pickup_request(
    pro_number: str,
    pickup_date: str,
    ready_time: str,
    close_time: str,
    order_id: str = "",
    shipment: dict = None,
) -> dict:
    try:
        payload = {
            "pro_number": pro_number,
            "pickup_date": pickup_date,
            "ready_time": ready_time,
            "close_time": close_time,
            "additional_instructions": f"CFC Order #{order_id}" if order_id else "",
        }

        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"{RL_QUOTE_API_URL}/pickup/create", data=data, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.HTTPError as http_err:
            body = http_err.read().decode()[:300]
            print(f"[SUPPLIER_POLL] Pickup HTTP error {http_err.code}: {body}")
            return {"success": False, "error": f"rl-quote HTTP {http_err.code}: {body}"}

        if order_id:
            _log_event(order_id, "pickup_request_fired", {
                "pro_number": pro_number, "pickup_date": pickup_date,
                "ready_time": ready_time, "close_time": close_time,
                "success": result.get("success"),
                "pickup_request_id": result.get("pickup_request_id"),
                "error": result.get("error") if not result.get("success") else None,
            })

        return result

    except Exception as e:
        print(f"[SUPPLIER_POLL] Pickup request exception: {e}")
        return {"success": False, "error": str(e)}


# =============================================================================
# BOL PDF FETCH — Option A: R+L Document Retrieval
# =============================================================================

def _fetch_bol_pdf_bytes(pro_number: str) -> Optional[bytes]:
    try:
        url = f"{RL_QUOTE_API_URL}/bol/{pro_number}/pdf"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                pdf_bytes = resp.read()
                if len(pdf_bytes) > 500:
                    print(f"[SUPPLIER_POLL] Option A: fetched PDF for PRO {pro_number} ({len(pdf_bytes)} bytes)")
                    return pdf_bytes
    except urllib.error.HTTPError as e:
        print(f"[SUPPLIER_POLL] Option A HTTP {e.code} for PRO {pro_number}")
    except Exception as e:
        print(f"[SUPPLIER_POLL] Option A failed: {e}")
    return None


# =============================================================================
# FALLBACK BOL PDF — Option B: reportlab
# =============================================================================

def _generate_fallback_bol_pdf(
    pro_number: str, order_id: str,
    shipper_name: str, shipper_address: str,
    consignee_name: str, consignee_address: str,
    pickup_date: str, pickup_time: str, weight: int,
) -> Optional[bytes]:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER
        import io

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter,
                                leftMargin=0.5*inch, rightMargin=0.5*inch,
                                topMargin=0.5*inch, bottomMargin=0.5*inch)
        styles = getSampleStyleSheet()
        navy = colors.HexColor("#1a365d")
        green = colors.HexColor("#059669")

        title_s = ParagraphStyle("t", fontSize=18, fontName="Helvetica-Bold", textColor=navy, alignment=TA_CENTER)
        hdr_s   = ParagraphStyle("h", fontSize=10, fontName="Helvetica-Bold", textColor=colors.white)
        lbl_s   = ParagraphStyle("l", fontSize=8,  fontName="Helvetica-Bold", textColor=colors.grey)
        val_s   = ParagraphStyle("v", fontSize=10, fontName="Helvetica")
        pro_s   = ParagraphStyle("p", fontSize=22, fontName="Helvetica-Bold", textColor=green, alignment=TA_CENTER)
        ftr_s   = ParagraphStyle("f", fontSize=8,  textColor=colors.grey, alignment=TA_CENTER)

        elems = []
        elems.append(Paragraph("BILL OF LADING", title_s))
        elems.append(Paragraph("R+L Carriers — Cabinets For Contractors", styles["Normal"]))
        elems.append(Spacer(1, 0.15*inch))

        pro_tbl = Table([[Paragraph(f"PRO: {pro_number}", pro_s)]], colWidths=[7.5*inch])
        pro_tbl.setStyle(TableStyle([
            ("BOX",(0,0),(-1,-1),2,navy),("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#f0fdf4")),
            ("TOPPADDING",(0,0),(-1,-1),10),("BOTTOMPADDING",(0,0),(-1,-1),10),
        ]))
        elems.append(pro_tbl)
        elems.append(Spacer(1, 0.15*inch))

        info_data = [
            [Paragraph("Pickup Date",lbl_s), Paragraph("Pickup Time",lbl_s), Paragraph("Order #",lbl_s)],
            [Paragraph(pickup_date,val_s), Paragraph(pickup_time,val_s), Paragraph(order_id,val_s)],
        ]
        info_tbl = Table(info_data, colWidths=[2.5*inch,2.5*inch,2.5*inch])
        info_tbl.setStyle(TableStyle([
            ("BOX",(0,0),(-1,-1),1,navy),("INNERGRID",(0,0),(-1,-1),0.5,colors.grey),
            ("BACKGROUND",(0,0),(-1,0),navy),("TOPPADDING",(0,0),(-1,-1),6),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8),
        ]))
        elems.append(info_tbl)
        elems.append(Spacer(1, 0.15*inch))

        addr_data = [
            [Paragraph("SHIPPER (FROM)",hdr_s), Paragraph("CONSIGNEE (TO)",hdr_s)],
            [Paragraph(shipper_name,val_s), Paragraph(consignee_name,val_s)],
            [Paragraph(shipper_address,val_s), Paragraph(consignee_address,val_s)],
        ]
        addr_tbl = Table(addr_data, colWidths=[3.75*inch,3.75*inch])
        addr_tbl.setStyle(TableStyle([
            ("BOX",(0,0),(-1,-1),1,navy),("INNERGRID",(0,0),(-1,-1),0.5,colors.lightgrey),
            ("BACKGROUND",(0,0),(-1,0),navy),("VALIGN",(0,0),(-1,-1),"TOP"),
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("LEFTPADDING",(0,0),(-1,-1),8),
        ]))
        elems.append(addr_tbl)
        elems.append(Spacer(1, 0.15*inch))

        comm_data = [
            [Paragraph("PIECES",hdr_s),Paragraph("WEIGHT (LBS)",hdr_s),
             Paragraph("CLASS",hdr_s),Paragraph("DESCRIPTION",hdr_s)],
            [Paragraph("1",val_s),Paragraph(str(weight),val_s),
             Paragraph("85",val_s),Paragraph("RTA Cabinetry",val_s)],
        ]
        comm_tbl = Table(comm_data, colWidths=[1.5*inch,2*inch,1.5*inch,2.5*inch])
        comm_tbl.setStyle(TableStyle([
            ("BOX",(0,0),(-1,-1),1,navy),("INNERGRID",(0,0),(-1,-1),0.5,colors.lightgrey),
            ("BACKGROUND",(0,0),(-1,0),navy),("TOPPADDING",(0,0),(-1,-1),6),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8),
        ]))
        elems.append(comm_tbl)
        elems.append(Spacer(1, 0.2*inch))
        elems.append(Paragraph(
            f"Generated by Cabinets For Contractors • PRO {pro_number} • R+L Carriers will verify at pickup.",
            ftr_s
        ))

        doc.build(elems)
        pdf_bytes = buf.getvalue()
        print(f"[SUPPLIER_POLL] Option B: generated fallback BOL PDF ({len(pdf_bytes)} bytes)")
        return pdf_bytes
    except Exception as e:
        print(f"[SUPPLIER_POLL] Option B fallback PDF failed: {e}")
        return None


# =============================================================================
# ADDRESS HELPERS
# =============================================================================

def _get_shipper_address_str(shipment: dict) -> str:
    try:
        from bol_routes import WAREHOUSE_ADDRESSES
        wh = WAREHOUSE_CODE_TO_FULL.get(shipment.get("warehouse", ""), shipment.get("warehouse", ""))
        info = WAREHOUSE_ADDRESSES.get(wh, {})
        return f"{info.get('address','')}, {info.get('city','')}, {info.get('state','')} {info.get('zip','')}"
    except Exception:
        return shipment.get("warehouse", "")


def _get_consignee_address_str(shipment: dict) -> str:
    parts = [shipment.get("street",""), shipment.get("city",""),
             f"{shipment.get('state','')} {shipment.get('zip_code','')}"]
    return ", ".join(p for p in parts if p.strip())


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


def _send_raw_email(to_email: str, subject: str, html_body: str,
                    pdf_bytes: Optional[bytes] = None, pdf_filename: str = "BOL.pdf") -> bool:
    try:
        import base64
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.mime.base import MIMEBase
        from email import encoders
        from gmail_sync import get_gmail_access_token

        token = get_gmail_access_token()
        if not token:
            print(f"[SUPPLIER_POLL] No Gmail token for {to_email}")
            return False

        if pdf_bytes:
            msg = MIMEMultipart("mixed")
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText("Please view in HTML email client.", "plain"))
            alt.attach(MIMEText(html_body, "html"))
            msg.attach(alt)
            part = MIMEBase("application", "pdf")
            part.set_payload(pdf_bytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=pdf_filename)
            msg.attach(part)
        else:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText("Please view in HTML email client.", "plain"))
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
        att = f" + PDF {len(pdf_bytes)}b" if pdf_bytes else ""
        print(f"[SUPPLIER_POLL] Email sent to {to_email}: {subject}{att}")
        return True
    except Exception as e:
        print(f"[SUPPLIER_POLL] Email failed to {to_email}: {e}")
        return False


def _bol_email_html(warehouse_name, order_id, pro_number, pickup_date, pickup_time, bol_pdf_url):
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:560px;margin:0 auto;background:white;border-radius:8px;padding:32px;">
    <h2 style="color:#1a365d;margin-top:0;">Bill of Lading — Order #{order_id}</h2>
    <p>The BOL is attached to this email. Please have the shipment ready for R+L pickup.</p>
    <table style="width:100%;border-collapse:collapse;font-size:15px;margin:16px 0;">
        <tr><td style="padding:8px 0;color:#666;width:140px;">PRO Number:</td>
            <td style="font-weight:700;font-size:20px;font-family:monospace;color:#059669;">{pro_number}</td></tr>
        <tr><td style="padding:8px 0;color:#666;">Pickup Date:</td><td style="font-weight:600;">{pickup_date}</td></tr>
        <tr><td style="padding:8px 0;color:#666;">Pickup Time:</td><td style="font-weight:600;">{pickup_time}</td></tr>
    </table>
    <p style="font-size:12px;color:#999;margin-top:24px;">Questions? Email <a href="mailto:orders@cabinetsforcontractors.net">orders@cabinetsforcontractors.net</a></p>
</div>
</body></html>"""


def _send_supplier_poll_email(
    to_email: str, order_id: str, form_url: str,
    poll_number: int = 1, is_critical: bool = False,
    # kept for backwards compat but ignored:
    warehouse_name: str = "", customer_name: str = "", order_total: float = 0,
) -> dict:
    """Minimal poll email — order number and form link only."""
    if is_critical:
        subject = f"🚨 URGENT — Order #{order_id} — Ship Date Required"
        urgency = "<p style='color:#DC2626;font-weight:700;'>⚠️ No ship date received. Please respond immediately.</p>"
    elif poll_number == 2:
        subject = f"📦 Reminder — Order #{order_id} — Ship Date Needed"
        urgency = "<p style='color:#D97706;font-weight:600;'>We are still waiting for a ship date on this order.</p>"
    else:
        subject = f"📦 Order #{order_id} — Ship Date Needed"
        urgency = "<p>Please enter the pickup date and time for this order.</p>"

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:32px;">
    <h2 style="color:#1a365d;margin-top:0;">Cabinets For Contractors — Order #{order_id}</h2>
    {urgency}
    <a href="{form_url}" style="display:inline-block;background:#1a365d;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;font-size:16px;margin:16px 0;">
        Enter Ship Date &amp; Time →
    </a>
    <p style="font-size:12px;color:#999;margin-top:24px;">Questions? Email <a href="mailto:orders@cabinetsforcontractors.net" style="color:#1a365d;">orders@cabinetsforcontractors.net</a></p>
</div></body></html>"""
    success = _send_raw_email(to_email=to_email, subject=subject, html_body=html)
    return {"success": success}


def _render_day_before_email(order_id, date_str, yes_url, no_url,
                              warehouse_name="", customer_name=""):
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#f5f5f5;padding:20px;">
<div style="max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:32px;">
    <h2 style="color:#1a365d;margin-top:0;">Order #{order_id} — Pickup Tomorrow?</h2>
    <p>Order <strong>#{order_id}</strong> is scheduled for R+L pickup <strong>tomorrow, {date_str}</strong>.</p>
    <p style="font-weight:600;color:#1a365d;margin:16px 0;">Still on track for tomorrow?</p>
    <div style="display:flex;gap:12px;margin:20px 0;flex-wrap:wrap;">
        <a href="{yes_url}" style="display:inline-block;background:#059669;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;">
            ✅ Yes — Enter Pickup Time →
        </a>
        <a href="{no_url}" style="display:inline-block;background:#DC2626;color:white;padding:14px 28px;border-radius:6px;text-decoration:none;font-weight:700;">
            ❌ No — Enter New Date →
        </a>
    </div>
    <p style="font-size:12px;color:#999;">Questions? Email <a href="mailto:orders@cabinetsforcontractors.net" style="color:#1a365d;">orders@cabinetsforcontractors.net</a></p>
</div></body></html>"""


def _send_cfc_no_response_alert(shipment: dict, hours: int):
    subject = f"🚨 CALL NEEDED — No Ship Date — Order #{shipment['order_id']} — {shipment['warehouse']}"
    html = f"<p style='color:#DC2626;font-weight:700;'>No ship date after {hours}hrs — call now.</p><p>Order #{shipment['order_id']}</p>"
    _send_raw_email(CFC_INTERNAL_EMAIL, subject, html)


def _send_cfc_push_alert(shipment: dict, new_date_str: str, weekday_name: str):
    subject = f"⚠️ CALL NEEDED — {shipment['warehouse']} Pushed #{shipment['order_id']} to {weekday_name}"
    html = f"<p><strong>Warehouse:</strong> {shipment['warehouse']}<br><strong>New Date:</strong> {new_date_str}</p>"
    _send_raw_email(CFC_INTERNAL_EMAIL, subject, html)


def _send_cfc_bol_fired_alert(shipment: dict, pro_number: str, pickup_date: str,
                               pickup_time: str, close_time: str = "",
                               pickup_confirmation=None, pickup_error=None):
    subject = f"✅ BOL Fired — Order #{shipment['order_id']} — PRO {pro_number}"
    pickup_line = f"<br><strong>Pickup ID:</strong> {pickup_confirmation}" if pickup_confirmation else ""
    close_line = f"<br><strong>Close Time:</strong> {close_time}" if close_time else ""
    error_line = f"<br><span style='color:#DC2626;'>⚠️ Pickup request failed: {pickup_error}</span>" if pickup_error else ""
    html = f"""<p><strong>Order:</strong> #{shipment['order_id']}<br>
    <strong>Warehouse:</strong> {shipment['warehouse']}<br>
    <strong>PRO:</strong> <span style="font-family:monospace;color:#059669;">{pro_number}</span><br>
    <strong>Pickup Date:</strong> {pickup_date}<br>
    <strong>Ready Time:</strong> {pickup_time}{close_line}{pickup_line}{error_line}</p>
    <p>BOL PDF will be emailed to warehouse within ~10 minutes.</p>"""
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
