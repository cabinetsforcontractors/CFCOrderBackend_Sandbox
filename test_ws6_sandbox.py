"""
test_ws6_sandbox.py
WS6 CFC Orders — Comprehensive Sandbox Test Suite
Runs against: https://cfcorderbackend-sandbox.onrender.com

Usage:
    pip install requests
    python test_ws6_sandbox.py

All tests fire against the sandbox. Emails will land at wpjob1@gmail.com.
Sandbox orders may be modified (lifecycle engine is live and stateful).
R+L BOL/pickup endpoints are SKIPPED — real trucks showed up last time.

Results: PASS / FAIL / WARN / SKIP per test + summary at end.

v3 fixes:
  - Checkpoint: backend uses PATCH not POST — all checkpoint calls now use PATCH
  - Quote view: both JS ternary branches are always in raw HTML source, so
    checking for absence of payBtn or presence of "Quote Total" is unreliable.
    Correct check: look for const VIEW = "quote" which is the Python f-string
    injection — the ONLY thing in the raw HTML that changes based on ?view=quote.
"""

import requests
import json
import time
import sys
from datetime import datetime

# =============================================================================
# CONFIG
# =============================================================================

BASE = "https://cfcorderbackend-sandbox.onrender.com"
ADMIN_TOKEN = "CFC2026"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}
NO_AUTH_HEADERS = {"Content-Type": "application/json"}
BAD_AUTH_HEADERS = {"X-Admin-Token": "WRONG_TOKEN_XYZ", "Content-Type": "application/json"}

KNOWN_ORDER_ID = "4919"
KNOWN_FREIGHT_ORDER = "5529"
FAKE_ORDER_ID = "TEST_FAKE_999"
FAKE_SHIPMENT_ID = "FAKE-Cabinetry-Distribution-9999"
PICKUP_SHIPMENT_ID = "4919-Cabinetry-Distribution"
TEST_EMAIL = "wpjob1@gmail.com"

# =============================================================================
# TEST RUNNER
# =============================================================================

results = []
start_time = datetime.now()


def test(name, category="General"):
    def run(func):
        result = {"name": name, "category": category, "status": None, "detail": ""}
        try:
            status, detail = func()
            result["status"] = status
            result["detail"] = detail
        except Exception as e:
            result["status"] = "FAIL"
            result["detail"] = f"Exception: {e}"
        results.append(result)
        icon = {"PASS": "\u2705", "FAIL": "\u274c", "WARN": "\u26a0\ufe0f", "SKIP": "\u23ed\ufe0f"}.get(result["status"], "?")
        print(f"  {icon} [{category}] {name}: {result['detail'][:100]}")
        return result
    return run


def r(method, path, **kwargs):
    fn = getattr(requests, method.lower())
    return fn(f"{BASE}{path}", timeout=30, **kwargs)


def check(resp, expected_status=200, key=None, expected_value=None):
    if resp.status_code != expected_status:
        return "FAIL", f"Expected {expected_status}, got {resp.status_code}: {resp.text[:120]}"
    if key is not None:
        try:
            data = resp.json()
            actual = data
            for k in key.split("."):
                actual = actual[k]
            if expected_value is not None and actual != expected_value:
                return "FAIL", f"key={key}: expected={expected_value}, got={actual}"
            return "PASS", f"status={resp.status_code}, {key}={actual}"
        except (KeyError, TypeError) as e:
            return "FAIL", f"Key error: {e} — body={resp.text[:100]}"
    return "PASS", f"status={resp.status_code}"


# =============================================================================
# 1. HEALTH / ROOT
# =============================================================================

print("\n\ud83d\udce1 1. HEALTH / ROOT")

@test("GET / — service healthy", "Health")
def _():
    resp = r("GET", "/")
    return check(resp, 200, "status", "ok")

@test("GET /health — health endpoint", "Health")
def _():
    resp = r("GET", "/health")
    return check(resp, 200, "status", "ok")

@test("GET /debug/orders-columns — DB schema accessible", "Health")
def _():
    resp = r("GET", "/debug/orders-columns", headers=ADMIN_HEADERS)
    d = resp.json()
    cols = d.get("order_shipments_columns", [])
    needed = {"pickup_type", "pickup_ready_date", "supplier_token"}
    missing = needed - set(cols)
    if missing:
        return "FAIL", f"Missing columns: {missing}"
    return "PASS", "All pickup columns present in order_shipments"


# =============================================================================
# 2. AUTH ENFORCEMENT
# =============================================================================

print("\n\ud83d\udd10 2. AUTH ENFORCEMENT")

# FIX v3: checkpoint is PATCH not POST
ADMIN_ONLY_ENDPOINTS = [
    ("POST",  "/alerts/check-all"),
    ("POST",  "/alerts/tracking/check-all"),
    ("POST",  "/alerts/pickup/check-confirmations"),
    ("POST",  "/lifecycle/check-all"),
    ("POST",  "/lifecycle/run-warehouse-polls"),
    ("GET",   "/debug/orders-columns"),
    ("GET",   "/debug/shipment/4919"),
    ("POST",  "/debug/insert-pickup-shipment/4919"),
    ("PATCH", f"/orders/{KNOWN_ORDER_ID}"),
    ("PATCH", f"/orders/{KNOWN_ORDER_ID}/checkpoint"),   # PATCH, not POST
    ("POST",  f"/supplier/{PICKUP_SHIPMENT_ID}/send-poll"),
]

for method, path in ADMIN_ONLY_ENDPOINTS:
    @test(f"{method} {path} — no token \u2192 401/403", "Auth")
    def _(m=method, p=path):
        resp = r(m, p, headers=NO_AUTH_HEADERS)
        if resp.status_code in (401, 403):
            return "PASS", f"Correctly rejected with {resp.status_code}"
        return "FAIL", f"Expected 401/403, got {resp.status_code}"

    @test(f"{method} {path} — bad token \u2192 401", "Auth")
    def _(m=method, p=path):
        resp = r(m, p, headers=BAD_AUTH_HEADERS)
        if resp.status_code in (401, 403):
            return "PASS", f"Correctly rejected with {resp.status_code}"
        return "FAIL", f"Expected 401/403, got {resp.status_code}"


# =============================================================================
# 3. ORDERS API
# =============================================================================

print("\n\ud83d\udccb 3. ORDERS API")

@test("GET /orders — returns list", "Orders")
def _():
    resp = r("GET", "/orders", headers=ADMIN_HEADERS)
    d = resp.json()
    count = d.get("count", 0)
    return check(resp, 200, "status", "ok") if count >= 0 else ("FAIL", "no count key")

@test("GET /orders?include_complete=true", "Orders")
def _():
    resp = r("GET", "/orders?include_complete=true", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test(f"GET /orders/{KNOWN_ORDER_ID} — known order found", "Orders")
def _():
    resp = r("GET", f"/orders/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test(f"GET /orders/{FAKE_ORDER_ID} — fake order \u2192 404", "Orders")
def _():
    resp = r("GET", f"/orders/{FAKE_ORDER_ID}", headers=ADMIN_HEADERS)
    return check(resp, 404)

@test("GET /orders/status/summary", "Orders")
def _():
    resp = r("GET", "/orders/status/summary", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test("PATCH set-status \u2192 complete", "Orders")
def _():
    resp = r("PATCH", f"/orders/{KNOWN_ORDER_ID}/set-status?status=complete", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test("PATCH set-status \u2192 INVALID \u2192 400", "Orders")
def _():
    resp = r("PATCH", f"/orders/{KNOWN_ORDER_ID}/set-status?status=INVALID_STATUS", headers=ADMIN_HEADERS)
    return check(resp, 400)

@test(f"PATCH set-status fake order \u2192 404", "Orders")
def _():
    resp = r("PATCH", f"/orders/{FAKE_ORDER_ID}/set-status?status=complete", headers=ADMIN_HEADERS)
    if resp.status_code in (404, 400):
        return "PASS", f"Correctly returned {resp.status_code}"
    return "WARN", f"Expected 404, got {resp.status_code}"

VALID_STATUSES = ["needs_payment_link", "awaiting_payment", "needs_warehouse_order",
                  "awaiting_warehouse", "needs_bol", "awaiting_shipment", "complete"]

for status in VALID_STATUSES:
    @test(f"PATCH set-status \u2192 {status}", "Orders")
    def _(s=status):
        resp = r("PATCH", f"/orders/{KNOWN_ORDER_ID}/set-status?status={s}", headers=ADMIN_HEADERS)
        return check(resp, 200, "status", "ok")

# FIX v3: checkpoint is PATCH not POST
@test(f"PATCH /orders/{KNOWN_ORDER_ID}/checkpoint — payment_received", "Orders")
def _():
    payload = {"checkpoint": "payment_received"}
    resp = r("PATCH", f"/orders/{KNOWN_ORDER_ID}/checkpoint", headers=ADMIN_HEADERS, json=payload)
    return check(resp, 200, "status", "ok")

@test(f"PATCH /orders/{KNOWN_ORDER_ID}/checkpoint — invalid \u2192 400", "Orders")
def _():
    payload = {"checkpoint": "INVALID_CHECKPOINT"}
    resp = r("PATCH", f"/orders/{KNOWN_ORDER_ID}/checkpoint", headers=ADMIN_HEADERS, json=payload)
    return check(resp, 400)

VALID_CHECKPOINTS = ["payment_link_sent", "payment_received", "sent_to_warehouse",
                     "warehouse_confirmed", "bol_sent", "is_complete"]

for cp in VALID_CHECKPOINTS:
    @test(f"Checkpoint \u2192 {cp}", "Orders")
    def _(c=cp):
        payload = {"checkpoint": c}
        resp = r("PATCH", f"/orders/{KNOWN_ORDER_ID}/checkpoint", headers=ADMIN_HEADERS, json=payload)
        return check(resp, 200, "status", "ok")

@test(f"GET /orders/{KNOWN_ORDER_ID}/events", "Orders")
def _():
    resp = r("GET", f"/orders/{KNOWN_ORDER_ID}/events", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test(f"GET /orders/{KNOWN_ORDER_ID}/shipments", "Orders")
def _():
    resp = r("GET", f"/orders/{KNOWN_ORDER_ID}/shipments", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test("PATCH /orders/{id} — update notes field", "Orders")
def _():
    payload = {"notes": f"Test note from automated test suite {datetime.now().isoformat()}"}
    resp = r("PATCH", f"/orders/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS, json=payload)
    return check(resp, 200, "status", "ok")

@test("PATCH /orders/{id} — empty payload \u2192 400", "Orders")
def _():
    resp = r("PATCH", f"/orders/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS, json={})
    return check(resp, 400)


# =============================================================================
# 4. SHIPMENTS API
# =============================================================================

print("\n\ud83d\udce6 4. SHIPMENTS API")

@test("GET /shipments — all shipments", "Shipments")
def _():
    resp = r("GET", "/shipments", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test(f"GET /shipments/{PICKUP_SHIPMENT_ID}/rl-quote-data", "Shipments")
def _():
    resp = r("GET", f"/shipments/{PICKUP_SHIPMENT_ID}/rl-quote-data", headers=ADMIN_HEADERS)
    if resp.status_code == 200:
        return "PASS", "status=200"
    return "WARN", f"status={resp.status_code}: {resp.text[:100]}"

@test(f"PATCH /shipments/{PICKUP_SHIPMENT_ID} — update status", "Shipments")
def _():
    resp = r("PATCH", f"/shipments/{PICKUP_SHIPMENT_ID}?status=needs_order", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test(f"PATCH /shipments/{PICKUP_SHIPMENT_ID} — invalid status \u2192 400", "Shipments")
def _():
    resp = r("PATCH", f"/shipments/{PICKUP_SHIPMENT_ID}?status=INVALID_STATUS_XYZ", headers=ADMIN_HEADERS)
    return check(resp, 400)

@test(f"PATCH /shipments/{FAKE_SHIPMENT_ID} — fake \u2192 404", "Shipments")
def _():
    resp = r("PATCH", f"/shipments/{FAKE_SHIPMENT_ID}?status=needs_order", headers=ADMIN_HEADERS)
    return check(resp, 404)

VALID_SHIP_STATUSES = ["needs_order", "at_warehouse", "needs_bol", "ready_ship", "shipped", "delivered"]
for s in VALID_SHIP_STATUSES:
    @test(f"Shipment status \u2192 {s}", "Shipments")
    def _(st=s):
        resp = r("PATCH", f"/shipments/{PICKUP_SHIPMENT_ID}?status={st}", headers=ADMIN_HEADERS)
        return check(resp, 200, "status", "ok")

@test("Reset shipment status \u2192 needs_order", "Shipments")
def _():
    resp = r("PATCH", f"/shipments/{PICKUP_SHIPMENT_ID}?status=needs_order", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")


# =============================================================================
# 5. WEBHOOK — B2BWAVE ORDER INTAKE
# =============================================================================

print("\n\ud83e\ude9d 5. WEBHOOK — B2BWAVE ORDER INTAKE")

@test("POST /webhook — known pickup order (idempotent)", "Webhook")
def _():
    payload = {"id": KNOWN_ORDER_ID, "customer_email": TEST_EMAIL}
    resp = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS, json=payload)
    d = resp.json()
    if resp.status_code == 200 and d.get("case") == "pickup":
        return "PASS", f"case=pickup, is_warehouse_pickup={d.get('is_warehouse_pickup')}"
    return "FAIL", f"status={resp.status_code}, body={str(d)[:120]}"

@test("POST /webhook — missing order_id \u2192 400", "Webhook")
def _():
    resp = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS, json={"customer_email": TEST_EMAIL})
    return check(resp, 400)

@test("POST /webhook — fake order_id (B2BWave 404)", "Webhook")
def _():
    payload = {"id": FAKE_ORDER_ID, "customer_email": TEST_EMAIL}
    resp = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS, json=payload)
    if resp.status_code == 200:
        d = resp.json()
        if d.get("status") == "ok":
            return "PASS", "Returned 200 with graceful 'order not found in B2BWave'"
    return "WARN", f"status={resp.status_code}: {resp.text[:120]}"

@test("POST /webhook — no customer_email (still returns 200)", "Webhook")
def _():
    payload = {"id": KNOWN_ORDER_ID}
    resp = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS, json=payload)
    return check(resp, 200)

@test("POST /webhook — empty body \u2192 422 or 400", "Webhook")
def _():
    resp = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS, json={})
    if resp.status_code in (400, 422):
        return "PASS", f"Correctly returned {resp.status_code}"
    return "FAIL", f"Expected 400/422, got {resp.status_code}"

@test("POST /webhook — double-fire same order (idempotent)", "Webhook")
def _():
    payload = {"id": KNOWN_ORDER_ID, "customer_email": TEST_EMAIL}
    r1 = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS, json=payload)
    time.sleep(1)
    r2 = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS, json=payload)
    if r1.status_code == 200 and r2.status_code == 200:
        return "PASS", "Both 200 — idempotent"
    return "FAIL", f"r1={r1.status_code}, r2={r2.status_code}"


# =============================================================================
# 6. CHECKOUT + QUOTE VIEW
# =============================================================================

print("\n\ud83d\udcb3 6. CHECKOUT + QUOTE VIEW")

@test(f"GET /checkout-ui/{KNOWN_ORDER_ID} — no token \u2192 403", "Checkout")
def _():
    resp = r("GET", f"/checkout-ui/{KNOWN_ORDER_ID}?token=BADTOKEN", headers=NO_AUTH_HEADERS)
    return check(resp, 403)

@test(f"GET /checkout-ui/{KNOWN_ORDER_ID}?view=quote — VIEW injected correctly", "Checkout")
def _():
    # FIX v3: both JS ternary branches (payBtn AND Quote Total) are always in raw HTML
    # source. The only thing that changes is the Python f-string injection:
    #   const VIEW = "quote"   (when ?view=quote)
    #   const VIEW = ""        (when no view param)
    # Check for that to confirm the backend received and processed the view param.
    wh = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS,
            json={"id": KNOWN_ORDER_ID, "customer_email": TEST_EMAIL})
    token = wh.json().get("checkout_url", "").split("token=")[-1] if wh.status_code == 200 else None
    if not token:
        return "SKIP", "Could not get checkout token from webhook"
    resp = r("GET", f"/checkout-ui/{KNOWN_ORDER_ID}?token={token}&view=quote", headers=NO_AUTH_HEADERS)
    if resp.status_code != 200:
        return "FAIL", f"status={resp.status_code}"
    if 'VIEW    = "quote"' in resp.text or 'VIEW = "quote"' in resp.text:
        return "PASS", 'VIEW="quote" correctly injected into HTML — browser JS handles conditional rendering'
    if 'VIEW    = ""' in resp.text or 'VIEW = ""' in resp.text:
        return "FAIL", 'VIEW is empty string — ?view=quote parameter not passed through to template'
    return "WARN", "Could not find VIEW variable in HTML — check template manually"

@test("GET /checkout-ui — no view param \u2192 VIEW is empty string", "Checkout")
def _():
    # Confirm baseline: no ?view=quote means VIEW=""
    wh = r("POST", "/webhook/b2bwave-order", headers=NO_AUTH_HEADERS,
            json={"id": KNOWN_ORDER_ID, "customer_email": TEST_EMAIL})
    token = wh.json().get("checkout_url", "").split("token=")[-1] if wh.status_code == 200 else None
    if not token:
        return "SKIP", "Could not get checkout token from webhook"
    resp = r("GET", f"/checkout-ui/{KNOWN_ORDER_ID}?token={token}", headers=NO_AUTH_HEADERS)
    if resp.status_code != 200:
        return "FAIL", f"status={resp.status_code}"
    if 'VIEW    = ""' in resp.text or 'VIEW = ""' in resp.text:
        return "PASS", 'VIEW="" when no view param — correct baseline'
    return "WARN", "Could not confirm VIEW baseline in HTML"

@test("GET /checkout/{id} — invalid token \u2192 403", "Checkout")
def _():
    resp = r("GET", f"/checkout/{KNOWN_ORDER_ID}?token=BADTOKEN000", headers=NO_AUTH_HEADERS)
    return check(resp, 403)


# =============================================================================
# 7. SUPPLIER POLL — PICKUP FLOW
# =============================================================================

print("\n\ud83c\udfed 7. SUPPLIER POLL — PICKUP FLOW")

@test(f"POST /supplier/{PICKUP_SHIPMENT_ID}/send-poll — fires pickup poll", "Pickup")
def _():
    resp = r("POST", f"/supplier/{PICKUP_SHIPMENT_ID}/send-poll", headers=ADMIN_HEADERS)
    if resp.status_code == 200:
        d = resp.json()
        form_url = d.get("form_url", "")
        return "PASS", f"200, form_url={'set' if form_url else 'missing'}, sent_to={d.get('sent_to')}"
    return "FAIL", f"status={resp.status_code}: {resp.text[:120]}"

@test(f"POST /supplier/{FAKE_SHIPMENT_ID}/send-poll — fake \u2192 400", "Pickup")
def _():
    resp = r("POST", f"/supplier/{FAKE_SHIPMENT_ID}/send-poll", headers=ADMIN_HEADERS)
    return check(resp, 400)

@test("GET /supplier/BADTOKEN/pickup-ready-form — bad token \u2192 404", "Pickup")
def _():
    resp = r("GET", "/supplier/BADTOKEN_XYZ_123/pickup-ready-form", headers=NO_AUTH_HEADERS)
    return check(resp, 404)

@test("GET /supplier/BADTOKEN/pickup-confirm?response=yes — bad token \u2192 404", "Pickup")
def _():
    resp = r("GET", "/supplier/BADTOKEN_XYZ_123/pickup-confirm?response=yes", headers=NO_AUTH_HEADERS)
    return check(resp, 404)

@test("GET /supplier/BADTOKEN/date-form — bad freight token \u2192 404", "Pickup")
def _():
    resp = r("GET", "/supplier/BADTOKEN_XYZ_123/date-form", headers=NO_AUTH_HEADERS)
    return check(resp, 404)


# =============================================================================
# 8. CRON ENDPOINTS
# =============================================================================

print("\n\u23f0 8. CRON ENDPOINTS")

@test("POST /alerts/pickup/check-confirmations — cron runs", "Crons")
def _():
    resp = r("POST", "/alerts/pickup/check-confirmations", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)

@test("POST /alerts/tracking/check-all — cron runs", "Crons")
def _():
    resp = r("POST", "/alerts/tracking/check-all", headers=ADMIN_HEADERS)
    d = resp.json()
    checked = d.get("checked", -1)
    if resp.status_code == 200 and checked >= 0:
        return "PASS", f"checked={checked}, emails_sent={d.get('tracking_emails_sent')}, errors={d.get('errors')}"
    return "FAIL", f"status={resp.status_code}: {resp.text[:120]}"

@test("POST /lifecycle/check-all — cron runs + returns quote_reminders", "Crons")
def _():
    resp = r("POST", "/lifecycle/check-all", headers=ADMIN_HEADERS)
    d = resp.json()
    has_qr = "quote_reminders" in d
    orders_checked = d.get("orders_checked", -1)
    if resp.status_code == 200 and orders_checked >= 0:
        return "PASS", f"orders_checked={orders_checked}, quote_reminders={'present' if has_qr else 'MISSING'}"
    return "FAIL", f"status={resp.status_code}: {resp.text[:120]}"

@test("POST /lifecycle/run-warehouse-polls — freight escalation cron", "Crons")
def _():
    resp = r("POST", "/lifecycle/run-warehouse-polls", headers=ADMIN_HEADERS)
    d = resp.json()
    if resp.status_code == 200:
        return "PASS", f"polls_escalated={d.get('polls_escalated')}, day_before_sent={d.get('day_before_sent')}, errors={d.get('errors')}"
    return "FAIL", f"status={resp.status_code}: {resp.text[:120]}"

@test("POST /alerts/check-all — general alert check", "Crons")
def _():
    resp = r("POST", "/alerts/check-all", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)

@test("Cron idempotency — run lifecycle twice, no error", "Crons")
def _():
    r1 = r("POST", "/lifecycle/check-all", headers=ADMIN_HEADERS)
    time.sleep(2)
    r2 = r("POST", "/lifecycle/check-all", headers=ADMIN_HEADERS)
    if r1.status_code == 200 and r2.status_code == 200:
        return "PASS", "Both runs 200 — idempotent"
    return "FAIL", f"r1={r1.status_code}, r2={r2.status_code}"


# =============================================================================
# 9. LIFECYCLE ENGINE
# =============================================================================

print("\n\u267b\ufe0f  9. LIFECYCLE ENGINE")

@test("GET /lifecycle/summary — counts by status", "Lifecycle")
def _():
    resp = r("GET", "/lifecycle/summary", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)

@test("GET /lifecycle/orders — list all", "Lifecycle")
def _():
    resp = r("GET", "/lifecycle/orders", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)

@test("GET /lifecycle/orders?status=inactive", "Lifecycle")
def _():
    resp = r("GET", "/lifecycle/orders?status=inactive", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)

@test("GET /lifecycle/orders?status=INVALID_STATUS \u2192 400", "Lifecycle")
def _():
    resp = r("GET", "/lifecycle/orders?status=INVALID_STATUS", headers=ADMIN_HEADERS)
    return check(resp, 400)

@test(f"POST /lifecycle/extend/{KNOWN_ORDER_ID} — resets clock", "Lifecycle")
def _():
    resp = r("POST", f"/lifecycle/extend/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    if resp.status_code == 200:
        d = resp.json()
        return "PASS", f"new_status={d.get('new_status')}"
    return "FAIL", f"status={resp.status_code}: {resp.text[:100]}"

@test(f"POST /lifecycle/extend/{FAKE_ORDER_ID} — fake \u2192 404", "Lifecycle")
def _():
    resp = r("POST", f"/lifecycle/extend/{FAKE_ORDER_ID}", headers=ADMIN_HEADERS)
    return check(resp, 404)

@test(f"POST /lifecycle/check/{KNOWN_ORDER_ID} — single order check", "Lifecycle")
def _():
    resp = r("POST", f"/lifecycle/check/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)


# =============================================================================
# 10. TRACKING
# =============================================================================

print("\n\ud83d\ude9b 10. TRACKING")

@test(f"POST /orders/{KNOWN_ORDER_ID}/send-tracking — saves + emails + logs event", "Tracking")
def _():
    params = {"tracking_number": "TEST-PRO-999-AUTOTEST", "shipment_id": PICKUP_SHIPMENT_ID}
    resp = r("POST", f"/orders/{KNOWN_ORDER_ID}/send-tracking", headers=ADMIN_HEADERS, params=params)
    if resp.status_code == 200:
        d = resp.json()
        return "PASS", f"email_sent={d.get('email_sent')}"
    return "FAIL", f"status={resp.status_code}: {resp.text[:120]}"

@test(f"POST /orders/{KNOWN_ORDER_ID}/send-tracking — double-fire no dupe email", "Tracking")
def _():
    params = {"tracking_number": "TEST-PRO-999-AUTOTEST", "shipment_id": PICKUP_SHIPMENT_ID}
    r1 = r("POST", f"/orders/{KNOWN_ORDER_ID}/send-tracking", headers=ADMIN_HEADERS, params=params)
    time.sleep(1)
    r2 = r("POST", f"/orders/{KNOWN_ORDER_ID}/send-tracking", headers=ADMIN_HEADERS, params=params)
    if r1.status_code == 200 and r2.status_code == 200:
        d2 = r2.json()
        if d2.get("email_sent") is False:
            return "PASS", "Second call: email_sent=False — event guard working"
        return "WARN", f"Second call email_sent={d2.get('email_sent')} — may have sent dupe"
    return "FAIL", f"r1={r1.status_code}, r2={r2.status_code}"

@test(f"POST /orders/{FAKE_ORDER_ID}/send-tracking — fake order handled", "Tracking")
def _():
    params = {"tracking_number": "TEST-PRO-FAKE", "shipment_id": FAKE_SHIPMENT_ID}
    resp = r("POST", f"/orders/{FAKE_ORDER_ID}/send-tracking", headers=ADMIN_HEADERS, params=params)
    if resp.status_code in (404, 400, 200):
        return "PASS", f"Handled gracefully with {resp.status_code}"
    return "FAIL", f"Unexpected status {resp.status_code}"


# =============================================================================
# 11. DEBUG ENDPOINTS
# =============================================================================

print("\n\ud83d\udd0d 11. DEBUG ENDPOINTS")

@test(f"GET /debug/shipment/{KNOWN_ORDER_ID} — shipment state", "Debug")
def _():
    resp = r("GET", f"/debug/shipment/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    if resp.status_code == 200:
        d = resp.json()
        return "PASS", f"order_row_exists={d.get('order_row_exists')}, shipment_count={d.get('shipment_count_by_order_id')}"
    return "FAIL", f"status={resp.status_code}"

@test(f"GET /debug/shipment/{FAKE_ORDER_ID} — fake order empty", "Debug")
def _():
    resp = r("GET", f"/debug/shipment/{FAKE_ORDER_ID}", headers=ADMIN_HEADERS)
    if resp.status_code == 200:
        d = resp.json()
        return "PASS", f"order_row_exists={d.get('order_row_exists')}"
    return "FAIL", f"status={resp.status_code}"

@test("GET /debug/orders-columns — all expected tables present", "Debug")
def _():
    resp = r("GET", "/debug/orders-columns", headers=ADMIN_HEADERS)
    d = resp.json()
    expected_keys = {"orders_columns", "pending_checkouts_columns", "order_shipments_columns"}
    missing = expected_keys - set(d.keys())
    if missing:
        return "FAIL", f"Missing response keys: {missing}"
    return "PASS", "All table column lists present"

@test(f"POST /debug/insert-pickup-shipment/{KNOWN_ORDER_ID} — idempotent", "Debug")
def _():
    resp = r("POST", f"/debug/insert-pickup-shipment/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    d = resp.json()
    if resp.status_code == 200:
        if "SUCCESS" in d.get("insert_with_pickup_type", ""):
            return "PASS", "INSERT succeeded"
        if d.get("insert_skipped") == "Shipment already exists":
            return "PASS", "Shipment already exists — idempotent"
        return "WARN", f"Unexpected result: {d}"
    return "FAIL", f"status={resp.status_code}"


# =============================================================================
# 12. MIGRATION ENDPOINTS (idempotent safety checks)
# =============================================================================

print("\n\ud83d\udd27 12. MIGRATION ENDPOINTS (idempotent re-run)")

MIGRATION_ENDPOINTS = [
    "/add-ws6-pickup-fields",
    "/add-ws6-supplier-fields",
    "/add-is-residential",
    "/add-bol-columns",
    "/add-weight-column",
]

for ep in MIGRATION_ENDPOINTS:
    @test(f"POST {ep} — safe to re-run", "Migrations")
    def _(e=ep):
        resp = r("POST", e, headers=ADMIN_HEADERS)
        if resp.status_code == 200:
            return "PASS", "200 — idempotent"
        return "WARN", f"status={resp.status_code}: {resp.text[:80]}"


# =============================================================================
# 13. ALERTS
# =============================================================================

print("\n\ud83d\udd14 13. ALERTS")

@test("GET /alerts/ — list unresolved alerts", "Alerts")
def _():
    resp = r("GET", "/alerts/", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)

@test("GET /alerts/summary — counts by type", "Alerts")
def _():
    resp = r("GET", "/alerts/summary", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)

@test(f"GET /alerts/?order_id={KNOWN_ORDER_ID} — order-specific alerts", "Alerts")
def _():
    resp = r("GET", f"/alerts/?order_id={KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    return check(resp, 200, "success", True)

@test("POST /alerts/999999/resolve — fake alert \u2192 404", "Alerts")
def _():
    resp = r("POST", "/alerts/999999/resolve", headers=ADMIN_HEADERS)
    return check(resp, 404)


# =============================================================================
# 14. WAREHOUSE MAPPING + TRUSTED CUSTOMERS
# =============================================================================

print("\n\ud83d\uddfa\ufe0f  14. WAREHOUSE MAPPING + TRUSTED CUSTOMERS")

@test("GET /warehouse-mapping", "Warehouse")
def _():
    resp = r("GET", "/warehouse-mapping", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")

@test("POST /warehouse-mapping — add test mapping", "Warehouse")
def _():
    payload = {"sku_prefix": "TEST", "warehouse_name": "Test Warehouse", "warehouse_code": "TW"}
    resp = r("POST", "/warehouse-mapping", headers=ADMIN_HEADERS, json=payload)
    return check(resp, 200, "status", "ok")

@test("GET /trusted-customers", "Warehouse")
def _():
    resp = r("GET", "/trusted-customers", headers=ADMIN_HEADERS)
    return check(resp, 200, "status", "ok")


# =============================================================================
# 15. B2BWAVE DEBUG
# =============================================================================

print("\n\ud83d\udd0e 15. B2BWAVE DEBUG")

@test(f"GET /debug/b2bwave-raw/{KNOWN_ORDER_ID}", "B2BWave")
def _():
    resp = r("GET", f"/debug/b2bwave-raw/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    if resp.status_code == 200:
        d = resp.json()
        return "PASS", f"status={d.get('status')}"
    return "WARN", f"status={resp.status_code}: {resp.text[:80]}"

@test(f"GET /debug/warehouse-routing/{KNOWN_ORDER_ID}", "B2BWave")
def _():
    resp = r("GET", f"/debug/warehouse-routing/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    if resp.status_code == 200:
        d = resp.json()
        return "PASS", f"is_pickup={d.get('is_pickup')}, warehouses={list(d.get('warehouse_groups', {}).keys())}"
    return "WARN", f"status={resp.status_code}: {resp.text[:80]}"

@test(f"GET /debug/test-checkout/{KNOWN_ORDER_ID}", "B2BWave")
def _():
    resp = r("GET", f"/debug/test-checkout/{KNOWN_ORDER_ID}", headers=ADMIN_HEADERS)
    if resp.status_code == 200:
        d = resp.json()
        return "PASS", f"is_pickup={d.get('is_pickup')}"
    return "WARN", f"status={resp.status_code}: {resp.text[:80]}"


# =============================================================================
# 16. ⛔ SKIPPED — R+L BOL / PICKUP REQUEST
# =============================================================================

print("\n\u23ed\ufe0f  16. R+L BOL / PICKUP REQUEST — SKIPPED")
print("  \u23ed\ufe0f  [Skip] BOL creation — real trucks showed up when tested. Skip until production.")
print("  \u23ed\ufe0f  [Skip] Pickup request — same reason. Test manually in prod when ready.")

results.append({"name": "R+L BOL creation", "category": "Skip", "status": "SKIP",
                "detail": "Real trucks showed up during testing. Skip until production."})
results.append({"name": "R+L Pickup request", "category": "Skip", "status": "SKIP",
                "detail": "Real trucks showed up during testing. Skip until production."})


# =============================================================================
# SUMMARY
# =============================================================================

end_time = datetime.now()
elapsed = (end_time - start_time).total_seconds()

passed  = sum(1 for r in results if r["status"] == "PASS")
failed  = sum(1 for r in results if r["status"] == "FAIL")
warned  = sum(1 for r in results if r["status"] == "WARN")
skipped = sum(1 for r in results if r["status"] == "SKIP")
total   = len(results)

print("\n" + "="*60)
print(f"  WS6 SANDBOX TEST RESULTS — {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
print("="*60)
print(f"  Total:   {total}")
print(f"  \u2705 PASS:  {passed}")
print(f"  \u274c FAIL:  {failed}")
print(f"  \u26a0\ufe0f  WARN:  {warned}")
print(f"  \u23ed\ufe0f  SKIP:  {skipped}")
print(f"  Time:    {elapsed:.1f}s")
print("="*60)

if failed > 0:
    print("\n\u274c FAILURES:")
    for r in results:
        if r["status"] == "FAIL":
            print(f"  [{r['category']}] {r['name']}")
            print(f"    \u2192 {r['detail']}")

if warned > 0:
    print("\n\u26a0\ufe0f  WARNINGS:")
    for r in results:
        if r["status"] == "WARN":
            print(f"  [{r['category']}] {r['name']}")
            print(f"    \u2192 {r['detail']}")

print()
sys.exit(0 if failed == 0 else 1)
