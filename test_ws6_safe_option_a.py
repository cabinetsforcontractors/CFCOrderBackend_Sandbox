import json
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_URL = "https://cfcorderbackend-sandbox.onrender.com"
ADMIN_TOKEN = "CFC2026"
ORDER_ID = "5554"


def get_json(path: str, admin: bool = False):
    url = f"{BASE_URL}{path}"
    headers = {}
    if admin:
        headers["X-Admin-Token"] = ADMIN_TOKEN

    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTPError {e.code} for {url}")
        print(body)
        sys.exit(1)
    except URLError as e:
        print(f"URLError for {url}: {e}")
        sys.exit(1)


def expect(condition: bool, message: str):
    if condition:
        print(f"[PASS] {message}")
    else:
        print(f"[FAIL] {message}")
        sys.exit(1)


def main():
    print("=== WS6 SAFE OPTION A TEST ===")

    status, readiness = get_json("/debug/env-readiness", admin=True)
    expect(status == 200, "/debug/env-readiness returned 200")
    expect(readiness.get("recommended_posture") == "safe_option_a", "recommended_posture == safe_option_a")
    expect(readiness.get("email_allowlist_active") is True, "email_allowlist_active == true")
    expect(readiness.get("b2bwave_mutations_enabled") is False, "b2bwave_mutations_enabled == false")
    expect(readiness.get("matches_production_literal") is True, "matches_production_literal == true")

    print(json.dumps(readiness, indent=2))

    status, order_payload = get_json(f"/orders/{ORDER_ID}", admin=True)
    expect(status == 200, f"/orders/{ORDER_ID} returned 200")
    order = order_payload.get("order", {})
    expect(order.get("order_id") == ORDER_ID, f"order_id == {ORDER_ID}")
    expect(bool(order.get("email")), "order has customer email")
    expect(bool(order.get("current_status")), "order current_status is non-empty")

    print(json.dumps({
        "order_id": order.get("order_id"),
        "customer_name": order.get("customer_name"),
        "email": order.get("email"),
        "current_status": order.get("current_status"),
        "payment_received": order.get("payment_received"),
        "updated_at": order.get("updated_at"),
    }, indent=2))

    status, shipment_payload = get_json(f"/debug/shipment/{ORDER_ID}", admin=True)
    expect(status == 200, f"/debug/shipment/{ORDER_ID} returned 200")
    expect(shipment_payload.get("order_row_exists") is True, "order_row_exists == true")
    expect(int(shipment_payload.get("shipment_count_by_order_id", 0)) >= 1, "shipment_count_by_order_id >= 1")

    print(json.dumps({
        "order_id": shipment_payload.get("order_id"),
        "shipment_count_by_order_id": shipment_payload.get("shipment_count_by_order_id"),
        "shipment_count_by_pattern": shipment_payload.get("shipment_count_by_pattern"),
        "shipments_by_order_id": shipment_payload.get("shipments_by_order_id", []),
    }, indent=2))

    print("[PASS] WS6 safe Option A posture verified.")


if __name__ == "__main__":
    main()
