# SESSION HANDOFF — CFC Orders (General)

**Last Updated:** 2026-03-02 (Session 7)
**Last Session:** Mar 2, 2026 — Fix rl-quote warehouses + Phase 3A AlertsEngine
**Session Before That:** Mar 2, 2026 — Full-stack audit + order lifecycle rules defined

---

## WHAT HAPPENED THIS SESSION (Mar 2 — Session 7)

### 1. rl-quote Warehouse Fix (Blocker #6 RESOLVED)
**Problem:** models.py only had LI defined, and LI had ROC's zip (30071 = Norcross GA instead of 32148 = Interlachen FL).

**Fix applied to `rl-quote:backend/models.py`:**
- Added all 12 warehouses with complete addresses, phones, and street data
- Sourced from `checkout.py` WAREHOUSES dict (authoritative)
- Added `WAREHOUSE_BY_ZIP` helper dict for zip-based lookups
- WarehouseCode enum expanded: LI, DL, GHI, ROC, LC, CS, CS_CA, BRAVURA, LOVE, ARTISAN, DURASTONE, LINDA

**Fix applied to `rl-quote:backend/main.py`:**
- Version bumped to 0.2.0
- Test UI dropdown now shows all 12 warehouses (dynamically generated from WAREHOUSES dict)
- `/quote/simple` endpoint uses `WAREHOUSE_BY_ZIP` instead of manual loop
- Test-rl endpoint uses correct LI data (Interlachen FL 32148)
- Default test values set to William's test case: 32148→32176, 1600 lbs, business
- Removed hardcoded `origin_zip: '30071'` from test UI JavaScript

**Commits:**
- `aeb5e7...` — Fix warehouse data in models.py
- `815589e...` — v0.2.0 main.py with all warehouses

**Pending verification:** Render auto-deploy, then test POST /quote/simple?origin_zip=32148&destination_zip=32176&weight_lbs=1600&is_residential=false

### 2. Phase 3A: AlertsEngine BUILT
Created two new files in cfc-orders repo:

**`alerts_engine.py`** — Core engine implementing all 8 ORD-A1 rules:

| # | Alert Type | Trigger | Critical After |
|---|------------|---------|----------------|
| 1 | needs_invoice | Order placed, no invoice sent | 24 biz hrs |
| 2 | awaiting_payment_long | Invoice sent, no payment | 24 biz hrs |
| 3 | needs_warehouse_order | Payment received, not sent to warehouse | 24 biz hrs |
| 4 | at_warehouse_long | Sent to warehouse, not confirmed | 24 biz hrs |
| 5 | needs_bol | Warehouse confirmed, no BOL | 24 biz hrs |
| 6 | ready_ship_long | BOL sent, not shipped | 24 biz hrs |
| 7 | tracking_not_sent | Shipped, no tracking email to customer | 24 biz hrs |
| 8 | delivery_confirm_needed | Shipped, no delivery confirmation | 96 biz hrs |

Key features:
- Business hours calculator: Mon-Fri, skips all US federal holidays (computed, not hardcoded)
- 8 hours per business day (24 biz hrs = 3 calendar days, 96 = 12 days)
- `check_all_orders()` — processes all active orders against all rules
- `check_order_alerts(order_id)` — single order check
- Auto-resolves alerts when condition no longer applies
- Deduplicates — won't create duplicate alerts
- Auto-resolves all alerts when order marked complete

**`alerts_routes.py`** — FastAPI router with endpoints:
- `POST /alerts/check-all` — cron endpoint, checks all active orders
- `POST /alerts/check/{order_id}` — check single order
- `GET /alerts/summary` — unresolved alert count by type
- `GET /alerts/` — list alerts (optional order_id filter)
- `POST /alerts/{alert_id}/resolve` — manually resolve an alert

**Commits:**
- `3c8530d...` — alerts_engine.py
- `4099a86...` — alerts_routes.py

### 3. Known Bug Identified
`checkout.py` line ~242 in cfc-orders has `freight_class="70"` hardcoded — should be "85". This is the audit's freight class bug. Separate fix for Phase 5.

---

## WIRING NEEDED (William local)

The alerts_routes.py router needs to be mounted in main.py. Add these two lines:

Near the top imports:
```
from alerts_routes import alerts_router
```

After the CORS middleware block:
```
app.include_router(alerts_router)
```

Then push:
```
cd C:\dev\CFCOrderBackend_Sandbox
```
```
git pull origin main
```
```
git push origin main
```

Also still pending from Session 6 — frontend cleanup:
```
cd C:\dev\CFCOrdersFrontend_Sandbox
```
```
git rm -r --cached node_modules
```
```
git rm -r --cached dist
```
```
git rm --cached cfc-frontend.zip
```
```
git commit -m "Remove committed junk: node_modules, dist, cfc-frontend.zip"
```
```
git push origin main
```

---

## BLOCKER STATUS (updated Mar 2, Session 7)

| # | Blocker | Status |
|---|---------|--------|
| 1 | rl-quote-sandbox private | ✅ RESOLVED — MCP bridge v2.6 has `rl-quote` alias |
| 2 | Render services dead | ✅ RESOLVED — paid tier, no sleep |
| 3 | PostgreSQL expired | ✅ RESOLVED |
| 4 | Hardcoded API key | ✅ RESOLVED |
| 5 | Frontend junk in repo | OPEN — needs William local git rm commands |
| 6 | Warehouse data wrong | ✅ RESOLVED — 12 warehouses, correct LI zip |
| 7 | Duplicate endpoint | OPEN — POST /rl/pickup/pro/{pro_number} defined twice in main.py |
| 8 | Freight class bug | OPEN — hardcoded "70" in checkout.py line ~242 |
| 9 | No authentication | OPEN — Phase 5 |
| 10 | AlertsEngine not wired | OPEN — needs 2-line import in main.py |

## BATTLE PLAN STATUS

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Cleanup & Hygiene | ✅ DONE (local git cleanup remaining) |
| 2 | RL-Quote Integration | ✅ DONE — 12 warehouses, correct data, v0.2.0 |
| 3A | AlertsEngine | ✅ CODE DONE — needs wiring (2 lines in main.py) |
| 3B | Order Lifecycle | NOT STARTED |
| 4 | Customer Communications | NOT STARTED |
| 5 | Backend Hardening | NOT STARTED |
| 6 | Frontend Redesign | NOT STARTED |
| 7 | Production Promotion | NOT STARTED |

## NEXT SESSION SHOULD

1. **Wire AlertsEngine** — William adds 2 import lines to main.py, pushes
2. **Test AlertsEngine** — hit POST /alerts/check-all, verify alert creation
3. **Test rl-quote** — verify 32148→32176 quote with correct LI origin
4. **Start Phase 3B** — Lifecycle engine (lifecycle_engine.py):
   - Add DB columns: `last_customer_email_at`, `lifecycle_status`, `lifecycle_deadline_at`
   - Write migration script
   - Build lifecycle_engine.py with check_all + process_order_lifecycle + extend_deadline + cancel_order
   - Wire into gmail_sync.py for last_customer_email_at tracking
5. **Fix freight class bug** — checkout.py "70" → "85" (quick win)
6. **Fix duplicate endpoint** — merge POST /rl/pickup/pro/{pro_number} (Phase 5 territory but quick)

## KEY REFERENCE FILES

- **Audit report**: CFC_Orders_Full_Stack_Audit.docx (delivered to William Mar 2)
- **Battle plan**: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md
- **Rules**: brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- **Master status**: brain:MASTER_STATUS.md (Workstream 6)
- **AlertsEngine**: cfc-orders:alerts_engine.py + cfc-orders:alerts_routes.py

## REPOS

- Sandbox backend: github.com/4wprince/CFCOrderBackend_Sandbox (v6.0.0)
- Sandbox frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox (v5.10.1)
- RL quote sandbox: github.com/4wprince/rl-quote-sandbox (MCP alias: `rl-quote`, v0.2.0)
- Prod backend: github.com/4wprince/CFCOrderBackend (outdated monolithic)
- Prod frontend: github.com/4wprince/CFCOrdersFrontend (behind sandbox)

## DEPLOY URLS

- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- RL-quote sandbox: rl-quote-sandbox.onrender.com
- Sandbox frontend: cfcordersfrontend-sandbox.vercel.app
- Production frontend: cfc-orders-frontend.vercel.app

## LOCAL REPOS ON WILLIAM'S MACHINE

- `C:\dev\CFCOrderBackend_Sandbox` — backend
- `C:\dev\CFCOrdersFrontend_Sandbox` — frontend
