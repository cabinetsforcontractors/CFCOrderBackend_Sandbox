"""
small_package.py — THE SMALL-PACKAGE LANE (UPS via Shippo) — born 8/4.

William 8/4: "one thing that I have completely forgotten about is small
package orders that we send via ups". Pirate Ship has NO public API and
its Square integration only imports orders into their UI — so the ROBOT
quotes through SHIPPO (his other-company API; SHIPPO_API_KEY env, test
key until his live key lands) and William keeps buying labels on Pirate
Ship. A delta-test vs real Pirate Ship prices decides the quote
adjustment factor later.

⚖️ PACKAGE-DIMS LAW (his 8/4 words; examples corrected to the rule by
his own ruling "use the size you calculated"):
  BASE cabinets  — widths 9/12/15/18/21/24 → package 27" wide;
                   widths 27-42 → cabinet width + 2";
                   package height 36.5", thickness 8".  B24 = 27 x 36.5 x 8
  WALL cabinets  — widths 9/12 → package 14" wide; 15"+ → width + 2";
                   package height = cabinet height + 2", thickness 6".
                   W0930 = 14 x 32 x 6
  WEIGHT         — the website weight (rta_products).
  Anything without a rule is listed as UNRULED — never guessed.

⚖️ UPS LIMITS (verified 8/4 on UPS's published rules):
  HARD (not accepted / $1,875 over-max fee): weight > 150 lbs,
    longest side > 108", length + girth > 165".
  LARGE PACKAGE surcharge: length > 96" OR length+girth > 130"
    OR weight > 110 lbs (billable minimum 90 lbs).
  ADDITIONAL HANDLING: weight > 50 lbs, longest side > 48",
    or second-longest side > 30".

Doors [admin]:
  GET  /small-package/plan/{order_id} — the package plan (dims, weights,
       per-package UPS flags; unruled lines listed honestly)
  POST /shippo/quote — {order_id, api_key?} → Shippo rates for the whole
       package set, cheapest first (api_key overrides env for the
       test-key phase only)
"""

import json
import os
import re
import urllib.request
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, Depends

from auth import require_admin

small_package_router = APIRouter(tags=["small-package"])

SHIPPO_API_BASE = "https://api.goshippo.com"

# BASE-family form prefixes that follow the base rule. Vanities (VA/VSB,
# 21" deep) and everything else stay UNRULED until William rules them.
_BASE_RE = re.compile(r"^(B|SB|FSB|DB|ER|BER)(\d{2})(?:$|[-_])")
_WALL_RE = re.compile(r"^W(\d{2})(\d{2})$")


def package_for_form(form: str) -> Optional[Dict]:
    """Package dims for a cabinet FORM (post-prefix sku, e.g. B12, W0930).
    Returns {w, h, t} in inches or None when no rule covers the form."""
    form = (form or "").strip().upper()
    m = _WALL_RE.match(form)
    if m:
        cab_w, cab_h = int(m.group(1)), int(m.group(2))
        pkg_w = 14 if cab_w <= 12 else cab_w + 2
        return {"w": pkg_w, "h": cab_h + 2, "t": 6}
    m = _BASE_RE.match(form)
    if m:
        cab_w = int(m.group(2))
        if cab_w <= 24:
            pkg_w = 27
        elif cab_w <= 42:
            pkg_w = cab_w + 2
        else:
            return None
        return {"w": pkg_w, "h": 36.5, "t": 8}
    return None


def ups_flags(dims: Dict, weight: float) -> Dict:
    """UPS limit verdicts for one package."""
    sides = sorted([dims["w"], dims["h"], dims["t"]], reverse=True)
    length, second = sides[0], sides[1]
    girth = 2 * (sides[1] + sides[2])
    lg = length + girth
    return {
        "length": length, "length_plus_girth": round(lg, 1),
        "hard_blocked": bool(weight > 150 or length > 108 or lg > 165),
        "large_package": bool(length > 96 or lg > 130 or weight > 110),
        "additional_handling": bool(weight > 50 or length > 48
                                    or second > 30),
    }


def build_package_plan(order_id: str) -> Dict:
    """Every item of the order → its own package(s) per the dims law."""
    from freight_routes import get_supplier_sheet
    from rta_database import get_skus_info
    sheet = get_supplier_sheet(order_id, True)
    if sheet.get("status") != "ok":
        return {"status": "error",
                "message": sheet.get("message") or "sheet failed"}
    items = []
    for wh, wdata in (sheet.get("warehouses") or {}).items():
        for i in wdata.get("items") or []:
            items.append({**i, "warehouse": wh})
    weights = get_skus_info([i.get("website_sku") for i in items
                             if i.get("website_sku")])
    packages, unruled = [], []
    for i in items:
        sku = (i.get("website_sku") or "").strip()
        qty = int(float(i.get("quantity") or 1))
        form = sku.split("-", 1)[1] if "-" in sku else sku
        dims = package_for_form(form)
        w_info = weights.get(sku) or {}
        weight = float(w_info.get("weight") or 0)
        if not dims or not weight:
            unruled.append({"sku": sku, "qty": qty,
                            "why": ("no dims rule" if not dims
                                    else "no weight on file")})
            continue
        packages.append({"sku": sku, "form": form, "qty": qty,
                         "warehouse": i["warehouse"],
                         "dims": dims, "weight_lbs": weight,
                         "ups": ups_flags(dims, weight)})
    total_w = sum(p["weight_lbs"] * p["qty"] for p in packages)
    return {"status": "ok", "order_id": order_id, "packages": packages,
            "unruled": unruled,
            "package_count": sum(p["qty"] for p in packages),
            "total_weight_lbs": round(total_w, 1),
            "ups_shippable": bool(packages) and not unruled
            and not any(p["ups"]["hard_blocked"] for p in packages)}


def _shippo_request(path: str, body: Dict, api_key: str) -> Dict:
    req = urllib.request.Request(
        f"{SHIPPO_API_BASE}{path}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"ShippoToken {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


@small_package_router.get("/small-package/plan/{order_id}")
def small_package_plan(order_id: str, _: bool = Depends(require_admin)):
    """The package plan for an order — dims, weights, UPS flags [admin]."""
    return build_package_plan(order_id)


@small_package_router.post("/shippo/quote")
def shippo_quote(payload: Dict = Body(...), _: bool = Depends(require_admin)):
    """Shippo rates for an order's whole package set [admin].
    Body: {order_id, api_key?} — api_key overrides the env during the
    test-key phase; the live key belongs in SHIPPO_API_KEY on Render."""
    order_id = str(payload.get("order_id") or "").strip()
    api_key = (payload.get("api_key") or "").strip() \
        or os.environ.get("SHIPPO_API_KEY", "").strip()
    if not api_key:
        return {"status": "error",
                "message": "no Shippo key — set SHIPPO_API_KEY or pass "
                           "api_key for a test-key run"}
    plan = build_package_plan(order_id)
    if plan.get("status") != "ok":
        return plan
    if not plan["packages"]:
        return {"status": "error", "message": "no ruled packages",
                "unruled": plan.get("unruled")}
    blocked = [p["sku"] for p in plan["packages"] if p["ups"]["hard_blocked"]]
    if blocked:
        return {"status": "error",
                "message": f"UPS hard limits exceeded: {', '.join(blocked)}"
                           f" — this order is LTL freight, not small package",
                "plan": plan}
    from db_helpers import get_db
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT customer_name, company_name, street, city,
                                  state, zip_code, phone, email
                           FROM orders WHERE order_id = %s""", (order_id,))
            row = cur.fetchone()
    if not row:
        return {"status": "error", "message": f"order {order_id} not found"}
    name, company, street, city, state, zip_code, phone, email = row
    from bol_routes import WAREHOUSE_ADDRESSES, _wh_key
    wh = plan["packages"][0]["warehouse"]
    src = WAREHOUSE_ADDRESSES.get(_wh_key(wh))
    if not src:
        return {"status": "error", "message": f"unknown warehouse '{wh}'"}
    parcels = []
    for p in plan["packages"]:
        sides = sorted([p["dims"]["w"], p["dims"]["h"], p["dims"]["t"]],
                       reverse=True)
        for _n in range(p["qty"]):
            parcels.append({"length": str(sides[0]), "width": str(sides[1]),
                            "height": str(sides[2]),
                            "distance_unit": "in",
                            "weight": str(p["weight_lbs"]),
                            "mass_unit": "lb"})
    try:
        shipment = _shippo_request("/shipments/", {
            "address_from": {"name": "Cabinets For Contractors",
                             "street1": src["address"], "city": src["city"],
                             "state": src["state"], "zip": src["zip"],
                             "country": "US", "phone": src["phone"]},
            "address_to": {"name": company or name or "Customer",
                           "street1": street or "", "city": city or "",
                           "state": state or "",
                           "zip": (zip_code or "").split("-")[0][:5],
                           "country": "US", "phone": phone or "",
                           "email": email or ""},
            "parcels": parcels,
            "async": False,
        }, api_key)
    except Exception as e:
        return {"status": "error", "message": f"Shippo call failed: {e}"}
    rates = []
    for r in shipment.get("rates") or []:
        rates.append({"carrier": r.get("provider"),
                      "service": (r.get("servicelevel") or {}).get("name"),
                      "amount": float(r.get("amount") or 0),
                      "days": r.get("estimated_days"),
                      "rate_id": r.get("object_id")})
    rates.sort(key=lambda r: r["amount"] or 9e9)
    return {"status": "ok", "order_id": order_id,
            "packages": plan["package_count"],
            "total_weight_lbs": plan["total_weight_lbs"],
            "warehouse": wh,
            "test_mode": api_key.startswith("shippo_test"),
            "rates": rates[:12],
            "messages": [m.get("text") for m in
                         (shipment.get("messages") or [])][:5],
            "note": "delta-test vs Pirate Ship before trusting amounts — "
                    "test keys return test rates"}
