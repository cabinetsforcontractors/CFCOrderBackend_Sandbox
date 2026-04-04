# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-04 (multi-warehouse shipping complete)
**Repo:** CFCOrderBackend_Sandbox / CFCOrdersFrontend_Sandbox

⛔ THIS IS THE SANDBOX REPO — NOW LIVE AS PRODUCTION
- Production backend: https://cfc-backend-b83s.onrender.com (repointed to CFCOrderBackend_Sandbox)
- Production frontend: https://cfc-orders-frontend.vercel.app (repointed to CFCOrdersFrontend_Sandbox)

---

## CURRENT STATE — Multi-warehouse shipping live.

**What was just built (2026-04-04):**

### Gap 1 — Backend: Per-warehouse shipment record auto-creation ✅
`checkout_routes.py` (sha `5b5e6c55`) — webhook now auto-creates one `order_shipments` row per warehouse group immediately when B2BWave order fires. Uses `calculate_order_shipping()` result, idempotent (checks `shipment_id` before insert). Matches sync_service `shipment_id` format: `{order_id}-{wh-short}`. Fields populated: `warehouse`, `status='needs_order'`, `origin_zip`, `weight`, `has_oversized`.

### Gap 2 — Admin table: All warehouses shown ✅
`App.jsx` v7.5.0 (sha `ecb8f13a`) — Warehouse column now renders all unique warehouses for the order as stacked tags. Multi-warehouse orders show e.g. `LI` + `DL` stacked vertically.

### Gap 3 — Actions tab: Per-warehouse shipping buttons ✅
`App.jsx` v7.5.0 — Actions tab now maps over all shipments and renders one "🚚 Ship: {warehouse}" button per shipment. No more `shipments[0]` hardcode.

---

## Phase 7 Status — COMPLETE ✅
- Step 1 (api.js CFC2026): done ✅
- Step 2 (Render repoint): done ✅ (auto-deploy)
- Step 3 (DB migrations): done ✅ (shared DB)
- Step 4 (Vercel repoint): done ✅ (auto-deploy)
- Step 5 (smoke test): done ✅ (confirmed working from UI)
- Step 6 (cleanup): DEFERRED — next session

---

## Deferred Items

| Item | Priority |
|------|----------|
| Step 6 cleanup: delete main2/4/7/8.py, fix .gitignore, archive old repos | Next session |
| Email subject line â encoding (minor formatting) | Low |
| R+L multi-warehouse auto-quote | Phase 8 |
| Lane C (RTA Weight) | Blocked on WS5 |

---

## Phase 8 — Shipment Tracking & Notification Engine

Top priority after cleanup. See previous handoff content for full spec.

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

## Key Files (current SHAs — 2026-04-04)

| File | SHA | Notes |
|------|-----|-------|
| cfc-orders:checkout_routes.py | 5b5e6c55 | Per-warehouse shipment auto-create in webhook |
| cfc-orders-frontend:src/App.jsx | ecb8f13a | v7.5.0 — multi-warehouse table + actions |
| cfc-orders:checkout.py | 76ff33fa | Unchanged |
| cfc-orders:orders_routes.py | 27ece593 | Unchanged |
| cfc-orders:main.py | e4cd70c1 | v6.2.0 unchanged |
| cfc-orders-frontend:src/components/ShippingManager.jsx | ea4de1bf | v5.9.8 unchanged |
| cfc-orders-frontend:src/components/ShipmentRow.jsx | 398b287a | v5.9.4 unchanged |

---

## Critical Reminders

- Production is now the sandbox repos. One codebase.
- Shared PostgreSQL DB — all migrations already done.
- ADMIN_API_KEY=CFC2026 on both Render envs.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- Lane C (RTA Weight) blocked on WS5 — do not touch.
- WS17 FILE LOCK still active.
- POWERSHELL: NEVER use &&. One command per block.
