# CFC Orders Sandbox — Battle Plan
**Created:** 2026-03-01
**Updated:** 2026-03-02 (Session 6 — audit results + order lifecycle rules)
**Goal:** Make the sandbox badass across multiple sessions, then deploy
**Approach:** Fix what's broken, integrate what's separate, build what's missing

---

## CURRENT STATE SNAPSHOT (Mar 2, 2026 — Post-Audit)

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

### Audit Summary (47 Issues Found)
| Severity | Count | Key Items |
|----------|-------|-----------|
| CRITICAL | 6 | No auth, duplicate endpoint, freight class=70 bug, dead code w/ API key |
| HIGH | 8 | Monolith main.py (3,151 lines), requirements.txt incomplete, StatusBar bug |
| MEDIUM | 15 | STATUS_MAP 3x duplicated, unused OrderComments.jsx, no search/filter |
| LOW | 12 | No accessibility, no loading skeletons, no keyboard shortcuts |
| ENHANCEMENT | 6 | Customer portal, tasks/todo, analytics, kanban, email automation |

### Database (PostgreSQL on Render)
- **9 tables**: orders, order_line_items, order_events, order_alerts, order_email_snippets, order_shipments, pending_checkouts, warehouse_mapping, trusted_customers
- **Alive** — auto_sync running successfully
- **order_status VIEW** exists for computed status

### Rules (brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md v1.2)
- **7 order statuses**: needs_payment_link → awaiting_payment → needs_warehouse_order → awaiting_warehouse → needs_bol → awaiting_shipment → complete
- **6 shipment statuses**: needs_order → at_warehouse → needs_bol → ready_ship → shipped → delivered
- **8 AlertsEngine rules** (ORD-A1): all become CRITICAL at 24 biz hours (except delivery at 96)
- **4 trusted customers** who can ship before payment
- **Weight thresholds**: <70 lbs → Shippo, >70 lbs → R+L LTL
- **Customer markup**: R+L quote + $50

---

## ORDER LIFECYCLE SYSTEM (NEW — William's Rules, Mar 2)

### Overview
Automated inactivity detection, escalation, archiving, and cancellation based on email activity between CFC and the customer regarding a specific order. This is the backbone of the new tab system.

### Tabs
| Tab | Shows | Description |
|-----|-------|-------------|
| **All** | Active orders | Existing tab, unchanged |
| **Inactive** | 7+ days no email activity | Auto-moved, can be reactivated by customer response |
| **Archived** | 30+ days no email activity | Read-only, reactivatable, pending deletion |

### Lifecycle Timeline
Clock starts from last customer email activity (to or from customer about that order).

| Calendar Day | Action | Type |
|-------------|--------|------|
| Day 6 | Send email: "Order hasn't been paid" | Auto reminder (does NOT reset clock) |
| Day 7 | Move order → **Inactive** tab | Auto status change |
| Day 29 | Send email: "Order marked inactive" | Auto reminder (does NOT reset clock) |
| Day 30 | Move order → **Archived** tab | Auto status change |
| Day 44 | Send email: "Order will be deleted tomorrow" | Auto reminder (does NOT reset clock) |
| Day 45 | Hit B2BWave API → **cancel order** on website | Auto cancellation |

### Clock Rules
- **Basis:** Last email to/from customer regarding that specific order
- **Reminder emails DO NOT reset clock** — system-generated emails are excluded
- **Customer response:** Any customer email about the order adds +7 days to ALL action timers
- **"Cancel" keyword:** If customer email contains "cancel" (fuzzy), immediately:
  1. Ping B2BWave API → mark order canceled
  2. Send customer cancellation confirmation email
  3. Move order to Canceled status

### Email Templates
| Template | Trigger | Content |
|----------|---------|---------|
| payment_reminder | Day 6 | "Your order #{id} hasn't been paid. [Payment link]" |
| inactive_notice | Day 29 | "Your order #{id} has been marked inactive due to no activity." |
| deletion_warning | Day 44 | "Your order #{id} will be canceled tomorrow if no action." |
| cancel_confirm | Customer says "cancel" | "Your order #{id} has been canceled per your request." |

### Backend Requirements
- **New DB fields:** `last_customer_email_at` (timestamp), `lifecycle_status` (enum: active/inactive/archived/canceled), `lifecycle_deadline_at` (computed)
- **New cron endpoint:** `POST /lifecycle/check-all` — runs daily, processes all orders against timeline
- **B2BWave integration:** Cancel order API call at day 45
- **Email parser enhancement:** Detect "cancel" keyword in customer emails (fuzzy match)
- **Gmail sync enhancement:** Track `last_customer_email_at` per order on every email processed
- **Reminder filter:** System-generated emails tagged so they don't reset the clock

### Frontend Requirements
- **Tab bar:** All | Inactive (count) | Archived (count)
- **Inactive tab:** Shows days since last activity, next action date, countdown badge, "Reactivate" button
- **Archived tab:** Read-only order view, "Reactivate" button, days until deletion, warning badge
- **Visual indicators:** Color-coded urgency (green→yellow→orange→red), countdown timers

---

## PHASE 1: CLEANUP & HYGIENE ✅ DONE (Session 1)

Completed. Local git cleanup commands remain (node_modules, dist removal from frontend repo).

---

## PHASE 2: RL-QUOTE INTEGRATION ✅ DONE (Session 2)

Completed. rl-quote-sandbox deployed, R+L API connectivity verified, MCP bridge v2.6 deployed.

**Remaining:** Fix warehouse data in models.py (6 warehouses, correct LI zip).

---

## PHASE 3: ALERTSENGINE + ORDER LIFECYCLE (Next)

### 3A: AlertsEngine (from ORD-A1)
1. Create `alerts_engine.py` module
2. Implement 8 alert rules from ORD-A1
3. Business hours calculator (Mon-Fri, skip US federal holidays)
4. Cron endpoint: `POST /alerts/check-all`
5. Wire into existing order_alerts table

### 3B: Order Lifecycle System (NEW)
6. Add DB fields: `last_customer_email_at`, `lifecycle_status`, `lifecycle_deadline_at`
7. DB migration script for new columns
8. Create `lifecycle_engine.py` module:
   - `check_all_orders()` — daily cron
   - `process_order_lifecycle(order)` — individual order check
   - `extend_deadline(order_id, days=7)` — customer response handler
   - `cancel_order(order_id)` — B2BWave API + email + status
9. Cron endpoint: `POST /lifecycle/check-all`
10. Integrate with gmail_sync.py — track `last_customer_email_at` on every email
11. Add "cancel" keyword detection to email parser
12. Tag system-generated emails so they don't reset clock

### 3C: Frontend Alerts
13. Alerts panel/badge showing unresolved alert count
14. Alert detail view per order
15. Resolve/dismiss alert buttons

**Deliverables**: Proactive alerting + automated order lifecycle management

---

## PHASE 4: CUSTOMER COMMUNICATIONS

### Email Templates (expanded with lifecycle emails)
1. Payment link email (checkout link + order summary)
2. Payment confirmation email
3. Shipping notification email (tracking number, carrier, ETA)
4. Delivery confirmation email
5. Payment reminder for trusted customers (ORD-T2)
6. **Day 6 payment reminder** (lifecycle)
7. **Day 29 inactive notice** (lifecycle)
8. **Day 44 deletion warning** (lifecycle)
9. **Cancel confirmation** (lifecycle)

### Backend
10. Enable GMAIL_SEND_ENABLED=true
11. Build email template engine (HTML templates with order data injection)
12. Endpoint: `POST /orders/{id}/send-email` with template selection
13. Auto-send triggers tied to status transitions AND lifecycle events
14. Email send logging in order_events table
15. **Tag outgoing emails as system-generated** (lifecycle clock exclusion)

### Frontend
16. "Send Email" button on order cards with template picker
17. Email history view per order

**Deliverables**: Automated customer communications at each lifecycle stage

---

## PHASE 5: BACKEND HARDENING

### Code Quality
1. **main.py decomposition** — 3,151 lines → route modules:
   - order_routes.py (order CRUD + status)
   - shipment_routes.py (shipment CRUD + methods)
   - sync_routes.py (B2BWave, Gmail, Square sync)
   - checkout_routes.py (checkout flow)
   - admin_routes.py (init-db, migrations, debug)
   - lifecycle_routes.py (lifecycle + alerts endpoints)
   - Keep main.py as app factory + router mounting only
2. **Fix duplicate endpoint** — merge POST /rl/pickup/pro/{pro_number} (lines 821 + 917)
3. **Fix freight class bug** — global 70→85, add FREIGHT_CLASS constant
4. **Config consolidation** — checkout.py and gmail_sync.py bypass config.py
5. **Fix bare except clauses** (2 found)
6. **Update Anthropic API version** in ai_summary.py
7. **Fix requirements.txt** — add all 12+ actual dependencies
8. **Delete dead files** — main2.py, main4.py, main7.py, main8.py, desktop.ini

### Security
9. **JWT authentication** — replace hardcoded password
10. **CORS whitelist** — only sandbox/production frontends
11. **Rate limiting** on public endpoints
12. **API key middleware** for service-to-service calls

### Database
13. Add indexes for common queries
14. Connection pooling verification

**Deliverables**: Clean, modular, secure codebase

---

## PHASE 6: FRONTEND REDESIGN

### Dashboard View (NEW)
1. Metric cards row — clickable status counts (Need Invoice, Await Pay, etc.)
2. Alerts banner — overdue/stuck orders highlighted
3. Quick actions toolbar

### Tab System (NEW — Lifecycle)
4. All | Inactive | Archived tabs with counts
5. Inactive tab: days since activity, next action, countdown, reactivate button
6. Archived tab: read-only, reactivate button, days until deletion

### Orders Table (Replace Grid)
7. Sortable, filterable table replacing card grid
8. Checkbox column for bulk actions
9. Inline status badges with lifecycle indicators

### Order Detail Panel (Replace Modal)
10. Slide-in side panel (keeps list visible)
11. Tabs: Overview | Shipments | History | Communications
12. Activity timeline (visual event log)
13. Quick action buttons (Send Email, Print BOL, etc.)

### Search & Filters
14. Text search (order ID, customer, company, email, SKU, tracking #, PRO #)
15. Filter chips (status, warehouse, date range, payment status)
16. Saved/bookmarkable views (requires React Router)

### Bulk Actions
17. Select multiple → batch status update, send email, export CSV, archive

### Component Architecture
18. Extract STATUS_MAP to shared constants.js
19. Wire up unused OrderComments.jsx
20. Fix StatusBar onRefresh prop
21. Add error boundaries, loading skeletons
22. Component tree organized by feature domain

**Deliverables**: Professional, modern order management interface

---

## PHASE 7: PRODUCTION PROMOTION

Only after Phases 1-6 are solid:
1. Copy all modules from sandbox → production backend repo
2. Update production config with all env vars
3. Update production frontend to point to production backend
4. Deploy production backend to Render
5. Deploy production frontend to Vercel
6. Smoke test all flows end-to-end
7. Monitor for 1 week before considering complete

---

## SESSION EXECUTION ORDER

| Session | Phase | Focus | Prerequisites |
|---------|-------|-------|---------------|
| ✅ Done | Phase 1 | Cleanup & Hygiene | — |
| ✅ Done | Phase 2 | RL-Quote Integration | Phase 1 |
| ✅ Done | Audit | Full-stack audit + UI/UX research | — |
| Next | Phase 3 | AlertsEngine + Order Lifecycle | Phase 1, warehouse fix |
| +1 | Phase 4 | Customer Communications | Phase 3 (lifecycle triggers emails) |
| +2 | Phase 5 | Backend Hardening | Phases 2-4 complete |
| +3 | Phase 6 | Frontend Redesign | Phase 5 complete + HTML mockup approved |
| +4 | Phase 7 | Production Deploy | All phases complete |

---

## KEY REFERENCE

### Repos
- Sandbox backend: github.com/4wprince/CFCOrderBackend_Sandbox
- Sandbox frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox
- Prod backend: github.com/4wprince/CFCOrderBackend
- Prod frontend: github.com/4wprince/CFCOrdersFrontend
- RL sandbox: github.com/4wprince/rl-quote-sandbox (MCP alias: `rl-quote`)

### Deploy URLs
- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- RL-quote: rl-quote-sandbox.onrender.com
- Sandbox frontend: cfcordersfrontend-sandbox.vercel.app
- Prod frontend: cfc-orders-frontend.vercel.app

### Env Vars Needed (current + new)
Current: DATABASE_URL, B2BWAVE_URL, B2BWAVE_USERNAME, B2BWAVE_API_KEY, ANTHROPIC_API_KEY, SHIPPO_API_KEY, SQUARE_ACCESS_TOKEN, SQUARE_ENVIRONMENT, CHECKOUT_BASE_URL, CHECKOUT_SECRET, GMAIL_SEND_ENABLED
Phase 2: SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN, RL_ACCOUNT_NUMBER
Phase 4: GMAIL_SEND_ENABLED=true (flip existing)

### Rules
- brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- 8 alert rules (ORD-A1), 7 order statuses (ORD-W1), 6 shipment statuses (ORD-SH1)
- Weight: <70 lbs → Shippo, >70 lbs → R+L LTL
- Customer markup: R+L quote + $50
- Freight class: 85 (always for RTA cabinets)
- Order lifecycle: 7-day inactive, 30-day archive, 45-day cancel (clock extends +7 on customer response)
