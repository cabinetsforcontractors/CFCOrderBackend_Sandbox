"""
daylight_order.py
Daylight order-integration: auto-build rateQuote and BOL requests straight from
an order id, instead of hand-assembling daylight.py fields.

Thin endpoints live in daylight_routes.py:
    GET  /daylight/order-quote/{order_id}   [admin]
    POST /daylight/order-bol/{order_id}     [admin]

For each warehouse leg of the order (same leg logic as freight_router):
  - resolve the ORIGIN zip (WAREHOUSE_ZIPS + the ESCS -> CA 90723 override)
  - pull ship weight / pallet count / over-length from freight_logic.plan_order
  - Daylight serves CA origins only: ineligible legs are REFUSED with a plain
    note, never silently quoted
  - shipper block = bol_routes.WAREHOUSE_ADDRESSES + BOL_SHIPPER_NAMES (street
    address matched by origin zip, so the ESCS override lands on Cabinet &
    Stone CA / Paramount, not Houston)
  - consignee block = the orders row (company/customer name, street, phone)
  - bill-to = Cabinets For Contractors (third party, same as the R+L BOL)
  - items get NMFC 79300-08, class 85 defaults

Residential is tri-state like freight_router: None -> auto-detect via Smarty
(assume residential if Smarty is down); True/False -> manual override.
liftgate stays a manual input.

Field shapes follow the public XSDs ({base}/rateQuote/schema, /image/schema).
billTerms on the BOL defaults to "Collect" (the proven 5695 pattern); pass
bill_terms="TP" for explicit third-party billing if Daylight asks for it.

STEP 1 SCOPE: assembly + fire only. No DB writes (probill/tracking persistence
lands with the step-2 delivery poller). Nothing here flips DAYLIGHT_BASE_URL —
requests go wherever daylight.py points (TEST until William's word).
"""

import datetime
import re

from psycopg2.extras import RealDictCursor

import daylight
from bol_routes import BOL_SHIPPER_NAMES, WAREHOUSE_ADDRESSES
from db_helpers import get_db
from freight_logic import plan_order
from freight_router import (
    DAYLIGHT_ORIGIN_ADDR,
    _detect_residential,
    _extract_daylight_net,
    _resolve_origin,
)

# NMFC defaults for RTA cabinets (William-ruled): 79300 sub 08, class 85.
NMFC_NUMBER = "79300"
NMFC_SUB_NUMBER = "08"
FREIGHT_CLASS = "85"

# Bill-to: CFC as third party — same block the R+L BOL template prints.
CFC_BILL_TO = {
    "billToName": "Cabinets For Contractors",
    "billToAddress1": "1472 Ocean Shore Blvd",
    "billToCity": "Ormond Beach",
    "billToState": "FL",
    "billToZip": "32176",
    "billToContactName": "Cabinets For Contractors",
    "billToContactNumber": "7709904885",
}


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _load_order(order_id):
    """Order row + line items grouped by warehouse. Returns (order, warehouses)."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT customer_name, company_name, street, street2, city,
                          state, zip_code, phone
                   FROM orders WHERE order_id = %s""",
                (order_id,),
            )
            order = cur.fetchone()
            cur.execute(
                "SELECT sku, quantity, warehouse FROM order_line_items WHERE order_id = %s",
                (order_id,),
            )
            rows = cur.fetchall()
    warehouses = {}
    for r in rows:
        wh = r.get("warehouse") or "UNMAPPED"
        warehouses.setdefault(wh, []).append({"sku": r["sku"], "quantity": r["quantity"]})
    return order, warehouses


def _shipper_for_origin(origin_zip, warehouse):
    """(shipper_label, address_info) for a leg. Zip match first so the
    ESCS -> 90723 override lands on Cabinet & Stone CA, not the Houston entry."""
    for name, info in WAREHOUSE_ADDRESSES.items():
        if info.get("zip") == origin_zip:
            return name, info
    info = WAREHOUSE_ADDRESSES.get(warehouse)
    if info:
        return warehouse, info
    return None, None


def _accessorial_list(over_length, residential, liftgate):
    accs = []
    if over_length:
        accs.append({"accName": "Delivery", "accId": "Overlength 8 ft but less than 12 ft"})
    if residential:
        accs.append({"accName": "Delivery", "accId": "Residential Delivery"})
        accs.append({"accName": "Delivery", "accId": "Lift Gate Delivery"})
    elif liftgate:
        accs.append({"accName": "Delivery", "accId": "Lift Gate Delivery"})
    return accs


def _item_block(order_id, weight, pallets):
    return {"item": [{
        "description": f"RTA cabinets - CFC Order {order_id}"[:40],
        "nmfcNumber": NMFC_NUMBER,
        "nmfcSubNumber": NMFC_SUB_NUMBER,
        "pcs": max(1, int(pallets or 1)),
        "pallets": max(1, int(pallets or 1)),
        "weight": int(round(weight)),
        "actualClass": FREIGHT_CLASS,
    }]}


def _build_legs(order_id, residential, liftgate):
    """Shared leg assembly. Returns (order, dest, residential, source, legs) or
    an error dict. Each leg carries everything both builders need."""
    order, warehouses = _load_order(order_id)
    if not order:
        return {"status": "error", "message": f"order {order_id} not found"}
    if not warehouses:
        return {"status": "error", "message": f"no line items found for order {order_id}"}

    dest_zip = (order.get("zip_code") or "").split("-")[0].strip()[:5]
    dest = {"zip": dest_zip, "city": order.get("city") or "", "state": order.get("state") or ""}
    if not dest_zip:
        return {"status": "error", "message": f"order {order_id} has no destination zip"}

    if residential is None:
        residential, residential_source = _detect_residential(order, dest_zip)
    else:
        residential_source = "manual"
    residential = bool(residential)

    plan = plan_order(warehouses, residential=False, liftgate=False)

    legs = []
    for wh, items in warehouses.items():
        p = plan["shipments"][wh]
        skus = [i.get("sku") for i in items]
        origin_zip, daylight_eligible = _resolve_origin(wh, skus)
        leg = {
            "warehouse": wh,
            "supplier": p.get("supplier"),
            "origin_zip": origin_zip,
            "daylight_eligible": daylight_eligible,
            "ship_weight_lb": p.get("ship_weight_lb") or 0.0,
            "pallets": p.get("pallets") or 0,
            "over_length": bool(p.get("has_long_item")),
            "missing_skus": p.get("missing_skus") or [],
            "notes": [],
        }
        if not daylight_eligible:
            leg["notes"].append(
                "REFUSED: origin not a Daylight lane (Daylight serves CA origins only)")
        if leg["ship_weight_lb"] <= 0 or leg["pallets"] <= 0:
            leg["daylight_eligible"] = False
            leg["notes"].append("REFUSED: no freight data (SKUs missing from rta_products)")
            if leg["missing_skus"]:
                leg["notes"].append(f"missing SKUs: {leg['missing_skus']}")
        legs.append(leg)

    return {"status": "ok", "order": order, "dest": dest, "residential": residential,
            "residential_source": residential_source, "legs": legs}


def _rate_quote_fields(order_id, leg, dest, pickup_date, residential, liftgate):
    """dyltRateQuoteReq minus auth, for one Daylight-eligible leg."""
    origin = DAYLIGHT_ORIGIN_ADDR.get(leg["origin_zip"]) or {
        "zip": leg["origin_zip"], "city": "", "state": ""}
    fields = {
        "billTerms": "Collect",
        "serviceType": "LTL",
        "pickupDate": pickup_date,
        "shipperInfo": {"customerAddress": {
            "zip": origin["zip"], "city": origin["city"], "state": origin["state"]}},
        "consigneeInfo": {"customerAddress": {
            "zip": dest["zip"], "city": dest["city"], "state": dest["state"]}},
        "items": _item_block(order_id, leg["ship_weight_lb"], leg["pallets"]),
    }
    accs = _accessorial_list(leg["over_length"], residential, liftgate)
    if accs:
        fields["accessorials"] = {"accessorial": accs}
    return fields


def _bol_fields(order_id, order, leg, dest, bol_date, bill_terms, residential, liftgate):
    """dyltImageReq minus auth, for one Daylight-eligible leg. Returns
    (fields, None) or (None, error_note)."""
    shipper_label, wh_info = _shipper_for_origin(leg["origin_zip"], leg["warehouse"])
    if not wh_info:
        return None, (f"no shipper street address for origin zip {leg['origin_zip']} "
                      f"/ warehouse '{leg['warehouse']}'")
    consignee_name = order.get("company_name") or order.get("customer_name") or "Customer"
    fields = {
        "billTerms": bill_terms,
        "serviceType": "LTL",
        "shipperName": BOL_SHIPPER_NAMES.get(shipper_label, "Cabinets For Contractors"),
        "shipperAddress1": wh_info["address"],
        "shipperCity": wh_info["city"],
        "shipperState": wh_info["state"],
        "shipperZip": wh_info["zip"],
        "shipperContactName": shipper_label,
        "shipperContactNumber": _digits(wh_info.get("phone")),
        "consigneeName": consignee_name,
        "consigneeAddress1": order.get("street") or "",
        "consigneeAddress2": order.get("street2") or "",
        "consigneeCity": dest["city"],
        "consigneeState": dest["state"],
        "consigneeZip": dest["zip"],
        "consigneeContactName": order.get("customer_name") or consignee_name,
        "consigneeContactNumber": _digits(order.get("phone")),
        **CFC_BILL_TO,
        "bolDate": bol_date,
        "items": _item_block(order_id, leg["ship_weight_lb"], leg["pallets"]),
        "shipReferences": {"shipReference": [
            {"referenceType": "P", "referenceNumber": str(order_id)}]},
    }
    accs = _accessorial_list(leg["over_length"], residential, liftgate)
    if accs:
        fields["accessorials"] = {"accessorial": accs}
    return fields, None


def order_quote(order_id, residential=None, liftgate=False, warehouse=None,
                pickup_date=None, execute=True):
    """Auto-build (and unless execute=False, fire) a Daylight rateQuote per
    eligible leg. Nothing is committed anywhere; a rate quote is just a price."""
    built = _build_legs(order_id, residential, liftgate)
    if built["status"] != "ok":
        return built
    pickup_date = pickup_date or datetime.date.today().isoformat()

    legs_out = []
    for leg in built["legs"]:
        if warehouse and leg["warehouse"] != warehouse:
            continue
        out = {k: leg[k] for k in ("warehouse", "supplier", "origin_zip",
                                   "daylight_eligible", "ship_weight_lb",
                                   "pallets", "over_length")}
        out["notes"] = list(leg["notes"])
        if not leg["daylight_eligible"]:
            legs_out.append(out)
            continue
        fields = _rate_quote_fields(order_id, leg, built["dest"], pickup_date,
                                    built["residential"], liftgate)
        out["request_fields"] = fields
        if execute:
            try:
                resp = daylight.rate_quote(fields)
                out["response"] = resp
                out["net"] = _extract_daylight_net(resp)
            except Exception as e:
                out["error"] = str(e)
        legs_out.append(out)

    if warehouse and not legs_out:
        return {"status": "error",
                "message": f"order {order_id} has no leg for warehouse '{warehouse}'"}
    return {
        "status": "ok",
        "order_id": order_id,
        "destination": built["dest"],
        "residential": built["residential"],
        "residential_source": built["residential_source"],
        "liftgate": liftgate,
        "pickup_date": pickup_date,
        "executed": bool(execute),
        "base_url": daylight.DAYLIGHT_BASE_URL,
        "legs": legs_out,
    }


def order_bol(order_id, warehouse=None, bol_date=None, bill_terms="Collect",
              residential=None, liftgate=False, execute=True):
    """Auto-build (and unless execute=False, fire) the Daylight BOL for ONE leg.
    If the order has several Daylight-eligible legs, `warehouse` must pick one.
    Returns the assembled fields plus (when executed) daylight.create_bol's
    result — pdf bytes ride under 'pdf' for the route layer to encode."""
    built = _build_legs(order_id, residential, liftgate)
    if built["status"] != "ok":
        return built

    eligible = [l for l in built["legs"] if l["daylight_eligible"]]
    if warehouse:
        eligible = [l for l in eligible if l["warehouse"] == warehouse]
    if not eligible:
        return {"status": "error",
                "message": f"order {order_id} has no Daylight-eligible leg"
                           + (f" for warehouse '{warehouse}'" if warehouse else ""),
                "legs": [{k: l[k] for k in ("warehouse", "origin_zip",
                                            "daylight_eligible", "notes")}
                         for l in built["legs"]]}
    if len(eligible) > 1:
        return {"status": "error",
                "message": (f"order {order_id} has {len(eligible)} Daylight-eligible "
                            "legs — pass ?warehouse= to pick one"),
                "warehouses": [l["warehouse"] for l in eligible]}

    leg = eligible[0]
    bol_date = bol_date or datetime.date.today().isoformat()
    fields, err = _bol_fields(order_id, built["order"], leg, built["dest"],
                              bol_date, bill_terms, built["residential"], liftgate)
    if err:
        return {"status": "error", "message": err}

    result = {
        "status": "ok",
        "order_id": order_id,
        "warehouse": leg["warehouse"],
        "residential": built["residential"],
        "residential_source": built["residential_source"],
        "liftgate": liftgate,
        "executed": bool(execute),
        "base_url": daylight.DAYLIGHT_BASE_URL,
        "request_fields": fields,
    }
    if execute:
        try:
            result["pdf"] = daylight.create_bol(fields)
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
    return result
