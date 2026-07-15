"""
freight_logic.py
Freight quote logic engine — turns order line items into per-supplier shipment
plans: pallet count/dims/weights (the R+L RateQuote payload) plus fee adders.

Calibration authority (2026-07-15, William-ruled; see
Desktop\\VERIFIED SOT 6_30_26\\ORDERS_BACKEND_WAREHOUSE_MAP_20260714\\PALLET_CALIBRATION_TRACK_20260715.md):
- declared pallet cube = carton cube x1.5 (measured 1.40-1.55 across GHI/C&S/ROC/DuraStone)
- shipment weight = SOT weight x1.08 (our SOT reads -7..-13% vs carrier-certified)
- over-dimension: any piece >=96in long that is NOT boxable trim (<=5in wide) rides an
  8-ft pallet -> $275 R+L over-dim exposure; composite two-box talls exempt
- supplier pallet profiles differ (48x40 vs 96x44 8-footers) -> pallet-count breakpoints differ
- palletizing fee $50/pallet (supplier pass-through); GHI note: fee doubles at the 1->2 break (~1,100 lb)
- linear-foot/capacity risk on big loads -> flag for TWO quotes / consider split shipment
- accessorials (residential $75, liftgate $62, notification $13) added when destination requires
- R+L minimum floor (~$330-355 all-in small shipments) comes back from the RateQuote API itself
"""

import math
from typing import Dict, List, Optional

from db_helpers import get_db
from psycopg2.extras import RealDictCursor

WEIGHT_FACTOR = 1.08      # SOT -> shipped weight
CUBE_MULTIPLIER = 1.5     # carton cube -> declared pallet cube
PALLETIZE_FEE = 50.0      # per pallet, supplier pass-through
OVER_DIM_FEE = 275.0      # R+L, any piece >= 96in unboxable
ACCESSORIALS = {"residential": 75.0, "liftgate": 62.0, "notification": 13.0}

# Per-supplier pallet profiles (footprint in inches, carton-cube and weight capacity per pallet).
# cap_cube is CARTON cube per pallet (declared = x1.5); cap_lb from observed real pallets.
PROFILES = {
    "ROC":             {"footprint": (96, 44), "max_h": 75, "cap_cube": 100.0, "cap_lb": 1900, "eight_ft": True},
    "Cabinet & Stone": {"footprint": (48, 40), "max_h": 75, "cap_cube": 55.0,  "cap_lb": 1600, "eight_ft": False},
    "GHI":             {"footprint": (48, 40), "max_h": 75, "cap_cube": 57.0,  "cap_lb": 1100, "eight_ft": False},
    "_default":        {"footprint": (48, 40), "max_h": 75, "cap_cube": 57.0,  "cap_lb": 1100, "eight_ft": False},
}
EIGHT_FT_PROFILE = {"footprint": (96, 40), "max_h": 75, "cap_cube": 100.0, "cap_lb": 1900, "eight_ft": True}


def _lookup_items(skus: List[str]) -> Dict[str, Dict]:
    if not skus:
        return {}
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT product_sku, supplier, weight, cube_ft3, requires_long_pallet,
                          width, height, depth
                   FROM rta_products WHERE product_sku = ANY(%s)""",
                (skus,),
            )
            return {r["product_sku"]: dict(r) for r in cur.fetchall()}


def plan_shipment(line_items: List[Dict], residential: bool = False,
                  liftgate: bool = False) -> Dict:
    """
    line_items: [{'sku': 'WSP-B12', 'quantity': 2}, ...] — ONE supplier/warehouse group.
    Returns the shipment plan: handling units (R+L RateQuote-ready), fees, flags.
    """
    skus = [i.get("sku", "") for i in line_items if i.get("sku")]
    info = _lookup_items(skus)

    carton_cube = 0.0
    sot_weight = 0.0
    long_item = False
    missing = []
    supplier = None
    for item in line_items:
        sku = item.get("sku", "")
        qty = float(item.get("quantity", 0) or 0)
        rec = info.get(sku)
        if not rec:
            missing.append(sku)
            continue
        supplier = supplier or rec.get("supplier")
        sot_weight += float(rec.get("weight") or 0) * qty
        carton_cube += float(rec.get("cube_ft3") or 0) * qty
        if rec.get("requires_long_pallet"):
            long_item = True

    ship_weight = round(sot_weight * WEIGHT_FACTOR, 1)
    profile = PROFILES.get(supplier or "", PROFILES["_default"])
    if long_item and not profile["eight_ft"]:
        profile = EIGHT_FT_PROFILE  # unboxable >=96in piece forces an 8-ft pallet profile

    pallets = max(
        math.ceil(carton_cube / profile["cap_cube"]) if carton_cube else 0,
        math.ceil(ship_weight / profile["cap_lb"]) if ship_weight else 0,
        1 if (carton_cube or ship_weight) else 0,
    )

    handling_units = []
    if pallets:
        declared_cube = carton_cube * CUBE_MULTIPLIER
        fp_l, fp_w = profile["footprint"]
        per_pallet_cube = declared_cube / pallets
        height = min(profile["max_h"], max(20, math.ceil(per_pallet_cube * 1728 / (fp_l * fp_w))))
        per_pallet_weight = math.ceil(ship_weight / pallets)
        handling_units = [{"pieces": 1, "package_type": "PLT",
                          "length": fp_l, "width": fp_w, "height": height,
                          "weight": per_pallet_weight, "nmfc_class": 85.0}
                         for _ in range(pallets)]

    fees = {"palletizing": PALLETIZE_FEE * pallets}
    if long_item:
        fees["over_dimension_exposure"] = OVER_DIM_FEE
    if residential:
        fees["residential"] = ACCESSORIALS["residential"]
    if liftgate:
        fees["liftgate"] = ACCESSORIALS["liftgate"]
    fees["notification"] = ACCESSORIALS["notification"]

    # linear-foot / capacity risk: 5+ standard pallets or 3+ eight-footers
    split_flag = pallets >= 5 or (profile["eight_ft"] and pallets >= 3)

    return {
        "supplier": supplier,
        "sot_weight_lb": round(sot_weight, 1),
        "ship_weight_lb": ship_weight,
        "carton_cube_ft3": round(carton_cube, 1),
        "declared_cube_ft3": round(carton_cube * CUBE_MULTIPLIER, 1),
        "pallets": pallets,
        "pallet_profile": {"footprint": profile["footprint"], "max_h": profile["max_h"],
                           "eight_ft": profile["eight_ft"] or long_item},
        "handling_units": handling_units,
        "fees": fees,
        "fees_total": round(sum(fees.values()), 2),
        "has_long_item": long_item,
        "split_quote_recommended": split_flag,
        "missing_skus": missing,
        "notes": ("Piece >=96in unboxable present: 8-ft pallet + $275 over-dim exposure. " if long_item else "")
                 + ("Linear-foot risk: pull TWO quotes and consider splitting into two shipments. " if split_flag else ""),
    }


def plan_order(warehouses: Dict[str, List[Dict]], residential: bool = False,
               liftgate: bool = False) -> Dict:
    """warehouses: {'GHI': [line_items...], 'ROC': [...]} -> plan per shipment + totals."""
    plans = {wh: plan_shipment(items, residential, liftgate) for wh, items in warehouses.items()}
    return {
        "shipments": plans,
        "total_pallets": sum(p["pallets"] for p in plans.values()),
        "total_fees": round(sum(p["fees_total"] for p in plans.values()), 2),
        "total_ship_weight_lb": round(sum(p["ship_weight_lb"] for p in plans.values()), 1),
    }
