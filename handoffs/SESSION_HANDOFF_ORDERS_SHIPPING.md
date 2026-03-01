# Shipping & Freight — Session Handoff
**Last Updated:** 2026-02-28
**Workstream:** CFC Orders
**Status:** Paused — rl-quote-sandbox private, Render services possibly dead

## What This Lane Covers
All shipping logic: R+L Carriers LTL freight quoting, Shippo small package rates, weight-based method selection, multi-warehouse split routing, and the standalone rl-quote-sandbox app that was never integrated.

## Current State
- **R+L LTL quoting** works in sandbox — rl_carriers.py, tested Dec 30 (Order 5261, 3 warehouses → $625.07)
- **Shippo small package** works — shippo_rates.py, tested Dec 30 (Order 5339, 1.75 lbs → USPS $19.19)
- **rl-quote-sandbox** is a SEPARATE standalone app with Smarty address validation + enhanced R+L API — NEVER merged into main system
- **R+L API auth is flaky** — key-in-JSON-body works, header auth doesn't
- **Pirateship** is manual — open in new tab, paste URL back
- **Box Truck / Li Delivery / Pickup** all working

## Key Files
- `CFCOrderBackend_Sandbox/rl_carriers.py` — 719 lines, R+L API (quotes, BOL, tracking, pickup)
- `CFCOrderBackend_Sandbox/shippo_rates.py` — 320 lines, small package shipping rates
- `CFCOrderBackend_Sandbox/rta_database.py` — 435 lines, SKU weight/dimension lookup
- `CFCOrderBackend_Sandbox/load_rta_data.py` — 161 lines, RTA database data loader
- `rl-quote-sandbox/backend/main.py` — PRIVATE, needs sharing
- `rl-quote-sandbox/backend/smarty_api.py` — PRIVATE, Smarty address validation
- `rl-quote-sandbox/backend/rl_api.py` — PRIVATE, enhanced R+L integration

## Active Bugs / Blockers
1. **rl-quote-sandbox repo is PRIVATE** — William needs to share 4 files: main.py, models.py, smarty_api.py, rl_api.py
2. **R+L API auth flaky** — key in JSON body works, in header doesn't
3. **Hardcoded R+L API key** in rl_api_test_clean.py — security risk, must delete
4. **Render services may be dead** — 2 months idle since Dec 30
5. **Auto-BOL creation** built in rl-quote but never integrated

## Next Steps
1. Resolve rl-quote-sandbox access (get 4 files from William)
2. Delete rl_api_test_clean.py (hardcoded API key)
3. Integrate Smarty address validation into sandbox backend
4. Upgrade rl_carriers.py with rl-quote-sandbox improvements (fix auth, add expiration/fees)
5. New endpoints: /validate-address, /quote/auto, /shipments/{id}/auto-bol
6. Schema updates: address_type, quote_expires_at, fee columns

## Rules & Decisions
- Weight thresholds: <80 lbs → Pirateship, 80-300 lbs → either, >300 lbs → R+L LTL
- Customer price markup: R+L quote + $50
- Freight class: 85 (always for RTA cabinets)
- 10 Suppliers/Warehouses: LI, DL, ROC, Go Bravura, Love-Milestone, Cabinet & Stone, DuraStone, L&C Cabinetry, GHI, Linda
- DYLT API (dylt.com) for California warehouse shipments
- Env vars needed: SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN, RL_ACCOUNT_NUMBER
