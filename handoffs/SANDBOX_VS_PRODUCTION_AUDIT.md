# CFC Orders — Sandbox vs Production Audit
**Started:** 2026-03-03
**Status:** Steps 1–3 COMPLETE. Step 4 (Promotion Plan) pending for Chat 2.

---

## ENVIRONMENT MAP

| | Sandbox (TRUTH) | Production (2+ months behind) |
|---|---|---|
| **Frontend URL** | https://cfcordersfrontend-sandbox.vercel.app | https://cfc-orders-frontend.vercel.app |
| **Backend URL** | https://cfcorderbackend-sandbox.onrender.com | https://cfc-backend-b83s.onrender.com |
| **Backend Version** | v6.0.0 | v5.9.1 |
| **Frontend repo** | cabinetsforcontractors/CFCOrdersFrontend_Sandbox (MCP: cfc-orders-frontend) | cabinetsforcontractors/CFCOrdersFrontend (NOT in MCP) |
| **Backend repo** | cabinetsforcontractors/CFCOrderBackend_Sandbox (MCP: cfc-orders) | Unknown (NOT in MCP) |
| **Local paths** | C:\dev\CFCOrderBackend_Sandbox + C:\dev\CFCOrdersFrontend_Sandbox | ⚠️ NO LOCAL CLONE EXISTS |
| **Config switching** | config.js: `IS_SANDBOX = true`, API_URL → sandbox backend | config.js: `IS_SANDBOX = false`, API_URL → production backend |

---

## STEP 1: SANDBOX INVENTORY (✅ COMPLETE)

### Backend File Inventory (45 files)

**Core Application:**
| File | Size | Description |
|------|------|-------------|
| main.py | 119,933 B (3,101 lines) | FastAPI app v6.0.0, ALL route definitions, checkout UI |
| config.py | 5,806 B | Env vars: DB, B2BWave, Anthropic, Shippo, RL, warehouses |
| schema.py | 9,615 B | PostgreSQL schema SQL |
| db_helpers.py | 10,295 B | DB connection pool, get_db(), alert/order helpers |
| db_migrations.py | 15,033 B | 9 migration functions (tables, columns, views) |
| requirements.txt | 443 B | Python dependencies |

**Sync & Integration:**
| File | Size | Description |
|------|------|-------------|
| sync_service.py | 13,041 B | B2BWave auto-sync scheduler + sync_order_from_b2bwave |
| b2bwave_api.py | 6,504 B | B2BWave REST API client (legacy, kept for compat) |
| gmail_sync.py | 23,147 B | Gmail API sync for order status updates |
| square_sync.py | 12,188 B | Square payment sync |

**Email System (Phase 4):**
| File | Size | Description |
|------|------|-------------|
| email_templates.py | 21,637 B | 9 HTML email templates (lifecycle + manual) |
| email_sender.py | 11,177 B | Gmail API send + dry run + history |
| email_routes.py | 6,266 B | Router: /email/templates, /orders/{id}/send-email, preview, history |
| email_wiring.py | 902 B | Mounts email_router |
| email_parser.py | 7,591 B | Parse B2BWave order emails |

**Alerts Engine (Phase 3A):**
| File | Size | Description |
|------|------|-------------|
| alerts_engine.py | 17,936 B | 8 alert rules (ORD-A1), check_all_orders, auto-resolve |
| alerts_routes.py | 3,089 B | Router: /alerts/check-all, /alerts/summary, /alerts/, resolve |

**Lifecycle Engine (Phase 3B):**
| File | Size | Description |
|------|------|-------------|
| lifecycle_engine.py | 24,243 B | 7/14/21 day timeline, check/extend/cancel/summary |
| lifecycle_routes.py | 7,071 B | Router: /lifecycle/check-all, extend, cancel, summary, orders |
| lifecycle_wiring.py | 1,718 B | Mounts lifecycle_router + migration endpoints |

**Shipping:**
| File | Size | Description |
|------|------|-------------|
| rl_carriers.py | 22,823 B | R+L Carriers direct API (quote, BOL, pickup, track, notify) |
| rl_quote_proxy.py | 9,716 B | Proxy to rl-quote-sandbox microservice (validate, quote, auto-quote) |
| shippo_rates.py | 9,482 B | Shippo small package rates |
| checkout.py | 22,706 B | Warehouse routing, shipping calc, Square payment links, checkout UI |

**AI:**
| File | Size | Description |
|------|------|-------------|
| ai_summary.py | 11,585 B | Anthropic API for order summaries (short + comprehensive) |
| ai_configure.py | 5,763 B | AI-powered UI config (Connie types natural language → CSS changes) |
| ai_configure_wiring.py | 458 B | Mounts ai_configure router |

**Data:**
| File | Size | Description |
|------|------|-------------|
| rta_database.py | 15,953 B | RTA product weights/shipping rules |
| load_rta_data.py | 5,359 B | Load RTA data from Excel |
| RTA_Cabinet_Database_42.xlsx | 1,805,569 B | SKU weight database |
| detection.py | 7,524 B | Payment link, RL quote, PRO number detection |

**Wiring:**
| File | Size | Description |
|------|------|-------------|
| startup_wiring.py | 1,699 B | One-call mount for lifecycle + email + ai_configure |

**Docs/Handoffs:** 8 files in handoffs/

---

### Backend Endpoints — Complete Catalog (81 endpoints)

**Root & Health (main.py):**
- `GET /` — Status + module health
- `GET /health` — Simple health check

**DB Migrations (main.py, 9 endpoints):**
- `POST /create-pending-checkouts-table`
- `POST /create-shipments-table`
- `POST /add-rl-fields`
- `POST /add-ps-fields`
- `POST /fix-shipment-columns`
- `POST /fix-sku-columns`
- `POST /fix-order-id-length`
- `POST /recreate-order-status-view`
- `POST /add-weight-column`
- `GET /debug/orders-columns`
- `POST /init-db`

**B2BWave Sync (main.py):**
- `GET /b2bwave/test`
- `POST /b2bwave/sync`
- `GET /b2bwave/order/{order_id}`
- `POST /gmail/sync`
- `POST /square/sync`
- `GET /square/status`

**Shippo (main.py):**
- `GET /shippo/status`
- `GET /shippo/rates`
- `POST /shippo/test`

**R+L Carriers Direct (main.py, 16 endpoints):**
- `GET /rl/status`
- `GET /rl/test`
- `GET /rl/quote`
- `GET /rl/track/{pro_number}`
- `POST /rl/bol`
- `GET /rl/bol/{pro_number}`
- `GET /rl/bol/{pro_number}/pdf`
- `GET /rl/bol/{pro_number}/labels`
- `POST /rl/pickup`
- `POST /rl/pickup/pro/{pro_number}`
- `GET /rl/pickup/{pickup_id}`
- `DELETE /rl/pickup/{pickup_id}`
- `GET /rl/pickup/pro/{pro_number}`
- `DELETE /rl/pickup/pro/{pro_number}`
- `POST /rl/notify`
- `GET /rl/notify/{pro_number}`
- `POST /rl/order/{order_id}/create-bol`
- `POST /rl/order/{order_id}/pickup`
- `GET /rl/order/{order_id}/shipments`

**RTA Database (main.py):**
- `GET /rta/status`
- `POST /rta/init`
- `GET /rta/sku/{sku}`
- `POST /rta/calculate-weight`

**Email Parsing & Detection (main.py):**
- `POST /parse-email`
- `POST /detect-payment-link`
- `POST /detect-payment-received`
- `POST /detect-rl-quote`
- `POST /detect-pro-number`

**Order CRUD (main.py):**
- `GET /orders`
- `GET /orders/{order_id}`
- `PATCH /orders/{order_id}`
- `DELETE /orders/{order_id}`
- `PATCH /orders/{order_id}/checkpoint`
- `PATCH /orders/{order_id}/set-status`
- `POST /orders/{order_id}/generate-summary`
- `POST /orders/{order_id}/comprehensive-summary`
- `POST /orders/{order_id}/add-email-snippet`
- `GET /orders/{order_id}/supplier-sheet-data`
- `GET /orders/status/summary`
- `GET /orders/{order_id}/events`

**Shipment Management (main.py):**
- `GET /orders/{order_id}/shipments`
- `GET /shipments`
- `PATCH /shipments/{shipment_id}`
- `GET /shipments/{shipment_id}/rl-quote-data`

**Warehouse Mapping (main.py):**
- `GET /warehouse-mapping`
- `POST /warehouse-mapping`

**Trusted Customers (main.py):**
- `GET /trusted-customers`
- `POST /trusted-customers`
- `DELETE /trusted-customers/{customer_id}`
- `POST /check-payment-alerts`

**Checkout Flow (main.py):**
- `GET /checkout-status`
- `GET /debug/b2bwave-raw/{order_id}`
- `GET /debug/warehouse-routing/{order_id}`
- `GET /debug/test-checkout/{order_id}`
- `POST /webhook/b2bwave-order`
- `GET /checkout/payment-complete`
- `GET /checkout/{order_id}`
- `POST /checkout/{order_id}/create-payment`
- `GET /checkout-ui/{order_id}` (HTML page)

**RL Quote Proxy (rl_quote_proxy.py, prefix /proxy):**
- `GET /proxy/health`
- `POST /proxy/validate-address`
- `POST /proxy/quote`
- `POST /proxy/auto-quote`
- `GET /proxy/warehouses`

**Alerts Engine (alerts_routes.py, prefix /alerts):**
- `POST /alerts/check-all`
- `POST /alerts/check/{order_id}`
- `GET /alerts/summary`
- `GET /alerts/`
- `POST /alerts/{alert_id}/resolve`

**Lifecycle Engine (lifecycle_routes.py, prefix /lifecycle):**
- `POST /lifecycle/check-all`
- `POST /lifecycle/check/{order_id}`
- `POST /lifecycle/extend/{order_id}`
- `POST /lifecycle/cancel/{order_id}`
- `GET /lifecycle/summary`
- `GET /lifecycle/orders`

**Lifecycle Migrations (lifecycle_wiring.py):**
- `POST /add-lifecycle-fields`
- `POST /backfill-lifecycle`

**Email Communications (email_routes.py):**
- `GET /email/templates`
- `GET /email/templates/{template_id}/preview`
- `POST /orders/{order_id}/send-email`
- `POST /orders/{order_id}/preview-email`
- `GET /orders/{order_id}/email-history`

**AI Configure (ai_configure.py, prefix /ai):**
- `POST /ai/configure`
- `GET /ai/ui-schema`

---

### Frontend File Inventory (20 files)

**Core:**
| File | Size | Description |
|------|------|-------------|
| src/App.jsx | 47,991 B | v7.2.0 — Dark theme, 8 tabs (All, Inactive, Pay, Need Invoice, At WH, Need BOL, Ship, Done), alerts bell, lifecycle badges, canceled indicator |
| src/config.js | 941 B | API_URL, IS_SANDBOX, APP_PASSWORD, env switcher |
| src/index.css | 25,416 B | v7.2.0 dark theme, canceled/inactive badges, panel styles |
| src/main.jsx | 230 B | React entry point |

**Components (11):**
| File | Size | Key Features |
|------|------|-------------|
| OrderCard.jsx | 17,548 B | Order card with status, checkpoints, AI summary, expand/collapse |
| EmailPanel.jsx | 16,535 B | Template picker, preview, send, history |
| ShippingManager.jsx | 13,563 B | Multi-warehouse shipping management |
| RLQuoteHelper.jsx | 13,421 B | Auto-quote via proxy, weight calc, quote display |
| ShipmentRow.jsx | 9,187 B | Per-warehouse shipment row with status/tracking |
| AiConfigPanel.jsx | 9,128 B | Natural language UI config (Connie's tool) |
| BrainChat.jsx | 7,597 B | v2.1.0 — BRAIN chat panel, hardcoded admin token |
| CustomerAddress.jsx | 5,048 B | Customer address display/edit |
| OrderComments.jsx | 4,865 B | Order comments/notes |
| StatusBar.jsx | 4,649 B | Status progression bar |
| statusStyles.css | 4,125 B | Status-specific color classes |

---

### Latest Sandbox Commits (as of 2026-03-03)

**Backend (last 5):**
1. `5df28920` — Update email templates: 7/14/21 day lifecycle
2. `988e40ae` — Update lifecycle timeline to 7/14/21 days
3. `8333193c` — Phase 5 Step 1: Delete patch_main.py
4. `e6b54897` — Phase 5 Step 1: Delete main_OLD.py
5. `7dd85247` — Phase 5 Step 1: Delete main.py.bak

**Frontend (last 5):**
1. `f9b57698` — fix: dark text color on Bill To h4
2. `332130c3` — fix: dark text color on Auto Quote Complete
3. `d90295cb` — v7.2.0: canceled/inactive badges, panel-open shift, tab styles
4. `c745005c` — BrainChat v2.1.0: hardcode admin token
5. `7a006448` — v7.2.0: All/Inactive tabs, lifecycle badges

---

## STEP 2: PRODUCTION INVENTORY (✅ COMPLETE)

### Method
No local production clone exists on William's machine. Only sandbox folders found:
- `C:\dev\CFCOrderBackend_Sandbox`
- `C:\dev\CFCOrdersFrontend_Sandbox`

Production repos are NOT in MCP. Inventory was done by probing the live production backend API.

### Production Backend — v5.9.1

**Root endpoint response:**
```json
{
  "status": "ok",
  "service": "CFC Order Workflow",
  "version": "5.9.1",
  "auto_sync": {"enabled": true, "interval_minutes": 15, "last_sync": "2026-03-03T13:22:20.710495+00:00", "running": false},
  "gmail_sync": {"enabled": true},
  "square_sync": {"enabled": true}
}
```

**Key observations:**
- Root response does NOT include `alerts_engine`, `lifecycle_engine`, `email_engine`, or `ai_configure` fields — these modules don't exist in production code
- Auto-sync, Gmail, and Square sync are all active and working
- Version 5.9.1 vs sandbox 6.0.0

### Production Endpoint Probe Results

| Endpoint | Status | Notes |
|----------|--------|-------|
| `GET /` | ✅ 200 | v5.9.1, auto_sync + gmail + square only |
| `GET /health` | ✅ 200 | `{"status":"ok","version":"5.9.1"}` |
| `GET /orders?limit=1` | ✅ 200 | Working — returns full order data with shipments |
| `GET /alerts/summary` | ❌ 404 | Alerts engine NOT deployed |
| `GET /lifecycle/summary` | ❌ 404 | Lifecycle engine NOT deployed |
| `GET /proxy/health` | ❌ 404 | RL quote proxy NOT deployed |
| `GET /email/templates` | ❌ 404 | Email comms NOT deployed |
| `GET /ai/ui-schema` | ❌ 404 | AI configure NOT deployed |
| `GET /checkout-status` | ❌ 404 | Checkout flow NOT deployed |
| `GET /rl/status` | ❌ 404 | R+L direct API NOT deployed |
| `GET /shippo/status` | ❌ 404 | Shippo NOT deployed |
| `GET /rta/status` | ❌ 404 | RTA database NOT deployed |

### Critical Finding: DB Schema Mismatch

The production `/orders` response includes lifecycle fields that SHOULD NOT exist in v5.9.1:
- `lifecycle_status`: "active"
- `lifecycle_deadline_at`: null
- `lifecycle_reminders_sent`: {}
- `clock_started_at`: null
- `last_customer_email_at`: "2026-03-03T07:19:26.306861+00:00"

**This means someone ran the lifecycle DB migration (`/add-lifecycle-fields`) on the production database, but never deployed the lifecycle engine code.** The DB is ahead of the code. This is GOOD for promotion — the schema is already there.

### Production Feature Summary

**Working in production:**
- Order CRUD (create, read, update, delete, checkpoints, set-status)
- B2BWave auto-sync (15-min interval)
- Gmail sync (order status from emails)
- Square sync (payment matching)
- Shipment management (per-warehouse)
- Warehouse mapping
- Trusted customers
- Email parsing
- Payment/RL quote/PRO number detection
- AI summaries (short + comprehensive via Anthropic)
- Order events logging

**NOT in production (all 404):**
- Alerts Engine (Phase 3A) — 5 endpoints
- Lifecycle Engine (Phase 3B) — 8 endpoints (but DB fields exist!)
- RL Quote Proxy (Phase 2) — 5 endpoints
- Email Communications (Phase 4) — 5 endpoints
- AI Configure — 2 endpoints
- Checkout Flow — 9 endpoints
- R+L Carriers Direct API — 19 endpoints
- Shippo rates — 3 endpoints
- RTA Database — 4 endpoints
- startup_wiring.py module system

---

## STEP 3: GAP ANALYSIS (✅ COMPLETE)

### Backend Gap: v5.9.1 → v6.0.0

Production is missing **60+ endpoints** across 10 feature groups. Here's the full breakdown:

| Feature Group | Sandbox Endpoints | Production | Gap |
|--------------|:-:|:-:|-----|
| Core CRUD + Sync | ~25 | ✅ ~25 | **PARITY** — both have orders, b2bwave, gmail, square |
| Alerts Engine | 5 | ❌ 0 | **MISSING** — alerts_engine.py, alerts_routes.py |
| Lifecycle Engine | 8 | ❌ 0 | **MISSING** — lifecycle_engine.py, lifecycle_routes.py, lifecycle_wiring.py (DB fields already exist!) |
| RL Quote Proxy | 5 | ❌ 0 | **MISSING** — rl_quote_proxy.py |
| Email Communications | 5 | ❌ 0 | **MISSING** — email_templates.py, email_sender.py, email_routes.py, email_wiring.py |
| AI Configure | 2 | ❌ 0 | **MISSING** — ai_configure.py, ai_configure_wiring.py |
| Checkout Flow | 9 | ❌ 0 | **MISSING** — checkout.py integration |
| R+L Direct API | 19 | ❌ 0 | **MISSING** — rl_carriers.py routes in main.py |
| Shippo | 3 | ❌ 0 | **MISSING** — shippo_rates.py routes in main.py |
| RTA Database | 4 | ❌ 0 | **MISSING** — rta_database.py, load_rta_data.py routes |
| startup_wiring | — | ❌ | **MISSING** — startup_wiring.py module loader |

### Backend Files: Sandbox-Only (likely missing from production)

These files exist in sandbox but almost certainly NOT in production (based on all their endpoints being 404):

| File | Lines | Purpose |
|------|-------|---------|
| alerts_engine.py | ~500 | 8 alert rules, check_all_orders |
| alerts_routes.py | 97 | /alerts/* router |
| lifecycle_engine.py | ~650 | 7/14/21 day timeline |
| lifecycle_routes.py | 189 | /lifecycle/* router |
| lifecycle_wiring.py | ~50 | Mounts lifecycle + migrations |
| email_templates.py | ~600 | 9 HTML email templates |
| email_sender.py | ~300 | Gmail API send |
| email_routes.py | 188 | /email/* + /orders/*/send-email router |
| email_wiring.py | ~25 | Mounts email router |
| rl_quote_proxy.py | 276 | /proxy/* router |
| ai_configure.py | 166 | /ai/* router |
| ai_configure_wiring.py | 16 | Mounts AI router |
| startup_wiring.py | 56 | One-call module mount |
| shippo_rates.py | ~250 | Shippo integration |
| rl_carriers.py | ~600 | R+L direct API |
| checkout.py | ~600 | Checkout flow |
| rta_database.py | ~400 | RTA weight lookup |
| load_rta_data.py | ~150 | RTA data loader |

**Total: 18 files sandbox has that production lacks**

### Frontend Gap

Production frontend is NOT accessible via MCP and no local clone exists, so we can't do a file-level diff. However, based on the backend gap:

**Sandbox frontend has (production likely lacks):**
- App.jsx v7.2.0 (production is probably v5.x or v6.x)
- Dark theme (v7.x feature)
- All/Inactive tabs (lifecycle integration)
- Alerts bell + dropdown
- Lifecycle badges (canceled/inactive indicators)
- EmailPanel.jsx (requires /email/* endpoints)
- AiConfigPanel.jsx (requires /ai/* endpoints)
- BrainChat.jsx v2.1.0 (hardcoded admin token)
- RLQuoteHelper.jsx auto-quote (requires /proxy/* endpoints)
- index.css v7.2.0 dark theme + badges

### "Lost Functionality" Check: Production → Sandbox

**VERDICT: NO lost functionality detected.**

The production root endpoint shows only: auto_sync, gmail_sync, square_sync. ALL of these exist in sandbox v6.0.0. The sandbox is a strict superset of production.

The one nuance: production's main.py (v5.9.1) may have slightly different implementations of the core CRUD endpoints. But since sandbox was forked FROM production and all core functionality was preserved, the risk is minimal.

**⚠️ One concern:** Production and sandbox share the SAME database (both `/orders?limit=1` return the same order #5465 with the same data). This means:
- They're already sharing state
- DB migrations done on one affect both
- This is WHY lifecycle fields already exist in production — sandbox migrations ran against the shared DB

---

## STEP 4: PROMOTION PLAN (⏳ PENDING — for Chat 2)

Chat 2 should build the step-by-step promotion checklist covering:

1. **Backend deployment strategy** — Options:
   - Option A: Point production Render service to sandbox repo (simplest)
   - Option B: Push sandbox code to production repo
   - Option C: Make sandbox THE production (rename/redirect)

2. **Backend env vars audit** — Compare sandbox vs production Render env vars:
   - DATABASE_URL (shared? separate?)
   - B2BWAVE_*, GMAIL_*, SQUARE_* credentials
   - ANTHROPIC_API_KEY
   - SHIPPO_API_KEY
   - RL_CARRIERS_API_KEY
   - RL_QUOTE_SANDBOX_URL
   - CHECKOUT_BASE_URL
   - GMAIL_SEND_ENABLED

3. **DB migration sequence** — What needs to run on production DB:
   - Lifecycle fields: ✅ ALREADY DONE
   - Other migrations needed?
   - Run /add-lifecycle-fields: SKIP (already there)
   - Run /backfill-lifecycle: May need to re-run

4. **Frontend deployment** — Config.js changes:
   - `IS_SANDBOX = false`
   - `API_URL = 'https://cfc-backend-b83s.onrender.com'` (or new URL)
   - `OTHER_ENV_URL` flip

5. **Testing checklist** — After promotion:
   - /health returns v6.0.0
   - /alerts/summary returns 200
   - /lifecycle/summary returns 200
   - /proxy/health returns 200
   - /email/templates returns 200
   - Orders still load
   - Auto-sync still works
   - Frontend loads, dark theme, all tabs

6. **Rollback plan** — If something breaks:
   - Render: revert to previous deploy
   - Vercel: revert to previous deployment
   - DB: No destructive changes, rollback not needed

### ⚠️ KEY DECISION FOR WILLIAM

**The biggest question is: Should production Render just point to the sandbox repo?**

Since there's no local production clone and the production repo isn't in MCP, the cleanest path might be:
- Production Render → point git source to `cabinetsforcontractors/CFCOrderBackend_Sandbox`
- Production Vercel → point git source to `cabinetsforcontractors/CFCOrdersFrontend_Sandbox`
- Update config.js to detect environment via env var instead of hardcoded IS_SANDBOX

This eliminates the two-repo problem entirely. Chat 2 should discuss this with William.
