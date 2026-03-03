# CFC Orders — Sandbox vs Production Audit
**Started:** 2026-03-03
**Status:** Step 1 COMPLETE (sandbox inventory). Steps 2–4 pending.

---

## ENVIRONMENT MAP

| | Sandbox (TRUTH) | Production (2+ months behind) |
|---|---|---|
| **Frontend URL** | https://cfcordersfrontend-sandbox.vercel.app | https://cfc-orders-frontend.vercel.app |
| **Backend URL** | https://cfcorderbackend-sandbox.onrender.com | https://cfc-backend-b83s.onrender.com |
| **Frontend repo** | 4wprince/CFCOrdersFrontend_Sandbox (MCP: cfc-orders-frontend) | 4wprince/CFCOrdersFrontend (NOT in MCP) |
| **Backend repo** | 4wprince/CFCOrderBackend_Sandbox (MCP: cfc-orders) | Unknown (NOT in MCP) |
| **Local path** | C:\Sandbox\CFCOrdersFrontend + C:\Sandbox\CFCOrderBackend | C:\CFCOrdersFrontend + C:\CFCOrderBackend |
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

## STEP 2: PRODUCTION INVENTORY (⏳ PENDING — needs William's PowerShell)

Run these commands on William's local machine:

### Command 1: Production backend health
```powershell
(Invoke-WebRequest -Uri "https://cfc-backend-b83s.onrender.com/health" -UseBasicParsing).Content
```

### Command 2: Production frontend file listing
```powershell
cd C:\CFCOrdersFrontend
Get-ChildItem -Recurse src\ -Include *.jsx,*.js,*.css | Select-Object FullName,Length
```

### Command 3: Production backend file listing
```powershell
cd C:\CFCOrderBackend
Get-ChildItem *.py | Select-Object Name,Length
```

### Command 4: Production frontend git log
```powershell
cd C:\CFCOrdersFrontend
git log --oneline -10
```

### Command 5: Production backend git log
```powershell
cd C:\CFCOrderBackend
git log --oneline -10
```

### Command 6: Production frontend config.js
```powershell
cd C:\CFCOrdersFrontend
Get-Content src\config.js
```

---

## STEP 3: GAP ANALYSIS (⏳ PENDING — needs Step 2 data)

Will create comparison table:
- Backend files: sandbox-only vs production-only
- Frontend components: sandbox-only vs production-only
- Feature matrix: which capabilities exist where
- "Lost functionality" check: anything in production NOT in sandbox

---

## STEP 4: PROMOTION PLAN (⏳ PENDING — needs Step 3)

Will create:
- Env vars checklist for production Render
- DB migration sequence
- Config.js changes
- File copy/push order
- Rollback plan
