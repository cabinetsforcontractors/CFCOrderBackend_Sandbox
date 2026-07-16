"""
freight_routes.py
Freight plan + supplier-sheet endpoints — expose the freight_logic engine and
the website-SKU -> supplier-SKU translation for automatic warehouse ordering.

GET /freight/plan/{order_id} [admin] — per-warehouse pallet plans (R+L
RateQuote-ready handling units + fees + flags). See freight_logic.py.

GET /freight/supplier-sheet/{order_id} [admin] — the warehouse order sheet with
each line translated to the SUPPLIER'S OWN SKU (rta_products.supplier_sku,
loaded from the SOT map SUPPLIER_SKU_MAP_20260716.csv). This is the payload for
the auto-email-to-warehouse workflow (William 2026-07-16): order placed in
website SKUs -> lookup -> send the warehouse their SKUs.
"""

from fastapi import APIRouter, Depends
from psycopg2.extras import RealDictCursor

from auth import require_admin
from config import SUPPLIER_INFO
from db_helpers import get_db
from freight_logic import plan_order

freight_router = APIRouter(tags=["freight"])


@freight_router.get("/freight/plan/{order_id}")
def get_freight_plan(order_id: str, residential: bool = False, liftgate: bool = False,
                     _: bool = Depends(require_admin)):
    """Per-warehouse freight plan for an order [admin]."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT sku, quantity, warehouse FROM order_line_items
                   WHERE order_id = %s""",
                (order_id,),
            )
            rows = cur.fetchall()
    if not rows:
        return {"status": "error", "message": f"no line items found for order {order_id}"}

    warehouses = {}
    for r in rows:
        wh = r.get("warehouse") or "UNMAPPED"
        warehouses.setdefault(wh, []).append({"sku": r["sku"], "quantity": r["quantity"]})

    plan = plan_order(warehouses, residential=residential, liftgate=liftgate)
    plan["status"] = "ok"
    plan["order_id"] = order_id
    return plan


@freight_router.get("/freight/supplier-sheet/{order_id}")
def get_supplier_sheet(order_id: str, _: bool = Depends(require_admin)):
    """
    Warehouse order sheet in SUPPLIER SKUs [admin].
    Groups the order's line items per warehouse and translates every website SKU
    to the supplier's own SKU via rta_products. Lines with no translation are
    returned under 'untranslated' — DO NOT auto-send those; they need a human.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
            if not order:
                return {"status": "error", "message": f"order {order_id} not found"}
            cur.execute(
                "SELECT sku, quantity, product_name, warehouse FROM order_line_items WHERE order_id = %s",
                (order_id,),
            )
            items = cur.fetchall()
            skus = [i["sku"] for i in items if i.get("sku")]
            lookup = {}
            if skus:
                cur.execute(
                    """SELECT product_sku, supplier, supplier_sku FROM rta_products
                       WHERE product_sku = ANY(%s)""",
                    (skus,),
                )
                lookup = {r["product_sku"]: r for r in cur.fetchall()}

    warehouses = {}
    for it in items:
        wh = it.get("warehouse") or "UNMAPPED"
        rec = lookup.get(it.get("sku") or "")
        if wh == "UNMAPPED" and rec and rec.get("supplier"):
            wh = rec["supplier"]
        if wh not in warehouses:
            sinfo = SUPPLIER_INFO.get(wh, {"name": wh, "address": "", "contact": "", "email": ""})
            warehouses[wh] = {
                "supplier_name": sinfo.get("name", wh),
                "supplier_email": sinfo.get("email", ""),
                "supplier_contact": sinfo.get("contact", ""),
                "items": [],
                "untranslated": [],
            }
        line = {
            "quantity": it.get("quantity") or 1,
            "website_sku": it.get("sku") or "",
            "supplier_sku": (rec or {}).get("supplier_sku") or "",
            "product_name": it.get("product_name") or "",
        }
        if line["supplier_sku"]:
            warehouses[wh]["items"].append(line)
        else:
            warehouses[wh]["untranslated"].append(line)

    ready = all(not w["untranslated"] for w in warehouses.values())
    return {
        "status": "ok",
        "order_id": order_id,
        "customer_name": order.get("company_name") or order.get("customer_name") or "",
        "comments": order.get("comments") or "",
        "warehouses": warehouses,
        "ready_to_send": ready,
    }
