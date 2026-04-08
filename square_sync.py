"""
Square Payment Sync Module for CFC Orders
Syncs completed payments from Square API and matches to orders by parsing
order numbers from payment descriptions.

On successful payment match:
  - Marks order as paid in DB
  - Fires payment_triggers.run_payment_triggers():
      Trigger 4: Sends payment_confirmation email to customer
      Trigger 2: Auto-creates BOL for all LTL warehouse shipments
"""

import os
import re
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple

# Square API Config
SQUARE_ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "").strip()
SQUARE_LOCATION_ID = os.environ.get("SQUARE_LOCATION_ID", "").strip()
SQUARE_API_BASE = "https://connect.squareup.com/v2"

def square_configured() -> bool:
    """Check if Square API credentials are configured"""
    return bool(SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID)

def square_api_request(endpoint: str, params: dict = None) -> dict:
    """Make a request to Square API"""
    if not square_configured():
        raise Exception("Square API not configured")

    url = f"{SQUARE_API_BASE}/{endpoint}"
    if params:
        query_string = "&".join(f"{k}={v}" for k, v in params.items() if v)
        url = f"{url}?{query_string}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {SQUARE_ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "Square-Version": "2024-01-18"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise Exception(f"Square API error {e.code}: {error_body}")

def extract_order_ids(description: str) -> List[str]:
    """
    Extract order IDs from Square payment description.

    Examples:
    - "5299-Creative Spaces" -> ["5299"]
    - "5317 & 5319 G&B CFC" -> ["5317", "5319"]
    - "5184 Broke and Poor CFC" -> ["5184"]
    - "Order #5299 Smith" -> ["5299"]
    - "5301, 5302 Johnson" -> ["5301", "5302"]
    """
    if not description:
        return []

    order_ids = []

    # Pattern 1: Number at start followed by hyphen (e.g., "5299-Creative Spaces")
    start_match = re.match(r'^(\d{4,5})', description)
    if start_match:
        order_ids.append(start_match.group(1))

    # Pattern 2: Find all 4-5 digit numbers starting with 5 (typical CFC order IDs)
    matches = re.findall(r'\b(5\d{3,4})\b', description)
    order_ids.extend(matches)

    # Pattern 3: Fallback - any 4-5 digit number
    if not order_ids:
        matches = re.findall(r'\b(\d{4,5})\b', description)
        order_ids.extend(matches)

    # Remove duplicates while preserving order
    seen = set()
    unique_ids = []
    for oid in order_ids:
        if oid not in seen:
            seen.add(oid)
            unique_ids.append(oid)

    return unique_ids

def get_recent_payments(hours_back: int = 24) -> List[dict]:
    """
    Get completed payments from Square API.
    Returns list of payment objects with amount, description, and timestamp.
    """
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    begin_time = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "location_id": SQUARE_LOCATION_ID,
        "begin_time": begin_time,
        "sort_order": "DESC"
    }

    result = square_api_request("payments", params)
    payments = result.get("payments", [])

    # Filter to only completed payments
    completed = [p for p in payments if p.get("status") == "COMPLETED"]

    return completed

def get_square_order(order_id: str) -> Optional[dict]:
    """Fetch a Square Order by ID to get line item names"""
    if not order_id:
        return None
    try:
        result = square_api_request(f"orders/{order_id}")
        return result.get("order")
    except Exception as e:
        print(f"[SQUARE] Error fetching order {order_id}: {e}")
        return None


def parse_payment_for_matching(payment: dict) -> dict:
    """
    Parse a Square payment object into a format for order matching.

    The payment link name (like "5317 & 5319 G&B CFC") is stored in:
    1. The linked Order's line_items[0].name (when created via Dashboard)
    2. The payment's "note" field (if set via API)

    Returns dict with:
    - payment_id: Square payment ID
    - amount: Payment amount in dollars
    - description: Payment note/description (contains order IDs)
    - order_ids: List of extracted CFC order IDs
    - created_at: Payment timestamp
    - customer_name: Customer name if available
    """
    amount_money = payment.get("amount_money", {})
    amount_cents = amount_money.get("amount", 0)
    amount_dollars = amount_cents / 100.0

    description = ""
    square_order_id = payment.get("order_id")

    # PRIMARY SOURCE: Fetch the linked Square Order to get line item name
    if square_order_id:
        order = get_square_order(square_order_id)
        if order:
            line_items = order.get("line_items", [])
            if line_items:
                description = line_items[0].get("name", "")

    # FALLBACK: Check payment note field
    if not description and payment.get("note"):
        description = payment.get("note")

    customer_name = None
    if payment.get("buyer_email_address"):
        customer_name = payment.get("buyer_email_address")

    return {
        "payment_id": payment.get("id"),
        "amount": amount_dollars,
        "description": description,
        "order_ids": extract_order_ids(description),
        "created_at": payment.get("created_at"),
        "customer_name": customer_name,
        "square_order_id": square_order_id,
        "raw_payment": payment
    }

def run_square_sync(conn, hours_back: int = 24) -> dict:
    """
    Main sync function - pulls payments from Square and updates orders.

    After marking an order as paid, fires payment_triggers.run_payment_triggers():
      - Trigger 4: payment_confirmation email
      - Trigger 2: auto-create BOL for LTL shipments

    Args:
        conn: Database connection
        hours_back: How many hours of payments to check (default 24)

    Returns:
        Dict with sync results
    """
    from psycopg2.extras import RealDictCursor

    if not square_configured():
        return {
            "status": "disabled",
            "reason": "Square API not configured. Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID."
        }

    results = {
        "status": "ok",
        "payments_found": 0,
        "orders_updated": [],
        "payments_unmatched": [],
        "errors": []
    }

    try:
        payments = get_recent_payments(hours_back)
        results["payments_found"] = len(payments)

        for payment in payments:
            parsed = parse_payment_for_matching(payment)
            order_ids = parsed["order_ids"]

            if not order_ids:
                results["payments_unmatched"].append({
                    "payment_id": parsed["payment_id"],
                    "amount": parsed["amount"],
                    "description": parsed["description"],
                    "reason": "no_order_ids_in_description"
                })
                continue

            for order_id in order_ids:
                try:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute("""
                            SELECT order_id, order_total, payment_received, customer_name, email
                            FROM orders
                            WHERE order_id = %s
                        """, (order_id,))
                        order = cur.fetchone()

                        if not order:
                            results["errors"].append({
                                "order_id": order_id,
                                "error": "order_not_found"
                            })
                            continue

                        if order["payment_received"]:
                            # Already marked as paid — skip triggers
                            continue

                        order_total = float(order["order_total"]) if order["order_total"] else 0

                        shipping_cost = None
                        if len(order_ids) == 1 and order_total > 0:
                            shipping_cost = parsed["amount"] - order_total
                            if shipping_cost < 0:
                                shipping_cost = None

                        cur.execute("""
                            UPDATE orders SET
                                payment_received = TRUE,
                                payment_received_at = NOW(),
                                payment_amount = %s,
                                shipping_cost = COALESCE(%s, shipping_cost),
                                updated_at = NOW()
                            WHERE order_id = %s
                        """, (
                            parsed["amount"] if len(order_ids) == 1 else None,
                            shipping_cost,
                            order_id
                        ))

                        cur.execute("""
                            INSERT INTO order_events (order_id, event_type, event_data, source)
                            VALUES (%s, 'payment_received', %s, 'square_api')
                        """, (order_id, json.dumps({
                            "square_payment_id": parsed["payment_id"],
                            "payment_amount": parsed["amount"],
                            "shipping_cost": shipping_cost,
                            "description": parsed["description"],
                            "multi_order_payment": len(order_ids) > 1
                        })))

                        conn.commit()

                        results["orders_updated"].append({
                            "order_id": order_id,
                            "payment_amount": parsed["amount"],
                            "shipping_cost": shipping_cost,
                            "square_payment_id": parsed["payment_id"]
                        })

                        # Update B2BWave order status to 4 ("Being Prepared")
                        try:
                            import base64 as _b64
                            import urllib.request as _urllib_req
                            import json as _json
                            from config import B2BWAVE_URL as _B2B_URL, B2BWAVE_USERNAME as _B2B_USER, B2BWAVE_API_KEY as _B2B_KEY
                            if _B2B_URL and _B2B_KEY:
                                _creds = _b64.b64encode(f"{_B2B_USER}:{_B2B_KEY}".encode()).decode()
                                _payload = _json.dumps({"status_order_id": 4}).encode()
                                _req = _urllib_req.Request(
                                    f"{_B2B_URL}/api/orders/{order_id}/change_status",
                                    data=_payload, method="PATCH"
                                )
                                _req.add_header("Authorization", f"Basic {_creds}")
                                _req.add_header("Content-Type", "application/json")
                                with _urllib_req.urlopen(_req, timeout=15) as _resp:
                                    _resp.read()
                                print(f"[SQUARE_SYNC] B2BWave order {order_id} marked paid (status 4)")
                        except Exception as b2b_err:
                            print(f"[SQUARE_SYNC] Failed to update B2BWave status for order {order_id}: {b2b_err}")

                        # Triggers 2 + 4: email confirmation + auto-BOL
                        try:
                            from payment_triggers import run_payment_triggers
                            from db_helpers import get_order_by_id
                            order_db = get_order_by_id(order_id)
                            if order_db:
                                trigger_results = run_payment_triggers(
                                    order_id=order_id,
                                    order_data=order_db,
                                    payment_amount=parsed["amount"]
                                )
                                print(f"[SQUARE] Payment triggers complete for order {order_id}: "
                                      f"email={trigger_results.get('email_confirmation', {}).get('success')}, "
                                      f"bols={len(trigger_results.get('bols', []))}")
                        except Exception as e:
                            print(f"[SQUARE] Payment triggers failed for order {order_id}: {e}")

                except Exception as e:
                    results["errors"].append({
                        "order_id": order_id,
                        "error": str(e)
                    })
                    conn.rollback()

    except Exception as e:
        results["status"] = "error"
        results["error"] = str(e)

    return results


def get_square_payment_details(payment_id: str) -> dict:
    """Get details for a specific Square payment by ID"""
    return square_api_request(f"payments/{payment_id}")


# Test function for development
if __name__ == "__main__":
    test_cases = [
        "5317 & 5319 G&B CFC",
        "5184 Broke and Poor CFC",
        "Order #5299 Smith Kitchen",
        "5301, 5302, 5303 Johnson",
        "CFC Payment 5288",
        "5277 Williams - Final",
    ]

    print("Testing order ID extraction:")
    for test in test_cases:
        ids = extract_order_ids(test)
        print(f"  '{test}' -> {ids}")
