"""
checkout_routes.py
FastAPI router for B2BWave checkout flow.

Phase 5B: Extracted from main.py

Public endpoints:
    POST /webhook/b2bwave-order         — B2BWave webhook (order placed)
    GET  /checkout/payment-complete     — Square payment callback
    GET  /checkout/{order_id}           — get checkout data (token-gated)
    POST /checkout/{order_id}/create-payment — create Square payment link
    GET  /checkout-ui/{order_id}        — serve checkout HTML page

Admin-only endpoints:
    GET  /checkout-status               — config debug
    GET  /debug/b2bwave-raw/{order_id}  — raw B2BWave response
    GET  /debug/warehouse-routing/{order_id} — warehouse routing debug
    GET  /debug/test-checkout/{order_id} — full checkout flow dry run

Mount in main.py with:
    from checkout_routes import checkout_router
    app.include_router(checkout_router)
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
        create_square_payment_link,
        generate_checkout_token,
        verify_checkout_token,
        WAREHOUSES,
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

checkout_router = APIRouter(tags=["checkout"])


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class CheckoutRequest(BaseModel):
    order_id: str
    shipping_address: Optional[dict] = None


# =============================================================================
# DEBUG / CONFIG  (admin-gated)
# =============================================================================

@checkout_router.get("/checkout-status")
def checkout_status(_: bool = Depends(require_admin)):
    """Debug endpoint to check checkout configuration."""
    try:
        from checkout import (
            B2BWAVE_URL as _URL,
            B2BWAVE_USERNAME as _USER,
            B2BWAVE_API_KEY as _KEY,
        )
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
    """Return raw B2BWave API response for an order."""
    if not SYNC_SERVICE_LOADED:
        return {"status": "error", "error": "sync_service not loaded"}
    try:
        data = b2bwave_api_request("orders", {"id_eq": order_id})
        return {"status": "ok", "raw_response": data}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@checkout_router.get("/debug/warehouse-routing/{order_id}")
def debug_warehouse_routing(order_id: str, _: bool = Depends(require_admin)):
    """Debug endpoint to test warehouse routing for an order."""
    if not CHECKOUT_ENABLED:
        return {"status": "error", "error": "checkout module not loaded"}
    try:
        from checkout import group_items_by_warehouse, get_warehouse_for_sku, WAREHOUSES as WH

        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "error": "Order not found in B2BWave"}

        line_items = order_data.get("line_items", [])
        item_routing = [
            {
                "sku": item.get("sku", ""),
                "name": item.get("product_name", ""),
                "qty": item.get("quantity", 0),
                "warehouse": get_warehouse_for_sku(item.get("sku", "")),
                "warehouse_info": WH.get(get_warehouse_for_sku(item.get("sku", "")), {}),
            }
            for item in line_items
        ]

        warehouse_groups = group_items_by_warehouse(line_items)

        return {
            "status": "ok",
            "order_id": order_id,
            "customer": order_data.get("customer_name", ""),
            "total_items": len(line_items),
            "item_routing": item_routing,
            "warehouse_groups": {
                wh: {
                    "warehouse_info": WH.get(wh, {}),
                    "item_count": len(items),
                    "items": [
                        {"sku": i.get("sku"), "name": i.get("product_name"), "qty": i.get("quantity")}
                        for i in items
                    ],
                }
                for wh, items in warehouse_groups.items()
            },
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


@checkout_router.get("/debug/test-checkout/{order_id}")
def debug_test_checkout(order_id: str, _: bool = Depends(require_admin)):
    """Dry-run the full checkout flow without triggering a webhook."""
    if not CHECKOUT_ENABLED:
        return {"status": "error", "error": "checkout module not loaded"}
    try:
        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "error": "Order not found in B2BWave"}

        token = generate_checkout_token(order_id)
        shipping_address = (
            order_data.get("shipping_address")
            or order_data.get("delivery_address")
            or {}
        )
        shipping_result = calculate_order_shipping(order_data, shipping_address)
        base = CHECKOUT_BASE_URL or "https://cfcorderbackend-sandbox.onrender.com"
        checkout_url = f"{base}/checkout-ui/{order_id}?token={token}"
        return {
            "status": "ok",
            "order_id": order_id,
            "customer": order_data.get("customer_name"),
            "customer_email": order_data.get("customer_email"),
            "token": token,
            "checkout_url": checkout_url,
            "api_url": f"{base}/checkout/{order_id}?token={token}",
            "destination": shipping_address,
            "shipping": shipping_result,
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


# =============================================================================
# WEBHOOK  (public — called by B2BWave)
# =============================================================================

@checkout_router.post("/webhook/b2bwave-order")
def b2bwave_order_webhook(payload: dict):
    """
    Webhook endpoint for B2BWave — triggered when an order is placed.

    Trigger 1: Generates checkout token, stores in DB, then emails the customer
    their payment link using the payment_link template.
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
                    customer_email  = EXCLUDED.customer_email,
                    checkout_token  = EXCLUDED.checkout_token,
                    created_at      = NOW()
                """,
                (str(order_id), customer_email, token),
            )

    # Trigger 1: Email payment link to customer
    email_result = None
    if customer_email:
        try:
            from email_sender import send_order_email
            order_data = fetch_b2bwave_order(str(order_id))
            if order_data:
                order_data['payment_link'] = checkout_url
                email_result = send_order_email(
                    order_id=str(order_id),
                    template_id='payment_link',
                    to_email=customer_email,
                    order_data=order_data,
                    triggered_by='b2bwave_webhook'
                )
                print(f"[WEBHOOK] Payment link email sent to {customer_email} for order {order_id}: {email_result.get('success')}")
            else:
                print(f"[WEBHOOK] Could not fetch B2BWave order {order_id} for email — skipping")
        except Exception as e:
            print(f"[WEBHOOK] Email send failed for order {order_id}: {e}")
            email_result = {'success': False, 'error': str(e)}

    return {
        "status": "ok",
        "order_id": order_id,
        "checkout_url": checkout_url,
        "message": "Checkout link generated",
        "email_sent": email_result.get('success') if email_result else False,
    }


# =============================================================================
# CHECKOUT FLOW  (token-gated, public-ish)
# =============================================================================

@checkout_router.get("/checkout/payment-complete")
def payment_complete(order: str, transactionId: Optional[str] = None):
    """Payment completion callback from Square."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_checkouts
                SET payment_completed_at = NOW(), transaction_id = %s
                WHERE order_id = %s
                """,
                (transactionId, order),
            )
            cur.execute(
                """
                UPDATE orders SET
                    payment_received    = TRUE,
                    payment_received_at = NOW(),
                    payment_method      = 'Square Checkout',
                    updated_at          = NOW()
                WHERE order_id = %s
                """,
                (order,),
            )
    return {"status": "ok", "message": "Payment completed", "order_id": order}


@checkout_router.get("/checkout/{order_id}")
def get_checkout_data(order_id: str, token: str):
    """Get checkout page data — order details with shipping quotes."""
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid or expired checkout link")

    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")

    shipping_address = (
        order_data.get("shipping_address") or order_data.get("delivery_address") or {}
    )
    shipping_result = calculate_order_shipping(order_data, shipping_address)

    return {
        "status": "ok",
        "order_id": order_id,
        "order": {
            "id": order_id,
            "customer_name": order_data.get("customer_name"),
            "customer_email": order_data.get("customer_email"),
            "company_name": order_data.get("company_name"),
            "line_items": order_data.get("line_items", []),
            "subtotal": order_data.get("subtotal") or order_data.get("total_price"),
        },
        "shipping": shipping_result,
        "payment_ready": shipping_result.get("grand_total", 0) > 0,
    }


@checkout_router.post("/checkout/{order_id}/create-payment")
def create_checkout_payment(order_id: str, token: str):
    """Create Square payment link for the order."""
    if not CHECKOUT_ENABLED:
        raise HTTPException(status_code=503, detail="Checkout not enabled")
    if not verify_checkout_token(order_id, token):
        raise HTTPException(status_code=403, detail="Invalid checkout token")

    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found")

    shipping_address = (
        order_data.get("shipping_address") or order_data.get("delivery_address") or {}
    )
    shipping_result = calculate_order_shipping(order_data, shipping_address)
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
                """
                UPDATE pending_checkouts
                SET payment_link         = %s,
                    payment_amount       = %s,
                    payment_initiated_at = NOW()
                WHERE order_id = %s
                """,
                (payment_url, grand_total, order_id),
            )

    return {"status": "ok", "payment_url": payment_url, "amount": grand_total}


# =============================================================================
# CHECKOUT UI  (serves HTML page)
# =============================================================================

@checkout_router.get("/checkout-ui/{order_id}")
def checkout_ui(order_id: str, token: str):
    """Serve the checkout page HTML."""
    if not verify_checkout_token(order_id, token):
        return HTMLResponse(
            content="<h1>Invalid or expired checkout link</h1>", status_code=403
        )

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Complete Your Order - CFC</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white;
                      border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 30px; }}
        h1 {{ color: #333; margin-bottom: 20px; }}
        h2 {{ color: #555; font-size: 18px; margin: 20px 0 10px;
              border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        .loading {{ text-align: center; padding: 40px; color: #666; }}
        .error {{ background: #fee; color: #c00; padding: 15px; border-radius: 4px; margin: 20px 0; }}
        .item {{ display: flex; justify-content: space-between; padding: 10px 0;
                 border-bottom: 1px solid #f0f0f0; }}
        .item-name {{ flex: 1; }}
        .item-qty {{ width: 60px; text-align: center; color: #666; }}
        .item-price {{ width: 100px; text-align: right; font-weight: 500; }}
        .shipment {{ background: #f9f9f9; padding: 15px; border-radius: 4px; margin: 10px 0; }}
        .shipment-header {{ font-weight: 600; color: #333; margin-bottom: 10px; }}
        .shipment-detail {{ font-size: 14px; color: #666; }}
        .totals {{ margin-top: 20px; padding-top: 20px; border-top: 2px solid #333; }}
        .total-row {{ display: flex; justify-content: space-between; padding: 8px 0; }}
        .total-row.grand {{ font-size: 20px; font-weight: 700; color: #333; }}
        .pay-button {{ display: block; width: 100%; background: #0066cc; color: white;
                       padding: 15px; border: none; border-radius: 4px;
                       font-size: 18px; cursor: pointer; margin-top: 20px; }}
        .pay-button:hover {{ background: #0055aa; }}
        .pay-button:disabled {{ background: #ccc; cursor: not-allowed; }}
        .residential-note {{ background: #fff3cd; padding: 10px; border-radius: 4px;
                              margin: 10px 0; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Complete Your Order</h1>
        <div id="content" class="loading">Loading order details...</div>
    </div>
    <script>
        const ORDER_ID = "{order_id}";
        const TOKEN = "{token}";
        const API_BASE = window.location.origin;
        async function loadCheckout() {{
            try {{
                const resp = await fetch(`${{API_BASE}}/checkout/${{ORDER_ID}}?token=${{TOKEN}}`);
                const data = await resp.json();
                if (data.status !== 'ok') throw new Error(data.detail || 'Failed to load order');
                renderCheckout(data);
            }} catch (err) {{
                document.getElementById('content').innerHTML =
                    `<div class="error">Error: ${{err.message}}</div>`;
            }}
        }}
        function renderCheckout(data) {{
            const order = data.order;
            const shipping = data.shipping;
            let html = `<h2>Order #${{ORDER_ID}}</h2>
                <p style="color:#666;margin-bottom:20px;">
                    ${{order.customer_name || ''}}
                    ${{order.company_name ? '(' + order.company_name + ')' : ''}}
                </p><h2>Items</h2>`;
            (order.line_items || []).forEach(item => {{
                const price = parseFloat(item.price || item.unit_price || 0);
                const qty = parseInt(item.quantity || 1);
                html += `<div class="item">
                    <div class="item-name">${{item.name || item.product_name || item.sku}}</div>
                    <div class="item-qty">x${{qty}}</div>
                    <div class="item-price">${{(price * qty).toFixed(2)}}</div>
                </div>`;
            }});
            html += `<h2>Shipping</h2>`;
            if (shipping.shipments && shipping.shipments.length > 0) {{
                shipping.shipments.forEach(ship => {{
                    const quoteOk = ship.quote && ship.quote.success;
                    html += `<div class="shipment">
                        <div class="shipment-header">📦 From: ${{ship.warehouse_name}} (${{ship.origin_zip}})</div>
                        <div class="shipment-detail">${{ship.items.length}} item(s) · ${{ship.weight}} lbs</div>
                        <div class="shipment-detail" style="margin-top:8px;">
                            ${{quoteOk
                                ? `<strong>Shipping: $${{ship.shipping_cost.toFixed(2)}}</strong>`
                                : `<span style="color:#c00">Quote unavailable</span>`}}
                        </div>
                    </div>`;
                }});
                if (shipping.shipments.some(s => s.shipping_method === 'ltl')) {{
                    html += `<div class="residential-note">🏠 Residential delivery includes liftgate service</div>`;
                }}
            }}
            html += `<div class="totals">
                <div class="total-row"><span>Items Subtotal</span><span>$${{shipping.total_items.toFixed(2)}}</span></div>
                <div class="total-row"><span>Shipping</span><span>$${{shipping.total_shipping.toFixed(2)}}</span></div>
                <div class="total-row grand"><span>Total</span><span>$${{shipping.grand_total.toFixed(2)}}</span></div>
            </div>
            <button class="pay-button" onclick="initiatePayment()" id="payBtn">
                Pay $${{shipping.grand_total.toFixed(2)}} with Card
            </button>`;
            document.getElementById('content').innerHTML = html;
        }}
        async function initiatePayment() {{
            const btn = document.getElementById('payBtn');
            btn.disabled = true; btn.textContent = 'Creating payment link...';
            try {{
                const resp = await fetch(
                    `${{API_BASE}}/checkout/${{ORDER_ID}}/create-payment?token=${{TOKEN}}`,
                    {{method: 'POST'}}
                );
                const data = await resp.json();
                if (data.payment_url) {{ window.location.href = data.payment_url; }}
                else {{ throw new Error(data.detail || 'Failed to create payment'); }}
            }} catch (err) {{
                alert('Payment error: ' + err.message);
                btn.disabled = false; btn.textContent = 'Pay with Card';
            }}
        }}
        loadCheckout();
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)
