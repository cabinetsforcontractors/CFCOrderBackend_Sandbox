"""
freight_router.py
Order-integrated carrier routing + all-in freight quoting engine.

One call quotes a whole order's freight, leg by leg, and picks the carrier.
The thin endpoint lives in carrier_routes.py:
    GET /freight/carrier-quote/{order_id}?residential=&liftgate=&origin_zip=  [admin]

For each warehouse shipment on the order:
  - resolve the ORIGIN zip (config.WAREHOUSE_ZIPS + the CA-Espresso override:
    Cabinet & Stone Espresso ships from Pico Rivera / Paramount CA, not Houston)
  - pull ship weight / pallet count / over-length from the freight plan
    (freight_logic.plan_order — the same engine /freight/plan uses)
  - quote R+L (rl-quote-sandbox) always; quote DAYLIGHT too on Daylight-eligible
    CA origins, and pick the cheaper carrier that actually serves the lane
  - add the SUPPLIER pallet pass-through (ROC/GHI/default $50/pallet; C&S flat $50)
  - return a per-leg breakdown + the order shipping total + which carrier won

ACCESSORIAL MODEL (William-ruled 2026-07-22):
  residential delivery bundles  residential $75 + lift gate $62 + notification $13
                                = $150/leg (the residential-vs-commercial delta)
  commercial + lift-gate-asked  = lift gate $62 only (no residential, no notification)
  commercial, no lift gate      = $0
The rl-quote-sandbox proxy does NOT apply residential/liftgate/notification, so we
quote R+L COMMERCIAL (is_residential=false) for a clean base and add the bundle here.
The R+L base already carries fuel + over-dimension ($275 8-ft) + CA compliance.
Daylight's net is all-in (accessorials ride in the request), so nothing is added to it.

RESIDENTIAL DETECTION (2026-07-23): `residential` is tri-state.
  None  -> auto-detect via Smarty (checkout.validate_address_full on the ship-to);
           Smarty down/unknown -> assume residential (the safe, higher-quote / CYA side).
  True/False -> manual override (skips Smarty).
`liftgate` stays a manual input (the "need a lift gate?" checkout tic feeds it later).

CA ORIGIN OVERRIDE (William-ruled 2026-07-24): BOTH Cabinet & Stone California
warehouses are real — Paramount 90723 (default) AND Pico Rivera 90660. Pass
`origin_zip` (must be one of the Daylight CA origins) to quote a shipment from
the other CA warehouse; it only applies to CA-eligible legs, never to
ROC/GHI/Houston legs.
"""

import json

from config import WAREHOUSE_ZIPS
from db_helpers import get_db
from freight_logic import plan_order
from rl_quote_proxy import _call_rl_sandbox
from psycopg2.extras import RealDictCursor

# --- accessorial constants (per leg) -----------------------------------------
RESIDENTIAL_FEE = 75.0
LIFTGATE_FEE = 62.0
NOTIFICATION_FEE = 13.0

# --- supplier pallet pass-through: ("per_pallet", rate) or ("flat", rate) -----
# Keyed by rta_products.supplier (plan["supplier"]) AND warehouse label as fallback.
SUPPLIER_PALLET_FEE = {
    "ROC": ("per_pallet", 50.0),
    "ROC Cabinetry": ("per_pallet", 50.0),
    "GHI": ("per_pallet", 50.0),
    "GHI Cabinets": ("per_pallet", 50.0),
    "Cabinet & Stone": ("flat", 50.0),       # C&S: flat $50 no matter how many pallets
    "Cabinet & Stone CA": ("flat", 50.0),
}
DEFAULT_PALLET_FEE = ("per_pallet", 50.0)

# --- Daylight-eligible origins (CA — Cabinet & Stone Pico Rivera / Paramount) --
DAYLIGHT_ORIGIN_ADDR = {
    "90723": {"zip": "90723", "city": "Paramount", "state": "CA"},
    "90660": {"zip": "90660", "city": "Pico Rivera", "state": "CA"},
}
# Cabinet & Stone Espresso ships from CA (Daylight lane), not the Houston default.
CA_ORIGIN_SKU_PREFIXES = ("ESCS",)


def _resolve_origin(warehouse, skus):
    """(origin_zip, daylight_eligible) for a warehouse group, honoring the
    C&S-Espresso -> CA override before the plain WAREHOUSE_ZIPS lookup."""
    wh = warehouse or ""
    if wh.lower().startswith("cabinet & stone"):
        if any((s or "").upper().startswith(CA_ORIGIN_SKU_PREFIXES) for s in skus):
            return "90723", True
    zip_ = WAREHOUSE_ZIPS.get(wh, "")
    if not zip_ and wh:
        wh_cmp = wh.lower().replace(" ", "").replace("&", "").replace("-", "")
        for name, z in WAREHOUSE_ZIPS.items():
            cmp = name.lower().replace(" ", "").replace("&", "").replace("-", "")
            if cmp == wh_cmp or wh_cmp in cmp or cmp in wh_cmp:
                zip_ = z
                break
    return zip_, (zip_ in DAYLIGHT_ORIGIN_ADDR)


def _supplier_pallet_fee(supplier, warehouse, pallets):
    mode, rate = SUPPLIER_PALLET_FEE.get(
        supplier or "", SUPPLIER_PALLET_FEE.get(warehouse or "", DEFAULT_PALLET_FEE))
    return round(rate * pallets, 2) if mode == "per_pallet" else round(rate, 2)


def _accessorials(residential, liftgate):
    """Returns (total, breakdown) for the destination accessorials we add to R+L."""
    if residential:
        return (round(RESIDENTIAL_FEE + LIFTGATE_FEE + NOTIFICATION_FEE, 2),
                {"residential": RESIDENTIAL_FEE, "liftgate": LIFTGATE_FEE,
                 "notification": NOTIFICATION_FEE})
    if liftgate:
        return LIFTGATE_FEE, {"liftgate": LIFTGATE_FEE}
    return 0.0, {}


def _detect_residential(order, dest_zip):
    """Auto-detect residential via Smarty. Returns (residential_bool, source_str).
    Smarty down/unknown -> assume residential (the safe, higher-quote side)."""
    try:
        from checkout import validate_address_full
        v = validate_address_full({
            "street": order.get("street") or "",
            "city": order.get("city") or "",
            "state": order.get("state") or "",
            "zip": dest_zip,
        })
        is_res = bool(v.get("is_residential", True))
        if v.get("success"):
            rdi = v.get("rdi") or ("Residential" if is_res else "Commercial")
            return is_res, f"smarty:{rdi}"
        return True, "smarty-unavailable:assumed-residential"
    except Exception as e:
        return True, f"smarty-error:assumed-residential ({str(e)[:60]})"


def _rl_quote(origin_zip, dest_zip, weight, oversized):
    """R+L COMMERCIAL base (fuel + over-dim + CA compliance; NO res/liftgate/notif).
    Returns (base_cost, quote_number) or (None, error_string)."""
    try:
        res = _call_rl_sandbox("quote/simple", method="POST", params={
            "origin_zip": origin_zip,
            "destination_zip": dest_zip,
            "weight_lbs": weight,
            "is_residential": "false",
            "is_oversized": str(bool(oversized)).lower(),
        })
    except Exception as e:
        return None, f"R+L error: {e}"
    q = res.get("quote", res) if isinstance(res, dict) else {}
    total = q.get("total_cost")
    if total is None:
        total = q.get("net_charge", q.get("price"))
    if total is None:
        return None, "R+L returned no price"
    try:
        return float(total), q.get("quote_number") or q.get("quoteNumber")
    except (TypeError, ValueError):
        return None, f"R+L price not numeric: {total!r}"


def _extract_daylight_net(obj):
    """Best-effort net-charge pull from a dyltRateQuoteResp (schema key unknown)."""
    priority = ["netcharge", "netamount", "totalnetcharge", "totalcharge",
                "grandtotal", "total", "amount", "charge"]
    found = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                    continue
                kl = str(k).lower()
                if kl in priority and kl not in found:
                    try:
                        found[kl] = float(str(v).replace("$", "").replace(",", ""))
                    except (TypeError, ValueError):
                        pass
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(obj)
    for k in priority:
        if k in found:
            return found[k]
    return None


def _daylight_quote(origin_zip, dest, weight, pallets, over_length, residential, liftgate):
    """Daylight all-in net for a CA-origin lane. Returns (net, raw) or (None, note)."""
    try:
        import daylight
    except ImportError:
        return None, "Daylight module unavailable"
    if not (daylight.is_configured() and daylight.mydaylight_configured()):
        return None, "Daylight not configured"
    origin = DAYLIGHT_ORIGIN_ADDR.get(origin_zip)
    if not origin:
        return None, "origin not a Daylight lane"

    accs = []
    if over_length:
        accs.append({"accName": "Delivery", "accId": "Overlength 8 ft but less than 12 ft"})
    if residential:
        accs.append({"accName": "Delivery", "accId": "Residential Delivery"})
        accs.append({"accName": "Delivery", "accId": "Lift Gate Delivery"})
    elif liftgate:
        accs.append({"accName": "Delivery", "accId": "Lift Gate Delivery"})

    fields = {
        "billTerms": "Collect",
        "serviceType": "LTL",
        "shipperInfo": {"customerAddress": origin},
        "consigneeInfo": {"customerAddress": {
            "zip": dest.get("zip", ""), "city": dest.get("city", ""),
            "state": dest.get("state", "")}},
        "items": {"item": [{
            "pcs": max(1, int(pallets or 1)), "pallets": max(1, int(pallets or 1)),
            "weight": int(round(weight)), "actualClass": "85",
            "description": "RTA cabinets"}]},
    }
    if accs:
        fields["accessorials"] = {"accessorial": accs}

    try:
        resp = daylight.rate_quote(fields)
    except Exception as e:
        return None, f"Daylight error: {e}"
    blob = json.dumps(resp).lower()
    if "does not service" in blob or "invalid lane" in blob:
        return None, "Daylight does not service this lane"
    net = _extract_daylight_net(resp)
    if net is None:
        return None, "Daylight returned no parseable rate"
    return round(float(net), 2), resp


def carrier_quote_order(order_id, residential=None, liftgate=False, origin_zip=None):
    """Quote every warehouse leg of an order and pick the carrier per leg.

    residential is tri-state: None -> auto-detect via Smarty (assume residential
    if Smarty is down); True/False -> manual override. liftgate is a manual input.
    origin_zip: optional CA-warehouse override (90723 Paramount / 90660 Pico
    Rivera) — applies ONLY to Daylight-eligible CA legs, never to other legs.
    Returns a per-leg breakdown + the order shipping total. Nothing is sent
    anywhere; this is a quote for a human.
    """
    if origin_zip and origin_zip not in DAYLIGHT_ORIGIN_ADDR:
        return {"status": "error",
                "message": (f"origin_zip '{origin_zip}' is not a known CA origin - "
                            f"valid: {sorted(DAYLIGHT_ORIGIN_ADDR)}")}

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT sku, quantity, warehouse FROM order_line_items WHERE order_id = %s",
                (order_id,),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT street, city, state, zip_code FROM orders WHERE order_id = %s",
                (order_id,),
            )
            order = cur.fetchone()
    if not rows:
        return {"status": "error", "message": f"no line items found for order {order_id}"}
    if not order:
        return {"status": "error", "message": f"order {order_id} not found"}

    dest_zip = (order.get("zip_code") or "").split("-")[0].strip()[:5]
    dest = {"zip": dest_zip, "city": order.get("city") or "", "state": order.get("state") or ""}

    # Residential: auto-detect via Smarty when not explicitly set.
    if residential is None:
        residential, residential_source = _detect_residential(order, dest_zip)
    else:
        residential_source = "manual"
    residential = bool(residential)

    warehouses = {}
    for r in rows:
        wh = r.get("warehouse") or "UNMAPPED"
        warehouses.setdefault(wh, []).append({"sku": r["sku"], "quantity": r["quantity"]})

    plan = plan_order(warehouses, residential=False, liftgate=False)
    acc_total, acc_breakdown = _accessorials(residential, liftgate)

    legs = []
    order_total = 0.0
    all_quoted = True
    for wh, items in warehouses.items():
        p = plan["shipments"][wh]
        skus = [i.get("sku") for i in items]
        leg_origin, daylight_eligible = _resolve_origin(wh, skus)
        origin_overridden = False
        if origin_zip and daylight_eligible and leg_origin != origin_zip:
            leg_origin = origin_zip
            origin_overridden = True
        supplier = p.get("supplier")
        weight = p.get("ship_weight_lb") or 0.0
        pallets = p.get("pallets") or 0
        over_length = bool(p.get("has_long_item"))
        pallet_fee = _supplier_pallet_fee(supplier, wh, pallets)

        leg = {
            "warehouse": wh,
            "supplier": supplier,
            "origin_zip": leg_origin,
            "dest_zip": dest_zip,
            "ship_weight_lb": weight,
            "pallets": pallets,
            "over_length": over_length,
            "supplier_pallet_fee": pallet_fee,
            "carrier": None,
            "carrier_base": None,
            "accessorials": {},
            "leg_total": None,
            "alternatives": {},
            "notes": [],
        }
        if origin_overridden:
            leg["notes"].append(
                f"origin overridden to {leg_origin} "
                f"({DAYLIGHT_ORIGIN_ADDR[leg_origin]['city']}) per request")

        if not leg_origin:
            leg["notes"].append(f"no origin zip mapped for warehouse '{wh}'")
            legs.append(leg)
            all_quoted = False
            continue
        if not dest_zip:
            leg["notes"].append("order has no destination zip")
            legs.append(leg)
            all_quoted = False
            continue
        if weight <= 0 or pallets <= 0:
            leg["notes"].append("no freight data (SKUs missing from rta_products)")
            if p.get("missing_skus"):
                leg["notes"].append(f"missing SKUs: {p['missing_skus']}")
            legs.append(leg)
            all_quoted = False
            continue

        # --- R+L (always) : commercial base + our accessorial bundle + pallet fee
        rl_base, rl_ref = _rl_quote(leg_origin, dest_zip, weight, over_length)
        rl_total = None
        if rl_base is not None:
            rl_total = round(rl_base + acc_total + pallet_fee, 2)
            leg["alternatives"]["R+L"] = {
                "base_commercial": round(rl_base, 2),
                "accessorials": acc_breakdown,
                "pallet_fee": pallet_fee,
                "total": rl_total,
                "quote_number": rl_ref,
            }
        else:
            leg["alternatives"]["R+L"] = {"error": rl_ref}

        # --- Daylight (CA origins only) : all-in net + pallet fee
        dl_total = None
        if daylight_eligible:
            dl_net, dl_raw = _daylight_quote(
                leg_origin, dest, weight, pallets, over_length, residential, liftgate)
            if dl_net is not None:
                dl_total = round(dl_net + pallet_fee, 2)
                leg["alternatives"]["Daylight"] = {
                    "net_all_in": dl_net, "pallet_fee": pallet_fee, "total": dl_total}
            else:
                leg["alternatives"]["Daylight"] = {"not_available": dl_raw}
        else:
            leg["alternatives"]["Daylight"] = {"not_available": "origin not a Daylight lane"}

        # --- pick the cheaper carrier that served the lane
        candidates = []
        if rl_total is not None:
            candidates.append(("R+L", rl_total, rl_base, acc_breakdown))
        if dl_total is not None:
            candidates.append(("Daylight", dl_total, dl_net, {}))
        if not candidates:
            leg["notes"].append("no carrier returned a rate")
            all_quoted = False
        else:
            carrier, total, base, acc = min(candidates, key=lambda c: c[1])
            leg["carrier"] = carrier
            leg["carrier_base"] = round(base, 2)
            leg["accessorials"] = acc
            leg["leg_total"] = total
            order_total = round(order_total + total, 2)
            if carrier == "Daylight" and rl_total is not None:
                leg["notes"].append(f"Daylight beat R+L (R+L would be ${rl_total})")

        legs.append(leg)

    return {
        "status": "ok",
        "order_id": order_id,
        "destination": dest,
        "residential": residential,
        "residential_source": residential_source,
        "liftgate": liftgate,
        "origin_zip_override": origin_zip,
        "legs": legs,
        "order_shipping_total": order_total if all_quoted else None,
        "all_legs_quoted": all_quoted,
        "note": ("residential bundles residential $75 + lift gate $62 + "
                 "notification $13 = $150/leg" if residential else
                 ("lift gate $62/leg" if liftgate else "commercial, no accessorials")),
    }
