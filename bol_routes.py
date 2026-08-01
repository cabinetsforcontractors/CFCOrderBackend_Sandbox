"""
bol_routes.py
FastAPI router for BOL generation — Phase 8.

Admin triggers BOL creation for a specific shipment.
Calls rl-quote /bol/create, stores PRO number + BOL URL on order_shipments,
marks bol_sent=TRUE, syncs PRO to orders.tracking, updates order checkpoint.

Blocker 2 fix: BOL requires warehouse_confirmed=TRUE before generating.
Blocker 3 fix: PRO number written to orders.tracking so alerts engine sees it.
William 8/1: force=true bypasses the gates (payment / warehouse confirmed /
already-sent) — his explicit override from the DO-box; every forced fire is
recorded as forced in the order event.

Endpoints:
    POST /bol/{shipment_id}/create   — generate BOL via R+L API  [admin]
    GET  /bol/{shipment_id}/status   — check BOL status for a shipment
"""

import os
import json
import urllib.request
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from psycopg2.extras import RealDictCursor

from auth import require_admin
from db_helpers import get_db
from config import WAREHOUSE_ZIPS, SUPPLIER_INFO

RL_QUOTE_API_URL = os.environ.get(
    "RL_QUOTE_API_URL", "https://rl-quote-sandbox.onrender.com"
).strip()

# BOL shipper name lookup — matches checkout.py WAREHOUSES bol_shipper_name
# Format: "Cabinets For Contractors-{first_letter}{last_2_zip}"
BOL_SHIPPER_NAMES = {
    "Cabinetry Distribution":  "Cabinets For Contractors-C48",
    "DL Cabinetry":            "Cabinets For Contractors-D56",
    "ROC Cabinetry":           "Cabinets For Contractors-R71",
    "GHI Cabinets":            "Cabinets For Contractors-G21",
    "Go Bravura":              "Cabinets For Contractors-G66",
    "Love-Milestone":          "Cabinets For Contractors-L24",
    "Artisan (fallback)":      "Cabinets For Contractors-A64",
    "Cabinet & Stone":         "Cabinets For Contractors-C43",
    "Cabinet & Stone CA":      "Cabinets For Contractors-C60",
    "DuraStone":               "Cabinets For Contractors-D37",
    "L&C Cabinetry":           "Cabinets For Contractors-L54",
    "Dealer Cabinetry":        "Cabinets For Contractors-D10",
}

# ONE Cabinet & Stone California warehouse (William-ruled 2026-07-24, round 2):
# Pico Rivera 90660, 7105 Paramount Blvd — confirmed by the real 5695 pickup
# (Daylight's own shipment record names this dock). The old "Paramount 90723 /
# 15500 Vermont Ave" entry was a phantom (delivery + warehouse addresses had
# become intertwined) and was removed. The DB warehouse name stays
# "Cabinet & Stone CA" (KEYS ARE PLUMBING); only its address/label changed.
# Addresses verified against each company's own published warehouse
# (2026-07-24 sweep); GHI street comes from a GHI invoice (William).
# Every entry carries a "label" — the human-readable location indicator
# (William's naming: C&S-Houston / C&S-Pico Rivera) printed wherever a person
# reads the shipper. DICT KEYS ARE PLUMBING (they must keep matching warehouse
# names from the DB) — never rename a key to relabel a warehouse; change the
# label field instead.
WAREHOUSE_ADDRESSES = {
    "Cabinetry Distribution":  {"label": "Cabinetry Distribution-Interlachen", "address": "561 Keuka Rd",              "city": "Interlachen",    "state": "FL", "zip": "32148", "phone": "6154106775"},
    "DL Cabinetry":            {"label": "DL-Jacksonville",                    "address": "8145 Baymeadows Way W",     "city": "Jacksonville",   "state": "FL", "zip": "32256", "phone": "9047231061"},
    "ROC Cabinetry":           {"label": "ROC-Norcross",                       "address": "505 Best Friend Ct Ste 580 & 560", "city": "Norcross", "state": "GA", "zip": "30071", "phone": "7702639800"},
    "GHI Cabinets":            {"label": "GHI-Palmetto",                       "address": "1807 48th Ave E Unit 110",  "city": "Palmetto",       "state": "FL", "zip": "34221", "phone": "9419819994"},
    "Go Bravura":              {"label": "Go Bravura-Houston",                 "address": "14200 Hollister St Ste 200", "city": "Houston",       "state": "TX", "zip": "77066", "phone": "8327562766"},
    "Love-Milestone":          {"label": "Love-Milestone-Orlando",             "address": "10963 Florida Crown Dr STE 100", "city": "Orlando",   "state": "FL", "zip": "32824", "phone": "4076017090"},
    "Artisan (fallback)":      {"label": "Artisan-Houston",                    "address": "10000 W Sam Houston Pkwy N Ste 100", "city": "Houston", "state": "TX", "zip": "77064", "phone": "8888066688"},
    "Cabinet & Stone":         {"label": "C&S-Houston",                        "address": "1760 Stebbins Dr",          "city": "Houston",        "state": "TX", "zip": "77043", "phone": "2818330980"},
    "Cabinet & Stone CA":      {"label": "C&S-Pico Rivera",                    "address": "7105 Paramount Blvd",       "city": "Pico Rivera",    "state": "CA", "zip": "90660", "phone": "5627748522"},
    "DuraStone":               {"label": "DuraStone-Houston",                  "address": "9815 North Fwy",            "city": "Houston",        "state": "TX", "zip": "77037", "phone": "2814479997"},
    "L&C Cabinetry":           {"label": "L&C-Virginia Beach",                 "address": "2028 Virginia Beach Blvd",  "city": "Virginia Beach", "state": "VA", "zip": "23454", "phone": "7574255544"},
    "Dealer Cabinetry":        {"label": "Dealer-Bremen",                      "address": "202 N Georgia Ave",         "city": "Bremen",         "state": "GA", "zip": "30110", "phone": "7705374422"},
}

bol_router = APIRouter(tags=["bol"])


def _get_shipment_with_order(shipment_id: str) -> Optional[dict]:
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT s.*,
                       o.customer_name, o.company_name,
                       o.street, o.street2, o.city, o.state, o.zip_code, o.phone,
                       o.order_total, o.total_weight,
                       o.payment_received, o.warehouse_confirmed
                FROM order_shipments s
                JOIN orders o ON s.order_id = o.order_id
                WHERE s.shipment_id = %s
                """,
                (shipment_id,),
            )
            return cur.fetchone()


def _call_rl_bol_create(payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{RL_QUOTE_API_URL}/bol/create",
        data=data,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


@bol_router.post("/bol/{shipment_id}/create")
def create_bol_for_shipment(
    shipment_id: str,
    pickup_date: Optional[str] = None,
    force: bool = False,
    _: bool = Depends(require_admin),
):
    """
    Generate a Bill of Lading for a shipment via R+L API.

    BLOCKER 2 FIX: Requires payment_received=TRUE AND warehouse_confirmed=TRUE.
    BOL can only be generated after the warehouse has confirmed they have the order.

    BLOCKER 3 FIX: PRO number is written to both order_shipments.pro_number AND
    orders.tracking so the alerts engine's 'ready_ship_long' rule resolves correctly.

    force=true (William 8/1): his explicit override — skips the payment /
    warehouse-confirmed / already-sent gates. A re-fire on an already-sent
    shipment creates a SECOND PRO with R+L; the DO-box preview says so
    before the fire, and the event records forced=true.

    pickup_date: optional MM/DD/YYYY — defaults to today if not supplied.
    """
    shipment = _get_shipment_with_order(shipment_id)
    if not shipment:
        raise HTTPException(status_code=404, detail=f"Shipment {shipment_id} not found")

    # BLOCKER 2 FIX — enforce correct step order (force = William's override)
    if not force:
        if not shipment.get("payment_received"):
            raise HTTPException(
                status_code=400,
                detail="Cannot generate BOL — payment not yet received for this order"
            )

        if not shipment.get("warehouse_confirmed"):
            raise HTTPException(
                status_code=400,
                detail="Cannot generate BOL — warehouse has not yet confirmed this order. "
                       "Mark 'Warehouse Confirmed' in the admin panel first."
            )

        if shipment.get("bol_sent"):
            raise HTTPException(
                status_code=400,
                detail=f"BOL already generated for shipment {shipment_id} — PRO: {shipment.get('pro_number')}"
            )

    warehouse_name = shipment["warehouse"]
    wh_info = WAREHOUSE_ADDRESSES.get(warehouse_name)
    if not wh_info:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown warehouse '{warehouse_name}' — cannot look up shipper address"
        )

    shipper_name = BOL_SHIPPER_NAMES.get(warehouse_name, "Cabinets For Contractors")
    consignee_name = shipment.get("company_name") or shipment.get("customer_name") or "Customer"
    dest_zip = (shipment.get("zip_code") or "").split("-")[0][:5]
    weight = int(float(shipment.get("weight") or shipment.get("total_weight") or 200))
    is_residential = bool(shipment.get("is_residential", True))

    payload = {
        "shipper_name": shipper_name,
        "shipper_address": wh_info["address"],
        "shipper_city": wh_info["city"],
        "shipper_state": wh_info["state"],
        "shipper_zip": wh_info["zip"],
        "shipper_phone": wh_info["phone"],
        "consignee_name": consignee_name,
        "consignee_address": shipment.get("street") or "",
        "consignee_city": shipment.get("city") or "",
        "consignee_state": shipment.get("state") or "",
        "consignee_zip": dest_zip,
        "consignee_phone": shipment.get("phone") or "",
        "weight_lbs": weight,
        "is_residential": is_residential,
        "order_id": shipment["order_id"],
        "pieces": 1,
        "description": "RTA Cabinetry",
        # WILLIAM'S PICKUP LAW 2026-07-27: no raw "today" defaults —
        # Mon-Thu 9-4 / Fri 9-3 Eastern, 2h same-day cutoff, no weekends
        "pickup_date": pickup_date or __import__("pickup_window").next_pickup_date_mmddyyyy(),
        "special_instructions": f"CFC Order #{shipment['order_id']}",
    }

    try:
        result = _call_rl_bol_create(payload)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"rl-quote BOL call failed: {str(e)}")

    if not result.get("success") or not result.get("pro_number"):
        raise HTTPException(
            status_code=502,
            detail=f"R+L BOL creation failed: {result.get('error', 'No PRO number returned')}"
        )

    pro_number = result["pro_number"]
    bol_pdf_url = result.get("bol_pdf_url", "")

    # Save to DB — both shipment record and orders table
    with get_db() as conn:
        with conn.cursor() as cur:
            # Update order_shipments
            cur.execute(
                """
                UPDATE order_shipments
                SET pro_number = %s,
                    bol_url = %s,
                    bol_sent = TRUE,
                    bol_sent_at = NOW(),
                    status = 'needs_bol',
                    updated_at = NOW()
                WHERE shipment_id = %s
                """,
                (pro_number, bol_pdf_url, shipment_id),
            )

            # BLOCKER 3 FIX: write PRO to orders.tracking AND orders.pro_number
            cur.execute(
                """
                UPDATE orders
                SET bol_sent = TRUE,
                    bol_sent_at = NOW(),
                    tracking = %s,
                    pro_number = %s,
                    updated_at = NOW()
                WHERE order_id = %s
                """,
                (pro_number, pro_number, shipment["order_id"]),
            )

            # Log event
            cur.execute(
                """
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'bol_created', %s, 'bol_api')
                """,
                (
                    shipment["order_id"],
                    json.dumps({
                        "shipment_id": shipment_id,
                        "warehouse": warehouse_name,
                        "shipper_name": shipper_name,
                        "pro_number": pro_number,
                        "bol_pdf_url": bol_pdf_url,
                        "weight_lbs": weight,
                        "is_residential": is_residential,
                        "pickup_date": pickup_date or "today",
                        "rl_attempts": result.get("attempts", 1),
                        "forced": bool(force),
                    }),
                ),
            )

    print(f"[BOL] Order {shipment['order_id']} / shipment {shipment_id} — BOL created, PRO: {pro_number}"
          + (" (FORCED past gates)" if force else ""))

    return {
        "status": "ok",
        "shipment_id": shipment_id,
        "order_id": shipment["order_id"],
        "warehouse": warehouse_name,
        "shipper_name": shipper_name,
        "pro_number": pro_number,
        "bol_pdf_url": bol_pdf_url,
        "weight_lbs": weight,
        "is_residential": is_residential,
        "rl_attempts": result.get("attempts", 1),
        "forced": bool(force),
        "message": f"BOL created — PRO {pro_number}",
    }


@bol_router.get("/bol/{shipment_id}/status")
def get_bol_status(shipment_id: str, _: bool = Depends(require_admin)):
    """Check BOL status for a shipment."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT shipment_id, order_id, warehouse, status,
                       pro_number, bol_sent, bol_sent_at, bol_url, is_residential
                FROM order_shipments
                WHERE shipment_id = %s
                """,
                (shipment_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Shipment {shipment_id} not found")

            return {
                "status": "ok",
                "shipment_id": shipment_id,
                "order_id": row["order_id"],
                "warehouse": row["warehouse"],
                "bol_sent": bool(row["bol_sent"]),
                "bol_sent_at": row["bol_sent_at"].isoformat() if row.get("bol_sent_at") else None,
                "pro_number": row.get("pro_number"),
                "bol_pdf_url": row.get("bol_url"),
                "is_residential": bool(row.get("is_residential", True)),
                "shipment_status": row["status"],
            }
