"""
rl_quote_proxy.py
Proxy module for rl-quote-sandbox microservice integration.
Calls the live rl-quote-sandbox Render service for address validation + freight quoting.
Phase 2 of CFC Orders Battle Plan.

Endpoints:
  POST /proxy/validate-address  — Validate + standardize via Smarty
  POST /proxy/quote             — Get R+L freight quote
  POST /proxy/auto-quote        — Validate + quote + markup in one call
  GET  /proxy/warehouses        — List available warehouses
  GET  /proxy/health            — Check rl-quote-sandbox connectivity
"""

import json
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import RL_QUOTE_SANDBOX_URL

router = APIRouter(prefix="/proxy", tags=["rl-quote-proxy"])

# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class AddressValidationRequest(BaseModel):
    street: str
    city: str = ""
    state: str = ""
    zip_code: str = ""

class FreightQuoteRequest(BaseModel):
    origin_zip: str
    dest_zip: str
    dest_city: str = ""
    dest_state: str = ""
    weight: float
    freight_class: str = "85"  # Always 85 for RTA cabinets
    is_residential: bool = True
    is_oversized: bool = False

class AutoQuoteRequest(BaseModel):
    """Combined validate + quote in one call"""
    origin_zip: str
    dest_street: str
    dest_city: str = ""
    dest_state: str = ""
    dest_zipcode: str = ""
    weight: float
    freight_class: str = "85"
    is_oversized: bool = False
    customer_markup: float = 50.00  # Default $50 markup per rules

# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _call_rl_sandbox(
    endpoint: str,
    method: str = "GET",
    data: dict = None,
    params: dict = None,
    timeout: int = 30
) -> dict:
    """
    Make request to rl-quote-sandbox service.

    Args:
        endpoint: API path (e.g. "quote/simple")
        method: HTTP method
        data: JSON body (for POST with body)
        params: Query string parameters (appended to URL)
        timeout: Request timeout in seconds
    """
    url = f"{RL_QUOTE_SANDBOX_URL.rstrip('/')}/{endpoint.lstrip('/')}"

    # Append query params if provided
    if params:
        query_string = urllib.parse.urlencode(params)
        url = f"{url}?{query_string}"

    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    if data:
        req.data = json.dumps(data).encode("utf-8")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        raise HTTPException(
            status_code=e.code,
            detail=f"rl-quote-sandbox error: {error_body}"
        )
    except urllib.error.URLError as e:
        raise HTTPException(
            status_code=503,
            detail=f"rl-quote-sandbox unreachable: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"rl-quote-sandbox proxy error: {str(e)}"
        )

# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/health")
def proxy_health():
    """Check if rl-quote-sandbox is reachable"""
    try:
        url = f"{RL_QUOTE_SANDBOX_URL.rstrip('/')}/warehouses"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {
                "status": "ok",
                "rl_quote_sandbox_url": RL_QUOTE_SANDBOX_URL,
                "rl_quote_sandbox_status": resp.status,
                "checked_at": datetime.utcnow().isoformat()
            }
    except Exception as e:
        return {
            "status": "error",
            "rl_quote_sandbox_url": RL_QUOTE_SANDBOX_URL,
            "error": str(e),
            "checked_at": datetime.utcnow().isoformat()
        }


@router.post("/validate-address")
def proxy_validate_address(req: AddressValidationRequest):
    """
    Validate and standardize a shipping address via Smarty.
    Returns corrected address with residential/commercial flag.
    """
    result = _call_rl_sandbox("validate-address", method="POST", data={
        "street": req.street,
        "city": req.city,
        "state": req.state,
        "zip_code": req.zip_code
    })
    return result


@router.post("/quote")
def proxy_freight_quote(req: FreightQuoteRequest):
    """
    Get R+L LTL freight quote via /quote/simple query params.
    Returns carrier price (before customer markup).
    """
    result = _call_rl_sandbox("quote/simple", method="POST", params={
        "origin_zip": req.origin_zip,
        "destination_zip": req.dest_zip,
        "weight_lbs": req.weight,
        "is_residential": str(req.is_residential).lower(),
        "is_oversized": str(req.is_oversized).lower()
    })
    return result


@router.post("/auto-quote")
def proxy_auto_quote(req: AutoQuoteRequest):
    """
    Combined workflow: validate address -> get freight quote -> apply markup.
    This is the main endpoint the frontend "Get Auto Quote" button calls.

    Returns:
        validated_address: Smarty-corrected address
        quote: R+L freight quote details
        carrier_price: Raw R+L price (total_cost from rl-quote-sandbox)
        customer_price: carrier_price + markup (default +$50)
        markup: The markup amount applied
    """
    # Step 1: Validate address
    try:
        address_result = _call_rl_sandbox("validate-address", method="POST", data={
            "street": req.dest_street,
            "city": req.dest_city,
            "state": req.dest_state,
            "zip_code": req.dest_zipcode
        })
    except HTTPException:
        # If address validation fails, still try to quote with raw address
        address_result = {
            "validated": False,
            "original": {
                "street": req.dest_street,
                "city": req.dest_city,
                "state": req.dest_state,
                "zip_code": req.dest_zipcode
            },
            "error": "Address validation unavailable, using original address"
        }

    # Extract validated ZIP (or fall back to original)
    validated_zip = req.dest_zipcode
    is_residential = True

    if address_result.get("validated") or address_result.get("address") or address_result.get("success"):
        addr = address_result.get("address", address_result)
        validated_zip = addr.get("zip_code", addr.get("zipcode", addr.get("zip", req.dest_zipcode)))
        # Smarty returns is_residential directly in the response
        if "is_residential" in address_result:
            is_residential = address_result["is_residential"]
        elif isinstance(addr.get("is_residential"), bool):
            is_residential = addr["is_residential"]

    # Step 2: Get freight quote via /quote/simple with POST + query params
    quote_result = _call_rl_sandbox("quote/simple", method="POST", params={
        "origin_zip": req.origin_zip,
        "destination_zip": validated_zip,
        "weight_lbs": req.weight,
        "is_residential": str(is_residential).lower(),
        "is_oversized": str(req.is_oversized).lower()
    })

    # Step 3: Extract price and apply markup
    # /quote/simple returns: { success, quote: { total_cost, customer_price, ... }, warnings }
    carrier_price = 0.0
    quote_number = None
    service_days = None

    if quote_result.get("success") and quote_result.get("quote"):
        q = quote_result["quote"]
        # total_cost = R+L's actual cost (base + fuel + accessorials)
        carrier_price = float(q.get("total_cost", 0))
        quote_number = q.get("quote_number")
        service_days = q.get("transit_days")
    elif quote_result.get("quote"):
        # Fallback: quote exists but success flag missing
        q = quote_result["quote"]
        carrier_price = float(q.get("total_cost", q.get("net_charge", q.get("price", 0))))
        quote_number = q.get("quote_number", q.get("quoteNumber"))
        service_days = q.get("transit_days", q.get("service_days"))
    elif quote_result.get("total_cost"):
        # Flat response shape fallback
        carrier_price = float(quote_result["total_cost"])
        quote_number = quote_result.get("quote_number")
        service_days = quote_result.get("transit_days")

    customer_price = round(carrier_price + req.customer_markup, 2)

    return {
        "success": True,
        "validated_address": address_result,
        "quote": quote_result.get("quote", quote_result),
        "quote_number": quote_number,
        "carrier_price": round(carrier_price, 2),
        "customer_price": customer_price,
        "markup": req.customer_markup,
        "service_days": service_days,
        "is_residential": is_residential,
        "freight_class": req.freight_class,
        "weight": req.weight,
        "is_oversized": req.is_oversized,
        "origin_zip": req.origin_zip,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.get("/warehouses")
def proxy_warehouses():
    """List warehouses from rl-quote-sandbox"""
    result = _call_rl_sandbox("warehouses", method="GET")
    return result
