"""
checkout.py
B2BWave order checkout with R+L shipping quotes and Square payment
"""

import os
import re
import json
import base64
import urllib.request
import urllib.error
import hmac
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any

# Config from environment
B2BWAVE_URL = os.environ.get("B2BWAVE_URL", "").strip().rstrip('/')
B2BWAVE_USERNAME = os.environ.get("B2BWAVE_USERNAME", "").strip()
B2BWAVE_API_KEY = os.environ.get("B2BWAVE_API_KEY", "").strip()

SQUARE_APP_ID = os.environ.get("SQUARE_APP_ID", "").strip()
SQUARE_ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "").strip()
SQUARE_LOCATION_ID = os.environ.get("SQUARE_LOCATION_ID", "").strip()
SQUARE_ENVIRONMENT = os.environ.get("SQUARE_ENVIRONMENT", "sandbox").strip()  # sandbox or production

RL_QUOTE_API_URL = os.environ.get("RL_QUOTE_API_URL", "https://rl-quote-sandbox.onrender.com").strip()

CHECKOUT_BASE_URL = os.environ.get("CHECKOUT_BASE_URL", "").strip()  # Your checkout page URL

# Warehouse data
# Warehouse information - full addresses for BOL creation
WAREHOUSES = {
    'LI': {
        'name': 'Liberty Industries',
        'address': '103 Trisket Ln',
        'city': 'Interlachen',
        'state': 'FL',
        'zip': '32148',
        'phone': '386-325-0825'
    },
    'DL': {
        'name': 'DL Cabinetry',
        'address': '7825 Parramore Rd',
        'city': 'Jacksonville',
        'state': 'FL',
        'zip': '32256',
        'phone': '904-886-5000'
    },
    'ROC': {
        'name': 'ROC Cabinetry',
        'address': '6015 Unity Dr',
        'city': 'Norcross',
        'state': 'GA',
        'zip': '30071',
        'phone': '770-263-9800'
    },
    'GHI': {
        'name': 'GHI Cabinets',
        'address': '1402 10th Ave E',
        'city': 'Palmetto',
        'state': 'FL',
        'zip': '34221',
        'phone': '941-981-9994'
    },
    'Go Bravura': {
        'name': 'Go Bravura',
        'address': '6910 Fulton St',
        'city': 'Houston',
        'state': 'TX',
        'zip': '77066',
        'phone': '832-326-7003'
    },
    'Love': {
        'name': 'Love-Milestone',
        'address': '7130 Overland Rd',
        'city': 'Orlando',
        'state': 'FL',
        'zip': '32824',
        'phone': '407-857-1985'
    },
    'ARTISAN': {
        'name': 'Artisan (fallback)',
        'address': '6910 Fulton St',
        'city': 'Houston',
        'state': 'TX',
        'zip': '77066',
        'phone': '832-326-7003'
    },
    'Cabinet & Stone': {
        'name': 'Cabinet & Stone',
        'address': '1760 Stebbins Dr',
        'city': 'Houston',
        'state': 'TX',
        'zip': '77043',
        'phone': '713-468-8062'
    },
    'Cabinet & Stone CA': {
        'name': 'Cabinet & Stone CA',
        'address': '15500 Vermont Ave',
        'city': 'Paramount',
        'state': 'CA',
        'zip': '90723',
        'phone': '562-774-8522'
    },
    'DuraStone': {
        'name': 'DuraStone',
        'address': '4506 Archie St',
        'city': 'Houston',
        'state': 'TX',
        'zip': '77037',
        'phone': '281-445-4700'
    },
    'L&C': {
        'name': 'L&C Cabinetry',
        'address': '2157 Vista Circle',
        'city': 'Virginia Beach',
        'state': 'VA',
        'zip': '23454',
        'phone': '757-425-5544'
    },
    'Linda': {
        'name': 'Dealer Cabinetry',
        'address': '200 Industrial Blvd',
        'city': 'Bremen',
        'state': 'GA',
        'zip': '30110',
        'phone': '770-537-4422'
    },
}

# SKU prefix to warehouse mapping
SKU_WAREHOUSE_MAP = {
    # LI
    'WSP': 'LI', 'GSP': 'LI', 'NBLK': 'LI',
    # DL
    'RW': 'DL', 'UFS': 'DL', 'CS': 'DL', 'EBK': 'DL',
    # ROC
    'EWD': 'ROC', 'EGD': 'ROC', 'EMB': 'ROC', 'BC': 'ROC',
    'DCW': 'ROC', 'DCT': 'ROC', 'DCH': 'ROC', 'NJGR': 'ROC', 'EJG': 'ROC',
    # GHI
    'APW': 'GHI', 'AKS': 'GHI', 'GRSH': 'GHI', 'NOR': 'GHI', 'SNS': 'GHI', 'SNW': 'GHI',
    # Go Bravura
    'HGW': 'Go Bravura', 'EMW': 'Go Bravura', 'EGG': 'Go Bravura', 'URC': 'Go Bravura',
    'WWW': 'Go Bravura', 'NDG': 'Go Bravura', 'NCC': 'Go Bravura', 'NBW': 'Go Bravura',
    'BX': 'Go Bravura', 'URW': 'Go Bravura',
    # Love-Milestone (preferred for these door styles, ARTISAN is fallback)
    'HSS': 'Love', 'LGS': 'Love', 'LGSS': 'Love', 'DG': 'Love',
    'EOK': 'Love', 'EWT': 'Love',
    # Cabinet & Stone (TX default, CA for MSCS)
    'BSN': 'Cabinet & Stone', 'SGCS': 'Cabinet & Stone', 'WOCS': 'Cabinet & Stone',
    'EWSCS': 'Cabinet & Stone', 'CAWN': 'Cabinet & Stone', 'ESCS': 'Cabinet & Stone', 'CS-': 'Cabinet & Stone',
    'BSW': 'Cabinet & Stone',  # Bright Snow White
    'MSCS': 'Cabinet & Stone CA',  # Ships from California
    # DuraStone
    'NSN': 'DuraStone', 'NBDS': 'DuraStone', 'CMEN': 'DuraStone', 'SIV': 'DuraStone',
    # L&C
    'SHLS': 'L&C', 'NS': 'L&C', 'RBLS': 'L&C', 'MGLS': 'L&C', 'BG': 'L&C', 'EDD': 'L&C', 'SWNG': 'L&C',
}

# Oversized detection keywords
OVERSIZED_KEYWORDS = ['PANTRY', 'OVEN', 'TALL', '96', 'BROOM', 'LINEN', 'UTILITY']


def detect_item_dimensions(name: str):
    """
    Parse product name to detect shipping dimension type.

    Rules (checked in order):
    1. X-separated numbers/dimensions → LTL truck shipping
       Examples: "1.5X96X.75 Ref Filler", "24WX84HX3D Refrigerator Panel", "1.5WX96H Refrigerator Panel"
    2. Single standalone number >= 84 → long package (small package, real length passed to Shippo)
       Examples: "96 Inside Corner Molding", "96 Crown Molding", "96 Outside Corner Molding"
    3. Otherwise → standard small package

    Returns:
        ('ltl', None)
        ('long_package', length_inches)
        ('standard', None)
    """
    name_upper = name.upper()

    # X-separated dimensions → LTL (e.g. "1.5X96X.75", "24WX84HX3D", "1.5WX96H")
    if re.search(r'\d+\.?\d*[WHD]?X\d+', name_upper):
        return ('ltl', None)

    # Single standalone number >= 84 → long package (Shippo with real length)
    numbers = re.findall(r'\b(\d+(?:\.\d+)?)\b', name)
    large_numbers = [float(n) for n in numbers if float(n) >= 84]
    if len(large_numbers) == 1:
        return ('long_package', int(large_numbers[0]))

    return ('standard', None)


def get_warehouse_for_sku(sku: str) -> Optional[str]:
    """Get warehouse code from SKU prefix"""
    # Extract prefix (first part before dash or numbers)
    prefix = sku.split('-')[0] if '-' in sku else sku
    # Remove trailing numbers
    prefix = ''.join(c for c in prefix if not c.isdigit()).upper()

    return SKU_WAREHOUSE_MAP.get(prefix)


def is_oversized(product_name: str) -> bool:
    """Check if product is oversized based on name"""
    name_upper = product_name.upper()
    return any(keyword in name_upper for keyword in OVERSIZED_KEYWORDS)


def group_items_by_warehouse(line_items: list) -> Dict[str, list]:
    """Group order items by their source warehouse"""
    groups = {}

    for item in line_items:
        sku = item.get('sku', '') or item.get('product_sku', '')
        warehouse = get_warehouse_for_sku(sku)

        if not warehouse:
            warehouse = 'UNKNOWN'

        if warehouse not in groups:
            groups[warehouse] = []

        groups[warehouse].append(item)

    return groups


def calculate_shipment_weight(items: list, order_total_weight: float = 0) -> float:
    """
    Calculate total weight for items.

    Priority:
    1. Use B2BWave's total_weight if available (for single-warehouse orders)
    2. Fall back to estimate: 30 lbs per cabinet

    Args:
        items: List of line items
        order_total_weight: B2BWave's total_weight for the order (0 if not available)

    Returns:
        Weight in lbs (minimum 1 lb)
    """
    # If B2BWave provided a weight, use it
    if order_total_weight and order_total_weight > 0:
        return max(order_total_weight, 1)

    # Fall back to estimate: 30 lbs per cabinet
    total_weight = 0
    for item in items:
        qty = item.get('quantity', 1)
        total_weight += qty * 30

    return max(total_weight, 1)  # Minimum 1 lb (removed 100 lb minimum)


def get_shipping_quote(origin_zip: str, dest_zip: str, weight: float, is_residential: bool, is_oversized: bool = False) -> Dict:
    """Get LTL shipping quote from R+L Carriers direct API"""
    # Try direct R+L Carriers API first
    try:
        from rl_carriers import get_simple_quote, is_configured
        if is_configured():
            result = get_simple_quote(
                origin_zip=origin_zip,
                dest_zip=dest_zip,
                weight_lbs=int(weight),
                freight_class="85"
            )
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


# =============================================================================
# SHIPPING METHOD SELECTION
# =============================================================================
#
# Rules (checked in order):
# 1. Item name has X-separated dimensions (e.g. "24WX84HX3D") → LTL
# 2. Weight >= 70 lbs → LTL
# 3. Item name has single number >= 84 (e.g. "96 Crown Molding") → small_package, pass real length to Shippo
# 4. Otherwise → small_package, standard dimensions
#
# Under 70 lbs → Shippo (UPS/USPS small package)
# 70 lbs and over → R+L (LTL freight)
# =============================================================================

SMALL_PACKAGE_WEIGHT_LIMIT = 70  # lbs - orders under this use Shippo


def get_shippo_quote(origin_zip: str, dest_zip: str, weight: float, is_residential: bool = True, length: int = None) -> Dict:
    """Get small package shipping quote from Shippo API"""
    try:
        shippo_url = os.environ.get("SHIPPO_API_URL", "").strip()
        if not shippo_url:
            # Use our backend's Shippo endpoint
            shippo_url = os.environ.get("CFC_BACKEND_URL", "https://cfcorderbackend-sandbox.onrender.com").strip()

        url = f"{shippo_url}/shippo/rates"
        params = {
            'origin_zip': origin_zip,
            'dest_zip': dest_zip,
            'weight_lbs': weight,
            'is_residential': 'true' if is_residential else 'false'
        }
        if length:
            params['length'] = length

        query_string = '&'.join(f"{k}={v}" for k, v in params.items())
        full_url = f"{url}?{query_string}"

        req = urllib.request.Request(full_url)

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data

    except Exception as e:
        return {'success': False, 'error': str(e)}


def select_shipping_method(weight: float, items: list):
    """
    Determine shipping method and parcel length.

    Returns: (method, parcel_length)
        method: 'small_package' or 'ltl'
        parcel_length: None for standard dims, int inches for long items

    Rules (checked in order):
    1. Item name has X-separated dimensions → LTL
    2. Weight >= 70 lbs → LTL
    3. Item name has single number >= 84 → small_package, use that length for Shippo
    4. Otherwise → small_package, standard dims
    """
    max_length = None
    for item in items:
        name = item.get('name', '')
        dim_type, length = detect_item_dimensions(name)
        if dim_type == 'ltl':
            print(f"[CHECKOUT] LTL forced by item dimensions in name: {name!r}")
            return ('ltl', None)
        if dim_type == 'long_package':
            if max_length is None or length > max_length:
                max_length = length
                print(f"[CHECKOUT] Long package detected ({length}\") from name: {name!r}")

    if weight >= SMALL_PACKAGE_WEIGHT_LIMIT:
        return ('ltl', None)

    return ('small_package', max_length)


def calculate_order_shipping(order_data: dict, dest_address: dict) -> Dict:
    """
    Calculate shipping for an entire order, grouped by warehouse.
    Uses Shippo for small packages (<70 lbs) and R+L for LTL (70+ lbs).

    Weight Priority:
    1. RTA database (SKU-level weights) - most accurate for split orders
    2. B2BWave total_weight - good for single warehouse orders
    3. Estimate at 30 lbs per item - fallback

    Returns:
        {
            'shipments': [
                {'warehouse': 'LI', 'items': [...], 'quote': {...}},
                {'warehouse': 'ROC', 'items': [...], 'quote': {...}},
            ],
            'total_shipping': 250.00,
            'total_items': 1500.00,
            'grand_total': 1750.00
        }
    """
    line_items = order_data.get('line_items', []) or order_data.get('products', [])

    # Get B2BWave's total weight (if available) as fallback
    b2bwave_total_weight = order_data.get('total_weight', 0)

    # Try to get weights from RTA database
    rta_weight_info = None
    try:
        from rta_database import calculate_order_weight_and_flags
        rta_weight_info = calculate_order_weight_and_flags(line_items)
        print(f"[CHECKOUT] RTA weight lookup: {rta_weight_info.get('total_weight')} lbs, long_pallet: {rta_weight_info.get('has_long_pallet_item')}")
    except Exception as e:
        print(f"[CHECKOUT] RTA database not available: {e}")

    # Group by warehouse
    warehouse_groups = group_items_by_warehouse(line_items)

    # Determine if residential
    is_residential = True  # Default to residential, could be overridden by Smarty validation

    dest_zip = dest_address.get('zip', '') or dest_address.get('postal_code', '')

    shipments = []
    total_shipping = 0

    # Build SKU to RTA info lookup
    sku_to_rta = {}
    if rta_weight_info and rta_weight_info.get('items'):
        for item_info in rta_weight_info['items']:
            sku_to_rta[item_info.get('sku', '')] = item_info

    for warehouse_code, items in warehouse_groups.items():
        if warehouse_code == 'UNKNOWN':
            shipments.append({
                'warehouse': 'UNKNOWN',
                'warehouse_name': 'Unknown Warehouse',
                'items': items,
                'quote': {'success': False, 'error': 'Could not determine warehouse for items'},
                'shipping_cost': 0,
                'shipping_method': 'unknown'
            })
            continue

        warehouse = WAREHOUSES.get(warehouse_code)
        if not warehouse:
            continue

        # Calculate weight for this warehouse's shipment using RTA data
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
                # Fallback: estimate 30 lbs per item
                warehouse_weight += 30 * qty

        # If no RTA data available at all, use B2BWave weight for single warehouse
        if warehouse_weight == 0 and b2bwave_total_weight > 0 and len(warehouse_groups) == 1:
            warehouse_weight = b2bwave_total_weight

        # Minimum 1 lb
        weight = max(warehouse_weight, 1)

        # Check for oversized using RTA long pallet flag OR keyword detection
        oversized = has_long_pallet or any(is_oversized(item.get('name', '')) for item in items)

        # Select shipping method — returns (method, parcel_length)
        shipping_method, parcel_length = select_shipping_method(weight, items)

        # Get quote from appropriate carrier
        if shipping_method == 'small_package':
            # Use Shippo for small packages; pass real length for long items (e.g. 96" trim)
            quote = get_shippo_quote(
                origin_zip=warehouse['zip'],
                dest_zip=dest_zip,
                weight=weight,
                is_residential=is_residential,
                length=parcel_length
            )

            shipping_cost = 0
            if quote.get('success') and quote.get('cheapest'):
                shipping_cost = quote['cheapest'].get('amount', 0)
        else:
            # Use R+L for LTL freight
            quote = get_shipping_quote(
                origin_zip=warehouse['zip'],
                dest_zip=dest_zip,
                weight=weight,
                is_residential=is_residential,
                is_oversized=oversized
            )

            shipping_cost = 0
            if quote.get('success') and quote.get('quote'):
                shipping_cost = quote['quote'].get('customer_price', 0)

        shipments.append({
            'warehouse': warehouse_code,
            'warehouse_name': warehouse['name'],
            'origin_zip': warehouse['zip'],
            'items': items,
            'weight': weight,
            'parcel_length': parcel_length,
            'is_oversized': oversized,
            'shipping_method': shipping_method,
            'quote': quote,
            'shipping_cost': shipping_cost
        })

        total_shipping += shipping_cost

    # Calculate item total
    total_items = 0
    for item in line_items:
        price = float(item.get('price', 0) or item.get('unit_price', 0) or 0)
        qty = int(item.get('quantity', 1) or 1)
        total_items += price * qty

    return {
        'shipments': shipments,
        'total_shipping': round(total_shipping, 2),
        'total_items': round(total_items, 2),
        'grand_total': round(total_items + total_shipping, 2),
        'destination': dest_address
    }


def fetch_b2bwave_order(order_id: str) -> Optional[Dict]:
    """Fetch order details from B2BWave API"""
    if not B2BWAVE_URL or not B2BWAVE_API_KEY:
        return None

    try:
        # Use list endpoint with filter (same as main.py)
        url = f"{B2BWAVE_URL}/api/orders.json?id_eq={order_id}"

        # Basic auth
        credentials = f"{B2BWAVE_USERNAME}:{B2BWAVE_API_KEY}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Basic {encoded_credentials}')

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            # API returns a list of {order: {...}} objects
            if isinstance(data, list) and len(data) > 0:
                # Extract the order from the nested structure
                raw_order = data[0].get('order', data[0])

                # Normalize the data structure for our checkout flow
                order_products = raw_order.get('order_products', [])
                line_items = []
                for op in order_products:
                    product = op.get('order_product', op)
                    line_items.append({
                        'sku': product.get('product_code', ''),
                        'name': product.get('product_name', ''),
                        'quantity': int(float(product.get('quantity', 1))),
                        'price': float(product.get('price', 0)),
                    })

                # Get total_weight from B2BWave (may be string like "8.0")
                total_weight_raw = raw_order.get('total_weight', 0)
                try:
                    total_weight = float(total_weight_raw) if total_weight_raw else 0
                except (ValueError, TypeError):
                    total_weight = 0

                return {
                    'id': raw_order.get('id'),
                    'customer_name': raw_order.get('customer_name'),
                    'customer_email': raw_order.get('customer_email'),
                    'customer_phone': raw_order.get('customer_phone', ''),
                    'company_name': raw_order.get('customer_company'),
                    'line_items': line_items,
                    'subtotal': float(raw_order.get('gross_total', 0)),
                    'total_weight': total_weight,  # B2BWave's actual weight
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
    """Create a Square payment link for the order"""
    if not SQUARE_ACCESS_TOKEN:
        print("[SQUARE] No access token configured")
        return None

    if not SQUARE_LOCATION_ID:
        print("[SQUARE] No location ID configured")
        return None

    try:
        # Square Checkout API
        base_url = "https://connect.squareupsandbox.com" if SQUARE_ENVIRONMENT == "sandbox" else "https://connect.squareup.com"
        url = f"{base_url}/v2/online-checkout/payment-links"

        payload = {
            "idempotency_key": f"order-{order_id}-{datetime.now().timestamp()}",
            "quick_pay": {
                "name": f"CFC Order #{order_id}",
                "price_money": {
                    "amount": amount_cents,
                    "currency": "USD"
                },
                "location_id": SQUARE_LOCATION_ID
            },
            "pre_populated_data": {
                "buyer_email": customer_email
            } if customer_email else {}
        }

        # Only add redirect_url if CHECKOUT_BASE_URL is set
        if CHECKOUT_BASE_URL:
            payload["checkout_options"] = {
                "redirect_url": f"{CHECKOUT_BASE_URL}/payment-complete?order={order_id}",
                "ask_for_shipping_address": False
            }

        data = json.dumps(payload).encode()

        print(f"[SQUARE] Creating payment link: {url}")
        print(f"[SQUARE] Payload: {payload}")

        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Authorization', f'Bearer {SQUARE_ACCESS_TOKEN}')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Square-Version', '2024-01-18')

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            print(f"[SQUARE] Response: {result}")
            return result.get('payment_link', {}).get('url')

    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"[SQUARE] HTTP Error {e.code}: {error_body}")
        return None
    except Exception as e:
        print(f"[SQUARE] Error creating payment link: {e}")
        return None


def generate_checkout_token(order_id: str) -> str:
    """Generate a secure token for checkout link"""
    secret = os.environ.get("CHECKOUT_SECRET", "default-secret-change-me")
    message = f"{order_id}-{datetime.now().strftime('%Y%m%d')}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()[:16]


def verify_checkout_token(order_id: str, token: str) -> bool:
    """Verify checkout token is valid"""
    expected = generate_checkout_token(order_id)
    return hmac.compare_digest(token, expected)
