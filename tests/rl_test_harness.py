"""
rl_test_harness.py
R+L API Price Validation Test Harness

Reads a CSV of real CFC orders, maps SKU prefix → warehouse,
hits the live rl-quote-sandbox API, and outputs results for
side-by-side comparison with R+L website manual quotes (via Chrome extension).

USAGE:
  python rl_test_harness.py orders.csv              # auto-detect columns, run first 5
  python rl_test_harness.py orders.csv --count 100  # run 100 orders
  python rl_test_harness.py orders.csv --list-columns  # show CSV columns and exit

After running, compare results.csv against Chrome extension manual quotes.
Pass criteria: API price within ±5% of R+L website price.
"""

import csv
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

# =============================================================================
# CONFIG
# =============================================================================

RL_QUOTE_API = "https://rl-quote-sandbox.onrender.com"
DEFAULT_FREIGHT_CLASS = "85"
DEFAULT_MARKUP = 50.00
DEFAULT_COUNT = 5  # POC = 5, then scale to 100

# =============================================================================
# SKU PREFIX → WAREHOUSE MAPPING
# Sourced from cfc-orders:checkout.py SKU_WAREHOUSE_MAP + cfc-data:warehouse_map.csv
# =============================================================================

WAREHOUSE_INFO = {
    "LI":               {"name": "Cabinetry Distribution", "city": "Interlachen",    "state": "FL", "zip": "32148"},
    "DL":               {"name": "DL Cabinetry",         "city": "Jacksonville",   "state": "FL", "zip": "32256"},
    "ROC":              {"name": "ROC Cabinetry",        "city": "Norcross",       "state": "GA", "zip": "30071"},
    "GHI":              {"name": "GHI Cabinets",         "city": "Palmetto",       "state": "FL", "zip": "34221"},
    "Go Bravura":       {"name": "Go Bravura",           "city": "Houston",        "state": "TX", "zip": "77066"},
    "Love":             {"name": "Love-Milestone",       "city": "Orlando",        "state": "FL", "zip": "32824"},
    "Cabinet & Stone":  {"name": "Cabinet & Stone TX",   "city": "Houston",        "state": "TX", "zip": "77043"},
    "Cabinet & Stone CA": {"name": "Cabinet & Stone CA", "city": "Paramount",      "state": "CA", "zip": "90723"},
    "DuraStone":        {"name": "DuraStone",            "city": "Houston",        "state": "TX", "zip": "77037"},
    "L&C":              {"name": "L&C Cabinetry",        "city": "Virginia Beach", "state": "VA", "zip": "23454"},
    "Linda":            {"name": "Dealer Cabinetry",     "city": "Bremen",         "state": "GA", "zip": "30110"},
}

SKU_PREFIX_MAP = {
    # LI
    "WSP": "LI", "GSP": "LI", "NBLK": "LI",
    # DL
    "RW": "DL", "UFS": "DL", "CS": "DL", "EBK": "DL", "BNG": "DL",
    # ROC
    "EWD": "ROC", "EGD": "ROC", "EMB": "ROC", "BC": "ROC",
    "DCW": "ROC", "DCT": "ROC", "DCH": "ROC", "NJGR": "ROC",
    "EJG": "ROC", "SNW": "ROC", "PG": "ROC",
    # GHI
    "APW": "GHI", "AKS": "GHI", "GRSH": "GHI", "NOR": "GHI",
    "SNS": "GHI",
    # Go Bravura
    "HGW": "Go Bravura", "EMW": "Go Bravura", "EGG": "Go Bravura",
    "URC": "Go Bravura", "WWW": "Go Bravura", "NDG": "Go Bravura",
    "NCC": "Go Bravura", "NBW": "Go Bravura", "BX": "Go Bravura",
    "URW": "Go Bravura",
    # Love-Milestone
    "HSS": "Love", "LGS": "Love", "LGSS": "Love", "DG": "Love",
    "EOK": "Love", "EWT": "Love", "SWO": "Love", "EDG": "Love",
    "RND": "Love", "RMW": "Love",
    # Cabinet & Stone
    "BSN": "Cabinet & Stone", "SGCS": "Cabinet & Stone",
    "WOCS": "Cabinet & Stone", "EWSCS": "Cabinet & Stone",
    "CAWN": "Cabinet & Stone", "ESCS": "Cabinet & Stone",
    "BSW": "Cabinet & Stone", "SAVNG": "Cabinet & Stone",
    "MSCS": "Cabinet & Stone CA",
    # DuraStone
    "NSN": "DuraStone", "NBDS": "DuraStone", "CMEN": "DuraStone",
    "SIV": "DuraStone", "NSLS": "DuraStone",
    # L&C
    "SHLS": "L&C", "NS": "L&C", "RBLS": "L&C", "MGLS": "L&C",
    "BG": "L&C", "EDD": "L&C", "SWNG": "L&C",
}

OVERSIZED_KEYWORDS = ["PANTRY", "OVEN", "TALL", "96", "BROOM", "LINEN", "UTILITY"]


# =============================================================================
# SKU → WAREHOUSE LOOKUP
# =============================================================================

def extract_prefix(sku: str) -> str:
    """
    Extract the letter prefix from a SKU like 'WSP-W3630' → 'WSP'
    Handles: PREFIX-CABINET, PREFIX_CABINET, etc.
    """
    if not sku:
        return ""
    sku = sku.strip().upper()

    # Split on dash first
    parts = sku.split("-")
    prefix_part = parts[0]

    # Strip trailing digits from the prefix part
    prefix = ""
    for ch in prefix_part:
        if ch.isalpha():
            prefix += ch
        else:
            break
    # But some prefixes have digits mid-stream (not our case), so also try full first part
    # if pure-alpha extraction fails
    if not prefix:
        prefix = prefix_part

    return prefix


def lookup_warehouse(sku: str):
    """
    Given a full SKU like 'WSP-W3630', return (warehouse_code, warehouse_info) or (None, None).
    Tries longest prefix match first.
    """
    prefix = extract_prefix(sku)
    if not prefix:
        return None, None

    # Try longest match first (e.g., 'LGSS' before 'LGS')
    for length in range(len(prefix), 0, -1):
        candidate = prefix[:length]
        if candidate in SKU_PREFIX_MAP:
            wh_code = SKU_PREFIX_MAP[candidate]
            return wh_code, WAREHOUSE_INFO.get(wh_code)

    return None, None


def is_oversized(product_name: str) -> bool:
    """Check if product name suggests oversized shipment."""
    name_upper = (product_name or "").upper()
    return any(kw in name_upper for kw in OVERSIZED_KEYWORDS)


# =============================================================================
# API CALLER
# =============================================================================

def call_rl_api(origin_zip: str, dest_zip: str, weight_lbs: float,
                is_residential: bool = True, is_oversized_flag: bool = False) -> dict:
    """
    Hit the live rl-quote-sandbox /quote/simple endpoint.
    Returns the full API response dict.
    """
    params = {
        "origin_zip": origin_zip,
        "destination_zip": dest_zip,
        "weight_lbs": weight_lbs,
        "is_residential": str(is_residential).lower(),
        "is_oversized": str(is_oversized_flag).lower(),
    }
    query = urllib.parse.urlencode(params)
    url = f"{RL_QUOTE_API}/quote/simple?{query}"

    req = urllib.request.Request(url, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else str(e)
        return {"success": False, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# CSV COLUMN DETECTION
# =============================================================================

# Common column name patterns (case-insensitive)
COLUMN_PATTERNS = {
    "order_id":     ["order_id", "order", "order_number", "order_no", "id", "order #"],
    "sku":          ["sku", "product_sku", "product_code", "item_sku", "item_code"],
    "product_name": ["product_name", "product", "name", "description", "product_desc", "item_name", "item"],
    "quantity":     ["quantity", "qty", "count", "units"],
    "weight":       ["weight", "total_weight", "weight_lbs", "lbs", "ship_weight"],
    "dest_street":  ["address", "street", "ship_address", "shipping_address", "address1", "ship_street"],
    "dest_city":    ["city", "ship_city", "shipping_city"],
    "dest_state":   ["state", "province", "ship_state", "shipping_state"],
    "dest_zip":     ["zip", "zip_code", "postal_code", "postal", "ship_zip", "zipcode"],
    "price":        ["price", "unit_price", "amount", "total", "line_total"],
}


def detect_columns(headers: list) -> dict:
    """
    Auto-detect which CSV columns map to which fields.
    Returns dict of field_name → column_index.
    """
    mapping = {}
    headers_lower = [h.strip().lower().replace(" ", "_") for h in headers]

    for field, patterns in COLUMN_PATTERNS.items():
        for i, h in enumerate(headers_lower):
            if h in patterns:
                mapping[field] = i
                break

    return mapping


def print_columns(headers: list, sample_row: list):
    """Print CSV columns for user to verify."""
    print("\n  CSV COLUMNS DETECTED:")
    print("  " + "-" * 60)
    mapping = detect_columns(headers)
    for i, h in enumerate(headers):
        mapped = [k for k, v in mapping.items() if v == i]
        tag = f"  ← {mapped[0]}" if mapped else ""
        sample = sample_row[i] if i < len(sample_row) else ""
        print(f"  [{i:2d}] {h:<30s} {str(sample)[:30]:<30s}{tag}")
    print()

    unmapped = [k for k in COLUMN_PATTERNS if k not in mapping]
    if unmapped:
        print(f"  ⚠️  UNMAPPED FIELDS: {', '.join(unmapped)}")
        print("      Edit COLUMN_OVERRIDES in the script if auto-detect misses columns.\n")


# =============================================================================
# MANUAL COLUMN OVERRIDES
# If auto-detect doesn't find your columns, set them here.
# Use the column INDEX (0-based) from --list-columns output.
# Set to None to skip / use default.
# =============================================================================

COLUMN_OVERRIDES = {
    # "order_id": 0,
    # "sku": 1,
    # "product_name": 2,
    # "quantity": 3,
    # "weight": 4,
    # "dest_street": 5,
    # "dest_city": 6,
    # "dest_state": 7,
    # "dest_zip": 8,
}


# =============================================================================
# MAIN TEST RUNNER
# =============================================================================

def get_field(row, col_map, field, default=""):
    """Safely get a field value from a row using the column mapping."""
    idx = col_map.get(field)
    if idx is not None and idx < len(row):
        return row[idx].strip()
    return default


def run_test(csv_path: str, count: int = DEFAULT_COUNT):
    """
    Main test runner.
    Reads CSV, groups by order if possible, maps SKU→warehouse, calls API.
    """
    print(f"\n{'='*70}")
    print(f"  R+L API PRICE VALIDATION TEST HARNESS")
    print(f"  API: {RL_QUOTE_API}")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Orders to test: {count}")
    print(f"{'='*70}\n")

    # Read CSV
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = list(reader)

    print(f"  CSV: {csv_path}")
    print(f"  Rows: {len(rows)} (+ header)")
    print(f"  Columns: {len(headers)}")

    # Build column mapping (overrides take precedence)
    col_map = detect_columns(headers)
    col_map.update({k: v for k, v in COLUMN_OVERRIDES.items() if v is not None})

    print_columns(headers, rows[0] if rows else [])

    # Check minimum required columns
    required = ["sku", "dest_zip"]
    missing = [r for r in required if r not in col_map]
    if missing:
        print(f"  ❌ MISSING REQUIRED COLUMNS: {', '.join(missing)}")
        print(f"     Set COLUMN_OVERRIDES in the script or rename CSV columns.")
        print(f"     Run with --list-columns to see what was detected.")
        sys.exit(1)

    # Group rows by order_id (if available), otherwise treat each row as its own shipment
    if "order_id" in col_map:
        orders = {}
        for row in rows:
            oid = get_field(row, col_map, "order_id", "UNKNOWN")
            if oid not in orders:
                orders[oid] = []
            orders[oid].append(row)
        order_list = list(orders.items())[:count]
        print(f"  Unique orders: {len(orders)}, testing first {len(order_list)}\n")
    else:
        # Each row is a separate test
        order_list = [(f"ROW-{i+1}", [row]) for i, row in enumerate(rows[:count])]
        print(f"  No order_id column — treating each row as separate shipment.\n")

    # Results storage
    results = []
    errors = []

    print(f"  {'ORDER':<12s} {'WAREHOUSE':<18s} {'ORIGIN':<8s} {'DEST':<8s} "
          f"{'WEIGHT':>8s} {'OVERSZ':>6s} {'API_TOTAL':>10s} {'API_CUST':>10s} {'STATUS':<10s}")
    print("  " + "-" * 110)

    for order_id, order_rows in order_list:
        # Determine warehouse from first SKU in order
        first_sku = ""
        for row in order_rows:
            first_sku = get_field(row, col_map, "sku")
            if first_sku:
                break

        wh_code, wh_info = lookup_warehouse(first_sku)

        if not wh_info:
            prefix = extract_prefix(first_sku)
            msg = f"  {order_id:<12s} ❌ SKU '{first_sku}' prefix '{prefix}' → NO WAREHOUSE MATCH"
            print(msg)
            errors.append({"order_id": order_id, "sku": first_sku, "prefix": prefix, "error": "no warehouse match"})
            continue

        origin_zip = wh_info["zip"]

        # Destination
        dest_zip = ""
        dest_city = ""
        dest_state = ""
        for row in order_rows:
            dest_zip = get_field(row, col_map, "dest_zip")
            dest_city = get_field(row, col_map, "dest_city")
            dest_state = get_field(row, col_map, "dest_state")
            if dest_zip:
                break

        if not dest_zip:
            msg = f"  {order_id:<12s} ❌ No destination ZIP found"
            print(msg)
            errors.append({"order_id": order_id, "error": "no dest zip"})
            continue

        # Weight — use order-level weight if available, else sum quantities × 30
        weight = 0
        for row in order_rows:
            w = get_field(row, col_map, "weight", "0")
            try:
                weight = float(w)
                if weight > 0:
                    break
            except ValueError:
                pass

        if weight <= 0:
            # Fallback: count items × 30 lbs
            total_qty = 0
            for row in order_rows:
                q = get_field(row, col_map, "quantity", "1")
                try:
                    total_qty += int(float(q))
                except ValueError:
                    total_qty += 1
            weight = max(total_qty * 30, 100)

        # Oversized check
        oversized = False
        for row in order_rows:
            pname = get_field(row, col_map, "product_name")
            if is_oversized(pname):
                oversized = True
                break

        # Call API
        api_result = call_rl_api(
            origin_zip=origin_zip,
            dest_zip=dest_zip,
            weight_lbs=weight,
            is_residential=True,
            is_oversized_flag=oversized,
        )

        # Extract prices
        total_cost = 0.0
        customer_price = 0.0
        quote_number = ""
        transit_days = ""
        status = "ERROR"

        if api_result.get("success") and api_result.get("quote"):
            q = api_result["quote"]
            total_cost = float(q.get("total_cost", 0))
            customer_price = float(q.get("customer_price", 0))
            quote_number = q.get("quote_number", "")
            transit_days = q.get("transit_days", "")
            status = "OK"
        elif api_result.get("error"):
            status = "FAIL"

        result_row = {
            "order_id": order_id,
            "sku_sample": first_sku,
            "prefix": extract_prefix(first_sku),
            "warehouse": wh_code,
            "warehouse_name": wh_info["name"],
            "origin_zip": origin_zip,
            "origin_city": wh_info["city"],
            "origin_state": wh_info["state"],
            "dest_zip": dest_zip,
            "dest_city": dest_city,
            "dest_state": dest_state,
            "weight_lbs": weight,
            "is_oversized": oversized,
            "is_residential": True,
            "freight_class": DEFAULT_FREIGHT_CLASS,
            "api_total_cost": round(total_cost, 2),
            "api_customer_price": round(customer_price, 2),
            "api_quote_number": quote_number,
            "api_transit_days": transit_days,
            "rl_website_price": "",       # ← FILL IN FROM CHROME EXTENSION
            "variance_pct": "",           # ← AUTO-CALC AFTER FILLING WEBSITE PRICE
            "pass_fail": "",              # ← AUTO-CALC (±5%)
            "status": status,
            "api_error": api_result.get("error", ""),
        }
        results.append(result_row)

        osz = "YES" if oversized else "no"
        tc = f"${total_cost:,.2f}" if status == "OK" else "—"
        cp = f"${customer_price:,.2f}" if status == "OK" else "—"
        print(f"  {order_id:<12s} {wh_code:<18s} {origin_zip:<8s} {dest_zip:<8s} "
              f"{weight:>8.0f} {osz:>6s} {tc:>10s} {cp:>10s} {status:<10s}")

        # Be polite to the API
        time.sleep(1)

    # Summary
    print("\n" + "=" * 70)
    ok_count = sum(1 for r in results if r["status"] == "OK")
    fail_count = sum(1 for r in results if r["status"] != "OK")
    print(f"  RESULTS: {ok_count} OK, {fail_count} FAILED, {len(errors)} SKIPPED")

    if errors:
        print(f"\n  SKIPPED ORDERS:")
        for e in errors:
            print(f"    - {e.get('order_id')}: {e.get('error')} (SKU: {e.get('sku', 'N/A')})")

    # Write results CSV
    out_path = csv_path.replace(".csv", "_rl_results.csv")
    if out_path == csv_path:
        out_path = "rl_test_results.csv"

    fieldnames = list(results[0].keys()) if results else []
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"\n  Results written to: {out_path}")
    print(f"  → Open in Excel/Sheets")
    print(f"  → Fill 'rl_website_price' column with Chrome extension quotes")
    print(f"  → variance_pct = (api_customer_price - rl_website_price) / rl_website_price * 100")
    print(f"  → pass_fail = 'PASS' if abs(variance_pct) <= 5 else 'FAIL'")
    print(f"\n{'='*70}\n")

    return results, out_path


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("  ERROR: Provide a CSV file path.")
        print("  Example: python rl_test_harness.py orders.csv")
        print("           python rl_test_harness.py orders.csv --count 100")
        print("           python rl_test_harness.py orders.csv --list-columns")
        sys.exit(1)

    csv_path = sys.argv[1]
    count = DEFAULT_COUNT

    if "--list-columns" in sys.argv:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = next(reader)
            sample = next(reader, [])
        print_columns(headers, sample)
        sys.exit(0)

    if "--count" in sys.argv:
        idx = sys.argv.index("--count")
        if idx + 1 < len(sys.argv):
            count = int(sys.argv[idx + 1])

    run_test(csv_path, count)
