"""
carrier_routes.py
Thin transport layer over freight_router.py (the carrier-routing engine).

GET /freight/carrier-quote/{order_id}?residential=&liftgate=  [admin]
    All-in freight quote for a whole order, leg by leg, with carrier routing.
    Tries Daylight first on Daylight-eligible (CA) origins, falls back to R+L,
    and picks the cheaper carrier that actually serves each lane. R+L legs add the
    residential bundle (res $75 + lift gate $62 + notification $13) because the
    rl-quote-sandbox omits it; every leg adds the supplier pallet fee (ROC/GHI
    $50/pallet, C&S flat $50). residential/liftgate are manual inputs now; Smarty
    + the checkout "need a lift gate?" tic fill them later. Nothing is sent — this
    is a quote for a human. Logic lives in freight_router.py.
"""

from fastapi import APIRouter, Depends

from auth import require_admin
from freight_router import carrier_quote_order

carrier_router = APIRouter(tags=["freight"])


@carrier_router.get("/freight/carrier-quote/{order_id}")
def get_carrier_quote(order_id: str, residential: bool = False, liftgate: bool = False,
                      _: bool = Depends(require_admin)):
    """All-in per-leg freight quote + carrier pick for an order [admin]."""
    return carrier_quote_order(order_id, residential=residential, liftgate=liftgate)
