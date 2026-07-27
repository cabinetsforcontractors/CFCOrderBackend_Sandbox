"""
square_links.py — Square Payment Links API (William's word 2026-07-27:
"we need to build a Payment Links API for square").

Closes step 3 of the 1-2-3-4: the robot creates the payment link itself
(POST /v2/online-checkout/payment-links, same SQUARE_ACCESS_TOKEN the
payment sync already uses), so invoices stop waiting on a hand-made link.

DESIGN FACTS:
  - quick_pay link: name carries "INV-{order_id}" -> the Square payment's
    description carries it -> square_sync's existing matcher hits EXACTLY
    (proven live today: William's hand link named "INV-5732" matched).
  - checkout_options.redirect_url -> the NEW site (env SQUARE_REDIRECT_URL,
    default https://cabinetsforcontractors.com) — per-link, ours to control;
    the account-level receipt setting (old-website FYI) is separate.
  - links are deletable (DELETE /v2/online-checkout/payment-links/{id}) —
    canceled orders can have their links killed.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Dict, Optional

SQUARE_API_BASE = "https://connect.squareup.com/v2"


def _square_request(method: str, path: str, body: Optional[Dict] = None) -> Dict:
    token = os.environ.get("SQUARE_ACCESS_TOKEN", "").strip()
    if not token:
        raise Exception("SQUARE_ACCESS_TOKEN not configured")
    req = urllib.request.Request(
        f"{SQUARE_API_BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Square-Version": "2024-01-18"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raise Exception(f"Square API {e.code}: {(e.read() or b'').decode()[:400]}")


def create_payment_link(order_id: str, amount: float,
                        name: str = "", note: str = "") -> Dict:
    """Create a quick-pay link for an order. Returns {'url','id','long_url'}.
    name defaults to 'INV-{order_id}' — NEVER change that shape without
    checking square_sync's matcher (it is the payment-matching key)."""
    location_id = os.environ.get("SQUARE_LOCATION_ID", "").strip()
    if not location_id:
        raise Exception("SQUARE_LOCATION_ID not configured")
    cents = int(round(float(amount) * 100))
    if cents < 100:
        raise Exception(f"amount ${amount:.2f} below Square's $1.00 link minimum")
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "quick_pay": {
            "name": (name or f"INV-{order_id}")[:255],
            "price_money": {"amount": cents, "currency": "USD"},
            "location_id": location_id,
        },
        "checkout_options": {
            "redirect_url": os.environ.get(
                "SQUARE_REDIRECT_URL",
                "https://cabinetsforcontractors.com").strip(),
        },
    }
    if note:
        body["payment_note"] = note[:500]
    data = _square_request("POST", "/online-checkout/payment-links", body)
    link = data.get("payment_link") or {}
    if not link.get("url"):
        raise Exception(f"no url in Square response: {json.dumps(data)[:300]}")
    return {"id": link.get("id"), "url": link.get("url"),
            "long_url": link.get("long_url"),
            "order_id": link.get("order_id")}


def delete_payment_link(link_id: str) -> Dict:
    _square_request("DELETE", f"/online-checkout/payment-links/{link_id}")
    return {"status": "ok", "deleted": link_id}
