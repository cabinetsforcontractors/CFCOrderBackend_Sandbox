# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-05 (weight allocation for multi-warehouse LTL)
**Repo:** CFCOrderBackend_Sandbox / CFCOrdersFrontend_Sandbox

⛔ THIS IS THE SANDBOX REPO — NOW LIVE AS PRODUCTION
- Production backend: https://cfc-backend-b83s.onrender.com (repointed to CFCOrderBackend_Sandbox)
- Production frontend: https://cfc-orders-frontend.vercel.app (repointed to CFCOrdersFrontend_Sandbox)

---

## CURRENT STATE — Multi-warehouse LTL auto-quote unblocked.

**What was just built (2026-04-05):**

### Weight Allocation — `orders_routes.py` (sha `4e530dfa`)
`GET /shipments/{shipment_id}/rl-quote-data` — when a shipment has no stored weight and the order spans multiple warehouses, the endpoint now computes a sales-proportional weight allocation from `order_line_items.line_total` grouped by warehouse. Returns `weight.value` (non-null) so the frontend auto-quote button is enabled.

Example — Order #5492:
- SHLS (L&C Cabinetry): $2,665.01 → 81.33% → 692.6 lbs
- WSP: $611.75 → 18.67% → 159.2 lbs
- Total weight: 851.8 lbs ✅

Weight note shown in UI: `"Sales-allocated (81.3% of order) ⚠️ Not production-ready — verify before use"`

---

## ⚠️ PRODUCTION GATE — Weight Allocation Not Production-Ready

**DO NOT use sales-allocated weights on live orders without review.**

Current logic allocates `orders.total_weight` proportionally by each warehouse's `SUM(line_total)`. This is an approximation — cabinet prices correlate loosely with weight but are not a substitute for real per-item weights.

| Warehouse | Sales | % | Allocated Weight | Real Weight |
|-----------|-------|---|-----------------|-------------|
| SHLS (L&C Cabinetry) | $2,665.01 | 81.33% | 692.6 lbs | Unknown until Lane C |
| WSP | $611.75 | 18.67% | 159.2 lbs | Unknown until Lane C |

**Production fix required:** Lane C (RTA Weight) must be completed (blocked on WS5 canonical master cleanup). Once Lane C loads real per-SKU weights from the RTA database, the allocation logic in `get_rl_quote_data` should be replaced with a `SUM(item_weight * quantity)` per warehouse query against `order_line_items` joined to the weight lookup.

Until then: treat sales-allocated weights as estimates. Always verify quote weight against physical shipment before booking R+L.

---

## Phase 7 Status — COMPLETE ✅
- Step 1 (api.js CFC2026): done ✅
- Step 2 (Render repoint): done ✅ (auto-deploy)
- Step 3 (DB migrations): done ✅ (shared DB)
- Step 4 (Vercel repoint): done ✅ (auto-deploy)
- Step 5 (smoke test): done ✅ (confirmed working from UI)
- Step 6 (cleanup): DONE ✅ — dead files never existed in sandbox; .gitignore already correct; repo archiving skipped (risk unclear)

---

## Deferred Items

| Item | Priority |
|------|----------|
| Email subject line â encoding (minor formatting) | Low |
| R+L multi-warehouse auto-quote weight accuracy | Blocked on Lane C / WS5 |
| Lane C (RTA Weight) | Blocked on WS5 |

---

## Phase 8 — Shipment Tracking & Notification Engine

Top priority next. See WS6_CFC_ORDERS.md for full spec.

### Warehouse Shipping Rules
- **LI (Cabinetry Distribution)** — always ships, any length. Watch Gmail for tracking/PRO.
- **LM (Love-Milestone)** — always ships, any length. Watch Gmail for tracking/PRO.
- **DL (DL Cabinetry)** — ships only if long pallet (≥96"). CFC arranges R+L for everything else.
- **All others** — CFC always arranges R+L. Supplier palletizes. CFC pulls PRO from R+L API.

### Track A — Warehouse Ships (LI, LM, DL long pallet)
Day 0: Payment confirmed. Warehouse notified. Pick list PDF (warehouse version) attached.
Day 2: Gmail scan for tracking/PRO. Found → auto-populate. Not found → send warehouse response form.
Form flow: Has it shipped? Yes → PRO entry → R+L polling every 4 business hours → Tracking Confirmed.
No response 24h → email William.

### Track B — CFC Arranges R+L (all others + DL short)
Day 0: Payment confirmed. BOL created. Warehouse notified.
Day 2: Form: "Is it palletized and ready for R+L pickup?"
Yes → R+L pickup ping → poll for PRO → Tracking Confirmed.
No response 24h → email William.

### Tracking Confirmed (both tracks)
1. Customer email: shipped + PRO + ETA
2. Poll R+L daily for ETA + stop number
3. Evening before delivery: "Your delivery is tomorrow"
4. Morning of delivery: Delivery Day email + customer pick sheet PDF + interactive pick sheet link
5. R+L shows delivered: Post-Delivery email

---

## Key Files (current SHAs — 2026-04-05)

| File | SHA | Notes |
|------|-----|-------|
| cfc-orders:orders_routes.py | 4e530dfa | Sales-based weight allocation in rl-quote-data |
| cfc-orders:checkout_routes.py | 5b5e6c55 | Per-warehouse shipment auto-create in webhook |
| cfc-orders-frontend:src/App.jsx | ecb8f13a | v7.5.0 — multi-warehouse table + actions |
| cfc-orders:checkout.py | 76ff33fa | Unchanged |
| cfc-orders:main.py | e4cd70c1 | v6.2.0 unchanged |
| cfc-orders-frontend:src/components/ShippingManager.jsx | ea4de1bf | v5.9.8 unchanged |
| cfc-orders-frontend:src/components/ShipmentRow.jsx | 398b287a | v5.9.4 unchanged |
| cfc-orders-frontend:src/components/RLQuoteHelper.jsx | 216707be | v5.9.1 unchanged — button auto-enables when weight.value non-null |

---

## Critical Reminders

- Production is now the sandbox repos. One codebase.
- Shared PostgreSQL DB — all migrations already done.
- ADMIN_API_KEY=CFC2026 on both Render envs.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- Lane C (RTA Weight) blocked on WS5 — do not touch.
- WS17 FILE LOCK still active.
- POWERSHELL: NEVER use &&. One command per block.
- ⚠️ Sales-allocated weights are estimates only — see PRODUCTION GATE above.
