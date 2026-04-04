"""
sync_service.py
B2BWave order sync and auto-sync scheduler for CFC Order Backend.

Auto-generates 6-bullet AI state summary for active orders after each sync.
"""

import json
import base64
import urllib.request
import urllib.error
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from psycopg2.extras import RealDictCursor

from config import (
    B2BWAVE_URL, B2BWAVE_USERNAME, B2BWAVE_API_KEY,
    AUTO_SYNC_INTERVAL_MINUTES, AUTO_SYNC_DAYS_BACK
)
from db_helpers import get_db
from email_parser import get_warehouses_for_skus

# Global state for auto-sync
last_auto_sync = None
auto_sync_running = False


class B2BWaveAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"B2BWave API Error ({status_code}): {message}")


def is_configured() -> bool:
    return bool(B2BWAVE_URL and B2BWAVE_USERNAME and B2BWAVE_API_KEY)


def b2bwave_api_request(endpoint: str, params: dict = None) -> dict:
    if not is_configured():
        raise B2BWaveAPIError(500, "B2BWave API not configured")

    url = f"{B2BWAVE_URL}/api/{endpoint}.json"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{query}"

    credentials = base64.b64encode(f"{B2BWAVE_USERNAME}:{B2BWAVE_API_KEY}".encode()).decode()
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        raise B2BWaveAPIError(e.code, f"HTTP Error: {e.reason}")
    except urllib.error.URLError as e:
        raise B2BWaveAPIError(500, f"Connection error: {str(e)}")


def sync_order_from_b2bwave(order_data: dict) -> dict:
    """Sync a single order from B2BWave API response to our database."""
    order = order_data.get('order', order_data)
    order_id = str(order.get('id'))

    customer_name = order.get('customer_name', '')
    company_name = order.get('customer_company', '')
    email = order.get('customer_email', '')
    phone = order.get('customer_phone', '')
    street = order.get('address', '')
    street2 = order.get('address2', '')
    city = order.get('city', '')
    state = order.get('province', '')
    zip_code = order.get('postal_code', '')
    comments = order.get('comments_customer', '')
    order_total = float(order.get('gross_total', 0) or 0)
    total_weight = float(order.get('total_weight', 0) or 0)

    submitted_at = order.get('submitted_at')
    if submitted_at:
        try:
            order_date = datetime.fromisoformat(submitted_at.replace('Z', '+00:00'))
        except:
            order_date = datetime.now(timezone.utc)
    else:
        order_date = datetime.now(timezone.utc)

    order_products = order.get('order_products', [])
    sku_prefixes = []
    line_items = []

    for op in order_products:
        product = op.get('order_product', op)
        product_code = product.get('product_code', '')
        product_name = product.get('product_name', '')
        quantity = float(product.get('quantity', 0) or 0)
        price = float(product.get('final_price', 0) or 0)

        if '-' in product_code:
            prefix = product_code.split('-')[0]
            if prefix and prefix not in sku_prefixes:
                sku_prefixes.append(prefix)

        line_items.append({'sku': product_code, 'product_name': product_name, 'quantity': quantity, 'price': price})

    warehouses = get_warehouses_for_skus(sku_prefixes)
    warehouse_1 = warehouses[0] if len(warehouses) > 0 else None
    warehouse_2 = warehouses[1] if len(warehouses) > 1 else None
    warehouse_3 = warehouses[2] if len(warehouses) > 2 else None
    warehouse_4 = warehouses[3] if len(warehouses) > 3 else None

    is_trusted = False
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id FROM trusted_customers
                WHERE LOWER(customer_name) = LOWER(%s)
                   OR LOWER(company_name) = LOWER(%s)
                   OR LOWER(email) = LOWER(%s)
            """, (customer_name, company_name, email))
            if cur.fetchone():
                is_trusted = True

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO orders (
                    order_id, order_date, customer_name, company_name,
                    street, street2, city, state, zip_code, phone, email,
                    comments, order_total, total_weight, warehouse_1, warehouse_2, warehouse_3, warehouse_4,
                    is_trusted_customer
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (order_id) DO UPDATE SET
                    customer_name = EXCLUDED.customer_name,
                    company_name = EXCLUDED.company_name,
                    street = EXCLUDED.street,
                    street2 = EXCLUDED.street2,
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    zip_code = EXCLUDED.zip_code,
                    phone = EXCLUDED.phone,
                    email = EXCLUDED.email,
                    comments = EXCLUDED.comments,
                    order_total = EXCLUDED.order_total,
                    total_weight = EXCLUDED.total_weight,
                    warehouse_1 = COALESCE(orders.warehouse_1, EXCLUDED.warehouse_1),
                    warehouse_2 = COALESCE(orders.warehouse_2, EXCLUDED.warehouse_2),
                    warehouse_3 = COALESCE(orders.warehouse_3, EXCLUDED.warehouse_3),
                    warehouse_4 = COALESCE(orders.warehouse_4, EXCLUDED.warehouse_4),
                    is_trusted_customer = EXCLUDED.is_trusted_customer,
                    updated_at = NOW()
                RETURNING order_id
            """, (
                order_id, order_date, customer_name, company_name,
                street, street2, city, state, zip_code, phone, email,
                comments, order_total, total_weight, warehouse_1, warehouse_2, warehouse_3, warehouse_4,
                is_trusted
            ))
            cur.fetchone()

            cur.execute("DELETE FROM order_line_items WHERE order_id = %s", (order_id,))

            for item in line_items:
                sku = item.get('sku', '')
                prefix = sku.split('-')[0] if '-' in sku else ''
                item_warehouse = None
                if prefix:
                    cur.execute("SELECT warehouse_name FROM warehouse_mapping WHERE UPPER(sku_prefix) = UPPER(%s)", (prefix,))
                    wh_row = cur.fetchone()
                    if wh_row:
                        item_warehouse = wh_row['warehouse_name']

                cur.execute("""
                    INSERT INTO order_line_items (order_id, sku, sku_prefix, product_name, quantity, price, warehouse)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (order_id, sku, prefix, item.get('product_name'), item.get('quantity'), item.get('price'), item_warehouse))

            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'b2bwave_sync', %s, 'api')
            """, (order_id, json.dumps({'sku_prefixes': sku_prefixes})))

            warehouses_list = [w for w in [warehouse_1, warehouse_2, warehouse_3, warehouse_4] if w]
            for wh in warehouses_list:
                wh_short = wh.replace(' & ', '-').replace(' ', '-')
                shipment_id = f"{order_id}-{wh_short}"
                cur.execute("SELECT id FROM order_shipments WHERE shipment_id = %s", (shipment_id,))
                if not cur.fetchone():
                    cur.execute("""
                        INSERT INTO order_shipments (order_id, shipment_id, warehouse, status)
                        VALUES (%s, %s, %s, 'needs_order')
                    """, (order_id, shipment_id, wh))

    return {
        'order_id': order_id,
        'customer_name': customer_name,
        'company_name': company_name,
        'city': city,
        'state': state,
        'zip_code': zip_code,
        'warehouse_1': warehouse_1,
        'warehouse_2': warehouse_2,
        'warehouse_3': warehouse_3,
        'warehouse_4': warehouse_4,
        'line_items_count': len(line_items)
    }


def refresh_ai_summaries_for_active_orders():
    """
    Generate/refresh the 6-bullet AI state summary for active orders.

    Only refreshes orders where:
    - ai_summary is null (never generated), OR
    - ai_summary_updated_at is more than 30 minutes old AND order is not complete

    Limits to 20 orders per cycle to avoid excessive API usage.
    """
    try:
        from ai_summary import generate_order_summary, is_configured as ai_is_configured
        if not ai_is_configured():
            return

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT order_id FROM orders
                    WHERE is_complete = FALSE
                    AND (
                        ai_summary IS NULL
                        OR ai_summary_updated_at < %s
                    )
                    ORDER BY
                        CASE WHEN ai_summary IS NULL THEN 0 ELSE 1 END,
                        updated_at DESC
                    LIMIT 20
                """, (cutoff,))
                rows = cur.fetchall()

        if not rows:
            return

        print(f"[AI-SUMMARY] Refreshing {len(rows)} order summaries")
        for row in rows:
            order_id = row['order_id']
            try:
                summary = generate_order_summary(order_id)
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE orders
                            SET ai_summary = %s, ai_summary_updated_at = NOW()
                            WHERE order_id = %s
                        """, (summary, order_id))
            except Exception as e:
                print(f"[AI-SUMMARY] Failed for order {order_id}: {e}")

        print(f"[AI-SUMMARY] Done refreshing summaries")

    except Exception as e:
        print(f"[AI-SUMMARY] Refresh error: {e}")


def run_auto_sync(gmail_sync_func=None, square_sync_func=None):
    """
    Background sync from B2BWave — runs every AUTO_SYNC_INTERVAL_MINUTES.
    After each B2BWave sync, refreshes AI state summaries for active orders.
    """
    global last_auto_sync, auto_sync_running

    while True:
        time.sleep(AUTO_SYNC_INTERVAL_MINUTES * 60)

        if not is_configured():
            print("[AUTO-SYNC] B2BWave not configured, skipping")
            continue

        try:
            auto_sync_running = True
            print(f"[AUTO-SYNC] Starting sync at {datetime.now()} (interval: {AUTO_SYNC_INTERVAL_MINUTES} min)")

            since_date = (datetime.now(timezone.utc) - timedelta(days=AUTO_SYNC_DAYS_BACK)).strftime("%Y-%m-%d")

            data = b2bwave_api_request("orders", {"submitted_at_gteq": since_date})
            orders_list = data if isinstance(data, list) else [data]

            synced = 0
            for order_data in orders_list:
                try:
                    sync_order_from_b2bwave(order_data)
                    synced += 1
                except Exception as e:
                    print(f"[AUTO-SYNC] Error syncing order: {e}")

            last_auto_sync = datetime.now(timezone.utc)
            print(f"[AUTO-SYNC] Completed: {synced} orders synced")

            # Gmail sync
            if gmail_sync_func:
                try:
                    with get_db() as conn:
                        gmail_results = gmail_sync_func(conn, hours_back=2)
                        print(f"[AUTO-SYNC] Gmail sync: {gmail_results}")
                except Exception as e:
                    print(f"[AUTO-SYNC] Gmail sync error: {e}")

            # Square sync
            if square_sync_func:
                try:
                    with get_db() as conn:
                        square_results = square_sync_func(conn, hours_back=24)
                        print(f"[AUTO-SYNC] Square sync: {square_results}")
                except Exception as e:
                    print(f"[AUTO-SYNC] Square sync error: {e}")

            # AI summary refresh — runs after every B2BWave sync
            refresh_ai_summaries_for_active_orders()

        except Exception as e:
            print(f"[AUTO-SYNC] Error: {e}")
        finally:
            auto_sync_running = False


def start_auto_sync_thread(gmail_sync_func=None, square_sync_func=None):
    """Start background sync thread — one thread only."""
    if is_configured():
        thread = threading.Thread(
            target=run_auto_sync,
            args=(gmail_sync_func, square_sync_func),
            daemon=True
        )
        thread.start()
        print(f"[AUTO-SYNC] Started — will sync every {AUTO_SYNC_INTERVAL_MINUTES} minutes")
        return True
    else:
        print("[AUTO-SYNC] B2BWave not configured, auto-sync disabled")
        return False


def get_sync_status() -> Dict:
    return {
        "configured": is_configured(),
        "last_sync": last_auto_sync.isoformat() if last_auto_sync else None,
        "running": auto_sync_running,
        "interval_minutes": AUTO_SYNC_INTERVAL_MINUTES
    }
