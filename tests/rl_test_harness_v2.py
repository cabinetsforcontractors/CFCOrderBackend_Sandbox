"""
rl_test_harness_v2.py
R+L API Price Validation — POC 5, then 100

Works with the reconciled CSV (rl_test_reconciled.csv) which has
warehouse ZIPs already mapped, city/state filled, and fields normalized.

USAGE:
  python rl_test_harness_v2.py rl_test_reconciled.csv              # first 5 (POC)
  python rl_test_harness_v2.py rl_test_reconciled.csv --count 100  # all 100

OUTPUT:
  Fills api_total_cost, api_customer_price, api_quote_number columns
  in a new _results.csv file. Then compare website_price (from Chrome
  extension) to api_customer_price. Pass = within ±5%.

NOTE ON LIFTGATE:
  Our API auto-applies liftgate when is_residential=true. The R+L website
  has a SEPARATE liftgate checkbox. The reconciled CSV tracks both fields
  so the Chrome extension can set them independently. If the API and
  website disagree, check whether the liftgate mismatch explains it.
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
DEFAULT_COUNT = 5


def call_rl_api(origin_zip, dest_zip, weight_lbs, is_residential, is_oversized):
    """
    Hit /quote/simple on the live rl-quote-sandbox.
    Returns full response dict.
    """
    params = {
        "origin_zip": origin_zip,
        "destination_zip": dest_zip,
        "weight_lbs": float(weight_lbs),
        "is_residential": str(is_residential).lower(),
        "is_oversized": str(is_oversized).lower(),
    }
    query = urllib.parse.urlencode(params)
    url = f"{RL_QUOTE_API}/quote/simple?{query}"

    req = urllib.request.Request(url, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else str(e)
        return {"success": False, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run(csv_path, count):
    print(f"\n{'='*74}")
    print(f"  R+L API PRICE VALIDATION TEST HARNESS v2")
    print(f"  API:   {RL_QUOTE_API}")
    print(f"  Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Count: {count}")
    print(f"{'='*74}\n")

    # Read CSV
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"  CSV: {csv_path}  ({len(rows)} rows)")
    test_rows = rows[:count]
    print(f"  Testing: {len(test_rows)} rows\n")

    # Header
    print(f"  {'ROW':>4s}  {'WH':<5s}  {'ORIG':<6s} → {'DEST':<6s}  "
          f"{'LBS':>6s}  {'RES':>3s} {'OVR':>3s} {'LFT':>3s}  "
          f"{'R+L TOTAL':>10s}  {'CUST PRICE':>10s}  {'QUOTE#':<14s} {'STATUS'}")
    print("  " + "-" * 98)

    ok = 0
    fail = 0

    for row in test_rows:
        rnum = row["row"]
        wh = row["warehouse_code"]
        o_zip = row["origin_zip"]
        d_zip = row["dest_zip"]
        wt = row["weight_lbs"]
        res = row["is_residential"].strip().upper() == "Y"
        ovr = row["is_oversized"].strip().upper() == "Y"
        lft = row["liftgate"].strip().upper() == "Y"

        # Call API
        result = call_rl_api(o_zip, d_zip, wt, res, ovr)

        if result.get("success") and result.get("quote"):
            q = result["quote"]
            tc = float(q.get("total_cost", 0))
            cp = float(q.get("customer_price", 0))
            qn = q.get("quote_number", "")
            row["api_total_cost"] = f"{tc:.2f}"
            row["api_customer_price"] = f"{cp:.2f}"
            row["api_quote_number"] = qn
            status = "OK"
            ok += 1
            print(f"  {rnum:>4s}  {wh:<5s}  {o_zip:<6s} → {d_zip:<6s}  "
                  f"{wt:>6s}  {'Y' if res else 'N':>3s} {'Y' if ovr else 'N':>3s} {'Y' if lft else 'N':>3s}  "
                  f"${tc:>9,.2f}  ${cp:>9,.2f}  {qn:<14s} {status}")
        else:
            err = result.get("error", "unknown")[:60]
            row["api_total_cost"] = "ERROR"
            row["api_customer_price"] = "ERROR"
            row["api_quote_number"] = ""
            fail += 1
            print(f"  {rnum:>4s}  {wh:<5s}  {o_zip:<6s} → {d_zip:<6s}  "
                  f"{wt:>6s}  {'Y' if res else 'N':>3s} {'Y' if ovr else 'N':>3s} {'Y' if lft else 'N':>3s}  "
                  f"{'—':>10s}  {'—':>10s}  {'':14s} FAIL: {err}")

        time.sleep(0.5)  # gentle on the API

    # Summary
    print(f"\n{'='*74}")
    print(f"  DONE: {ok} OK, {fail} FAILED out of {len(test_rows)}")

    # Liftgate mismatch warning
    mismatches = 0
    for row in test_rows:
        res = row["is_residential"].strip().upper() == "Y"
        lft = row["liftgate"].strip().upper() == "Y"
        if res != lft:
            mismatches += 1
    if mismatches:
        print(f"\n  ⚠️  LIFTGATE MISMATCH WARNING: {mismatches} rows where residential ≠ liftgate")
        print(f"     Our API auto-sets liftgate=residential. R+L website has separate checkbox.")
        print(f"     This WILL cause price differences on those rows — not a bug, just different inputs.")
        print(f"     For fair comparison, set liftgate = residential on the R+L website too,")
        print(f"     OR note which rows have the mismatch and exclude from ±5% check.")

    # Write results
    out_path = csv_path.replace(".csv", "_results.csv")
    if out_path == csv_path:
        out_path = "rl_results.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=test_rows[0].keys())
        writer.writeheader()
        writer.writerows(test_rows)

    print(f"\n  Results: {out_path}")
    print(f"\n  NEXT STEPS:")
    print(f"  1. Open {out_path} in Google Sheets")
    print(f"  2. Use Claude Chrome extension to fill 'website_price' from R+L website")
    print(f"  3. In Sheets, add formula for variance_pct:")
    print(f"     = (api_customer_price - website_price) / website_price * 100")
    print(f"  4. pass_fail = IF(ABS(variance_pct) <= 5, \"PASS\", \"FAIL\")")
    print(f"  5. If 5/5 PASS → rerun with --count 100")
    print(f"\n{'='*74}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("  Usage: python rl_test_harness_v2.py <csv_path> [--count N]")
        sys.exit(1)

    csv_path = sys.argv[1]
    count = DEFAULT_COUNT

    if "--count" in sys.argv:
        idx = sys.argv.index("--count")
        if idx + 1 < len(sys.argv):
            count = int(sys.argv[idx + 1])

    run(csv_path, count)
