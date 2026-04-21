"""
test_ws6_email_guard.py
Smoke test for WS6 Option A email guardrail posture.
"""

import os
import sys
import json
import urllib.request
import urllib.error

SANDBOX_BASE_URL = os.environ.get(
    "CFC_SANDBOX_BASE_URL",
    "https://cfcorderbackend-sandbox.onrender.com",
).strip().rstrip("/")

ADMIN_TOKEN = os.environ.get("CFC_ADMIN_TOKEN", "").strip()

def _get_env_readiness():
    if not ADMIN_TOKEN:
        raise RuntimeError("CFC_ADMIN_TOKEN env var not set")
    req = urllib.request.Request(f"{SANDBOX_BASE_URL}/debug/env-readiness")
    req.add_header("X-Admin-Token", ADMIN_TOKEN)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def test_email_allowlist_active():
    data = _get_env_readiness()
    assert data.get("email_allowlist_active") is True

def test_b2bwave_mutations_disabled():
    data = _get_env_readiness()
    assert data.get("b2bwave_mutations_enabled") is False

def test_recommended_posture_is_safe_option_a():
    data = _get_env_readiness()
    assert data.get("recommended_posture") == "safe_option_a"

def test_tenant_is_production_class():
    data = _get_env_readiness()
    assert data.get("matches_production_literal") is True

def _run_all():
    checks = [
        ("email_allowlist_active", test_email_allowlist_active),
        ("b2bwave_mutations_disabled", test_b2bwave_mutations_disabled),
        ("recommended_posture == safe_option_a", test_recommended_posture_is_safe_option_a),
        ("tenant is production-class", test_tenant_is_production_class),
    ]
    failures = []
    for name, fn in checks:
        try:
            fn()
            print(f"[PASS] {name}")
        except Exception as e:
            failures.append((name, str(e)))
            print(f"[FAIL] {name}: {e}")

    if failures:
        print("\nFAILED")
        sys.exit(1)
    print("\nAll checks passed")
    sys.exit(0)

if __name__ == "__main__":
    _run_all()
