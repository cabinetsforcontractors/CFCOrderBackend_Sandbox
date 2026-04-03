"""
shipping_routes.py
FastAPI router for Shipping: R+L Carriers (direct API), Shippo, RTA Database.

Phase 5: Extracted from main.py
Phase 5C: require_admin wired to all write/delete endpoints

Mount in main.py with:
    from shipping_routes import shipping_router
    app.include_router(shipping_router)

Endpoints — R+L Carriers (direct):
    GET    /rl/status
    GET    /rl/test
    GET    /rl/quote
    GET    /rl/track/{pro_number}
    POST   /rl/bol                              [admin]
    GET    /rl/bol/{pro_number}
    GET    /rl/bol/{pro_number}/pdf
    GET    /rl/bol/{pro_number}/labels
    POST   /rl/pickup                           [admin]
    POST   /rl/pickup/pro/{pro_number}          [admin]
    GET    /rl/pickup/pro/{pro_number}
    DELETE /rl/pickup/pro/{pro_number}          [admin]
    GET    /rl/pickup/{pickup_id}
    DELETE /rl/pickup/{pickup_id}               [admin]
    POST   /rl/notify                           [admin]
    GET    /rl/notify/{pro_number}
    POST   /rl/order/{order_id}/create-bol      [admin]
    POST   /rl/order/{order_id}/pickup          [admin]
    GET    /rl/order/{order_id}/shipments

Endpoints — Shippo:
    GET    /shippo/status
    GET    /shippo/rates
    POST   /shippo/test                         [admin]

Endpoints — RTA Database:
    GET    /rta/status
    POST   /rta/init                            [admin]
    GET    /rta/sku/{sku}
    POST   /rta/calculate-weight

NOTE: /proxy/* (rl-quote-sandbox microservice) is handled by rl_quote_proxy.py.
"""

import os
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from config import SHIPPO_API_KEY

# =============================================================================
# OPTIONAL MODULE LOADS
# =============================================================================

try:
    from rl_carriers import (
        get_simple_quote as rl_get_simple_quote,
        get_rate_quote as rl_get_rate_quote,
        test_connection as rl_test_connection,
        is_configured as rl_is_configured,
        track_shipment as rl_track_shipment,
    )
    RL_CARRIERS_LOADED = True
except ImportError:
    RL_CARRIERS_LOADED = False
    print("[STARTUP] shipping_routes: rl_carriers module not found")

try:
    from shippo_rates import get_simple_rate, test_shippo as _test_shippo
    SHIPPO_ENABLED = bool(SHIPPO_API_KEY)
except ImportError:
    SHIPPO_ENABLED = False
    print("[STARTUP] shipping_routes: shippo_rates module not found")

try:
    from rta_database import (
        init_rta_table,
        get_sku_info,
        calculate_order_weight_and_flags,
        get_rta_stats,
    )
    RTA_DB_ENABLED = True
except ImportError:
    RTA_DB_ENABLED = False
    print("[STARTUP] shipping_routes: rta_database module not found")


# =============================================================================
# ROUTER
# =============================================================================

shipping_router = APIRouter(tags=["shipping"])


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class RLBolRequest(BaseModel):
    """Request model for creating BOL"""
    # Shipper
    shipper_name: str
    shipper_address: str
    shipper_city: str
    shipper_state: str
    shipper_zip: str
    shipper_phone: str
    shipper_address2: Optional[str] = ""
    # Consignee
    consignee_name: str
    consignee_address: str
    consignee_city: str
    consignee_state: str
    consignee_zip: str
    consignee_phone: str
    consignee_address2: Optional[str] = ""
    consignee_email: Optional[str] = ""
    # Shipment
    weight_lbs: int
    pieces: int = 1
    description: str = "RTA Cabinets"
    freight_class: str = "85"
    # Reference
    po_number: Optional[str] = ""
    quote_number: Optional[str] = ""
    special_instructions: Optional[str] = ""
    # Pickup
    include_pickup: bool = False
    pickup_date: Optional[str] = None
    pickup_ready_time: str = "09:00"
    pickup_close_time: str = "17:00"


class RLPickupRequest(BaseModel):
    """Request model for creating a standalone pickup"""
    shipper_name: str
    shipper_address: str
    shipper_city: str
    shipper_state: str
    shipper_zip: str
    shipper_phone: str
    shipper_address2: Optional[str] = ""
    dest_city: str
    dest_state: str
    dest_zip: str
    weight_lbs: int
    pieces: int = 1
    pickup_date: Optional[str] = None
    ready_time: str = "09:00"
    close_time: str = "17:00"
    contact_name: Optional[str] = ""
    contact_email: Optional[str] = ""
    additional_instructions: Optional[str] = ""


class RLNotificationRequest(BaseModel):
    """Request model for setting up shipment notifications"""
    pro_number: str
    email_addresses: List[str]
    events: Optional[List[str]] = None  # Default: all events


# =============================================================================
# R+L CARRIERS — STATUS / TEST / QUOTE / TRACK
# =============================================================================

@shipping_router.get("/rl/status")
def rl_status():
    """Check R+L Carriers API configuration status."""
    if not RL_CARRIERS_LOADED:
        return {"configured": False, "message": "rl_carriers module not loaded"}

    env_key = os.environ.get("RL_CARRIERS_API_KEY", "")
    return {
        "configured": rl_is_configured(),
        "module_loaded": True,
        "api_url": "https://api.rlc.com",
        "key_length": len(env_key) if env_key else 0,
        "key_prefix": (env_key[:8] + "...") if len(env_key) > 8 else "not set",
    }


@shipping_router.get("/rl/test")
def rl_test():
    """Test R+L Carriers API connection."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")
    return rl_test_connection()


@shipping_router.get("/rl/quote")
def rl_quote(
    origin_zip: str,
    dest_zip: str,
    weight_lbs: int,
    freight_class: str = "85",
):
    """Get LTL freight quote from R+L Carriers."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")

    try:
        result = rl_get_simple_quote(
            origin_zip=origin_zip,
            dest_zip=dest_zip,
            weight_lbs=weight_lbs,
            freight_class=freight_class,
        )
        return {"status": "ok", "quote": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.get("/rl/track/{pro_number}")
def rl_track(pro_number: str):
    """Track shipment by PRO number."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")

    try:
        return {"status": "ok", "shipment": rl_track_shipment(pro_number)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =============================================================================
# R+L CARRIERS — BOL
# =============================================================================

@shipping_router.post("/rl/bol")
def rl_create_bol(request: RLBolRequest, _: bool = Depends(require_admin)):
    """Create Bill of Lading with R+L Carriers. [admin]"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")

    try:
        from rl_carriers import create_bol
        result = create_bol(
            shipper_name=request.shipper_name,
            shipper_address=request.shipper_address,
            shipper_address2=request.shipper_address2,
            shipper_city=request.shipper_city,
            shipper_state=request.shipper_state,
            shipper_zip=request.shipper_zip,
            shipper_phone=request.shipper_phone,
            consignee_name=request.consignee_name,
            consignee_address=request.consignee_address,
            consignee_address2=request.consignee_address2,
            consignee_city=request.consignee_city,
            consignee_state=request.consignee_state,
            consignee_zip=request.consignee_zip,
            consignee_phone=request.consignee_phone,
            consignee_email=request.consignee_email,
            weight_lbs=request.weight_lbs,
            pieces=request.pieces,
            description=request.description,
            freight_class=request.freight_class,
            po_number=request.po_number,
            quote_number=request.quote_number,
            special_instructions=request.special_instructions,
            include_pickup=request.include_pickup,
            pickup_date=request.pickup_date,
            pickup_ready_time=request.pickup_ready_time,
            pickup_close_time=request.pickup_close_time,
        )
        return {"status": "ok", "bol": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.get("/rl/bol/{pro_number}")
def rl_get_bol(pro_number: str):
    """Get BOL details by PRO number."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import get_bol
        return {"status": "ok", "bol": get_bol(pro_number)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.get("/rl/bol/{pro_number}/pdf")
def rl_get_bol_pdf(pro_number: str):
    """Get BOL as PDF (base64 encoded)."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import print_bol_pdf
        return {"status": "ok", "pdf_base64": print_bol_pdf(pro_number)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.get("/rl/bol/{pro_number}/labels")
def rl_get_labels(pro_number: str, num_labels: int = 4):
    """Get shipping labels as PDF (base64 encoded)."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import print_shipping_labels
        return {"status": "ok", "pdf_base64": print_shipping_labels(pro_number, num_labels)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =============================================================================
# R+L CARRIERS — PICKUP
# Note: static paths (/rl/pickup/pro/{pro}) registered BEFORE param paths
# (/rl/pickup/{pickup_id}) to avoid routing ambiguity.
# =============================================================================

@shipping_router.post("/rl/pickup/pro/{pro_number}")
def rl_pickup_for_pro(
    pro_number: str,
    pickup_date: Optional[str] = None,
    ready_time: str = "09:00 AM",
    close_time: str = "05:00 PM",
    contact_name: Optional[str] = "",
    contact_email: Optional[str] = "",
    _: bool = Depends(require_admin),
):
    """Schedule pickup for an existing BOL by PRO number. [admin]"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")

    try:
        from rl_carriers import create_pickup_for_pro
        result = create_pickup_for_pro(
            pro_number=pro_number,
            pickup_date=pickup_date,
            ready_time=ready_time,
            close_time=close_time,
            contact_name=contact_name,
            contact_email=contact_email,
        )
        return {"status": "ok", "pro_number": pro_number, "pickup": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.get("/rl/pickup/pro/{pro_number}")
def rl_get_pickup_by_pro(pro_number: str):
    """Get pickup request details by PRO number."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import get_pickup_by_pro
        return {"status": "ok", "pickup": get_pickup_by_pro(pro_number)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.delete("/rl/pickup/pro/{pro_number}")
def rl_cancel_pickup_by_pro(pro_number: str, reason: str = "Order cancelled", _: bool = Depends(require_admin)):
    """Cancel a pickup request by PRO number. [admin]"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import cancel_pickup_by_pro
        return {"status": "ok", "result": cancel_pickup_by_pro(pro_number, reason)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.post("/rl/pickup")
def rl_create_pickup(request: RLPickupRequest, _: bool = Depends(require_admin)):
    """Create standalone pickup request with R+L Carriers. [admin]"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")

    try:
        from rl_carriers import create_pickup_request
        result = create_pickup_request(
            shipper_name=request.shipper_name,
            shipper_address=request.shipper_address,
            shipper_address2=request.shipper_address2,
            shipper_city=request.shipper_city,
            shipper_state=request.shipper_state,
            shipper_zip=request.shipper_zip,
            shipper_phone=request.shipper_phone,
            dest_city=request.dest_city,
            dest_state=request.dest_state,
            dest_zip=request.dest_zip,
            weight_lbs=request.weight_lbs,
            pieces=request.pieces,
            pickup_date=request.pickup_date,
            ready_time=request.ready_time,
            close_time=request.close_time,
            contact_name=request.contact_name,
            contact_email=request.contact_email,
            additional_instructions=request.additional_instructions,
        )
        return {"status": "ok", "pickup": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.get("/rl/pickup/{pickup_id}")
def rl_get_pickup(pickup_id: int):
    """Get pickup request details by pickup ID."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import get_pickup_request
        return {"status": "ok", "pickup": get_pickup_request(pickup_id)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.delete("/rl/pickup/{pickup_id}")
def rl_cancel_pickup(pickup_id: int, reason: str = "Order cancelled", _: bool = Depends(require_admin)):
    """Cancel a pickup request by pickup ID. [admin]"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import cancel_pickup_request
        return {"status": "ok", "result": cancel_pickup_request(pickup_id, reason)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =============================================================================
# R+L CARRIERS — NOTIFICATIONS
# =============================================================================

@shipping_router.post("/rl/notify")
def rl_setup_notification(request: RLNotificationRequest, _: bool = Depends(require_admin)):
    """Set up shipment notifications. [admin]"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import setup_shipment_notification
        result = setup_shipment_notification(
            pro_number=request.pro_number,
            email_addresses=request.email_addresses,
            events=request.events,
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.get("/rl/notify/{pro_number}")
def rl_get_notification(pro_number: str):
    """Get notification settings for a shipment."""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    try:
        from rl_carriers import get_shipment_notification
        return {"status": "ok", "notifications": get_shipment_notification(pro_number)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =============================================================================
# R+L CARRIERS — ORDER-BASED BOL / PICKUP / SHIPMENTS
# =============================================================================

@shipping_router.post("/rl/order/{order_id}/create-bol")
def rl_create_order_bol(
    order_id: str,
    warehouse_code: str,
    include_pickup: bool = False,
    pickup_date: Optional[str] = None,
    special_instructions: Optional[str] = "",
    _: bool = Depends(require_admin),
):
    """
    Create BOL for a specific warehouse shipment from an order.
    Uses warehouse addresses from checkout.py and customer info from B2BWave. [admin]
    """
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")

    try:
        from checkout import WAREHOUSES, fetch_b2bwave_order, calculate_order_shipping
        from rl_carriers import create_bol

        warehouse = WAREHOUSES.get(warehouse_code)
        if not warehouse:
            return {"status": "error", "message": f"Unknown warehouse: {warehouse_code}"}

        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "message": f"Order {order_id} not found"}

        shipping = order_data.get("shipping_address", {})
        customer_name = order_data.get("customer_name", "Customer")
        company_name = order_data.get("company_name") or customer_name

        dest_address = {
            "address": shipping.get("address", ""),
            "city": shipping.get("city", ""),
            "state": shipping.get("state", ""),
            "zip": shipping.get("zip", ""),
            "country": shipping.get("country", "US"),
        }
        shipping_calc = calculate_order_shipping(order_data, dest_address)

        warehouse_shipment = next(
            (s for s in shipping_calc.get("shipments", []) if s.get("warehouse") == warehouse_code),
            None,
        )
        if not warehouse_shipment:
            return {
                "status": "error",
                "message": f"No shipment found for warehouse {warehouse_code} in order {order_id}",
            }

        weight = warehouse_shipment.get("weight", 100)
        items = warehouse_shipment.get("items", [])
        pieces = len(items) if items else 1

        item_descriptions = [
            f"{item.get('quantity', 1)}x {item.get('name', item.get('sku', 'Cabinet'))}"
            for item in items[:3]
        ]
        description = "; ".join(item_descriptions)
        if len(items) > 3:
            description += f" +{len(items) - 3} more items"
        if len(description) > 100:
            description = f"RTA Cabinets - {len(items)} items"

        quote_number = ""
        if warehouse_shipment.get("quote", {}).get("quote", {}):
            quote_number = warehouse_shipment["quote"]["quote"].get("quote_number", "")

        result = create_bol(
            shipper_name=warehouse.get("name"),
            shipper_address=warehouse.get("address", ""),
            shipper_city=warehouse.get("city"),
            shipper_state=warehouse.get("state"),
            shipper_zip=warehouse.get("zip"),
            shipper_phone=warehouse.get("phone", ""),
            consignee_name=company_name,
            consignee_address=shipping.get("address", ""),
            consignee_address2=shipping.get("address2", ""),
            consignee_city=shipping.get("city", ""),
            consignee_state=shipping.get("state", ""),
            consignee_zip=shipping.get("zip", ""),
            consignee_phone=order_data.get("customer_phone", ""),
            consignee_email=order_data.get("customer_email", ""),
            weight_lbs=int(weight),
            pieces=pieces,
            description=description,
            freight_class="85",
            po_number=order_id,
            quote_number=quote_number,
            special_instructions=special_instructions,
            include_pickup=include_pickup,
            pickup_date=pickup_date,
        )

        return {
            "status": "ok",
            "order_id": order_id,
            "warehouse": warehouse_code,
            "bol": result,
            "shipment_details": {
                "weight": weight,
                "pieces": pieces,
                "description": description,
            },
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.post("/rl/order/{order_id}/pickup")
def rl_create_order_pickup(
    order_id: str,
    warehouse_code: str,
    pickup_date: Optional[str] = None,
    ready_time: str = "09:00",
    close_time: str = "17:00",
    additional_instructions: Optional[str] = "",
    _: bool = Depends(require_admin),
):
    """Create pickup request for a warehouse shipment from an order. [admin]"""
    if not RL_CARRIERS_LOADED:
        raise HTTPException(status_code=503, detail="rl_carriers module not loaded")
    if not rl_is_configured():
        raise HTTPException(status_code=503, detail="RL_CARRIERS_API_KEY not configured")

    try:
        from checkout import WAREHOUSES, fetch_b2bwave_order, calculate_order_shipping
        from rl_carriers import create_pickup_request

        warehouse = WAREHOUSES.get(warehouse_code)
        if not warehouse:
            return {"status": "error", "message": f"Unknown warehouse: {warehouse_code}"}

        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "message": f"Order {order_id} not found"}

        shipping = order_data.get("shipping_address", {})
        dest_address = {
            "address": shipping.get("address", ""),
            "city": shipping.get("city", ""),
            "state": shipping.get("state", ""),
            "zip": shipping.get("zip", ""),
            "country": shipping.get("country", "US"),
        }
        shipping_calc = calculate_order_shipping(order_data, dest_address)

        warehouse_shipment = next(
            (s for s in shipping_calc.get("shipments", []) if s.get("warehouse") == warehouse_code),
            None,
        )
        if not warehouse_shipment:
            return {
                "status": "error",
                "message": f"No shipment found for warehouse {warehouse_code}",
            }

        weight = warehouse_shipment.get("weight", 100)
        items = warehouse_shipment.get("items", [])
        pieces = len(items) if items else 1

        result = create_pickup_request(
            shipper_name=warehouse.get("name"),
            shipper_address=warehouse.get("address", ""),
            shipper_city=warehouse.get("city"),
            shipper_state=warehouse.get("state"),
            shipper_zip=warehouse.get("zip"),
            shipper_phone=warehouse.get("phone", ""),
            dest_city=shipping.get("city", ""),
            dest_state=shipping.get("state", ""),
            dest_zip=shipping.get("zip", ""),
            weight_lbs=int(weight),
            pieces=pieces,
            pickup_date=pickup_date,
            ready_time=ready_time,
            close_time=close_time,
            contact_name=warehouse.get("name"),
            contact_email=order_data.get("customer_email", ""),
            additional_instructions=additional_instructions or f"Order #{order_id}",
        )

        return {
            "status": "ok",
            "order_id": order_id,
            "warehouse": warehouse_code,
            "pickup": result,
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


@shipping_router.get("/rl/order/{order_id}/shipments")
def rl_get_order_shipments(order_id: str):
    """Get R+L-ready shipment info for an order (for BOL creation UI)."""
    try:
        from checkout import WAREHOUSES, fetch_b2bwave_order, calculate_order_shipping

        order_data = fetch_b2bwave_order(order_id)
        if not order_data:
            return {"status": "error", "message": f"Order {order_id} not found"}

        shipping = order_data.get("shipping_address", {})
        dest_address = {
            "address": shipping.get("address", ""),
            "city": shipping.get("city", ""),
            "state": shipping.get("state", ""),
            "zip": shipping.get("zip", ""),
            "country": shipping.get("country", "US"),
        }
        shipping_calc = calculate_order_shipping(order_data, dest_address)

        shipments = []
        for s in shipping_calc.get("shipments", []):
            wh_code = s.get("warehouse")
            wh_info = WAREHOUSES.get(wh_code, {})
            shipments.append(
                {
                    "warehouse_code": wh_code,
                    "warehouse_name": wh_info.get("name", wh_code),
                    "warehouse_address": {
                        "address": wh_info.get("address", ""),
                        "city": wh_info.get("city", ""),
                        "state": wh_info.get("state", ""),
                        "zip": wh_info.get("zip", ""),
                        "phone": wh_info.get("phone", ""),
                    },
                    "weight": s.get("weight", 0),
                    "parcel_length": s.get("parcel_length"),
                    "items_count": len(s.get("items", [])),
                    "shipping_method": s.get("shipping_method"),
                    "shipping_cost": s.get("shipping_cost", 0),
                    "quote_number": s.get("quote", {})
                    .get("quote", {})
                    .get("quote_number"),
                    "needs_bol": s.get("shipping_method") == "ltl",
                }
            )

        customer_name = order_data.get("customer_name", "")

        return {
            "status": "ok",
            "order_id": order_id,
            "customer": {
                "name": customer_name,
                "email": order_data.get("customer_email", ""),
                "company": order_data.get("company_name", ""),
                "address": shipping.get("address", ""),
                "address2": shipping.get("address2", ""),
                "city": shipping.get("city", ""),
                "state": shipping.get("state", ""),
                "zip": shipping.get("zip", ""),
                "phone": order_data.get("customer_phone", ""),
            },
            "shipments": shipments,
            "total_shipping": shipping_calc.get("total_shipping", 0),
            "ltl_shipments_count": sum(1 for s in shipments if s.get("needs_bol")),
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


# =============================================================================
# SHIPPO — Small Package Shipping Rates
# =============================================================================

@shipping_router.get("/shippo/status")
def shippo_status():
    """Check Shippo API configuration status."""
    return {
        "configured": SHIPPO_ENABLED,
        "api_key_set": bool(SHIPPO_API_KEY),
        "message": "Shippo API configured"
        if SHIPPO_ENABLED
        else "Set SHIPPO_API_KEY environment variable",
    }


@shipping_router.get("/shippo/rates")
def get_shippo_rates(
    origin_zip: str,
    dest_zip: str,
    weight_lbs: float,
    is_residential: bool = True,
    length: Optional[float] = None,
):
    """
    Get small package shipping rates from Shippo.
    Example: /shippo/rates?origin_zip=30071&dest_zip=33859&weight_lbs=10
    Optional: &length=96 for long items (e.g. trim molding)
    """
    if not SHIPPO_ENABLED:
        raise HTTPException(status_code=503, detail="Shippo API not configured")

    return get_simple_rate(
        origin_zip=origin_zip,
        dest_zip=dest_zip,
        weight_lbs=weight_lbs,
        is_residential=is_residential,
        length=length,
    )


@shipping_router.post("/shippo/test")
def test_shippo_api(_: bool = Depends(require_admin)):
    """Test Shippo API connection. [admin]"""
    if not SHIPPO_ENABLED:
        raise HTTPException(status_code=503, detail="Shippo API not configured")
    return _test_shippo()


# =============================================================================
# RTA DATABASE — SKU Weights and Shipping Rules
# =============================================================================

@shipping_router.get("/rta/status")
def rta_status():
    """Check RTA database status and stats."""
    if not RTA_DB_ENABLED:
        return {"configured": False, "message": "RTA database module not loaded"}
    try:
        return {"configured": True, "stats": get_rta_stats()}
    except Exception as e:
        return {"configured": True, "error": str(e)}


@shipping_router.post("/rta/init")
def rta_init_table(_: bool = Depends(require_admin)):
    """Initialize the RTA products table. [admin]"""
    if not RTA_DB_ENABLED:
        raise HTTPException(status_code=503, detail="RTA database module not loaded")
    return init_rta_table()


@shipping_router.get("/rta/sku/{sku}")
def rta_get_sku(sku: str):
    """Look up a single SKU."""
    if not RTA_DB_ENABLED:
        raise HTTPException(status_code=503, detail="RTA database module not loaded")
    info = get_sku_info(sku)
    if not info:
        raise HTTPException(status_code=404, detail=f"SKU {sku} not found")
    return info


@shipping_router.post("/rta/calculate-weight")
def rta_calculate_weight(request: dict):
    """
    Calculate total weight and check for long-pallet items.
    Body: {"line_items": [{"sku": "NJGR-WF342", "quantity": 1}, ...]}
    """
    if not RTA_DB_ENABLED:
        raise HTTPException(status_code=503, detail="RTA database module not loaded")
    return calculate_order_weight_and_flags(request.get("line_items", []))
