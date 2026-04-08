"""
quote_routes.py
FastAPI router for B2BWave quote management and abandoned cart nudges.

Endpoints:
    POST /quotes/check-all           -- Cron: check for new/updated quotes + abandoned carts
    POST /quotes/{order_id}/send     -- Admin: manually send/resend a quote email
    GET  /quotes/list                -- List all B2BWave admin quotes
    GET  /quotes/abandoned-carts     -- List all B2BWave abandoned carts
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from auth import require_admin
from config import B2BWAVE_URL

quote_router = APIRouter(prefix="/quotes", tags=["quotes"])


@quote_router.post("/check-all")
def check_all_quotes(_: bool = Depends(require_admin)):
    """CRON: Check for new/updated B2BWave quotes and send quote emails."""
    from quote_engine import check_and_send_quotes, check_abandoned_carts
    quote_result = check_and_send_quotes()
    cart_result = check_abandoned_carts()
    return {"status": "ok", "quotes": quote_result, "abandoned_carts": cart_result}


@quote_router.post("/{order_id}/send")
def send_quote_manual(order_id: str, _: bool = Depends(require_admin)):
    """Admin: manually send or resend a quote email for a B2BWave order."""
    from checkout import fetch_b2bwave_order, generate_checkout_token, calculate_order_shipping
    from quote_engine import send_quote_email

    order_data = fetch_b2bwave_order(order_id)
    if not order_data:
        raise HTTPException(status_code=404, detail="Order not found in B2BWave")

    base = os.environ.get("CHECKOUT_BASE_URL", "").strip()
    token = generate_checkout_token(order_id, long_lived=True)
    checkout_url = f"{base}/checkout-ui/{order_id}?token={token}&view=quote"

    shipping_address = order_data.get("shipping_address") or {}
    try:
        shipping_result = calculate_order_shipping(order_data, shipping_address)
    except Exception:
        shipping_result = {}
    order_data["shipping_result"] = shipping_result
    order_data["payment_link"] = checkout_url
    order_data["b2bwave_portal_url"] = f"{B2BWAVE_URL}/customer/orders"

    success = send_quote_email(order_id, order_data, checkout_url)
    return {"status": "ok", "email_sent": success, "quote_url": checkout_url}


@quote_router.get("/list")
def list_quotes(_: bool = Depends(require_admin)):
    """List all B2BWave admin quotes (status 1, submitted_by_class=User)."""
    from quote_engine import fetch_b2bwave_temporary_orders, is_admin_quote
    orders = fetch_b2bwave_temporary_orders()
    quotes = [
        {
            "order_id": str(o.get("id", "")),
            "customer_name": o.get("customer_name", ""),
            "company_name": o.get("customer_company", ""),
            "email": o.get("customer_email", ""),
            "gross_total": o.get("gross_total", "0"),
            "updated_at": o.get("updated_at", ""),
            "products_count": len(o.get("order_products", [])),
        }
        for o in orders if is_admin_quote(o)
    ]
    return {"status": "ok", "count": len(quotes), "quotes": quotes}


@quote_router.get("/abandoned-carts")
def list_abandoned_carts(_: bool = Depends(require_admin)):
    """List all B2BWave abandoned carts (status 1, Customer, no submitted_at)."""
    from quote_engine import fetch_b2bwave_temporary_orders, is_abandoned_cart
    from business_days import business_days_since
    from datetime import datetime

    orders = fetch_b2bwave_temporary_orders()
    carts = []
    for o in orders:
        if is_abandoned_cart(o):
            updated = o.get("updated_at") or o.get("created_at", "")
            age = 0
            if updated:
                try:
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    age = business_days_since(dt)
                except Exception:
                    pass
            carts.append({
                "order_id": str(o.get("id", "")),
                "customer_name": o.get("customer_name", ""),
                "company_name": o.get("customer_company", ""),
                "email": o.get("customer_email", ""),
                "gross_total": o.get("gross_total", "0"),
                "updated_at": updated,
                "age_business_days": age,
            })
    return {"status": "ok", "count": len(carts), "abandoned_carts": carts}
