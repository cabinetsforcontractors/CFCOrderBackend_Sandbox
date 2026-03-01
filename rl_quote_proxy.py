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
    zipcode: str = ""

class FreightQuoteRequest(BaseModel):
    origin_zip: str
    dest_zip: str
    dest_city: str = ""
    dest_state: str = ""
    weight: float
    freight_class: str = "85"  # Always 85 for RTA cabinets
    is_residential: bool = True

class AutoQuoteRequest(BaseModel):
    """Combined validate + quote in one call"""
    origin_zip: str
    dest_street: str
    dest_city: str = ""
    dest_state: str = ""
    dest_zipcode: str = ""
    weight: float
    freight_class: str = "85"
    customer_markup: float = 50.00  # Default $50 markup per rules

# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _call_rl_sandbox(endpoint: str, method: str = "GET", data: dict = None, timeout: int = 30) -> dict:
    """Make request to rl-quote-sandbox service"""
    url = f"{RL_QUOTE_SANDBOX_URL.rstrip('/')}/{endpoint.lstrip('/')}"

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
        "zipcode": req.zipcode
    })
    return result


@router.post("/quote")
def proxy_freight_quote(req: FreightQuoteRequest):
    """
    Get R+L LTL freight quote.
    Returns carrier price (before customer markup).
    """
    result = _call_rl_sandbox("quote", method="POST", data={
        "origin_zip": req.origin_zip,
        "dest_zip": req.dest_zip,
        "dest_city": req.dest_city,
        "dest_state": req.dest_state,
        "weight": req.weight,
        "freight_class": req.freight_class,
        "is_residential": req.is_residential
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
        carrier_price: Raw R+L price
        customer_price: carrier_price + markup (default +$50)
        markup: The markup amount applied
    """
    # Step 1: Validate address
    try:
        address_result = _call_rl_sandbox("validate-address", method="POST", data={
            "street": req.dest_street,
            "city": req.dest_city,
            "state": req.dest_state,
            "zipcode": req.dest_zipcode
        })
    except HTTPException:
        # If address validation fails, still try to quote with raw address
        address_result = {
            "validated": False,
            "original": {
                "street": req.dest_street,
                "city": req.dest_city,
                "state": req.dest_state,
                "zipcode": req.dest_zipcode
            },
            "error": "Address validation unavailable, using original address"
        }

    # Extract validated ZIP (or fall back to original)
    validated_zip = req.dest_zipcode
    validated_city = req.dest_city
    validated_state = req.dest_state
    is_residential = True

    if address_result.get("validated") or address_result.get("address"):
        addr = address_result.get("address", address_result)
        validated_zip = addr.get("zipcode", addr.get("zip", req.dest_zipcode))
        validated_city = addr.get("city", req.dest_city)
        validated_state = addr.get("state", req.dest_state)
        # Smarty returns dpv_vacant or rdi field for residential detection
        rdi = addr.get("rdi", addr.get("is_residential", ""))
        if isinstance(rdi, str):
            is_residential = rdi.lower() != "commercial"
        elif isinstance(rdi, bool):
            is_residential = rdi

    # Step 2: Get freight quote
    quote_result = _call_rl_sandbox("quote", method="POST", data={
        "origin_zip": req.origin_zip,
        "dest_zip": validated_zip,
        "dest_city": validated_city,
        "dest_state": validated_state,
        "weight": req.weight,
        "freight_class": req.freight_class,
        "is_residential": is_residential
    })

    # Step 3: Extract price and apply markup
    carrier_price = 0.0
    quote_number = None
    service_days = None

    # Handle various response shapes from rl-quote-sandbox
    if quote_result.get("quote"):
        q = quote_result["quote"]
        carrier_price = float(q.get("net_charge", q.get("price", q.get("total", 0))))
        quote_number = q.get("quote_number", q.get("quoteNumber"))
        service_days = q.get("service_days", q.get("serviceDays"))
    elif quote_result.get("net_charge"):
        carrier_price = float(quote_result["net_charge"])
        quote_number = quote_result.get("quote_number")
        service_days = quote_result.get("service_days")
    elif quote_result.get("price"):
        carrier_price = float(quote_result["price"])
        quote_number = quote_result.get("quote_number")
        service_days = quote_result.get("service_days")

    customer_price = round(carrier_price + req.customer_markup, 2)

    return {
        "success": True,
        "validated_address": address_result,
        "quote": quote_result,
        "quote_number": quote_number,
        "carrier_price": round(carrier_price, 2),
        "customer_price": customer_price,
        "markup": req.customer_markup,
        "service_days": service_days,
        "is_residential": is_residential,
        "freight_class": req.freight_class,
        "weight": req.weight,
        "origin_zip": req.origin_zip,
        "timestamp": datetime.utcnow().isoformat()
    }


@router.get("/warehouses")
def proxy_warehouses():
    """List warehouses from rl-quote-sandbox"""
    result = _call_rl_sandbox("warehouses", method="GET")
    return result
