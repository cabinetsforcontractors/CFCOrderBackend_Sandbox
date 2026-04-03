"""
shippo_rates.py
Shippo API integration for small package shipping rates (UPS, FedEx, USPS)
"""

import os
import json
import urllib.request
import urllib.error
from typing import Optional, Dict, List, Any

# Config from environment
SHIPPO_API_KEY = os.environ.get("SHIPPO_API_KEY", "").strip()
SHIPPO_API_URL = "https://api.goshippo.com"

# Default parcel dimensions for cabinets (can be overridden)
DEFAULT_PARCEL = {
    "length": "24",
    "width": "18",
    "height": "6",
    "distance_unit": "in",
    "mass_unit": "lb"
}


def shippo_request(endpoint: str, method: str = "GET", data: dict = None) -> Optional[Dict]:
    """Make authenticated request to Shippo API"""
    if not SHIPPO_API_KEY:
        print("[SHIPPO] No API key configured")
        return None

    url = f"{SHIPPO_API_URL}/{endpoint}"

    try:
        if data:
            req_data = json.dumps(data).encode()
            req = urllib.request.Request(url, data=req_data, method=method)
        else:
            req = urllib.request.Request(url, method=method)

        req.add_header('Authorization', f'ShippoToken {SHIPPO_API_KEY}')
        req.add_header('Content-Type', 'application/json')

        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"[SHIPPO] HTTP Error {e.code}: {error_body}")
        return None
    except Exception as e:
        print(f"[SHIPPO] Error: {e}")
        return None


def get_shipping_rates(
    origin_name: str,
    origin_street: str,
    origin_city: str,
    origin_state: str,
    origin_zip: str,
    dest_name: str,
    dest_street: str,
    dest_city: str,
    dest_state: str,
    dest_zip: str,
    weight_lbs: float,
    length: str = None,
    width: str = None,
    height: str = None,
    is_residential: bool = True
) -> Dict:
    """
    Get shipping rates from Shippo for small packages.

    Returns:
        {
            'success': True/False,
            'rates': [
                {
                    'provider': 'UPS',
                    'service': 'Ground',
                    'amount': 12.50,
                    'currency': 'USD',
                    'estimated_days': 5,
                    'rate_id': 'xxx'  # For purchasing label later
                },
                ...
            ],
            'cheapest': {...},  # Cheapest rate
            'fastest': {...},   # Fastest rate
            'error': None or error message
        }
    """
    if not SHIPPO_API_KEY:
        return {'success': False, 'error': 'Shippo API not configured', 'rates': []}

    # Build shipment request
    shipment_data = {
        "address_from": {
            "name": origin_name,
            "street1": origin_street,
            "city": origin_city,
            "state": origin_state,
            "zip": origin_zip,
            "country": "US"
        },
        "address_to": {
            "name": dest_name,
            "street1": dest_street,
            "city": dest_city,
            "state": dest_state,
            "zip": dest_zip,
            "country": "US",
            "is_residential": is_residential
        },
        "parcels": [{
            "length": length or DEFAULT_PARCEL["length"],
            "width": width or DEFAULT_PARCEL["width"],
            "height": height or DEFAULT_PARCEL["height"],
            "distance_unit": DEFAULT_PARCEL["distance_unit"],
            "weight": str(weight_lbs),
            "mass_unit": DEFAULT_PARCEL["mass_unit"]
        }],
        "async": False  # Wait for rates synchronously
    }

    print(f"[SHIPPO] Getting rates: {origin_zip} -> {dest_zip}, {weight_lbs} lbs, length={length or DEFAULT_PARCEL['length']}\"")

    result = shippo_request("shipments", method="POST", data=shipment_data)

    if not result:
        return {'success': False, 'error': 'Failed to create shipment', 'rates': []}

    if result.get('status') == 'ERROR':
        messages = result.get('messages', [])
        error_msg = messages[0].get('text') if messages else 'Unknown error'
        return {'success': False, 'error': error_msg, 'rates': []}

    # Parse rates
    raw_rates = result.get('rates', [])

    if not raw_rates:
        return {'success': False, 'error': 'No rates returned', 'rates': []}

    rates = []
    for r in raw_rates:
        rate = {
            'provider': r.get('provider'),
            'service': r.get('servicelevel', {}).get('name', ''),
            'service_token': r.get('servicelevel', {}).get('token', ''),
            'amount': float(r.get('amount', 0)),
            'currency': r.get('currency', 'USD'),
            'estimated_days': r.get('estimated_days'),
            'rate_id': r.get('object_id'),
            'arrives_by': r.get('arrives_by'),
            'duration_terms': r.get('duration_terms', '')
        }
        rates.append(rate)

    # Sort by price
    rates.sort(key=lambda x: x['amount'])

    # Find cheapest and fastest
    cheapest = rates[0] if rates else None

    # Find fastest (lowest estimated_days, excluding None)
    rates_with_days = [r for r in rates if r['estimated_days'] is not None]
    fastest = min(rates_with_days, key=lambda x: x['estimated_days']) if rates_with_days else cheapest

    return {
        'success': True,
        'rates': rates,
        'cheapest': cheapest,
        'fastest': fastest,
        'error': None,
        'shipment_id': result.get('object_id')
    }


def get_simple_rate(
    origin_zip: str,
    dest_zip: str,
    weight_lbs: float,
    is_residential: bool = True,
    length: float = None,
) -> Dict:
    """
    Simplified rate lookup using just ZIP codes and weight.
    Uses placeholder addresses since Shippo requires full addresses.

    Pass length (inches) for long items like trim molding (e.g. length=96).
    If not provided, uses default parcel dimensions (24x18x6 in).

    Returns the cheapest rate found.
    """
    return get_shipping_rates(
        origin_name="Warehouse",
        origin_street="123 Warehouse St",
        origin_city="City",
        origin_state="FL",
        origin_zip=origin_zip,
        dest_name="Customer",
        dest_street="456 Customer St",
        dest_city="City",
        dest_state="FL",
        dest_zip=dest_zip,
        weight_lbs=weight_lbs,
        length=str(int(length)) if length else None,
        is_residential=is_residential,
    )


def purchase_label(rate_id: str) -> Dict:
    """
    Purchase a shipping label for a given rate.

    Returns:
        {
            'success': True/False,
            'label_url': URL to download PDF label,
            'tracking_number': tracking number,
            'error': None or error message
        }
    """
    if not SHIPPO_API_KEY:
        return {'success': False, 'error': 'Shippo API not configured'}

    transaction_data = {
        "rate": rate_id,
        "label_file_type": "PDF",
        "async": False
    }

    result = shippo_request("transactions", method="POST", data=transaction_data)

    if not result:
        return {'success': False, 'error': 'Failed to create transaction'}

    if result.get('status') != 'SUCCESS':
        messages = result.get('messages', [])
        error_msg = messages[0].get('text') if messages else result.get('status', 'Unknown error')
        return {'success': False, 'error': error_msg}

    return {
        'success': True,
        'label_url': result.get('label_url'),
        'tracking_number': result.get('tracking_number'),
        'tracking_url_provider': result.get('tracking_url_provider'),
        'transaction_id': result.get('object_id'),
        'error': None
    }


def validate_address(
    name: str,
    street: str,
    city: str,
    state: str,
    zip_code: str,
    country: str = "US"
) -> Dict:
    """
    Validate an address using Shippo.

    Returns:
        {
            'valid': True/False,
            'is_residential': True/False,
            'suggested_address': {...} or None,
            'messages': [...]
        }
    """
    address_data = {
        "name": name,
        "street1": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "country": country,
        "validate": True
    }

    result = shippo_request("addresses", method="POST", data=address_data)

    if not result:
        return {'valid': False, 'error': 'Failed to validate address'}

    validation = result.get('validation_results', {})
    is_valid = validation.get('is_valid', False)

    return {
        'valid': is_valid,
        'is_residential': result.get('is_residential'),
        'messages': validation.get('messages', []),
        'suggested_address': {
            'street1': result.get('street1'),
            'street2': result.get('street2'),
            'city': result.get('city'),
            'state': result.get('state'),
            'zip': result.get('zip'),
            'country': result.get('country')
        } if is_valid else None
    }


# Quick test function
def test_shippo():
    """Test Shippo API connection and get sample rates"""
    print(f"[SHIPPO] Testing with API key: {SHIPPO_API_KEY[:10]}..." if SHIPPO_API_KEY else "[SHIPPO] No API key!")

    # Test rate lookup: ROC warehouse (30071) to Lake Wales FL (33859)
    result = get_simple_rate(
        origin_zip="30071",
        dest_zip="33859",
        weight_lbs=10,
        is_residential=True
    )

    print(f"[SHIPPO] Test result: {json.dumps(result, indent=2)}")
    return result


if __name__ == "__main__":
    test_shippo()
