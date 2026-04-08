"""
quote_engine.py
CFC Orders Quote Engine -- B2BWave quote detection, email sending, abandoned cart nudges.

B2BWave quotes are orders with status_order_id=1 ("Temporary").
- Admin quotes: submitted_by_class == "User" (William created)
- Customer carts: submitted_by_class == "Customer", submitted_at is None

Uses business_days_since() for all timer calculations.
"""

import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db_helpers import get_db
from psycopg2.extras import RealDictCursor
from business_days import business_days_since
from config import B2BWAVE_URL, B2BWAVE_USERNAME, B2BWAVE_API_KEY

CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "https://cfcorderbackend-sandbox.onrender.com").strip()


# =============================================================================
# B2BWAVE QUOTE FETCHING
# =============================================================================

def fetch_b2bwave_temporary_orders() -> List[Dict]:
    """Fetch all status 1 ('Temporary') orders from B2BWave."""
    if not B2BWAVE_URL or not B2BWAVE_API_KEY:
        return []
    try:
        url = f"{B2BWAVE_URL}/api/orders.json?status_order_id_eq=1"
        creds = base64.b64encode(f"{B2BWAVE_USERNAME}:{B2BWAVE_API_KEY}".encode()).decode()
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {creds}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        orders = []
        if isinstance(data, list):
            for item in data:
                order = item.get("order", item)
                orders.append(order)
        return orders
    except Exception as e:
        print(f"[QUOTE] Error fetching B2BWave temporary orders: {e}")
        return []


# =============================================================================
# CLASSIFICATION
# =============================================================================

def is_admin_quote(order: dict) -> bool:
    """Returns True if the order was created by admin (William)."""
    submitted_by = order.get("submitted_by", {})
    if isinstance(submitted_by, dict):
        return order.get("submitted_by_class") == "User"
    return False


def is_abandoned_cart(order: dict) -> bool:
    """Returns True if the order is a customer-created cart with no submission."""
    return (
        order.get("submitted_by_class") == "Customer"
        and order.get("submitted_at") is None
    )


# =============================================================================
# ENSURE PENDING CHECKOUT ROW
# =============================================================================

def _ensure_pending_checkout(order_id: str, email: str, token: str):
    """Ensure a pending_checkouts row exists for this order."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO pending_checkouts (order_id, customer_email, checkout_token, created_at)
                       VALUES (%s, %s, %s, NOW())
                       ON CONFLICT (order_id) DO NOTHING""",
                    (order_id, email, token),
                )
    except Exception as e:
        print(f"[QUOTE] Error ensuring pending_checkout for {order_id}: {e}")


# =============================================================================
# QUOTE EMAIL SEND
# =============================================================================

def send_quote_email(order_id: str, order_data: dict, checkout_url: str) -> bool:
    """Send a quote email for a B2BWave order. Returns True if sent."""
    try:
        from checkout_routes import _get_gmail_token, _send_gmail_message
        from checkout import generate_checkout_token
        from email_templates import render_template, get_template_subject

        customer_email = order_data.get("customer_email") or order_data.get("email", "")
        if not customer_email:
            print(f"[QUOTE] No email for order {order_id}")
            return False

        token = _get_gmail_token()
        if not token:
            print(f"[QUOTE] No Gmail token available")
            return False

        # Ensure pending_checkout row
        checkout_token = generate_checkout_token(order_id, long_lived=True)
        _ensure_pending_checkout(order_id, customer_email, checkout_token)

        # Render template
        order_data["order_id"] = order_id
        order_data["payment_link"] = checkout_url
        order_data["b2bwave_portal_url"] = f"{B2BWAVE_URL}/customer/orders"
        html = render_template("quote_email", order_data)
        if not html:
            print(f"[QUOTE] Template quote_email not found")
            return False

        subject = get_template_subject("quote_email", order_data)
        _send_gmail_message(token, customer_email, subject, html)

        # Update tracking columns
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE pending_checkouts
                       SET is_quote = TRUE,
                           quote_sent_at = NOW(),
                           quote_email_count = COALESCE(quote_email_count, 0) + 1,
                           checkout_token = %s
                       WHERE order_id = %s""",
                    (checkout_token, order_id),
                )
                cur.execute(
                    """INSERT INTO order_events (order_id, event_type, event_data, source)
                       VALUES (%s, 'quote_email_sent', %s, 'quote_engine')""",
                    (order_id, json.dumps({"email": customer_email, "url": checkout_url})),
                )

        print(f"[QUOTE] Quote email sent to {customer_email} for order {order_id}")
        return True
    except Exception as e:
        print(f"[QUOTE] Failed to send quote email for {order_id}: {e}")
        return False


# =============================================================================
# ABANDONED CART NUDGE
# =============================================================================

def send_abandoned_cart_nudge(order_id: str, order_data: dict, nudge_number: int) -> bool:
    """Send an abandoned cart nudge email. nudge_number: 1 or 2."""
    try:
        from checkout_routes import _get_gmail_token, _send_gmail_message
        from email_templates import render_template, get_template_subject

        customer_email = order_data.get("customer_email") or order_data.get("email", "")
        if not customer_email:
            return False

        token = _get_gmail_token()
        if not token:
            return False

        _ensure_pending_checkout(order_id, customer_email, "")

        order_data["order_id"] = order_id
        order_data["b2bwave_portal_url"] = f"{B2BWAVE_URL}/customer/orders"
        html = render_template("abandoned_cart_nudge", order_data)
        if not html:
            return False

        subject = get_template_subject("abandoned_cart_nudge", order_data)
        _send_gmail_message(token, customer_email, subject, html)

        col = "abandoned_nudge_1_sent_at" if nudge_number == 1 else "abandoned_nudge_2_sent_at"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE pending_checkouts SET {col} = NOW() WHERE order_id = %s",
                    (order_id,),
                )
                cur.execute(
                    """INSERT INTO order_events (order_id, event_type, event_data, source)
                       VALUES (%s, 'abandoned_cart_nudge', %s, 'quote_engine')""",
                    (order_id, json.dumps({"nudge": nudge_number, "email": customer_email})),
                )

        print(f"[QUOTE] Abandoned cart nudge {nudge_number} sent to {customer_email} for order {order_id}")
        return True
    except Exception as e:
        print(f"[QUOTE] Failed to send cart nudge for {order_id}: {e}")
        return False


# =============================================================================
# CRON: CHECK AND SEND QUOTES
# =============================================================================

def check_and_send_quotes() -> dict:
    """
    Cron function: fetch B2BWave admin quotes, send quote emails for new/updated ones.
    """
    summary = {"checked": 0, "sent": 0, "updated": 0, "errors": []}
    try:
        from checkout import generate_checkout_token

        orders = fetch_b2bwave_temporary_orders()
        admin_quotes = [o for o in orders if is_admin_quote(o)]
        summary["checked"] = len(admin_quotes)

        for order in admin_quotes:
            order_id = str(order.get("id", ""))
            if not order_id:
                continue
            try:
                email = order.get("customer_email", "")
                if not email:
                    continue

                # Check if already sent
                with get_db() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(
                            "SELECT quote_sent_at, quote_b2bwave_updated_at FROM pending_checkouts WHERE order_id = %s",
                            (order_id,),
                        )
                        existing = cur.fetchone()

                b2b_updated = order.get("updated_at", "")
                token = generate_checkout_token(order_id, long_lived=True)
                checkout_url = f"{CHECKOUT_BASE_URL}/checkout-ui/{order_id}?token={token}&view=quote"

                order_data = _b2bwave_order_to_template_data(order)

                if not existing or not existing.get("quote_sent_at"):
                    # Never sent -- send now
                    if send_quote_email(order_id, order_data, checkout_url):
                        summary["sent"] += 1
                        _update_b2bwave_timestamp(order_id, b2b_updated)
                elif b2b_updated and existing.get("quote_b2bwave_updated_at"):
                    # Check if B2BWave order was updated since last send
                    last_known = str(existing["quote_b2bwave_updated_at"])
                    if b2b_updated > last_known:
                        if send_quote_email(order_id, order_data, checkout_url):
                            summary["updated"] += 1
                            _update_b2bwave_timestamp(order_id, b2b_updated)

            except Exception as e:
                summary["errors"].append({"order_id": order_id, "error": str(e)})

    except Exception as e:
        summary["errors"].append({"error": str(e)})
    return summary


# =============================================================================
# CRON: CHECK ABANDONED CARTS
# =============================================================================

def check_abandoned_carts() -> dict:
    """
    Cron function: find abandoned B2BWave carts and send nudge emails.
    Nudge 1 at 3 business days, nudge 2 at 7 business days.
    """
    summary = {"checked": 0, "nudge_1_sent": 0, "nudge_2_sent": 0, "errors": []}
    try:
        orders = fetch_b2bwave_temporary_orders()
        carts = [o for o in orders if is_abandoned_cart(o)]
        summary["checked"] = len(carts)

        for order in carts:
            order_id = str(order.get("id", ""))
            if not order_id:
                continue
            try:
                email = order.get("customer_email", "")
                if not email:
                    continue

                updated_at_str = order.get("updated_at") or order.get("created_at", "")
                if not updated_at_str:
                    continue

                try:
                    updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                except Exception:
                    continue

                days = business_days_since(updated_at)

                # Check existing nudge state
                with get_db() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(
                            "SELECT abandoned_nudge_1_sent_at, abandoned_nudge_2_sent_at FROM pending_checkouts WHERE order_id = %s",
                            (order_id,),
                        )
                        existing = cur.fetchone()

                nudge_1_sent = existing and existing.get("abandoned_nudge_1_sent_at")
                nudge_2_sent = existing and existing.get("abandoned_nudge_2_sent_at")

                order_data = _b2bwave_order_to_template_data(order)

                if days >= 3 and not nudge_1_sent:
                    if send_abandoned_cart_nudge(order_id, order_data, 1):
                        summary["nudge_1_sent"] += 1
                elif days >= 7 and not nudge_2_sent:
                    if send_abandoned_cart_nudge(order_id, order_data, 2):
                        summary["nudge_2_sent"] += 1

            except Exception as e:
                summary["errors"].append({"order_id": order_id, "error": str(e)})

    except Exception as e:
        summary["errors"].append({"error": str(e)})
    return summary


# =============================================================================
# HELPERS
# =============================================================================

def _b2bwave_order_to_template_data(order: dict) -> dict:
    """Convert raw B2BWave order to template-compatible dict."""
    order_products = order.get("order_products", [])
    line_items = []
    for op in order_products:
        product = op.get("order_product", op)
        qty = int(float(product.get("quantity", 1) or 1))
        price = float(product.get("final_price", 0) or 0)
        line_items.append({
            "sku": product.get("product_code", ""),
            "name": product.get("product_name", ""),
            "quantity": qty,
            "price": price,
            "line_total": round(price * qty, 2),
        })
    return {
        "order_id": str(order.get("id", "")),
        "customer_name": order.get("customer_name", "Valued Customer"),
        "customer_email": order.get("customer_email", ""),
        "email": order.get("customer_email", ""),
        "company_name": order.get("customer_company", ""),
        "order_total": float(order.get("gross_total", 0) or 0),
        "order_date": (order.get("submitted_at") or order.get("updated_at") or "")[:10],
        "line_items": line_items,
        "shipping_address": {
            "address": order.get("address", ""),
            "address2": order.get("address2", ""),
            "city": order.get("city", ""),
            "state": order.get("province", ""),
            "zip": order.get("postal_code", ""),
        },
    }


def _update_b2bwave_timestamp(order_id: str, updated_at: str):
    """Store the B2BWave updated_at timestamp for change detection."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pending_checkouts SET quote_b2bwave_updated_at = %s WHERE order_id = %s",
                    (updated_at, order_id),
                )
    except Exception as e:
        print(f"[QUOTE] Failed to update B2BWave timestamp for {order_id}: {e}")
