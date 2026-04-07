"""
checkout_routes.py
FastAPI router for B2BWave checkout flow.

ADDRESS CLASSIFICATION WORKFLOW:
  Case A — Smarty confirmed (rdi populated): calculate shipping, send invoice, done.
  Case B — Smarty uncertain (rdi empty):     send verify-details email, customer
            goes through Step 1 (confirm/correct address) → Step 2 (classify) → Step 3 (pay).
  Case C — Smarty failed entirely:           same as B, edit box pre-opened in Step 1.

WAREHOUSE PICKUP WORKFLOW:
  Case pickup — shipping_option_id == 2 / "Warehouse Pick Up":
    Skip Smarty, skip R+L quote. Invoice with $0 shipping.
    Supplier poll asks "When will this be ready for customer pickup?" (not R+L pickup).
    After ready date: second poll "Has customer picked up?" → Yes → complete / No → escalate.

BLOCKER 1 FIX (2026-04-06): After successful invoice email send (Case A), the webhook
now marks payment_link_sent=TRUE and payment_link_sent_at=NOW() on the orders table.

WS6 (2026-04-06): Shipment INSERT now saves quote_number from shipping result.
  quote_number is passed to R+L BOL creation to lock in the quoted rate.
  NOTE: pickup shipments do NOT include quote_number (no R+L quote for pickups).

Public endpoints:
    POST /webhook/b2bwave-order
    GET  /checkout/payment-complete
    GET  /checkout/{order_id}
    POST /checkout/{order_id}/create-payment
    POST /checkout/{order_id}/confirm-address
    POST /checkout/{order_id}/classify-address
    GET  /checkout/{order_id}/confirm-commercial
    GET  /checkout-ui/{order_id}

Admin-only endpoints:
    GET  /checkout-status
    GET  /debug/b2bwave-raw/{order_id}
    GET  /debug/warehouse-routing/{order_id}
    GET  /debug/test-checkout/{order_id}
"""

import os
import traceback
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from auth import require_admin
from db_helpers import get_db

try:
    from checkout import (
        calculate_order_shipping,
        fetch_b2bwave_order,
        fetch_b2bwave_customer_address,
        update_b2bwave_order_address,
        validate_address_full,
        create_square_payment_link,
        generate_checkout_token,
        verify_checkout_token,
        detect_warehouse_pickup,
        ADDRESS_TYPE_MAP,
        WAREHOUSES,
        TARIFF_RATE,
        group_items_by_warehouse,
    )
    CHECKOUT_ENABLED = True
except ImportError as e:
    print(f"[STARTUP] checkout module not found: {e}")
    CHECKOUT_ENABLED = False

try:
    from sync_service import b2bwave_api_request
    SYNC_SERVICE_LOADED = True
except ImportError:
    SYNC_SERVICE_LOADED = False

CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "").strip()
WAREHOUSE_NOTIFICATION_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "cabinetsforcontractors@gmail.com"
).strip()

checkout_router = APIRouter(tags=["checkout"])


class AddressUpdateRequest(BaseModel):
    street: str
    street2: Optional[str] = ""
    city: str
    state: str
    zip: str


class ConfirmAddressRequest(BaseModel):
    """Step 1 submission — address confirmed as-is or corrected."""
    address_is_correct: bool
    street: Optional[str] = None
    street2: Optional[str] = ""
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None


class ClassifyAddressRequest(BaseModel):
    """Step 2 submission — customer selects address type."""
    address_type: str  # residential_existing | commercial_existing | residential_new_construction
                       # commercial_new_construction | rural | military


# =============================================================================
# GMAIL HELPERS
# =============================================================================

def _get_gmail_token():
    try:
        from gmail_sync import get_gmail_access_token
        return get_gmail_access_token()
    except Exception:
        return None


def _send_gmail_message(token: str, to: str, subject: str, body: str):
    import json, base64, urllib.request
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart("alternative")
    msg["From"] = "william@cabinetsforcontractors.net"
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
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


def _send_verify_address_email(order_id: str, order_data: dict, checkout_url: str, address_found: bool):
    try:
        token = _get_gmail_token()
        if not token:
            return
        customer_email = order_data.get('customer_email', '')
        if not customer_email:
            return
        customer_name = order_data.get('customer_name', 'Valued Customer')
        first_name = customer_name.split()[0] if customer_name else 'there'
        if address_found:
            reason = "We were unable to automatically classify your delivery address as residential or commercial."
        else:
            reason = "We were unable to automatically verify your delivery address."
        body = f"""Hi {first_name},

Thank you for your order #{order_id} with Cabinets For Contractors.

{reason} Before we can calculate your shipping quote, we need you to confirm a few details.

Please click the link below — it only takes about 30 seconds:

{checkout_url}

If you have any questions, call (770) 990-4885 or reply to this email.

Thank you,
William Prince
Cabinets For Contractors
(770) 990-4885
"""
        _send_gmail_message(
            token, customer_email,
            f"Action Needed — Confirm Delivery Details for Order #{order_id}",
            body
        )
        print(f"[WEBHOOK] Verify-address email sent to {customer_email} for order {order_id}")
    except Exception as e:
        print(f"[WEBHOOK] Failed to send verify-address email for order {order_id}: {e}")


def _send_internal_address_alert(order_id: str, order_data: dict, address: dict, case: str, detail: str):
    try:
        token = _get_gmail_token()
        if not token:
            return
        customer = order_data.get('customer_name', 'Unknown')
        company = order_data.get('company_name', '')
        addr_str = f"{address.get('address', '')} {address.get('address2', '')}".strip()
        addr_str += f"\n{address.get('city', '')}, {address.get('state', '')} {address.get('zip', '')}"
        label = "UNCERTAIN CLASSIFICATION" if case == 'B' else "ADDRESS NOT FOUND"
        body = f"""⚠️ {label} — Customer notified to verify

Order #{order_id}
Customer: {customer}{f' ({company})' if company else ''}
Email: {order_data.get('customer_email', '')}

Address: {addr_str}
Detail: {detail}

Customer has been sent a verification email. No invoice sent yet.
They will classify their address in the checkout flow.

Admin: https://cfcordersfrontend-sandbox.vercel.app
"""
        _send_gmail_message(
            token, WAREHOUSE_NOTIFICATION_EMAIL,
            f"⚠️ Address Needs Verification — Order #{order_id} — {customer}",
            body
        )
    except Exception as e:
        print(f"[WEBHOOK] Failed to send internal address alert for order {order_id}: {e}")


def _send_internal_order_notification(order_id: str, order_data: dict, shipping_result: dict, checkout_url: str):
    try:
        token = _get_gmail_token()
        if not token:
            return
        customer = order_data.get('customer_name', 'Unknown')
        company = order_data.get('company_name', '')
        line_items = order_data.get('line_items', [])
        grand_total = shipping_result.get('grand_total', 0) if shipping_result else 0
        items_text = "\n".join(
            f"  {i.get('sku', '')} — {i.get('name', '')} x{i.get('quantity', 1)} @ ${i.get('price', 0):.2f}"
            for i in line_items
        )
        body = f"""New Order #{order_id}

Customer: {customer}{f' ({company})' if company else ''}
Date: {order_data.get('order_date', '')}

Items:
{items_text}

Total Due: ${grand_total:,.2f}
Invoice sent to: {order_data.get('customer_email', '')}
Checkout URL: {checkout_url}
"""
        _send_gmail_message(
            token, WAREHOUSE_NOTIFICATION_EMAIL,
            f"New Order #{order_id} — {customer}{f' ({company})' if company else ''} — ${grand_total:,.2f}",
            body
        )
    except Exception as e:
        print(f"[WEBHOOK] Internal notification failed for order {order_id}: {e}")


def _send_internal_pickup_notification(
    order_id: str, order_data: dict, grand_total: float,
    checkout_url: str, warehouse_groups: dict
):
    """Warehouse pickup order — internal CFC notification."""
    try:
        token = _get_gmail_token()
        if not token:
            return
        customer = order_data.get('customer_name', 'Unknown')
        company  = order_data.get('company_name', '')
        line_items = order_data.get('line_items', [])
        items_text = "\n".join(
            f"  {i.get('sku', '')} — {i.get('name', '')} x{i.get('quantity', 1)}"
            for i in line_items
        )
        warehouses_text = ", ".join(
            WAREHOUSES.get(wh, {}).get('name', wh)
            for wh in warehouse_groups if wh != 'UNKNOWN'
        ) or "unknown"
        body = f"""🏭 WAREHOUSE PICKUP ORDER — #{order_id}

Customer: {customer}{f' ({company})' if company else ''}
Email: {order_data.get('customer_email', '')}
Date: {order_data.get('order_date', '')}

Warehouse(s): {warehouses_text}
Total Due: ${grand_total:,.2f} (shipping $0 — customer picking up)

Items:
{items_text}

Invoice with $0 shipping sent to customer.
When admin clicks "Send to Warehouse", supplier will be asked:
  "When will this order be ready for customer pickup?"

Checkout URL: {checkout_url}
Admin: https://cfcordersfrontend-sandbox.vercel.app
"""
        _send_gmail_message(
            token, WAREHOUSE_NOTIFICATION_EMAIL,
            f"🏭 Pickup Order #{order_id} — {customer}{f' ({company})' if company else ''} — ${grand_total:,.2f}",
            body
        )
    except Exception as e:
        print(f"[WEBHOOK PICKUP] Internal notification failed for order {order_id}: {e}")


def _send_commercial_confirmed_email(order_id: str, order_data: dict):
    try:
        token = _get_gmail_token()
        if not token:
            return
        customer = order_data.get('customer_name', 'Unknown') if order_data else 'Unknown'
        company = order_data.get('company_name', '') if order_data else ''
        customer_email = order_data.get('customer_email', '') if order_data else ''
        body = f"""⚠️ COMMERCIAL ADDRESS CONFIRMED BY CUSTOMER

Order #{order_id}
Customer: {customer}{f' ({company})' if company else ''}
Email: {customer_email}

The customer clicked "This is a commercial address" in their invoice email.

ACTION REQUIRED:
1. Recalculate shipping as commercial (no residential/liftgate accessorials)
2. Send a corrected invoice manually
3. Tell customer NOT to pay the original invoice

Admin: https://cfcordersfrontend-sandbox.vercel.app
"""
        _send_gmail_message(
            token, WAREHOUSE_NOTIFICATION_EMAIL,
            f"⚠️ Commercial Address Confirmed — Order #{order_id} — RESEND INVOICE",
            body
        )
        print(f"[CHECKOUT] Commercial confirmed email sent for order {order_id}")
    except Exception as e:
        print(f"[CHECKOUT] Failed to send commercial confirmed email for order {order_id}: {e}")


# =============================================================================
# WAREHOUSE PICKUP WEBHOOK HANDLER
# =============================================================================

def _ensure_order_row(order_id: str, order_data: dict):
    """
    Guarantee the orders row exists before inserting order_shipments (FK constraint).
    The B2BWave sync service may not have run yet when the webhook fires.
    Safe to call multiple times — ON CONFLICT DO NOTHING.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO orders (order_id, updated_at)
                       VALUES (%s, NOW())
                       ON CONFLICT (order_id) DO NOTHING""",
                    (order_id,)
                )
    except Exception as e:
        print(f"[WEBHOOK] _ensure_order_row failed (non-fatal — may be schema mismatch): {e}")


def _handle_pickup_webhook(
    order_id: str, order_data: dict, customer_email: str,
    checkout_url: str, base: str, token: str,
) -> dict:
    """
    Warehouse pickup order flow (shipping_option_id == 2):
    - Skip Smarty address validation (customer coming to us)
    - Skip R+L rate quote and BOL flow
    - Invoice sent with $0 shipping
    - Shipment record created with pickup_type='warehouse_pickup'
    - Supplier poll (when admin sends to warehouse) asks:
        "When will Order #XXXX be ready for customer pickup?"
    - After ready date: second poll "Has customer picked up?" → complete or escalate

    NOTE: pickup shipments intentionally omit quote_number (no R+L quote involved).
    """
    line_items = order_data.get('line_items', [])
    total_items = sum(
        float(i.get('price', 0)) * int(i.get('quantity', 1))
        for i in line_items
    )
    tariff_amount = round(total_items * TARIFF_RATE, 2)
    grand_total   = round(total_items + tariff_amount, 2)

    # Build $0 shipping_result for the invoice template
    shipping_result = {
        'shipments': [],
        'total_items': round(total_items, 2),
        'tariff_rate': TARIFF_RATE,
        'tariff_amount': tariff_amount,
        'total_shipping': 0.0,
        'grand_total': grand_total,
        'destination': {},
        'is_residential': False,
        'is_pickup': True,
    }

    # Ensure the orders row exists before creating shipments (FK constraint).
    # Webhook fires immediately; sync service may not have run yet.
    _ensure_order_row(order_id, order_data)

    # Create shipment records per warehouse.
    # pickup_type='warehouse_pickup' distinguishes from freight shipments.
    # quote_number intentionally excluded — pickups have no R+L quote.
    warehouse_groups = group_items_by_warehouse(line_items)
    created_count = 0
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                for wh_code, items in warehouse_groups.items():
                    if not wh_code or wh_code == 'UNKNOWN':
                        continue
                    wh_info = WAREHOUSES.get(wh_code, {})
                    wh_name = wh_info.get('name', wh_code)
                    ship_id = f"{order_id}-{wh_name.replace(' & ', '-').replace(' ', '-')}"
                    cur.execute("SELECT id FROM order_shipments WHERE shipment_id = %s", (ship_id,))
                    if not cur.fetchone():
                        weight = sum(30 * int(i.get('quantity', 1)) for i in items)
                        try:
                            # Primary: with pickup_type column (added by add_ws6_pickup_fields migration)
                            cur.execute(
                                """INSERT INTO order_shipments
                                   (order_id, shipment_id, warehouse, status, origin_zip,
                                    weight, has_oversized, is_residential, pickup_type)
                                   VALUES (%s, %s, %s, 'needs_order', %s, %s, FALSE, FALSE, 'warehouse_pickup')""",
                                (order_id, ship_id, wh_name, wh_info.get('zip', ''), weight)
                            )
                        except Exception:
                            # Fallback: pickup_type column not yet added
                            conn.rollback()
                            cur.execute(
                                """INSERT INTO order_shipments
                                   (order_id, shipment_id, warehouse, status, origin_zip,
                                    weight, has_oversized, is_residential)
                                   VALUES (%s, %s, %s, 'needs_order', %s, %s, FALSE, FALSE)""",
                                (order_id, ship_id, wh_name, wh_info.get('zip', ''), weight)
                            )
                        created_count += 1
        print(f"[WEBHOOK PICKUP] {created_count} shipment records created for order {order_id}")
    except Exception as e:
        print(f"[WEBHOOK PICKUP] Shipment record error: {e}")

    # Mark is_pickup on order
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE orders SET is_pickup = TRUE, updated_at = NOW() WHERE order_id = %s",
                    (order_id,)
                )
    except Exception as e:
        print(f"[WEBHOOK PICKUP] is_pickup update skipped: {e}")

    # Send invoice with $0 shipping
    email_result = None
    if customer_email:
        try:
            from email_sender import send_order_email
            customer_id = order_data.get('customer_id')
            if customer_id:
                try:
                    billing_address = fetch_b2bwave_customer_address(str(customer_id))
                    if billing_address:
                        order_data['billing_address'] = billing_address
                except Exception:
                    pass
            order_data['payment_link'] = checkout_url
            order_data['shipping_result'] = shipping_result
            order_data['is_residential'] = False
            order_data['confirm_commercial_url'] = f"{base}/checkout/{order_id}/confirm-commercial?token={token}"
            email_result = send_order_email(
                order_id=order_id, template_id='payment_link',
                to_email=customer_email, order_data=order_data,
                triggered_by='b2bwave_webhook_pickup'
            )
            print(f"[WEBHOOK PICKUP] Invoice email order {order_id}: success={email_result.get('success')}")
            if email_result.get('success'):
                try:
                    with get_db() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE orders SET payment_link_sent = TRUE, "
                                "payment_link_sent_at = NOW(), updated_at = NOW() "
                                "WHERE order_id = %s",
                                (order_id,)
                            )
                except Exception as me:
                    print(f"[WEBHOOK PICKUP] payment_link_sent update failed: {me}")
        except Exception as e:
            print(f"[WEBHOOK PICKUP] Email failed: {e}")
            email_result = {'success': False, 'error': str(e)}

    _send_internal_pickup_notification(order_id, order_data, grand_total, checkout_url, warehouse_groups)

    return {
        "status": "ok",
        "order_id": order_id,
        "checkout_url": checkout_url,
        "case": "pickup",
        "is_warehouse_pickup": True,
        "total_items": round(total_items, 2),
        "tariff": tariff_amount,
        "shipping": 0,
        "grand_total": grand_total,
        "email_sent": email_result.get('success') if email_result else False,
    }


# =============================================================================
# DB STATE HELPER
# =============================================================================

def _get_checkout_state(order_id: str) -> dict:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT address_pending, address_validation_error,
                           address_classification_needed, address_initially_found,
                           address_type_confirmed, is_residential_customer_confirmed
                    FROM pending_checkouts WHERE order_id = %s
                """, (order_id,))
                row = cur.fetchone()
                if not row:
                    return {}
                return {
                    'address_pending': bool(row[0]),
                    'address_validation_error': row[1],
                    'address_classification_needed': bool(row[2]) if row[2] is not None else False,
                    'address_initially_found': bool(row[3]) if row[3] is not None else True,
                    'address_type_confirmed': row[4],
                    'is_residential_customer_confirmed': row[5],
                }
    except Exception as e:
        print(f"[CHECKOUT] Failed to read checkout state for {order_id}: {e}")
        return {}


# =============================================================================
# DEBUG / CONFIG
# =============================================================================

@checkout_router.get("/checkout-status")
def checkout_status(_: bool = Depends(require_admin)):
    try:
        from checkout import B2BWAVE_URL as _URL, B2BWAVE_USERNAME as _USER, B2BWAVE_API_KEY as _KEY
        checkout_b2bwave = f"{_URL} / {_USER} / {'set' if _KEY else 'not set'}"
    except Exception:
        checkout_b2bwave = "import failed"
    from config import B2BWAVE_URL as MAIN_B2BWAVE_URL
    gmail_send = os.environ.get("GMAIL_SEND_ENABLED", "false").lower() == "true"
    return {
        "checkout_enabled": CHECKOUT_ENABLED,
        "checkout_base_url": CHECKOUT_BASE_URL or "(not set)",
        "gmail_send_enabled": gmail_send,
        "checkout_b2bwave_config": checkout_b2bwave,
        "main_b2bwave_url": MAIN_B2BWAVE_URL or "(not set)",
    }


@checkout_router.get("/debug/b2bwave-raw/{order_id}")
def debug_b2bwave_raw(order_id: str, _: bool = Depends(require_admin)):
    if not SYNC_SERVICE_LOADED:
        return {"status": "error", "error": "sync_service not loaded"}
    try:
        data = b2bwave_api_request("orders", {"id_eq": order_id})
        return {"status": "ok", "raw_response": data}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@checkout_router.get("/debug/warehouse-routing/{order_id}")
def debug_warehouse_routing(order_id: str, _: bool = Depends(require_admin)):
    if not CHECKOUT_ENABLED:
        return {"status": "error", "error": "checkout module not loaded"}
    try:
        from checkout import group_items_by_warehouse, get_warehouse_for_sku
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "error": "Order not found in B2BWave"}
        line_items = order_data.get("line_items", [])
        warehouse_groups = group_items_by_warehouse(line_items)
        return {
            "status": "ok", "order_id": order_id,
            "customer": order_data.get("customer_name", ""),
            "shipping_option": order_data.get("shipping_option_name", ""),
            "is_pickup": detect_warehouse_pickup(order_data),
            "total_items": len(line_items),
            "item_routing": [
                {"sku": i.get("sku", ""), "qty": i.get("quantity", 0),
                 "warehouse": get_warehouse_for_sku(i.get("sku", ""))}
                for i in line_items
            ],
            "warehouse_groups": {wh: {"item_count": len(its)} for wh, its in warehouse_groups.items()},
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


@checkout_router.get("/debug/test-checkout/{order_id}")
def debug_test_checkout(order_id: str, _: bool = Depends(require_admin)):
    if not CHECKOUT_ENABLED:
        return {"status": "error", "error": "checkout module not loaded"}
    try:
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "error": "Order not found in B2BWave"}
        token = generate_checkout_token(order_id)
        base = CHECKOUT_BASE_URL or "https://cfcorderbackend-sandbox.onrender.com"
        is_pickup = detect_warehouse_pickup(order_data)
        if is_pickup:
            return {
                "status": "ok", "order_id": order_id,
                "customer": order_data.get("customer_name"),
                "is_pickup": True,
                "shipping_option": order_data.get("shipping_option_name"),
                "token": token,
                "checkout_url": f"{base}/checkout-ui/{order_id}?token={token}",
            }
        shipping_address = order_data.get("shipping_address") or {}
        shipping_result = calculate_order_shipping(order_data, shipping_address)
        return {
            "status": "ok", "order_id": order_id,
            "customer": order_data.get("customer_name"),
            "customer_email": order_data.get("customer_email"),
            "is_pickup": False,
            "token": token,
            "checkout_url": f"{base}/checkout-ui/{order_id}?token={token}",
            "destination": shipping_address,
            "shipping": shipping_result,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


# =============================================================================
# WEBHOOK
# =============================================================================

@checkout_router.post("/webhook/b2bwave-order")
def b2bwave_order_webhook(payload: dict):
    """
    B2BWave webhook — fired when an order is placed.

    Case pickup (shipping_option_id == 2): skip Smarty/R+L, $0 invoice, pickup poll flow.
    Case A (Smarty confirmed rdi):  calculate shipping → send invoice email → done.
    Case B (Smarty uncertain rdi):  send verify-details email → customer classifies in UI.
    Case C (Smarty failed):         same as B, edit box pre-opened.
    """
    if not CHECKOUT_ENABLED:
        return {"status": "error", "message": "Checkout module not enabled"}

    order_id = payload.get("id") or payload.get("order_id")
    customer_email = payload.get("customer_email") or payload.get("email")
    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id")

    token = generate_checkout_token(str(order_id))
    base = CHECKOUT_BASE_URL or "https://cfcorderbackend-sandbox.onrender.com"
    checkout_url = f"{base}/checkout-ui/{order_id}?token={token}"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_checkouts (order_id, customer_email, checkout_token, created_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (order_id) DO UPDATE SET
                    customer_email = EXCLUDED.customer_email,
                    checkout_token = EXCLUDED.checkout_token,
                    created_at = NOW()
                """,
                (str(order_id), customer_email, token),
            )

    order_data = fetch_b2bwave_order(str(order_id))
    if not order_data:
        return {"status": "ok", "order_id": order_id, "checkout_url": checkout_url,
                "message": "Order not found in B2BWave"}

    # ------------------------------------------------------------------
    # Warehouse Pickup — skip Smarty, skip R+L, $0 invoice
    # ------------------------------------------------------------------
    if detect_warehouse_pickup(order_data):
        print(f"[WEBHOOK] Order {order_id} — Warehouse Pickup detected "
              f"(option: {order_data.get('shipping_option_name', '')})")
        return _handle_pickup_webhook(
            order_id=str(order_id),
            order_data=order_data,
            customer_email=customer_email,
            checkout_url=checkout_url,
            base=base,
            token=token,
        )

    # ------------------------------------------------------------------
    # Freight orders — Smarty validation
    # ------------------------------------------------------------------
    shipping_address = order_data.get("shipping_address") or {}
    validation = validate_address_full(shipping_address)

    if not validation['success'] or validation.get('is_uncertain'):
        case = 'B' if validation['success'] else 'C'
        address_initially_found = validation['success']

        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE pending_checkouts
                        SET address_classification_needed = TRUE,
                            address_initially_found = %s,
                            address_validation_error = %s
                        WHERE order_id = %s
                        """,
                        (address_initially_found,
                         validation.get('error') or 'rdi empty',
                         str(order_id))
                    )
        except Exception as e:
            print(f"[WEBHOOK] DB update failed for order {order_id}: {e}")

        _send_verify_address_email(str(order_id), order_data, checkout_url, address_initially_found)
        _send_internal_address_alert(
            str(order_id), order_data, shipping_address, case,
            validation.get('error') or 'rdi empty — residential/commercial unknown'
        )

        print(f"[WEBHOOK] Order {order_id} Case {case} — customer sent to classification flow")
        return {
            "status": "classification_needed",
            "order_id": order_id,
            "case": case,
            "checkout_url": checkout_url,
            "message": f"Case {case} — customer notified to verify delivery details",
        }

    print(f"[WEBHOOK] Order {order_id} Case A — {'residential' if validation['is_residential'] else 'commercial'}")

    email_result = None
    shipping_result = None

    if customer_email:
        try:
            from email_sender import send_order_email

            customer_id = order_data.get('customer_id')
            if customer_id:
                billing_address = fetch_b2bwave_customer_address(str(customer_id))
                if billing_address:
                    order_data['billing_address'] = billing_address

            try:
                shipping_result = calculate_order_shipping(
                    order_data, shipping_address,
                    is_residential_override=validation['is_residential']
                )
            except Exception as se:
                print(f"[WEBHOOK] Shipping calc failed for order {order_id}: {se}")
                shipping_result = None

            if shipping_result and shipping_result.get('shipments'):
                try:
                    with get_db() as wh_conn:
                        with wh_conn.cursor() as wh_cur:
                            created = 0
                            for ship in shipping_result['shipments']:
                                wh_code = ship.get('warehouse')
                                if not wh_code or wh_code == 'UNKNOWN':
                                    continue
                                wh_name = ship.get('warehouse_name', wh_code)
                                ship_id = f"{order_id}-{wh_name.replace(' & ', '-').replace(' ', '-')}"
                                wh_cur.execute("SELECT id FROM order_shipments WHERE shipment_id = %s", (ship_id,))
                                if not wh_cur.fetchone():
                                    ship_quote_number = (
                                        (ship.get('quote') or {}).get('quote', {}).get('quote_number') or
                                        (ship.get('quote') or {}).get('quote_number') or
                                        ''
                                    )
                                    wh_cur.execute(
                                        """INSERT INTO order_shipments
                                           (order_id, shipment_id, warehouse, status, origin_zip,
                                            weight, has_oversized, is_residential, quote_number)
                                           VALUES (%s, %s, %s, 'needs_order', %s, %s, %s, %s, %s)""",
                                        (str(order_id), ship_id, wh_name,
                                         ship.get('origin_zip', ''), ship.get('weight'),
                                         ship.get('is_oversized', False), validation['is_residential'],
                                         ship_quote_number)
                                    )
                                    created += 1
                    print(f"[WEBHOOK] {created} shipment records created for order {order_id}")
                except Exception as wh_e:
                    print(f"[WEBHOOK] Shipment record creation failed: {wh_e}")

            order_data['payment_link'] = checkout_url
            order_data['shipping_result'] = shipping_result
            order_data['is_residential'] = validation['is_residential']
            order_data['confirm_commercial_url'] = f"{base}/checkout/{order_id}/confirm-commercial?token={token}"

            email_result = send_order_email(
                order_id=str(order_id), template_id='payment_link',
                to_email=customer_email, order_data=order_data,
                triggered_by='b2bwave_webhook'
            )
            print(f"[WEBHOOK] Invoice email order {order_id}: success={email_result.get('success')}")

            if email_result.get('success'):
                try:
                    with get_db() as mark_conn:
                        with mark_conn.cursor() as mark_cur:
                            mark_cur.execute(
                                "UPDATE orders SET payment_link_sent = TRUE, "
                                "payment_link_sent_at = NOW(), updated_at = NOW() "
                                "WHERE order_id = %s",
                                (str(order_id),)
                            )
                    print(f"[WEBHOOK] payment_link_sent marked TRUE for order {order_id}")
                except Exception as mark_e:
                    print(f"[WEBHOOK] Failed to mark payment_link_sent for order {order_id}: {mark_e}")

            _send_internal_order_notification(order_id, order_data, shipping_result, checkout_url)

        except Exception as e:
            print(f"[WEBHOOK] Email send failed for order {order_id}: {e}")
            email_result = {'success': False, 'error': str(e)}

    return {
        "status": "ok",
        "order_id": order_id,
        "checkout_url": checkout_url,
        "case": "A",
        "is_residential": validation['is_residential'],
        "email_sent": email_result.get('success') if email_result else False,
    }


# =============================================================================
# CHECKOUT API ENDPOINTS
# =============================================================================

@checkout_router.get("/checkout/payment-complete")
def payment_complete(order: str, transactionId: Optional[str] = None):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pending_checkouts SET payment_completed_at = NOW(), transaction_id = %s WHERE order_id = %s",
                (transactionId, order)
            )
            cur.execute(
                "UPDATE orders SET payment_received = TRUE, payment_received_at = NOW(), "
                "payment_method = 'Square Checkout', updated_at = NOW() WHERE order_id = %s",
                (order,)
            )
    return {"status": "ok", "message": "Payment completed", "order_id": order}


@checkout_router.get("/checkout/{order_id}")
def get_checkout_data(order_id: str, token: str):
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid or expired checkout link")

    state = _get_checkout_state(order_id)

    if state.get('address_classification_needed') and not state.get('address_type_confirmed'):
        order_data = fetch_b2bwave_order(order_id)
        shipping_address = (order_data or {}).get("shipping_address") or {}
        return {
            "status": "classification_needed",
            "order_id": order_id,
            "address_initially_found": state.get('address_initially_found', True),
            "shipping_address": shipping_address,
            "customer_name": (order_data or {}).get("customer_name", ""),
            "company_name": (order_data or {}).get("company_name", ""),
        }

    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")

    is_residential_override = None
    if state.get('address_type_confirmed'):
        is_residential_override = state.get('is_residential_customer_confirmed')

    billing_address = None
    customer_id = order_data.get('customer_id')
    if customer_id:
        billing_address = fetch_b2bwave_customer_address(str(customer_id))

    shipping_address = order_data.get("shipping_address") or {}
    shipping_result = calculate_order_shipping(order_data, shipping_address,
                                               is_residential_override=is_residential_override)

    base = CHECKOUT_BASE_URL or "https://cfcorderbackend-sandbox.onrender.com"

    return {
        "status": "ok",
        "order_id": order_id,
        "order": {
            "id": order_id,
            "customer_name": order_data.get("customer_name"),
            "customer_email": order_data.get("customer_email"),
            "company_name": order_data.get("company_name"),
            "line_items": order_data.get("line_items", []),
            "subtotal": order_data.get("order_total", 0),
        },
        "shipping": shipping_result,
        "shipping_address": shipping_address,
        "billing_address": billing_address,
        "is_residential": shipping_result.get("is_residential", True),
        "address_type_confirmed": state.get('address_type_confirmed'),
        "confirm_commercial_url": f"{base}/checkout/{order_id}/confirm-commercial?token={token}",
        "payment_ready": shipping_result.get("grand_total", 0) > 0,
    }


@checkout_router.post("/checkout/{order_id}/confirm-address")
def confirm_address(order_id: str, token: str, body: ConfirmAddressRequest):
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid checkout token")

    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")

    shipping_address = order_data.get("shipping_address") or {}

    if not body.address_is_correct:
        if not body.street or not body.city or not body.state or not body.zip:
            raise HTTPException(status_code=400, detail="Street, city, state and ZIP required")
        new_address = {
            'street': body.street, 'street2': body.street2 or '',
            'city': body.city, 'state': body.state.upper(), 'zip': body.zip
        }
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE orders SET street = %s, street2 = %s, city = %s, "
                        "state = %s, zip_code = %s, updated_at = NOW() WHERE order_id = %s",
                        (body.street, body.street2 or '', body.city,
                         body.state.upper(), body.zip, order_id)
                    )
        except Exception as e:
            print(f"[CHECKOUT] Local DB address update failed for {order_id}: {e}")
        update_b2bwave_order_address(order_id, new_address)
        shipping_address = new_address

    validation = validate_address_full(shipping_address)

    if validation['success'] and not validation.get('is_uncertain'):
        is_res = validation['is_residential']
        addr_type = 'residential_existing' if is_res else 'commercial_existing'
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE pending_checkouts
                           SET address_classification_needed = FALSE,
                               address_type_confirmed = %s,
                               is_residential_customer_confirmed = %s
                           WHERE order_id = %s""",
                        (addr_type, is_res, order_id)
                    )
        except Exception as e:
            print(f"[CHECKOUT] DB update after Smarty confirm failed: {e}")

        return {
            "status": "ok",
            "need_classification": False,
            "is_residential": is_res,
            "message": "Address confirmed by Smarty"
        }
    else:
        return {
            "status": "ok",
            "need_classification": True,
            "message": "Address could not be confirmed — customer must classify"
        }


@checkout_router.post("/checkout/{order_id}/classify-address")
def classify_address(order_id: str, token: str, body: ClassifyAddressRequest):
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid checkout token")

    valid_types = list(ADDRESS_TYPE_MAP.keys())
    if body.address_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid address_type. Must be one of: {valid_types}")

    is_residential = ADDRESS_TYPE_MAP[body.address_type]

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE pending_checkouts
                       SET address_classification_needed = FALSE,
                           address_type_confirmed = %s,
                           is_residential_customer_confirmed = %s
                       WHERE order_id = %s""",
                    (body.address_type, is_residential, order_id)
                )
    except Exception as e:
        print(f"[CHECKOUT] Classification save failed for {order_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to save classification")

    return {
        "status": "ok",
        "address_type": body.address_type,
        "is_residential": is_residential,
        "message": "Classification saved — proceed to checkout"
    }


@checkout_router.get("/checkout/{order_id}/confirm-commercial")
def confirm_commercial(order_id: str, token: str):
    if not verify_checkout_token(order_id, token):
        return HTMLResponse(content="<h1>Invalid or expired link</h1>", status_code=403)

    order_data = None
    try:
        order_data = fetch_b2bwave_order(order_id)
    except Exception:
        pass

    _send_commercial_confirmed_email(order_id, order_data)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Commercial Address Confirmed</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #f5f5f5; display: flex; align-items: center;
               justify-content: center; min-height: 100vh; padding: 20px; }}
        .card {{ max-width: 520px; width: 100%; background: white; border-radius: 10px;
                box-shadow: 0 2px 16px rgba(0,0,0,0.1); padding: 40px 36px; text-align: center; }}
        .icon {{ font-size: 52px; margin-bottom: 20px; }}
        h1 {{ color: #1a365d; font-size: 22px; margin-bottom: 14px; }}
        p {{ color: #4a5568; font-size: 15px; line-height: 1.7; margin-bottom: 12px; }}
        .warning {{ color: #b45309; font-weight: 600; background: #fffbeb;
                   padding: 12px; border-radius: 6px; margin: 16px 0; font-size: 14px; }}
        .contact {{ margin-top: 28px; padding-top: 20px; border-top: 1px solid #e2e8f0;
                   font-size: 14px; color: #718096; }}
        .contact a {{ color: #2563eb; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">&#x2705;</div>
        <h1>Thank You for Confirming</h1>
        <p>Your message has been sent to the Cabinets For Contractors team.</p>
        <div class="warning">
            &#x26A0;&#xFE0F; Do not pay the current invoice. We will review your order
            and send a corrected invoice for the commercial shipping rate within one business day.
        </div>
        <p>We appreciate you letting us know — this ensures you receive the correct shipping quote.</p>
        <div class="contact">
            Questions? Call <a href="tel:7709904885">(770) 990-4885</a> or email
            <a href="mailto:william@cabinetsforcontractors.net">william@cabinetsforcontractors.net</a>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@checkout_router.post("/checkout/{order_id}/create-payment")
def create_checkout_payment(order_id: str, token: str):
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid checkout token")

    state = _get_checkout_state(order_id)
    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")

    is_residential_override = state.get('is_residential_customer_confirmed') \
        if state.get('address_type_confirmed') else None

    shipping_address = order_data.get("shipping_address") or {}
    shipping_result = calculate_order_shipping(order_data, shipping_address,
                                               is_residential_override=is_residential_override)
    grand_total = shipping_result.get("grand_total", 0)
    if grand_total <= 0:
        raise HTTPException(status_code=400, detail="Invalid order total")

    payment_url = create_square_payment_link(
        int(grand_total * 100), order_id, order_data.get("customer_email", "")
    )
    if not payment_url:
        raise HTTPException(status_code=500, detail="Failed to create payment link")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pending_checkouts SET payment_link = %s, payment_amount = %s, "
                "payment_initiated_at = NOW() WHERE order_id = %s",
                (payment_url, grand_total, order_id)
            )

    return {"status": "ok", "payment_url": payment_url, "amount": grand_total}


# =============================================================================
# CHECKOUT UI — Single-page, multi-step
# =============================================================================

@checkout_router.get("/checkout-ui/{order_id}")
def checkout_ui(order_id: str, token: str):
    if not verify_checkout_token(order_id, token):
        return HTMLResponse(content="<h1>Invalid or expired checkout link</h1>", status_code=403)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Complete Your Order - CFC</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; border-radius: 8px;
                     box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 30px; }}
        h1 {{ color: #1a365d; margin-bottom: 20px; }}
        h2 {{ color: #555; font-size: 18px; margin: 20px 0 10px;
             border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        .loading {{ text-align: center; padding: 60px; color: #666; font-size: 16px; }}
        .error {{ background: #fee; color: #c00; padding: 15px; border-radius: 4px; margin: 20px 0; }}
        .step-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
                     padding: 24px; margin-bottom: 20px; }}
        .step-card h2 {{ border: none; margin-top: 0; font-size: 20px; color: #1a365d; }}
        .step-card p {{ color: #4a5568; font-size: 15px; line-height: 1.7; margin-bottom: 12px; }}
        .address-highlight {{ background: #FFFBEB; border: 2px solid #F59E0B; border-radius: 6px;
                             padding: 14px 16px; margin: 14px 0; font-size: 15px;
                             color: #1a202c; line-height: 1.7; }}
        .address-label-top {{ font-size: 10px; font-weight: 700; color: #D97706;
                             text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }}
        .btn {{ display: inline-block; padding: 11px 24px; border-radius: 6px;
               font-size: 15px; font-weight: 600; cursor: pointer; border: none; font-family: inherit; }}
        .btn-primary {{ background: #2563eb; color: white; }}
        .btn-primary:hover {{ background: #1d4ed8; }}
        .btn-primary:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #f1f5f9; color: #334155; }}
        .btn-secondary:hover {{ background: #e2e8f0; }}
        .btn-row {{ display: flex; gap: 12px; margin-top: 16px; flex-wrap: wrap; }}
        .btn-full {{ display: block; width: 100%; padding: 15px; font-size: 18px; font-weight: 700; text-align: center; }}
        .edit-form {{ margin-top: 14px; display: none; }}
        .edit-form.open {{ display: block; }}
        .edit-form input {{ width: 100%; padding: 9px 11px; border: 1px solid #cbd5e0;
                          border-radius: 4px; font-size: 14px; margin-bottom: 8px; font-family: inherit; }}
        .form-row-3 {{ display: grid; grid-template-columns: 1fr 70px 90px; gap: 8px; }}
        .form-actions {{ display: flex; gap: 8px; margin-top: 4px; }}
        .msg-ok {{ color: #166534; background: #d1fae5; padding: 8px 12px; border-radius: 4px; font-size: 13px; margin-top: 8px; }}
        .msg-err {{ color: #991b1b; background: #fee2e2; padding: 8px 12px; border-radius: 4px; font-size: 13px; margin-top: 8px; }}
        .classify-list {{ margin: 14px 0; }}
        .classify-item {{ display: block; padding: 13px 16px; margin-bottom: 8px;
                         border: 2px solid #e2e8f0; border-radius: 8px; cursor: pointer;
                         font-size: 15px; color: #1a202c; transition: border-color .15s; }}
        .classify-item:hover {{ border-color: #93c5fd; background: #eff6ff; }}
        .classify-item input {{ margin-right: 10px; width: 18px; height: 18px; vertical-align: middle; cursor: pointer; }}
        .classify-item.selected {{ border-color: #2563eb; background: #eff6ff; }}
        .item {{ display: flex; justify-content: space-between; padding: 10px 0;
                border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
        .item-sku {{ width: 110px; font-family: monospace; color: #718096; font-size: 12px; }}
        .item-name {{ flex: 1; padding: 0 8px; }}
        .item-qty {{ width: 50px; text-align: center; color: #666; }}
        .item-price {{ width: 90px; text-align: right; font-weight: 500; }}
        .shipment {{ background: #f9f9f9; padding: 15px; border-radius: 4px; margin: 10px 0; }}
        .totals {{ margin-top: 16px; padding-top: 16px; border-top: 1px solid #ddd; }}
        .total-row {{ display: flex; justify-content: space-between; padding: 7px 0; font-size: 14px; color: #555; }}
        .total-row.grand {{ font-size: 20px; font-weight: 700; color: #1a365d;
                           border-top: 2px solid #1a365d; margin-top: 8px; padding-top: 12px; }}
        .residential-notice {{ background: #EFF6FF; border: 1px solid #BFDBFE; border-radius: 8px; padding: 16px; margin: 20px 0; }}
        .residential-notice p {{ color: #1E40AF; margin-bottom: 8px; font-size: 14px; }}
        .residential-notice p:last-child {{ margin-bottom: 0; }}
        .address-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }}
        @media (max-width: 600px) {{ .address-row {{ grid-template-columns: 1fr; }} }}
        .address-block {{ padding: 14px; border-radius: 6px; border: 1px solid #e2e8f0; background: #f7fafc; }}
        .address-block.ship-to {{ border-color: #F59E0B; background: #FFFBEB; }}
        .address-blabel {{ font-size: 10px; font-weight: 700; color: #718096;
                          text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }}
        .ship-to .address-blabel {{ color: #D97706; }}
        .address-content {{ font-size: 13px; color: #1a202c; line-height: 1.6; }}
        .modal-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6);
                         z-index: 1000; align-items: center; justify-content: center; }}
        .modal-overlay.active {{ display: flex; }}
        .modal {{ background: white; border-radius: 8px; padding: 28px;
                 max-width: 520px; width: 90%; max-height: 85vh; overflow-y: auto; }}
        .modal h3 {{ color: #1a365d; margin-bottom: 16px; font-size: 18px; }}
        .modal ul {{ margin: 12px 0 20px 20px; }}
        .modal ul li {{ margin-bottom: 10px; font-size: 14px; color: #333; line-height: 1.5; }}
        .modal-buttons {{ display: flex; gap: 12px; }}
    </style>
</head>
<body>
<div class="container">
    <h1>Complete Your Order</h1>
    <div id="content" class="loading">Loading your order&#8230;</div>
</div>

<div class="modal-overlay" id="policyModal">
    <div class="modal">
        <h3>&#x26A0;&#xFE0F; Please Review Our Policies</h3>
        <p style="font-size:14px;color:#555;margin-bottom:12px;">By proceeding to payment you agree to the following terms:</p>
        <ul>
            <li><strong>No returns</strong> on assembled or installed cabinets.</li>
            <li><strong>20% restocking fee</strong> on returned undamaged items in original packaging.</li>
            <li>Damaged items must be noted on the <strong>delivery receipt</strong> and reported within <strong>48 hours</strong>.</li>
            <li>Buyer is responsible for <strong>verifying all measurements</strong> before ordering.</li>
            <li>Minor <strong>color variation</strong> between samples and production run is normal.</li>
            <li>Shipping quotes are estimates; final cost may vary for remote locations.</li>
        </ul>
        <div class="modal-buttons">
            <button class="btn btn-secondary" onclick="declinePolicy()">Decline</button>
            <button class="btn btn-primary" onclick="agreeAndPay()">I Agree &mdash; Proceed to Payment</button>
        </div>
    </div>
</div>

<script>
const ORDER_ID = "{order_id}";
const TOKEN   = "{token}";
const BASE    = window.location.origin;
let grandTotal = 0;
let confirmCommercialUrl = '';

async function boot() {{
    try {{
        const resp = await fetch(`${{BASE}}/checkout/${{ORDER_ID}}?token=${{TOKEN}}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Failed to load order');
        if (data.status === 'classification_needed') {{ renderStep1(data); }}
        else if (data.status === 'ok') {{ renderStep3(data); }}
        else {{ showError('Unexpected response from server.'); }}
    }} catch (err) {{ showError(err.message); }}
}}

function showError(msg) {{
    document.getElementById('content').innerHTML = `<div class="error">&#x26A0; ${{msg}}</div>`;
}}

function renderStep1(data) {{
    const addr = data.shipping_address || {{}};
    const street  = addr.address || addr.street || '';
    const street2 = addr.address2 || addr.street2 || '';
    const city    = addr.city || '';
    const state   = addr.state || '';
    const zip     = addr.zip || '';
    const addrFull = [street, street2, [city, state, zip].filter(Boolean).join(', ')].filter(Boolean).join('<br>');
    const preOpen = !data.address_initially_found;
    document.getElementById('content').innerHTML = `
        <div class="step-card">
            <h2>&#x1F4CD; Confirm Your Delivery Address</h2>
            <p>Please verify that the address below is where your cabinets should be delivered.</p>
            <div class="address-highlight">
                <div class="address-label-top">Delivery Address</div>
                ${{addrFull || '<em style="color:#999;">No address on file</em>'}}
            </div>
            ${{!data.address_initially_found ? '<p style="color:#b45309;font-weight:600;">&#x26A0; We could not find this address in our system. Please check it below.</p>' : ''}}
            <div class="btn-row">
                <button class="btn btn-primary" id="btnYes" onclick="step1Yes()">&#x2713;&nbsp; Yes, this is correct</button>
                <button class="btn btn-secondary" onclick="step1No()">&#x270E;&nbsp; No, I need to correct it</button>
            </div>
            <div class="edit-form${{preOpen ? ' open' : ''}}" id="editForm">
                <input type="text" id="eStreet" placeholder="Street address" value="${{street}}">
                <input type="text" id="eStreet2" placeholder="Apt / Suite (optional)" value="${{street2}}">
                <div class="form-row-3">
                    <input type="text" id="eCity" placeholder="City" value="${{city}}">
                    <input type="text" id="eState" placeholder="ST" maxlength="2" value="${{state}}">
                    <input type="text" id="eZip" placeholder="ZIP" maxlength="5" value="${{zip}}">
                </div>
                <div class="form-actions">
                    <button class="btn btn-primary" onclick="step1SaveCorrection()">Save &amp; Continue</button>
                    <button class="btn btn-secondary" onclick="cancelEdit()">Cancel</button>
                </div>
                <div id="editMsg"></div>
            </div>
        </div>`;
}}

function step1No() {{ document.getElementById('editForm').className = 'edit-form open'; }}
function cancelEdit() {{ document.getElementById('editForm').className = 'edit-form'; }}

async function step1Yes() {{
    const btn = document.getElementById('btnYes');
    btn.disabled = true; btn.textContent = 'Checking\u2026';
    await _submitConfirmAddress({{ address_is_correct: true }});
}}

async function step1SaveCorrection() {{
    const street = document.getElementById('eStreet').value.trim();
    const city   = document.getElementById('eCity').value.trim();
    const state  = document.getElementById('eState').value.trim().toUpperCase();
    const zip    = document.getElementById('eZip').value.trim();
    const street2 = (document.getElementById('eStreet2') || {{}}).value || '';
    if (!street || !city || !state || !zip) {{
        document.getElementById('editMsg').innerHTML = '<div class="msg-err">Please fill in street, city, state, and ZIP.</div>';
        return;
    }}
    document.getElementById('editMsg').innerHTML = '<div style="color:#666;font-size:13px;">Saving and verifying\u2026</div>';
    await _submitConfirmAddress({{ address_is_correct: false, street, street2, city, state, zip }});
}}

async function _submitConfirmAddress(payload) {{
    try {{
        const resp = await fetch(`${{BASE}}/checkout/${{ORDER_ID}}/confirm-address?token=${{TOKEN}}`,
            {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(payload) }});
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Error confirming address');
        if (data.need_classification) {{ renderStep2(); }} else {{ await loadStep3(); }}
    }} catch (err) {{ showError(err.message); }}
}}

const CLASSIFY_OPTIONS = [
    {{ value:'residential_existing', label:'Existing residential address', sub:'House, condo, townhome, apartment' }},
    {{ value:'commercial_existing', label:'Existing commercial address', sub:'Business, office, showroom, warehouse' }},
    {{ value:'residential_new_construction', label:'New residential construction site', sub:'Home currently being built' }},
    {{ value:'commercial_new_construction', label:'New commercial construction site', sub:'Business currently being built' }},
    {{ value:'rural', label:'Rural / farm / remote area', sub:'' }},
    {{ value:'military', label:'Military / APO / FPO address', sub:'' }},
];
let selectedType = null;

function renderStep2() {{
    const items = CLASSIFY_OPTIONS.map(o => `
        <label class="classify-item" id="cl_${{o.value}}" onclick="selectType('${{o.value}}')">
            <input type="radio" name="addrtype" value="${{o.value}}">
            <strong>${{o.label}}</strong>
            ${{o.sub ? `<span style="color:#718096;font-size:13px;margin-left:4px;">\u2014 ${{o.sub}}</span>` : ''}}
        </label>`).join('');
    document.getElementById('content').innerHTML = `
        <div class="step-card">
            <h2>&#x1F3E2; How would you classify this delivery location?</h2>
            <p>This helps us calculate the correct shipping rate.</p>
            <div class="classify-list">${{items}}</div>
            <div class="btn-row">
                <button class="btn btn-primary btn-full" id="btnClassify" onclick="submitClassify()" disabled>Continue &rarr;</button>
            </div>
            <div id="classifyMsg"></div>
        </div>`;
}}

function selectType(val) {{
    selectedType = val;
    CLASSIFY_OPTIONS.forEach(o => {{
        const el = document.getElementById('cl_' + o.value);
        if (el) el.className = 'classify-item' + (o.value === val ? ' selected' : '');
    }});
    const btn = document.getElementById('btnClassify');
    if (btn) btn.disabled = false;
}}

async function submitClassify() {{
    if (!selectedType) return;
    const btn = document.getElementById('btnClassify');
    btn.disabled = true; btn.textContent = 'Saving\u2026';
    try {{
        const resp = await fetch(`${{BASE}}/checkout/${{ORDER_ID}}/classify-address?token=${{TOKEN}}`,
            {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{ address_type: selectedType }}) }});
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Classification failed');
        await loadStep3();
    }} catch (err) {{ showError(err.message); }}
}}

async function loadStep3() {{
    try {{
        const resp = await fetch(`${{BASE}}/checkout/${{ORDER_ID}}?token=${{TOKEN}}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Failed to load checkout');
        renderStep3(data);
    }} catch (err) {{ showError(err.message); }}
}}

function fmtAddr(a) {{
    if (!a) return '';
    const s1 = a.street || a.address || '';
    const s2 = a.street2 || a.address2 || '';
    const csz = [a.city, a.state, a.zip].filter(Boolean).join(', ');
    return [s1, s2, csz].filter(Boolean).join('<br>');
}}

function renderStep3(data) {{
    const order   = data.order || {{}};
    const shipping = data.shipping || {{}};
    const shAddr  = data.shipping_address || {{}};
    const billAddr = data.billing_address;
    grandTotal    = shipping.grand_total || 0;
    confirmCommercialUrl = data.confirm_commercial_url || '';
    const displayName = order.company_name || order.customer_name || '';
    const shipStreet  = shAddr.address || shAddr.street || '';
    const shipStreet2 = shAddr.address2 || shAddr.street2 || '';
    const shipCity    = shAddr.city || '';
    const shipState   = shAddr.state || '';
    const shipZip     = shAddr.zip || '';

    const billHtml = billAddr ? `
        <div class="address-block">
            <div class="address-blabel">&#128184; Bill To</div>
            <div class="address-content"><strong>${{billAddr.company_name || displayName}}</strong><br>${{fmtAddr(billAddr)}}</div>
        </div>` : '';

    const shipHtml = `
        <div class="address-block ship-to">
            <div class="address-blabel">&#128230; Ship To \u2014 Delivery Address</div>
            <div class="address-content">
                <strong>${{displayName}}</strong><br>
                ${{shipStreet}}${{shipStreet2 ? '<br>' + shipStreet2 : ''}}<br>
                ${{[shipCity, shipState, shipZip].filter(Boolean).join(', ')}}
            </div>
        </div>`;

    let itemsHtml = '';
    (order.line_items || []).forEach(item => {{
        const price = parseFloat(item.price || 0);
        const qty   = parseInt(item.quantity || 1);
        itemsHtml += `<div class="item">
            <div class="item-sku">${{item.sku || ''}}</div>
            <div class="item-name">${{item.name || item.sku}}</div>
            <div class="item-qty">x${{qty}}</div>
            <div class="item-price">$${{(price * qty).toFixed(2)}}</div>
        </div>`;
    }});

    let shipmentsHtml = '';
    (shipping.shipments || []).forEach(ship => {{
        const ok = ship.quote && ship.quote.success;
        shipmentsHtml += `<div class="shipment">
            <div style="font-weight:600;margin-bottom:6px;">&#x1F4E6; From: ${{ship.warehouse_name}} (${{ship.origin_zip}})</div>
            <div style="font-size:13px;color:#666;">${{ship.items.length}} item(s) &middot; ${{ship.weight}} lbs</div>
            <div style="font-size:13px;color:#666;margin-top:6px;">
                ${{ok ? `<strong>Shipping: $${{ship.shipping_cost.toFixed(2)}}</strong>` : '<span style="color:#c00">Quote unavailable</span>'}}
            </div>
        </div>`;
    }});

    const isRes = data.is_residential !== false;
    const resNotice = isRes ? `
        <div class="residential-notice">
            <p><strong>&#x1F4CD; Delivery Address Classification</strong></p>
            <p>Your delivery address has been classified as a <strong>residential address</strong>. Residential deliveries include liftgate service at delivery.</p>
            <p>If this is actually a <strong>commercial address</strong> (business with a loading dock or forklift), <strong>do not pay this invoice</strong>.</p>
            <p style="margin-top:12px;">
                <a href="${{confirmCommercialUrl}}" target="_blank"
                   style="display:inline-block;background:#DC2626;color:white;padding:9px 20px;border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;">
                    This is a commercial address &rarr;
                </a>
            </p>
        </div>` : `
        <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:12px 16px;margin:16px 0;font-size:14px;color:#166534;">
            &#x1F3E2; Your delivery address has been classified as <strong>commercial</strong>. No liftgate surcharge applies.
        </div>`;

    const tariffPct = Math.round((shipping.tariff_rate || 0.08) * 100);
    document.getElementById('content').innerHTML = `
        <h2>Order #${{ORDER_ID}}</h2>
        <p style="color:#666;margin-bottom:16px;">${{displayName}}</p>
        <div class="address-row">${{billHtml}}${{shipHtml}}</div>
        <h2>Items</h2>${{itemsHtml}}
        <h2>Shipping</h2>${{shipmentsHtml}}
        ${{resNotice}}
        <div class="totals">
            <div class="total-row"><span>Items Subtotal</span><span>$${{shipping.total_items.toFixed(2)}}</span></div>
            <div class="total-row"><span>Tariff (${{tariffPct}}%)</span><span>$${{shipping.tariff_amount.toFixed(2)}}</span></div>
            <div class="total-row"><span>Shipping</span><span>$${{shipping.total_shipping.toFixed(2)}}</span></div>
            <div class="total-row grand"><span>Total Due</span><span>$${{shipping.grand_total.toFixed(2)}}</span></div>
        </div>
        <button class="btn btn-primary btn-full" onclick="showPolicyModal()" id="payBtn" style="margin-top:20px;">
            Pay $${{shipping.grand_total.toFixed(2)}} with Card
        </button>`;
}}

function showPolicyModal() {{ document.getElementById('policyModal').classList.add('active'); }}
function declinePolicy()   {{ document.getElementById('policyModal').classList.remove('active'); }}

async function agreeAndPay() {{
    document.getElementById('policyModal').classList.remove('active');
    const btn = document.getElementById('payBtn');
    btn.disabled = true; btn.textContent = 'Creating payment link\u2026';
    try {{
        const resp = await fetch(`${{BASE}}/checkout/${{ORDER_ID}}/create-payment?token=${{TOKEN}}`, {{ method: 'POST' }});
        const data = await resp.json();
        if (data.payment_url) {{ window.location.href = data.payment_url; }}
        else throw new Error(data.detail || 'Failed to create payment');
    }} catch (err) {{
        alert('Payment error: ' + err.message);
        btn.disabled = false;
        btn.textContent = `Pay $${{grandTotal.toFixed(2)}} with Card`;
    }}
}}

boot();
</script>
</body>
</html>"""

    return HTMLResponse(content=html)
