# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-05 (multi-warehouse LTL auto-quote complete)
**Repo:** CFCOrderBackend_Sandbox / CFCOrdersFrontend_Sandbox

⛔ THIS IS THE SANDBOX REPO — NOW LIVE AS PRODUCTION
- Production backend: https://cfc-backend-b83s.onrender.com (repointed to CFCOrderBackend_Sandbox)
- Production frontend: https://cfc-orders-frontend.vercel.app (repointed — unverified, use sandbox URL for testing)
- Sandbox frontend: https://cfcordersfrontend-sandbox.vercel.app

---

## CURRENT STATE — Multi-warehouse LTL "Quote All" working end-to-end.

**What was built this session (2026-04-05):**

### Fix 1 — `orders_routes.py` (sha `6c4ac2fa`)
Weight allocation query changed from `COALESCE(SUM(line_total), 0)` to `SUM(COALESCE(line_total, price * quantity, 0))`. Fallback to `price * quantity` when `line_total` is null — which it was for all pre-fix orders since sync never wrote it.

### Fix 2 — `sync_service.py` (sha `21156ca7`)
Added `line_total = round(qty * price, 2)` to the B2BWave sync INSERT. All future syncs now populate `line_total` correctly. Fix 1 remains as safety fallback.

### Feature — `App.jsx` v7.6.0 (sha `e2b5a999`)
"⚡ Quote All Warehouses (LTL)" button in Actions tab. One click: loops all shipments, calls `rl-quote-data` + `/proxy/auto-quote` for each in sequence, displays combined results panel with per-warehouse breakdown and total shipping cost.

**Tested on order #5492:**
- L&C Cabinetry: 692.8 lbs → $351.84 carrier / $401.84 customer
- LI: 159 lbs → $351.84 carrier / $401.84 customer ⚠️ identical to L&C — may be R+L minimum floor rate, verify on live order
- Total: $803.68

---

## ⚠️ PRODUCTION GATE — Weight Allocation Not Production-Ready

**DO NOT use sales-allocated weights on live orders without review.**

Current logic allocates `orders.total_weight` proportionally by each warehouse's `SUM(line_total)` (or `price * quantity` fallback). This is an approximation — prices correlate loosely with weight.

| Warehouse | Sales | % | Allocated Weight |
|-----------|-------|---|-----------------|
| SHLS (L&C Cabinetry) | $2,665.01 | 81.33% | 692.6 lbs |
| WSP (LI) | $611.75 | 18.67% | 159.2 lbs |

**Production fix:** Lane C (RTA Weight) must complete (blocked on WS5). Replace allocation with `SUM(item_weight * quantity)` per warehouse from real SKU weight data.

---

## Step 6 Cleanup — DONE ✅
- Dead files (main2/4/7/8.py): never existed in sandbox
- .gitignore: already correct
- Repo archiving: skipped (production Vercel connection unverified — do not archive until confirmed)

---

## Phase 8 — Next
Shipment tracking & notification engine. See `brain:workstreams/WS6_CFC_ORDERS.md` for full spec.

---

## Key Files (current SHAs — 2026-04-05)

| File | SHA | Notes |
|------|-----|-------|
| cfc-orders:orders_routes.py | 6c4ac2fa | COALESCE fallback for weight allocation |
| cfc-orders:sync_service.py | 21156ca7 | line_total now written on B2BWave sync |
| cfc-orders:checkout_routes.py | 5b5e6c55 | Per-warehouse shipment auto-create |
| cfc-orders:checkout.py | 76ff33fa | Unchanged |
| cfc-orders:main.py | e4cd70c1 | v6.2.0 unchanged |
| cfc-orders-frontend:src/App.jsx | e2b5a999 | v7.6.0 — Quote All Warehouses LTL |
| cfc-orders-frontend:src/components/ShippingManager.jsx | ea4de1bf | v5.9.8 unchanged |
| cfc-orders-frontend:src/components/ShipmentRow.jsx | 398b287a | v5.9.4 unchanged |
| cfc-orders-frontend:src/components/RLQuoteHelper.jsx | 216707be | v5.9.1 unchanged |

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
- ⚠️ LI quote returning same price as L&C on order #5492 — may be R+L minimum floor, verify on next real order.
