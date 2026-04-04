"""
checkout_routes.py
FastAPI router for B2BWave checkout flow.

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
WAREHOUSE_NOTIFICATION_EMAIL = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL", "cabinetsforcontractors@gmail.com").strip()

checkout_router = APIRouter(tags=["checkout"])


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
                    "items": [{"sku": i.get("sku"), "name": i.get("product_name"), "qty": i.get("quantity")} for i in items],
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
        shipping_address = order_data.get("shipping_address") or order_data.get("delivery_address") or {}
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

    Trigger 1: Generates checkout token, stores in DB, calculates shipping,
    then emails the customer their payment link + PDF invoice.
    Also sends internal notification to CFC with order summary.
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

    email_result = None
    shipping_result = None

    if customer_email:
        try:
            from email_sender import send_order_email
            order_data = fetch_b2bwave_order(str(order_id))
            if order_data:
                # Calculate shipping so it's available for the invoice
                shipping_address = order_data.get("shipping_address") or {}
                try:
                    shipping_result = calculate_order_shipping(order_data, shipping_address)
                except Exception as se:
                    print(f"[WEBHOOK] Shipping calc failed for order {order_id}: {se}")
                    shipping_result = None

                order_data['payment_link'] = checkout_url
                order_data['shipping_result'] = shipping_result  # for PDF + template

                # Send invoice email with PDF to customer
                email_result = send_order_email(
                    order_id=str(order_id),
                    template_id='payment_link',
                    to_email=customer_email,
                    order_data=order_data,
                    triggered_by='b2bwave_webhook'
                )
                print(f"[WEBHOOK] Invoice email order {order_id}: success={email_result.get('success')}, pdf={email_result.get('pdf_attached')}")

                # Internal notification to CFC
                _send_internal_order_notification(order_id, order_data, shipping_result, checkout_url)

            else:
                print(f"[WEBHOOK] Could not fetch B2BWave order {order_id} — skipping email")
        except Exception as e:
            print(f"[WEBHOOK] Email send failed for order {order_id}: {e}")
            email_result = {'success': False, 'error': str(e)}

    return {
        "status": "ok",
        "order_id": order_id,
        "checkout_url": checkout_url,
        "message": "Checkout link generated",
        "email_sent": email_result.get('success') if email_result else False,
        "pdf_attached": email_result.get('pdf_attached') if email_result else False,
    }


def _send_internal_order_notification(order_id: str, order_data: dict, shipping_result: dict, checkout_url: str):
    """Send internal notification to CFC when a new order arrives."""
    try:
        from gmail_sync import get_gmail_access_token
        import json
        import base64
        import urllib.request
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        token = get_gmail_access_token()
        if not token:
            return

        customer = order_data.get('customer_name', 'Unknown')
        company = order_data.get('company_name', '')
        order_date = order_data.get('order_date', '')
        line_items = order_data.get('line_items', [])
        grand_total = shipping_result.get('grand_total', 0) if shipping_result else 0

        items_text = "\n".join(
            f"  {item.get('sku', '')} — {item.get('name', '')} x{item.get('quantity', 1)} @ ${item.get('price', 0):.2f}"
            for item in line_items
        )

        body_text = f"""New Order #{order_id}

Customer: {customer}{f' ({company})' if company else ''}
Date: {order_date}

Items:
{items_text}

Total Due: ${grand_total:,.2f}

Invoice sent to: {order_data.get('customer_email', '')}
Checkout URL: {checkout_url}
"""

        msg = MIMEMultipart("alternative")
        msg["From"] = "william@cabinetsforcontractors.net"
        msg["To"] = WAREHOUSE_NOTIFICATION_EMAIL
        msg["Subject"] = f"New Order #{order_id} — {customer}{f' ({company})' if company else ''} — ${grand_total:,.2f}"
        msg.attach(MIMEText(body_text, "plain"))

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
            print(f"[WEBHOOK] Internal notification sent for order {order_id}")

    except Exception as e:
        print(f"[WEBHOOK] Internal notification failed for order {order_id}: {e}")


# =============================================================================
# CHECKOUT FLOW  (token-gated)
# =============================================================================

@checkout_router.get("/checkout/payment-complete")
def payment_complete(order: str, transactionId: Optional[str] = None):
    """Payment completion callback from Square."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pending_checkouts SET payment_completed_at = NOW(), transaction_id = %s WHERE order_id = %s",
                (transactionId, order),
            )
            cur.execute(
                """UPDATE orders SET payment_received = TRUE, payment_received_at = NOW(),
                   payment_method = 'Square Checkout', updated_at = NOW() WHERE order_id = %s""",
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

    shipping_address = order_data.get("shipping_address") or order_data.get("delivery_address") or {}
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
            "subtotal": order_data.get("order_total", 0),
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

    shipping_address = order_data.get("shipping_address") or order_data.get("delivery_address") or {}
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
                "UPDATE pending_checkouts SET payment_link = %s, payment_amount = %s, payment_initiated_at = NOW() WHERE order_id = %s",
                (payment_url, grand_total, order_id),
            )

    return {"status": "ok", "payment_url": payment_url, "amount": grand_total}


# =============================================================================
# CHECKOUT UI  (serves HTML page with policy agreement popup)
# =============================================================================

@checkout_router.get("/checkout-ui/{order_id}")
def checkout_ui(order_id: str, token: str):
    """Serve the checkout page HTML with policy agreement popup."""
    if not verify_checkout_token(order_id, token):
        return HTMLResponse(content="<h1>Invalid or expired checkout link</h1>", status_code=403)

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Complete Your Order - CFC</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 30px; }}
        h1 {{ color: #1a365d; margin-bottom: 20px; }}
        h2 {{ color: #555; font-size: 18px; margin: 20px 0 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        .loading {{ text-align: center; padding: 40px; color: #666; }}
        .error {{ background: #fee; color: #c00; padding: 15px; border-radius: 4px; margin: 20px 0; }}
        .item {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
        .item-sku {{ width: 110px; font-family: monospace; color: #718096; font-size: 12px; }}
        .item-name {{ flex: 1; padding: 0 8px; }}
        .item-qty {{ width: 50px; text-align: center; color: #666; }}
        .item-price {{ width: 90px; text-align: right; font-weight: 500; }}
        .shipment {{ background: #f9f9f9; padding: 15px; border-radius: 4px; margin: 10px 0; }}
        .shipment-header {{ font-weight: 600; color: #333; margin-bottom: 6px; }}
        .shipment-detail {{ font-size: 13px; color: #666; }}
        .totals {{ margin-top: 16px; padding-top: 16px; border-top: 1px solid #ddd; }}
        .total-row {{ display: flex; justify-content: space-between; padding: 7px 0; font-size: 14px; color: #555; }}
        .total-row.grand {{ font-size: 20px; font-weight: 700; color: #1a365d; border-top: 2px solid #1a365d; margin-top: 8px; padding-top: 12px; }}
        .pay-button {{ display: block; width: 100%; background: #2563eb; color: white; padding: 15px; border: none; border-radius: 6px; font-size: 18px; font-weight: 700; cursor: pointer; margin-top: 20px; }}
        .pay-button:hover {{ background: #1d4ed8; }}
        .pay-button:disabled {{ background: #ccc; cursor: not-allowed; }}
        /* Policy modal */
        .modal-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 1000; align-items: center; justify-content: center; }}
        .modal-overlay.active {{ display: flex; }}
        .modal {{ background: white; border-radius: 8px; padding: 28px; max-width: 520px; width: 90%; max-height: 85vh; overflow-y: auto; }}
        .modal h3 {{ color: #1a365d; margin-bottom: 16px; font-size: 18px; }}
        .modal ul {{ margin: 12px 0 20px 20px; }}
        .modal ul li {{ margin-bottom: 10px; font-size: 14px; color: #333; line-height: 1.5; }}
        .modal-buttons {{ display: flex; gap: 12px; }}
        .btn-agree {{ flex: 1; background: #2563eb; color: white; border: none; padding: 12px; border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer; }}
        .btn-decline {{ flex: 1; background: #f1f5f9; color: #555; border: none; padding: 12px; border-radius: 6px; font-size: 15px; cursor: pointer; }}
        .residential-note {{ background: #fff3cd; padding: 10px; border-radius: 4px; margin: 10px 0; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Complete Your Order</h1>
        <div id="content" class="loading">Loading order details...</div>
    </div>

    <!-- Policy Agreement Modal -->
    <div class="modal-overlay" id="policyModal">
        <div class="modal">
            <h3>⚠️ Please Review Our Policies</h3>
            <p style="font-size:14px;color:#555;margin-bottom:12px;">By proceeding to payment you agree to the following terms:</p>
            <ul>
                <li><strong>No returns on assembled or installed cabinets.</strong></li>
                <li><strong>20% restocking fee</strong> on returned undamaged items in original packaging.</li>
                <li>Damaged items must be noted on the <strong>delivery receipt</strong> and reported within <strong>48 hours</strong> of delivery.</li>
                <li>Buyer is responsible for <strong>verifying all measurements</strong> before ordering — incorrect sizing is not grounds for return.</li>
                <li>Minor <strong>color variation</strong> between door samples and production run is normal.</li>
                <li>Shipping quotes are estimates; final shipping cost may vary for remote locations.</li>
            </ul>
            <div class="modal-buttons">
                <button class="btn-decline" onclick="declinePolicy()">Decline</button>
                <button class="btn-agree" onclick="agreeAndPay()">I Agree — Proceed to Payment</button>
            </div>
        </div>
    </div>

    <script>
        const ORDER_ID = "{order_id}";
        const TOKEN = "{token}";
        const API_BASE = window.location.origin;
        let grandTotal = 0;

        async function loadCheckout() {{
            try {{
                const resp = await fetch(`${{API_BASE}}/checkout/${{ORDER_ID}}?token=${{TOKEN}}`);
                const data = await resp.json();
                if (data.status !== 'ok') throw new Error(data.detail || 'Failed to load order');
                renderCheckout(data);
            }} catch (err) {{
                document.getElementById('content').innerHTML = `<div class="error">Error: ${{err.message}}</div>`;
            }}
        }}

        function renderCheckout(data) {{
            const order = data.order;
            const shipping = data.shipping;
            grandTotal = shipping.grand_total || 0;

            let html = `<h2>Order #${{ORDER_ID}}</h2>
                <p style="color:#666;margin-bottom:20px;">
                    ${{order.customer_name || ''}}
                    ${{order.company_name ? ' (' + order.company_name + ')' : ''}}
                </p><h2>Items</h2>`;

            (order.line_items || []).forEach(item => {{
                const price = parseFloat(item.price || 0);
                const qty = parseInt(item.quantity || 1);
                html += `<div class="item">
                    <div class="item-sku">${{item.sku || ''}}</div>
                    <div class="item-name">${{item.name || item.sku}}</div>
                    <div class="item-qty">x${{qty}}</div>
                    <div class="item-price">$${{(price * qty).toFixed(2)}}</div>
                </div>`;
            }});

            html += `<h2>Shipping</h2>`;
            if (shipping.shipments && shipping.shipments.length > 0) {{
                shipping.shipments.forEach(ship => {{
                    const quoteOk = ship.quote && ship.quote.success;
                    html += `<div class="shipment">
                        <div class="shipment-header">📦 From: ${{ship.warehouse_name}} (${{ship.origin_zip}})</div>
                        <div class="shipment-detail">${{ship.items.length}} item(s) · ${{ship.weight}} lbs</div>
                        <div class="shipment-detail" style="margin-top:6px;">
                            ${{quoteOk ? `<strong>Shipping: $${{ship.shipping_cost.toFixed(2)}}</strong>` : `<span style="color:#c00">Quote unavailable</span>`}}
                        </div>
                    </div>`;
                }});
                if (shipping.shipments.some(s => s.shipping_method === 'ltl')) {{
                    html += `<div class="residential-note">🏠 Residential delivery includes liftgate service</div>`;
                }}
            }}

            const tariffPct = Math.round((shipping.tariff_rate || 0.08) * 100);
            html += `<div class="totals">
                <div class="total-row"><span>Items Subtotal</span><span>$${{shipping.total_items.toFixed(2)}}</span></div>
                <div class="total-row"><span>Tariff (${{tariffPct}}%)</span><span>$${{shipping.tariff_amount.toFixed(2)}}</span></div>
                <div class="total-row"><span>Shipping</span><span>$${{shipping.total_shipping.toFixed(2)}}</span></div>
                <div class="total-row grand"><span>Total Due</span><span>$${{shipping.grand_total.toFixed(2)}}</span></div>
            </div>
            <button class="pay-button" onclick="showPolicyModal()" id="payBtn">
                Pay $${{shipping.grand_total.toFixed(2)}} with Card
            </button>`;

            document.getElementById('content').innerHTML = html;
        }}

        function showPolicyModal() {{
            document.getElementById('policyModal').classList.add('active');
        }}

        function declinePolicy() {{
            document.getElementById('policyModal').classList.remove('active');
        }}

        async function agreeAndPay() {{
            document.getElementById('policyModal').classList.remove('active');
            const btn = document.getElementById('payBtn');
            btn.disabled = true;
            btn.textContent = 'Creating payment link...';
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
                btn.disabled = false;
                btn.textContent = `Pay $${{grandTotal.toFixed(2)}} with Card`;
            }}
        }}

        loadCheckout();
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)
