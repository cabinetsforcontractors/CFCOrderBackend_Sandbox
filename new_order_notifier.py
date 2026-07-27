"""
new_order_notifier.py — Beat 2 (William 2026-07-25): the cloned "New Order"
admin email, fired by US instead of B2BWave.

Why: .COM storefront orders will push into B2BWave with prevent_emails, so
B2BWave's native admin notification never fires for them. William's inbox
habit (read the New Order email, forward to the supplier) must survive.
This watcher detects newly-synced orders and sends HIM the same-shaped
email — internal mail, sends directly, draft-first does not apply.

Modes (env NEW_ORDER_NOTIFY_MODE):
  "com" (default) — notify ONLY orders whose B2BWave customer_order_reference
                    starts with COM- (storefront origin; no native email).
                    .NET orders keep their native notification, no duplicates.
  "all"           — notify every new order (for after cutover, when the
                    native channel dies entirely).
  "off"           — watch and record, never send.

State: new_order_notices (order_id PK, channel, notified_at). First run
BACKFILLS every existing order silently — nobody gets 200 emails.
Safety: never notifies orders older than 7 days; test-registry ids skipped.

Rides gmail sync as section 9. Manual doors [admin]:
  GET  /new-order-watch                      — mode + recent notices
  POST /new-order-watch/run                  — run the watch now
  POST /new-order-watch/run?order_id=&force= — drill: notify one order regardless
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from auth import require_admin
from db_helpers import get_db

new_order_router = APIRouter(tags=["new-order-watch"])

NOTIFY_TO = os.environ.get("NEW_ORDER_NOTIFY_TO",
                           "orders@cabinetsforcontractors.com").strip()
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL", "https://cfcordersfrontend-sandbox.vercel.app").strip()
MAX_AGE_DAYS = 7

_table_ready = False


def _mode() -> str:
    return os.environ.get("NEW_ORDER_NOTIFY_MODE", "com").strip().lower()


def _ensure_table(conn) -> bool:
    """Create state table; on first creation backfill all existing orders
    silently. Returns True if a backfill happened."""
    global _table_ready
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS new_order_notices (
                order_id    VARCHAR(50) PRIMARY KEY,
                channel     VARCHAR(30) NOT NULL,
                notified_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        cur.execute("SELECT COUNT(*) FROM new_order_notices")
        empty = cur.fetchone()[0] == 0
        if empty:
            cur.execute("""
                INSERT INTO new_order_notices (order_id, channel)
                SELECT order_id, 'backfill' FROM orders
                ON CONFLICT (order_id) DO NOTHING
            """)
    conn.commit()
    _table_ready = True
    return empty


def _order_reference(order_id: str) -> str:
    """customer_order_reference from the live B2BWave record ('' on any miss)."""
    try:
        from substitutions import fetch_b2b_order
        remote = fetch_b2b_order(order_id)
        return str((remote or {}).get("customer_order_reference") or "")
    except Exception as e:
        print(f"[NEW-ORDER] reference lookup failed for {order_id}: {e}")
        return ""


def _build_email(order: dict, items: list) -> tuple:
    """(subject, html) — same shape as B2BWave's native admin notification."""
    oid = order["order_id"]
    company = order.get("company_name") or order.get("customer_name") or ""
    subject = f"Order {company}-(#{oid})"

    rows = "".join(
        f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee'>{i['sku']}</td>"
        f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>{i['product_name'] or ''}</td>"
        f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:center'>{i['quantity']}</td>"
        f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right'>"
        f"${float(i['line_total'] or 0):,.2f}</td></tr>"
        for i in items)
    items_table = (
        "<table style='border-collapse:collapse;margin:10px 0'>"
        "<tr><th style='text-align:left;padding:4px 8px'>SKU</th>"
        "<th style='text-align:left;padding:4px 8px'>Item</th>"
        "<th style='padding:4px 8px'>Qty</th>"
        "<th style='padding:4px 8px'>Total</th></tr>" + rows + "</table>"
        if items else "<p>(no line items synced yet)</p>")

    total = float(order.get("order_total") or 0)
    comments = (order.get("comments") or "").strip()
    html = f"""<div style="font-family:Arial,sans-serif;font-size:14px;color:#333">
<h2 style="margin:0 0 12px">New Order <span style="color:#888;font-weight:400">(storefront — no native B2BWave email)</span></h2>
<p style="margin:2px 0"><strong>Order ID:</strong> {oid}</p>
<p style="margin:2px 0"><strong>Name:</strong> {order.get('customer_name') or ''}</p>
<p style="margin:2px 0"><strong>Company:</strong> {company}</p>
<p style="margin:2px 0">{order.get('street') or ''} {order.get('street2') or ''}<br>
{order.get('city') or ''} {order.get('state') or ''} {order.get('zip_code') or ''}</p>
<p style="margin:2px 0"><strong>Phone:</strong> {order.get('phone') or ''}</p>
<p style="margin:2px 0"><strong>Email:</strong> {order.get('email') or ''}</p>
{f'<p style="margin:2px 0"><strong>Comments:</strong> {comments}</p>' if comments else ''}
<p style="margin:8px 0"><strong>Total: ${total:,.2f}</strong></p>
{items_table}
<p style="margin:12px 0"><a href="{DASHBOARD_URL}">Open the CFC Orders dashboard</a></p>
</div>"""
    return subject, html


def _notify_order(conn, order_id: str, forced: bool = False) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT order_id, customer_name, company_name, email, phone,
                   street, street2, city, state, zip_code, comments, order_total
            FROM orders WHERE order_id = %s
        """, (order_id,))
        row = cur.fetchone()
        if not row:
            return {"order_id": order_id, "result": "not_found"}
        cols = ["order_id", "customer_name", "company_name", "email", "phone",
                "street", "street2", "city", "state", "zip_code", "comments",
                "order_total"]
        order = dict(zip(cols, row))
        cur.execute("""
            SELECT sku, product_name, quantity, line_total
            FROM order_line_items WHERE order_id = %s ORDER BY id
        """, (order_id,))
        items = [{"sku": r[0], "product_name": r[1], "quantity": r[2],
                  "line_total": r[3]} for r in cur.fetchall()]

    subject, html = _build_email(order, items)
    from supplier_orders import _send_email
    res = _send_email(order_id, NOTIFY_TO, subject, html,
                      triggered_by="new_order_notifier")
    ok = bool(res.get("success"))
    channel = ("forced-drill" if forced else "emailed") if ok else "email-failed"
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO new_order_notices (order_id, channel) VALUES (%s, %s)
            ON CONFLICT (order_id) DO UPDATE
                SET channel = EXCLUDED.channel, notified_at = NOW()
        """, (order_id, channel))
    conn.commit()
    return {"order_id": order_id, "result": channel, "to": NOTIFY_TO}


def run_new_order_watch(conn) -> dict:
    """Main pass — call from gmail sync section 9 or the manual door."""
    out = {"mode": _mode(), "seen": 0, "notified": [], "recorded": []}
    if _ensure_table(conn):
        out["backfilled"] = True
        return out

    try:
        from test_registry import test_order_ids
        test_ids = test_order_ids()
    except Exception:
        test_ids = set()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT o.order_id FROM orders o
            LEFT JOIN new_order_notices n ON n.order_id = o.order_id
            WHERE n.order_id IS NULL
              AND o.created_at > NOW() - INTERVAL '%s days'
            ORDER BY o.created_at ASC
        """ % MAX_AGE_DAYS)
        fresh = [str(r[0]) for r in cur.fetchall()]

    for oid in fresh:
        out["seen"] += 1
        if oid in test_ids:
            _record(conn, oid, "test-order")
            out["recorded"].append({oid: "test-order"})
            continue
        mode = _mode()
        if mode == "off":
            _record(conn, oid, "mode-off")
            out["recorded"].append({oid: "mode-off"})
            continue
        if mode == "com":
            ref = _order_reference(oid)
            if not ref.upper().startswith("COM-"):
                _record(conn, oid, "native-covered")
                out["recorded"].append({oid: "native-covered"})
                continue
        out["notified"].append(_notify_order(conn, oid))
        # BEAT C (William 2026-07-27, "lets make it real"): a first-seen
        # order invoices ITSELF — quote + Square link + v4 invoice email.
        # Guarded inside run_auto_invoice (env gate, test registry, already
        # invoiced/paid, quote-complete; failures alert orders@, never block
        # the watcher).
        try:
            from auto_invoice import run_auto_invoice
            out.setdefault("auto_invoiced", []).append(
                run_auto_invoice(oid, triggered_by="new_order_watch"))
        except Exception as e:
            print(f"[NEW-ORDER] auto-invoice hook failed {oid}: {e}")
    return out


def _record(conn, order_id: str, channel: str):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO new_order_notices (order_id, channel) VALUES (%s, %s)
            ON CONFLICT (order_id) DO NOTHING
        """, (order_id, channel))
    conn.commit()


@new_order_router.get("/new-order-watch")
def watch_state(_: bool = Depends(require_admin)):
    with get_db() as conn:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_id, channel, notified_at FROM new_order_notices
                ORDER BY notified_at DESC LIMIT 25
            """)
            recent = [{"order_id": r[0], "channel": r[1],
                       "at": r[2].isoformat() if r[2] else ""}
                      for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) FROM new_order_notices")
            total = cur.fetchone()[0]
    return {"status": "ok", "mode": _mode(), "notify_to": NOTIFY_TO,
            "tracked": total, "recent": recent}


@new_order_router.post("/new-order-watch/run")
def run_now(order_id: str = "", force: bool = False,
            _: bool = Depends(require_admin)):
    with get_db() as conn:
        if order_id and force:
            _ensure_table(conn)
            return {"status": "ok", "drill": _notify_order(conn, order_id, forced=True)}
        result = run_new_order_watch(conn)
    return {"status": "ok", **result}
