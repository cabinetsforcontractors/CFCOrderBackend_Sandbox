"""
checkout.py
B2BWave order checkout with R+L shipping quotes and Square payment
"""

import os
import re
import json
import time
import base64
import urllib.request
import urllib.error
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any

B2BWAVE_URL = os.environ.get("B2BWAVE_URL", "").strip().rstrip('/')
B2BWAVE_USERNAME = os.environ.get("B2BWAVE_USERNAME", "").strip()
B2BWAVE_API_KEY = os.environ.get("B2BWAVE_API_KEY", "").strip()

SQUARE_APP_ID = os.environ.get("SQUARE_APP_ID", "").strip()
SQUARE_ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "").strip()
SQUARE_LOCATION_ID = os.environ.get("SQUARE_LOCATION_ID", "").strip()
SQUARE_ENVIRONMENT = os.environ.get("SQUARE_ENVIRONMENT", "sandbox").strip()

RL_QUOTE_API_URL = os.environ.get("RL_QUOTE_API_URL", "https://rl-quote-sandbox.onrender.com").strip()
CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "").strip()

TARIFF_RATE = 0.08

# =============================================================================
# WAREHOUSE DEFINITIONS
# bol_shipper_name: what appears in the Shipper field on the R+L BOL.
# Format: "Cabinets For Contractors-{first_letter_of_name}{last_2_zip}"
# All codes are unique across all 12 warehouses — verified 2026-04-06.
# Supplier name never appears on the BOL.
# =============================================================================
WAREHOUSES = {
    'LI': {
        'name': 'Cabinetry Distribution',
        'address': '561 Keuka Rd', 'city': 'Interlachen', 'state': 'FL', 'zip': '32148',
        'phone': '(615) 410-6775',
        'bol_shipper_name': 'Cabinets For Contractors-C48',
    },
    'DL': {
        'name': 'DL Cabinetry',
        'address': '7825 Parramore Rd', 'city': 'Jacksonville', 'state': 'FL', 'zip': '32256',
        'phone': '904-886-5000',
        'bol_shipper_name': 'Cabinets For Contractors-D56',
    },
    'ROC': {
        'name': 'ROC Cabinetry',
        'address': '6015 Unity Dr', 'city': 'Norcross', 'state': 'GA', 'zip': '30071',
        'phone': '770-263-9800',
        'bol_shipper_name': 'Cabinets For Contractors-R71',
    },
    'GHI': {
        'name': 'GHI Cabinets',
        'address': '1402 10th Ave E', 'city': 'Palmetto', 'state': 'FL', 'zip': '34221',
        'phone': '941-981-9994',
        'bol_shipper_name': 'Cabinets For Contractors-G21',
    },
    'Go Bravura': {
        'name': 'Go Bravura',
        'address': '6910 Fulton St', 'city': 'Houston', 'state': 'TX', 'zip': '77066',
        'phone': '832-326-7003',
        'bol_shipper_name': 'Cabinets For Contractors-G66',
    },
    'Love': {
        'name': 'Love-Milestone',
        'address': '10963 Florida Crown Dr STE 100', 'city': 'Orlando', 'state': 'FL', 'zip': '32824',
        'phone': '407-601-7090',
        'bol_shipper_name': 'Cabinets For Contractors-L24',
    },
    'ARTISAN': {
        'name': 'Artisan (fallback)',
        'address': '6910 Fulton St', 'city': 'Houston', 'state': 'TX', 'zip': '77066',
        'phone': '832-326-7003',
        'bol_shipper_name': 'Cabinets For Contractors-A66',
    },
    'Cabinet & Stone': {
        'name': 'Cabinet & Stone',
        'address': '1760 Stebbins Dr', 'city': 'Houston', 'state': 'TX', 'zip': '77043',
        'phone': '713-468-8062',
        'bol_shipper_name': 'Cabinets For Contractors-C43',
    },
    'Cabinet & Stone CA': {
        'name': 'Cabinet & Stone CA',
        'address': '15500 Vermont Ave', 'city': 'Paramount', 'state': 'CA', 'zip': '90723',
        'phone': '562-774-8522',
        'bol_shipper_name': 'Cabinets For Contractors-C23',
    },
    'DuraStone': {
        'name': 'DuraStone',
        'address': '4506 Archie St', 'city': 'Houston', 'state': 'TX', 'zip': '77037',
        'phone': '281-445-4700',
        'bol_shipper_name': 'Cabinets For Contractors-D37',
    },
    'L&C': {
        'name': 'L&C Cabinetry',
        'address': '2157 Vista Circle', 'city': 'Virginia Beach', 'state': 'VA', 'zip': '23454',
        'phone': '757-425-5544',
        'bol_shipper_name': 'Cabinets For Contractors-L54',
    },
    'Linda': {
        'name': 'Dealer Cabinetry',
        'address': '200 Industrial Blvd', 'city': 'Bremen', 'state': 'GA', 'zip': '30110',
        'phone': '770-537-4422',
        'bol_shipper_name': 'Cabinets For Contractors-D10',
    },
}

SKU_WAREHOUSE_MAP = {
    'WSP': 'LI', 'GSP': 'LI', 'NBLK': 'LI',
    'RW': 'DL', 'UFS': 'DL', 'CS': 'DL', 'EBK': 'DL',
    'EWD': 'ROC', 'EGD': 'ROC', 'EMB': 'ROC', 'BC': 'ROC',
    'DCW': 'ROC', 'DCT': 'ROC', 'DCH': 'ROC', 'NJGR': 'ROC', 'EJG': 'ROC',
    'APW': 'GHI', 'AKS': 'GHI', 'GRSH': 'GHI', 'NOR': 'GHI', 'SNS': 'GHI', 'SNW': 'GHI',
    'HGW': 'Go Bravura', 'EMW': 'Go Bravura', 'EGG': 'Go Bravura', 'URC': 'Go Bravura',
    'WWW': 'Go Bravura', 'NDG': 'Go Bravura', 'NCC': 'Go Bravura', 'NBW': 'Go Bravura',
    'BX': 'Go Bravura', 'URW': 'Go Bravura',
    'HSS': 'Love', 'LGS': 'Love', 'LGSS': 'Love', 'DG': 'Love', 'EOK': 'Love', 'EWT': 'Love',
    'SWO': 'Love', 'EDG': 'Love', 'RND': 'Love', 'RMW': 'Love', 'BGR': 'Love',
    'BSN': 'Cabinet & Stone', 'SGCS': 'Cabinet & Stone', 'WOCS': 'Cabinet & Stone',
    'EWSCS': 'Cabinet & Stone', 'CAWN': 'Cabinet & Stone', 'ESCS': 'Cabinet & Stone',
    'CS-': 'Cabinet & Stone', 'BSW': 'Cabinet & Stone', 'MSCS': 'Cabinet & Stone CA',
    'NSN': 'DuraStone', 'NBDS': 'DuraStone', 'CMEN': 'DuraStone', 'SIV': 'DuraStone',
    'SHLS': 'L&C', 'NS': 'L&C', 'RBLS': 'L&C', 'MGLS': 'L&C', 'BG': 'L&C', 'EDD': 'L&C', 'SWNG': 'L&C',
}

OVERSIZED_KEYWORDS = ['PANTRY', 'OVEN', 'TALL', 'BROOM', 'LINEN', 'UTILITY']

# Maps customer address type selection to is_residential bool
ADDRESS_TYPE_MAP = {
    'residential_existing': True,
    'commercial_existing': False,
    'residential_new_construction': True,
    'commercial_new_construction': False,
    'rural': True,
    'military': True,
}

# B2BWave shipping_option_id for warehouse pickup
WAREHOUSE_PICKUP_OPTION_ID = '2'
WAREHOUSE_PICKUP_OPTION_NAME = 'warehouse pick up'


def detect_warehouse_pickup(order_data: dict) -> bool:
    """
    Returns True if this B2BWave order is a warehouse pickup order.
    Keyed on shipping_option_id == 2 OR shipping_option_name contains 'Warehouse Pick Up'.
    """
    opt_id   = str(order_data.get('shipping_option_id') or '').strip()
    opt_name = str(order_data.get('shipping_option_name') or '').strip().lower()
    return opt_id == WAREHOUSE_PICKUP_OPTION_ID or WAREHOUSE_PICKUP_OPTION_NAME in opt_name


def detect_item_dimensions(name: str):
    name_upper = name.upper()
    if re.search(r'\d+\.?\d*[WHD]?X\d+', name_upper):
        return ('ltl', None)
    numbers = re.findall(r'\b(\d+(?:\.\d+)?)\b', name)
    large_numbers = [float(n) for n in numbers if float(n) >= 84]
    if len(large_numbers) == 1:
        return ('long_package', int(large_numbers[0]))
    return ('standard', None)


def get_warehouse_for_sku(sku: str) -> Optional[str]:
    prefix = sku.split('-')[0] if '-' in sku else sku
    prefix = ''.join(c for c in prefix if not c.isdigit()).upper()
    return SKU_WAREHOUSE_MAP.get(prefix)


def is_oversized(product_name: str) -> bool:
    return any(keyword in product_name.upper() for keyword in OVERSIZED_KEYWORDS)


def group_items_by_warehouse(line_items: list) -> Dict[str, list]:
    groups = {}
    for item in line_items:
        sku = item.get('sku', '') or item.get('product_sku', '')
        warehouse = get_warehouse_for_sku(sku) or 'UNKNOWN'
        if warehouse not in groups:
            groups[warehouse] = []
        groups[warehouse].append(item)
    return groups


def get_bol_shipper_name(warehouse_code: str) -> str:
    """Return the BOL shipper name for a given warehouse code."""
    wh = WAREHOUSES.get(warehouse_code, {})
    return wh.get('bol_shipper_name', 'Cabinets For Contractors')


# =============================================================================
# ADDRESS VALIDATION — Smarty with 3-attempt retry
# =============================================================================

def validate_address_full(dest_address: dict) -> dict:
    """
    Call Smarty via rl-quote-sandbox to validate address.
    Retries up to 3 times with 1-second delays.
    """
    street = dest_address.get('address', '') or dest_address.get('street', '')
    city = dest_address.get('city', '')
    state = dest_address.get('state', '')
    zip_code = (dest_address.get('zip', '') or dest_address.get('postal_code', ''))[:5]

    if not street or not zip_code:
        return {
            'success': False,
            'is_residential': True,
            'is_uncertain': False,
            'address': None,
            'rdi': '',
            'error': 'Missing street address or ZIP code',
            'attempts': 0
        }

    payload = json.dumps({
        'street': street,
        'city': city,
        'state': state,
        'zip_code': zip_code
    }).encode()

    last_error = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                f"{RL_QUOTE_API_URL}/validate-address",
                data=payload,
                method='POST'
            )
            req.add_header('Content-Type', 'application/json')
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())

                if result.get('success') and result.get('address'):
                    addr = result['address']
                    is_res = bool(addr.get('is_residential', True))
                    rdi = addr.get('smarty_rdi', '')
                    is_uncertain = not rdi
                    label = 'residential' if is_res else 'commercial'
                    suffix = ' (uncertain)' if is_uncertain else ''
                    print(f"[CHECKOUT] Smarty attempt {attempt}/3 OK: {street[:30]}, {zip_code} → {label}{suffix}")
                    return {
                        'success': True,
                        'is_residential': is_res,
                        'is_uncertain': is_uncertain,
                        'address': addr,
                        'rdi': rdi,
                        'error': None,
                        'attempts': attempt
                    }
                else:
                    last_error = result.get('error', 'Address not found or invalid')
                    print(f"[CHECKOUT] Smarty attempt {attempt}/3 failed: {last_error}")

        except Exception as e:
            last_error = str(e)
            print(f"[CHECKOUT] Smarty attempt {attempt}/3 exception: {e}")

        if attempt < 3:
            time.sleep(1)

    print(f"[CHECKOUT] Smarty failed after 3 attempts: {street}, {zip_code}")
    return {
        'success': False,
        'is_residential': True,
        'is_uncertain': False,
        'address': None,
        'rdi': '',
        'error': last_error or 'Smarty validation failed after 3 attempts',
        'attempts': 3
    }


def validate_address_residential(dest_address: dict) -> bool:
    result = validate_address_full(dest_address)
    return result['is_residential']


# =============================================================================
# B2BWAVE HELPERS
# =============================================================================

def fetch_b2bwave_customer_address(customer_id: str) -> Optional[Dict]:
    if not B2BWAVE_URL or not B2BWAVE_API_KEY or not customer_id:
        return None
    try:
        url = f"{B2BWAVE_URL}/api/customers/{customer_id}.json"
        credentials = f"{B2BWAVE_USERNAME}:{B2BWAVE_API_KEY}"
        encoded = base64.b64encode(credentials.encode()).decode()
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Basic {encoded}')
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            customer = data.get('customer', data) if isinstance(data, dict) else data
            return {
                'company_name': customer.get('name', '') or customer.get('company_name', ''),
                'street': customer.get('address', ''),
                'street2': customer.get('address2', ''),
                'city': customer.get('city', ''),
                'state': customer.get('state', ''),
                'zip': customer.get('postal_code', '') or customer.get('zip', ''),
            }
    except Exception as e:
        print(f"[B2BWAVE] Error fetching customer {customer_id}: {e}")
        return None


def update_b2bwave_order_address(order_id: str, address: Dict) -> bool:
    if not B2BWAVE_URL or not B2BWAVE_API_KEY:
        return False
    try:
        url = f"{B2BWAVE_URL}/api/orders/{order_id}.json"
        credentials = f"{B2BWAVE_USERNAME}:{B2BWAVE_API_KEY}"
        encoded = base64.b64encode(credentials.encode()).decode()
        payload = json.dumps({
            'order': {
                'address': address.get('street', ''),
                'address2': address.get('street2', ''),
                'city': address.get('city', ''),
                'province': address.get('state', ''),
                'postal_code': address.get('zip', ''),
            }
        }).encode()
        req = urllib.request.Request(url, data=payload, method='PUT')
        req.add_header('Authorization', f'Basic {encoded}')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
            print(f"[B2BWAVE] Order {order_id} address updated")
            return True
    except Exception as e:
        print(f"[B2BWAVE] Error updating order {order_id} address: {e}")
        return False


# =============================================================================
# SHIPPING QUOTE
# =============================================================================

def get_shipping_quote(origin_zip: str, dest_zip: str, weight: float, is_residential: bool, is_oversized: bool = False) -> Dict:
    try:
        from rl_carriers import get_simple_quote, is_configured
        if is_configured():
            result = get_simple_quote(origin_zip=origin_zip, dest_zip=dest_zip, weight_lbs=int(weight), freight_class="85")
            return {
                'success': True,
                'quote': {
                    'quote_number': result.get('quote_number'),
                    'customer_price': result.get('net_charge'),
                    'service_days': result.get('service_days'),
                    'carrier': result.get('carrier', 'R+L Carriers'),
                    'service': result.get('service', 'Standard LTL')
                }
            }
        else:
            return {'success': False, 'error': 'R+L Carriers API not configured'}
    except ImportError as e:
        return {'success': False, 'error': f'rl_carriers module not available: {e}'}
    except Exception as e:
        return {'success': False, 'error': f'R+L API error: {str(e)}'}


SMALL_PACKAGE_WEIGHT_LIMIT = 70


def get_shippo_quote(origin_zip: str, dest_zip: str, weight: float, is_residential: bool = True, length: int = None) -> Dict:
    try:
        shippo_url = os.environ.get("SHIPPO_API_URL", "").strip() or os.environ.get("CFC_BACKEND_URL", "https://cfcorderbackend-sandbox.onrender.com").strip()
        params = {'origin_zip': origin_zip, 'dest_zip': dest_zip, 'weight_lbs': weight, 'is_residential': 'true' if is_residential else 'false'}
        if length:
            params['length'] = length
        query_string = '&'.join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(f"{shippo_url}/shippo/rates?{query_string}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {'success': False, 'error': str(e)}


def select_shipping_method(weight: float, items: list):
    max_length = None
    for item in items:
        dim_type, length = detect_item_dimensions(item.get('name', ''))
        if dim_type == 'ltl':
            return ('ltl', None)
        if dim_type == 'long_package':
            if max_length is None or length > max_length:
                max_length = length
    if weight >= SMALL_PACKAGE_WEIGHT_LIMIT:
        return ('ltl', None)
    return ('small_package', max_length)


def calculate_order_shipping(order_data: dict, dest_address: dict, is_residential_override: Optional[bool] = None) -> Dict:
    """
    Calculate shipping for an entire order, grouped by warehouse.
    is_residential_override: skips Smarty when customer has already classified their address.
    """
    line_items = order_data.get('line_items', []) or order_data.get('products', [])
    b2bwave_total_weight = order_data.get('total_weight', 0)

    rta_weight_info = None
    try:
        from rta_database import calculate_order_weight_and_flags
        rta_weight_info = calculate_order_weight_and_flags(line_items)
    except Exception as e:
        print(f"[CHECKOUT] RTA database not available: {e}")

    warehouse_groups = group_items_by_warehouse(line_items)
    dest_zip = dest_address.get('zip', '') or dest_address.get('postal_code', '')

    if is_residential_override is not None:
        is_residential = is_residential_override
        print(f"[CHECKOUT] Using customer-confirmed is_residential={is_residential}")
    else:
        is_residential = validate_address_residential(dest_address)

    shipments = []
    total_shipping = 0

    sku_to_rta = {}
    if rta_weight_info and rta_weight_info.get('items'):
        for item_info in rta_weight_info['items']:
            sku_to_rta[item_info.get('sku', '')] = item_info

    for warehouse_code, items in warehouse_groups.items():
        if warehouse_code == 'UNKNOWN':
            shipments.append({
                'warehouse': 'UNKNOWN', 'warehouse_name': 'Unknown Warehouse', 'items': items,
                'quote': {'success': False, 'error': 'Could not determine warehouse for items'},
                'shipping_cost': 0, 'shipping_method': 'unknown', 'is_residential': is_residential,
            })
            continue

        warehouse = WAREHOUSES.get(warehouse_code)
        if not warehouse:
            continue

        warehouse_weight = 0
        has_long_pallet = False
        for item in items:
            sku = item.get('sku', '')
            qty = item.get('quantity', 1)
            rta_info = sku_to_rta.get(sku)
            if rta_info:
                warehouse_weight += rta_info.get('line_weight', 0)
                if rta_info.get('requires_long_pallet'):
                    has_long_pallet = True
            else:
                warehouse_weight += 30 * qty

        if warehouse_weight == 0 and b2bwave_total_weight > 0 and len(warehouse_groups) == 1:
            warehouse_weight = b2bwave_total_weight

        weight = max(warehouse_weight, 1)
        oversized = has_long_pallet or any(is_oversized(item.get('name', '')) for item in items)
        shipping_method, parcel_length = select_shipping_method(weight, items)

        if shipping_method == 'small_package':
            quote = get_shippo_quote(origin_zip=warehouse['zip'], dest_zip=dest_zip, weight=weight, is_residential=is_residential, length=parcel_length)
            shipping_cost = quote['cheapest'].get('amount', 0) if quote.get('success') and quote.get('cheapest') else 0
        else:
            quote = get_shipping_quote(origin_zip=warehouse['zip'], dest_zip=dest_zip, weight=weight, is_residential=is_residential, is_oversized=oversized)
            shipping_cost = quote['quote'].get('customer_price', 0) if quote.get('success') and quote.get('quote') else 0

        shipments.append({
            'warehouse': warehouse_code,
            'warehouse_name': warehouse['name'],
            'bol_shipper_name': warehouse.get('bol_shipper_name', 'Cabinets For Contractors'),
            'origin_zip': warehouse['zip'],
            'items': items, 'weight': weight, 'parcel_length': parcel_length, 'is_oversized': oversized,
            'shipping_method': shipping_method, 'quote': quote, 'shipping_cost': shipping_cost,
            'is_residential': is_residential,
        })
        total_shipping += shipping_cost

    total_items = sum(float(item.get('price', 0) or 0) * int(item.get('quantity', 1) or 1) for item in line_items)
    tariff_amount = round(total_items * TARIFF_RATE, 2)
    grand_total = round(total_items + tariff_amount + total_shipping, 2)

    return {
        'shipments': shipments,
        'total_items': round(total_items, 2),
        'tariff_rate': TARIFF_RATE,
        'tariff_amount': tariff_amount,
        'total_shipping': round(total_shipping, 2),
        'grand_total': grand_total,
        'destination': dest_address,
        'is_residential': is_residential,
    }


def fetch_b2bwave_order(order_id: str) -> Optional[Dict]:
    if not B2BWAVE_URL or not B2BWAVE_API_KEY:
        return None
    try:
        url = f"{B2BWAVE_URL}/api/orders.json?id_eq={order_id}"
        credentials = f"{B2BWAVE_USERNAME}:{B2BWAVE_API_KEY}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Basic {encoded_credentials}')
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, list) and len(data) > 0:
                raw_order = data[0].get('order', data[0])
                order_products = raw_order.get('order_products', [])
                line_items = []
                for op in order_products:
                    product = op.get('order_product', op)
                    unit_price = float(product.get('final_price') or product.get('price') or 0)
                    qty = int(float(product.get('quantity', 1)))
                    line_items.append({
                        'sku': product.get('product_code', ''),
                        'name': product.get('product_name', ''),
                        'quantity': qty,
                        'price': unit_price,
                        'line_total': round(unit_price * qty, 2),
                    })
                total_weight_raw = raw_order.get('total_weight', 0)
                try:
                    total_weight = float(total_weight_raw) if total_weight_raw else 0
                except (ValueError, TypeError):
                    total_weight = 0
                submitted_at = raw_order.get('submitted_at', '')
                try:
                    order_date = datetime.fromisoformat(submitted_at.replace('Z', '+00:00')).strftime('%B %d, %Y') if submitted_at else ''
                except Exception:
                    order_date = submitted_at or ''
                order_total = float(raw_order.get('gross_total', 0) or 0)
                customer_email = raw_order.get('customer_email', '')
                customer_id = str(raw_order.get('customer_id', '')) if raw_order.get('customer_id') else None
                return {
                    'id': raw_order.get('id'),
                    'customer_id': customer_id,
                    'customer_name': raw_order.get('customer_name'),
                    'customer_email': customer_email,
                    'email': customer_email,
                    'customer_phone': raw_order.get('customer_phone', ''),
                    'company_name': raw_order.get('customer_company'),
                    'line_items': line_items,
                    'order_total': order_total,
                    'subtotal': order_total,
                    'order_date': order_date,
                    'total_weight': total_weight,
                    'shipping_option_id': str(raw_order.get('shipping_option_id') or ''),
                    'shipping_option_name': raw_order.get('shipping_option_name', ''),
                    'shipping_address': {
                        'address': raw_order.get('address', ''),
                        'address2': raw_order.get('address2', ''),
                        'city': raw_order.get('city', ''),
                        'state': raw_order.get('province', ''),
                        'zip': raw_order.get('postal_code', ''),
                        'country': raw_order.get('country', 'US'),
                    },
                    'comments': raw_order.get('comments_customer', ''),
                }
            return None
    except Exception as e:
        print(f"[B2BWAVE] Error fetching order {order_id}: {e}")
        return None


def create_square_payment_link(amount_cents: int, order_id: str, customer_email: str) -> Optional[str]:
    if not SQUARE_ACCESS_TOKEN or not SQUARE_LOCATION_ID:
        return None
    try:
        base_url = "https://connect.squareupsandbox.com" if SQUARE_ENVIRONMENT == "sandbox" else "https://connect.squareup.com"
        payload = {
            "idempotency_key": f"order-{order_id}-{datetime.now().timestamp()}",
            "quick_pay": {
                "name": f"CFC Order #{order_id}",
                "price_money": {"amount": amount_cents, "currency": "USD"},
                "location_id": SQUARE_LOCATION_ID
            },
            "pre_populated_data": {"buyer_email": customer_email} if customer_email else {}
        }
        if CHECKOUT_BASE_URL:
            payload["checkout_options"] = {"redirect_url": f"{CHECKOUT_BASE_URL}/payment-complete?order={order_id}", "ask_for_shipping_address": False}
        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"{base_url}/v2/online-checkout/payment-links", data=data, method='POST')
        req.add_header('Authorization', f'Bearer {SQUARE_ACCESS_TOKEN}')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Square-Version', '2024-01-18')
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()).get('payment_link', {}).get('url')
    except Exception as e:
        print(f"[SQUARE] Error: {e}")
        return None


def generate_checkout_token(order_id: str) -> str:
    secret = os.environ.get("CHECKOUT_SECRET", "default-secret-change-me")
    message = f"{order_id}-{datetime.now().strftime('%Y%m%d')}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()[:16]


def verify_checkout_token(order_id: str, token: str) -> bool:
    expected = generate_checkout_token(order_id)
    return hmac.compare_digest(token, expected)
