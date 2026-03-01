# CFC Orders Sandbox — Battle Plan
**Created:** 2026-03-01
**Goal:** Make the sandbox badass across multiple sessions, then deploy
**Approach:** Fix what's broken, integrate what's separate, build what's missing

---

## CURRENT STATE SNAPSHOT (Mar 1, 2026)

### Services (ALL ALIVE)
| Service | URL | Version | Status |
|---------|-----|---------|--------|
| Sandbox Backend | cfcorderbackend-sandbox.onrender.com | v6.0.0 | ✅ Running, auto-sync active |
| Sandbox Frontend | cfcordersfrontend-sandbox.vercel.app | v5.10.1 | ✅ Running |
| rl-quote-sandbox | rl-quote-sandbox.onrender.com | v0.1.0 | ✅ Running (separate service) |
| Production Backend | (Render) | ~v5.7 | ✅ Running but 2mo behind |
| Production Frontend | cfc-orders-frontend.vercel.app | ~v5.10 | ✅ Running |

### Render Service IDs
- rl-quote-sandbox: `srv-d58g4163jp1c73bg91pg`
- CFCOrderBackend-Sandbox: `srv-d4tu1e24d50c73b6952g`

### Backend Codebase (CFCOrderBackend_Sandbox)
- **main.py** — 121KB, ~3,100 lines, 84 endpoints (FastAPI)
- **16 modules** — ai_summary, b2bwave_api, checkout, config, db_helpers, db_migrations, detection, email_parser, gmail_sync, load_rta_data, rl_carriers, rta_database, schema, shippo_rates, square_sync, sync_service
- **Dead files** — main2.py (134K), main4.py (131K), main7.py (113K), main8.py (103K), rl_api_test_clean.py (3K w/ HARDCODED API KEY), desktop.ini — total ~484KB garbage
- **openapi.json** — stale (says v5.9.0, server is v6.0.0)
- **requirements.txt** — only 4 packages (fastapi, uvicorn, psycopg2-binary, httpx) — missing many used in code
- **RTA_Cabinet_Database_42.xlsx** — 1.8MB data file committed to repo

### Frontend Codebase (CFCOrdersFrontend_Sandbox)
- **19 real source files**, 2,242 junk files (node_modules + dist committed to git)
- **8 JSX components**: App.jsx (17K), components/App.jsx (10K), CustomerAddress.jsx (5K), OrderCard.jsx (17K), OrderComments.jsx (5K), RLQuoteHelper.jsx (8K), ShipmentRow.jsx (9K), ShippingManager.jsx (13K), StatusBar.jsx (5K)
- **cfc-frontend.zip** committed to repo (8K)
- **Duplicate App.jsx** — src/App.jsx AND src/components/App.jsx both exist
- **config.js** points to sandbox backend ✅
- **Password**: `cfc2025` (hardcoded in config.js)

### rl-quote-sandbox (SEPARATE service, private repo)
- **Live endpoints**: POST /validate-address, POST /quote, GET /warehouses, /docs, /test-ui
- **GitHub repo**: 4wprince/rl-quote-sandbox — PRIVATE, can't read source
- **Has**: Smarty address validation, R+L freight quoting, warehouse list
- **Needs**: Source files shared OR repo made public for integration

### Database (PostgreSQL on Render)
- **7 tables**: orders, order_line_items, order_events, order_alerts, order_email_snippets, order_shipments, pending_checkouts, warehouse_mapping, trusted_customers
- **Alive** — auto_sync running successfully as of today
- **order_status VIEW** exists for computed status

### Rules (brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md v1.2)
- **7 order statuses**: needs_payment_link → awaiting_payment → needs_warehouse_order → awaiting_warehouse → needs_bol → awaiting_shipment → complete
- **6 shipment statuses**: needs_order → at_warehouse → needs_bol → ready_ship → shipped → delivered
- **8 AlertsEngine rules** (ORD-A1): all become CRITICAL at 24 biz hours (except delivery at 96)
- **4 trusted customers** who can ship before payment
- **7 golden conversation examples** for AI calibration
- **Weight thresholds**: <70 lbs → Shippo, >70 lbs → R+L LTL
- **Customer markup**: R+L quote + $50

---

## PHASE 1: CLEANUP & HYGIENE (Session 1 — ~30 min)

### Backend Repo
1. **DELETE dead files**: main2.py, main4.py, main7.py, main8.py, rl_api_test_clean.py, desktop.ini
2. **Fix requirements.txt**: Add all actually-used packages (anthropic, shippo, square, pandas, openpyxl, pydantic, google-auth, google-api-python-client, etc.)
3. **Update openapi.json** to match v6.0.0 (or delete and let FastAPI auto-generate)
4. **Update README.md** with real documentation (architecture, endpoints, env vars, deploy)

### Frontend Repo
5. **Fix .gitignore**: Add node_modules/, dist/, *.zip
6. **Remove committed junk**: node_modules dir, dist dir, cfc-frontend.zip from git history (or at minimum from HEAD)
7. **Resolve duplicate App.jsx**: Determine which is canonical (src/App.jsx vs src/components/App.jsx), delete the other

### Blocker Resolution
8. **rl-quote-sandbox access**: William makes repo public OR shares the 4 files (main.py, models.py, smarty_api.py, rl_api.py)
   - ALTERNATIVE: Since service is live, we could also just call its API from sandbox backend as a microservice (no code merge needed)

**Deliverables**: Clean repos, no dead code, proper .gitignore, accurate docs

---

## PHASE 2: RL-QUOTE INTEGRATION (Session 2 — full session)

### Option A: Merge Code Into Sandbox Backend
- Copy smarty_api.py, rl_api.py logic into sandbox backend
- Upgrade rl_carriers.py with better auth (key in JSON body, not header)
- Add address validation: `POST /validate-address` endpoint
- Add auto-quoting: `POST /quote/auto` endpoint (Smarty validate → R+L quote in one call)
- Add auto-BOL: `POST /shipments/{id}/auto-bol` endpoint
- Schema updates: add address_type, quote_expires_at, fee columns to order_shipments
- Update config.py with SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN env vars

### Option B: Microservice Architecture (Keep Separate)
- Sandbox backend calls rl-quote-sandbox API for address validation + quoting
- Add proxy endpoints that pass through to rl-quote-sandbox
- Simpler integration, keeps services independent
- rl-quote-sandbox continues running on its own Render service

### Frontend
- Add "Get Auto Quote" button to RLQuoteHelper.jsx
- Show validated address before quoting
- Display quote with customer markup (+$50) pre-calculated

**Deliverables**: Address validation + freight quoting integrated into main workflow

---

## PHASE 3: ALERTSENGINE (Session 3 — full session)

### Backend
1. Create `alerts_engine.py` module
2. Implement 8 alert rules from ORD-A1:
   - needs_invoice (order placed, no invoice after 24 biz hrs)
   - awaiting_payment_long (invoice sent, no payment after 24 biz hrs)
   - needs_warehouse_order (paid, not sent to warehouse after 24 biz hrs)
   - at_warehouse_long (at warehouse, not confirmed after 24 biz hrs)
   - needs_bol (warehouse confirmed, no BOL after 24 biz hrs)
   - ready_ship_long (BOL sent, not shipped after 24 biz hrs)
   - tracking_not_sent (shipped, no tracking email after 24 biz hrs)
   - delivery_confirm_needed (shipped, no delivery confirm after 96 biz hrs)
3. Business hours calculator (Mon-Fri, skip US federal holidays)
4. Cron endpoint: `POST /alerts/check-all` — runs all 8 rules
5. Auto-scheduler: Add alerts check to existing sync_service.py interval (or separate cron)
6. Wire into existing order_alerts table

### Frontend
7. Alerts panel/badge showing unresolved alert count
8. Alert detail view per order
9. Resolve/dismiss alert buttons

**Deliverables**: Proactive alerting system running on schedule

---

## PHASE 4: CUSTOMER COMMUNICATIONS (Session 4 — full session)

### Email Templates
1. Payment link email (checkout link + order summary)
2. Payment confirmation email
3. Shipping notification email (tracking number, carrier, ETA)
4. Delivery confirmation email
5. Payment reminder for trusted customers (ORD-T2)

### Backend
6. Enable GMAIL_SEND_ENABLED=true
7. Build email template engine (HTML templates with order data injection)
8. Endpoint: `POST /orders/{id}/send-email` with template selection
9. Auto-send triggers tied to status transitions
10. Email send logging in order_events table

### Frontend
11. "Send Email" button on order cards with template picker
12. Email history view per order

**Deliverables**: Automated customer communications at each lifecycle stage

---

## PHASE 5: BACKEND HARDENING (Session 5 — full session)

### Code Quality
1. **main.py decomposition** — 121KB/3,100 lines is too big. Extract logical groups:
   - order_routes.py (order CRUD + status)
   - shipment_routes.py (shipment CRUD + methods)
   - sync_routes.py (B2BWave, Gmail, Square sync endpoints)
   - checkout_routes.py (checkout flow)
   - admin_routes.py (init-db, migrations, debug endpoints)
   - Keep main.py as app factory + router mounting only
2. **Config consolidation** — checkout.py and gmail_sync.py bypass config.py, fix this
3. **Fix bare except clauses** (2 found in audit)
4. **Update Anthropic API version** in ai_summary.py
5. **Error handling** — consistent error responses across all endpoints
6. **Logging** — structured logging with request IDs

### Security
7. **Remove hardcoded password** from frontend config.js → env var or auth system
8. **CORS configuration** — verify only sandbox/production frontends allowed
9. **Rate limiting** on public endpoints

### Database
10. **Add indexes** if missing for common queries
11. **Connection pooling** — verify db_helpers.py handles this properly

**Deliverables**: Clean, modular, secure codebase ready for production

---

## PHASE 6: FRONTEND POLISH (Session 6 — full session)

### UX Improvements
1. **Dashboard view** — order count by status, alerts badge, recent activity
2. **Real-time refresh** — auto-poll or WebSocket for live order updates
3. **Better order cards** — clearer status indicators, action buttons per state
4. **Bulk actions** — select multiple orders, batch status update
5. **Search/filter** — by customer, status, warehouse, date range
6. **Mobile responsive** — verify/fix for tablet use in warehouse

### Integration
7. **Inline R+L quoting** — quote from within order detail, not separate helper
8. **Shipment timeline** — visual status progression per shipment
9. **Email log** — show sent emails per order

### Tech Debt
10. **Component cleanup** — resolve any duplicate/dead components
11. **Error boundaries** — graceful error handling in UI
12. **Loading states** — proper spinners/skeletons

**Deliverables**: Professional, fast, usable order management interface

---

## PHASE 7: PRODUCTION PROMOTION (Session 7+)

Only after Phases 1-6 are solid:
1. Copy all modules from sandbox → production backend repo
2. Update production config with all env vars
3. Update production frontend to point to production backend
4. Deploy production backend to Render
5. Deploy production frontend to Vercel
6. Smoke test all flows end-to-end
7. Monitor for 1 week before considering sandbox work complete

---

## SESSION EXECUTION ORDER

| Session | Phase | Focus | Prerequisites |
|---------|-------|-------|--------------|
| Next | Phase 1 | Cleanup & Hygiene | rl-quote-sandbox access decision |
| +1 | Phase 2 | RL-Quote Integration | Phase 1 complete, rl-quote access |
| +2 | Phase 3 | AlertsEngine | Phase 1 complete |
| +3 | Phase 4 | Customer Communications | Phase 3 (alerts trigger emails) |
| +4 | Phase 5 | Backend Hardening | Phases 2-4 complete |
| +5 | Phase 6 | Frontend Polish | Phase 5 complete |
| +6 | Phase 7 | Production Deploy | All phases complete |

Note: Phases 2 and 3 can run in parallel (different code areas). Phase 4 depends on Phase 3. Phase 5 should wait until feature work is done to avoid refactoring twice.

---

## KEY REFERENCE

### Repos
- Sandbox backend: github.com/4wprince/CFCOrderBackend_Sandbox
- Sandbox frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox
- Prod backend: github.com/4wprince/CFCOrderBackend
- Prod frontend: github.com/4wprince/CFCOrdersFrontend
- RL sandbox: github.com/4wprince/rl-quote-sandbox (PRIVATE)

### Deploy URLs
- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- RL-quote: rl-quote-sandbox.onrender.com
- Sandbox frontend: cfcordersfrontend-sandbox.vercel.app
- Prod frontend: cfc-orders-frontend.vercel.app

### Env Vars Needed (current + new)
Current: DATABASE_URL, B2BWAVE_URL, B2BWAVE_USERNAME, B2BWAVE_API_KEY, ANTHROPIC_API_KEY, SHIPPO_API_KEY, SQUARE_ACCESS_TOKEN, SQUARE_ENVIRONMENT, CHECKOUT_BASE_URL, CHECKOUT_SECRET, GMAIL_SEND_ENABLED
New for Phase 2: SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN, RL_ACCOUNT_NUMBER
New for Phase 4: GMAIL_SEND_ENABLED=true (flip existing)

### Rules
- brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- 8 alert rules (ORD-A1), 7 order statuses (ORD-W1), 6 shipment statuses (ORD-SH1)
- Weight: <70 lbs → Shippo, >70 lbs → R+L LTL
- Customer markup: R+L quote + $50
- Freight class: 85 (always for RTA cabinets)
