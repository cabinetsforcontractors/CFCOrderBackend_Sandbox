"""
freight_routes.py
Freight plan endpoints — expose the freight_logic engine.

GET /freight/plan/{order_id} [admin] — group the order's line items by warehouse
and return per-shipment pallet plans (R+L RateQuote-ready handling units + fees
+ flags). See freight_logic.py for the calibration authority.
"""

from fastapi import APIRouter, Depends
from psycopg2.extras import RealDictCursor

from auth import require_admin
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
