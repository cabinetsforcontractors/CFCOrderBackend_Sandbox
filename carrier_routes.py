"""
carrier_routes.py
Thin transport layer over freight_router.py (the carrier-routing engine).

GET /freight/carrier-quote/{order_id}?residential=&liftgate=&origin_zip=  [admin]
    All-in freight quote for a whole order, leg by leg, with carrier routing.
    Tries Daylight first on Daylight-eligible (CA) origins, falls back to R+L,
    and picks the cheaper carrier that actually serves each lane. R+L legs add the
    residential bundle (res $75 + lift gate $62 + notification $13) because the
    rl-quote-sandbox omits it; every leg adds the supplier pallet fee (ROC/GHI
    $50/pallet, C&S flat $50).

    residential is tri-state:
      - omit it            -> auto-detect via Smarty on the ship-to (assume
                              residential if Smarty is down)
      - ?residential=true  -> force residential
      - ?residential=false -> force commercial
    liftgate stays a manual input (the "need a lift gate?" checkout tic feeds it
    later). origin_zip picks which Cabinet & Stone CA warehouse the shipment
    leaves from (90723 Paramount default / 90660 Pico Rivera) — CA legs only.
    Nothing is sent — this is a quote for a human. Logic in freight_router.py.
"""

from typing import Optional

from fastapi import APIRouter, Depends

from auth import require_admin
from freight_router import carrier_quote_order

carrier_router = APIRouter(tags=["freight"])


@carrier_router.get("/freight/carrier-quote/{order_id}")
def get_carrier_quote(order_id: str, residential: Optional[bool] = None,
                      liftgate: bool = False, origin_zip: Optional[str] = None,
                      _: bool = Depends(require_admin)):
    """All-in per-leg freight quote + carrier pick for an order [admin].
    Omit `residential` to auto-detect it via Smarty. `origin_zip` picks the
    CA warehouse (90723 Paramount / 90660 Pico Rivera) for CA legs."""
    return carrier_quote_order(order_id, residential=residential,
                               liftgate=liftgate, origin_zip=origin_zip)
