# CFC Orders Sandbox Backend — Audit Report

**Date:** February 28, 2026
**Repo:** github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox (main branch)
**Files audited:** 22 Python files + requirements.txt
**Syntax errors:** 0 (all 22 .py files pass AST parse)
**Total endpoints:** 85 → 84 after dedup fix

---

## FIXES APPLIED (Feb 28, 2026)

### ✅ Fix 1: rl_api_test_clean.py — DELETE PENDING (manual GitHub action)
Hardcoded R+L API key on line 14. File must be deleted from repo.

### ✅ Fix 2: /orders/status/summary path ordering
Moved ABOVE /orders/{order_id} so FastAPI matches it correctly.

### ✅ Fix 3: Duplicate POST /rl/pickup/pro/{pro_number}
Merged lines 815 + 911 into single handler with all params (contact_name, contact_email, additional_instructions).

### ✅ Fix 4: Freight class 70 → 85
Changed all 7 code defaults + 2 docstrings across main.py, rl_carriers.py, checkout.py.

### ✅ Fix 5: Dead file deletion — PENDING (manual GitHub action)
Delete: main2.py, main4.py, main7.py, main8.py, rl_api_test_clean.py, desktop.ini (~475KB)

### ✅ Fix 6: requirements.txt
Added pandas, openpyxl, pydantic. Removed unused httpx.

---

## REMAINING ISSUES (not yet fixed)

- B-2: R+L auth method (key in header vs body) — deferred to Step 2 integration
- Config duplication (checkout.py, gmail_sync.py bypass config.py) — tech debt
- Bare except clauses (2) — minor
- Anthropic API version stale — minor
- TODO: checkout email sending not implemented
