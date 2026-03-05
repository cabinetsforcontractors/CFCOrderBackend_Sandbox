# CFC Orders Sandbox — Battle Plan
**Created:** 2026-03-01
**Updated:** 2026-03-04 (Audit fix — reconciled against master, completed phases marked done)
**Goal:** Make the sandbox badass across multiple sessions, then deploy
**Approach:** Fix what's broken, integrate what's separate, build what's missing

---

## CURRENT STATE SNAPSHOT (Mar 4, 2026 — Post-Session 17)

### Services (ALL ALIVE)
| Service | URL | Version | Status |
|---------|-----|---------|--------|
| Sandbox Backend | cfcorderbackend-sandbox.onrender.com | v6.0.0 | ✅ Running |
| Sandbox Frontend | cfcordersfrontend-sandbox.vercel.app | **v7.2.2** | ✅ Live — dark theme + alerts + panel click-close |
| rl-quote-sandbox | rl-quote-sandbox.onrender.com | v0.1.0 | ✅ Running |
| Production Backend | (Render) | ~v5.7 | ✅ Running but 2mo behind |
| Production Frontend | cfc-orders-frontend.vercel.app | ~v5.10 | ✅ Running |

### Render Service IDs
- rl-quote-sandbox: `srv-d58g4163jp1c73bg91pg`
- CFCOrderBackend-Sandbox: `srv-d4tu1e24d50c73b6952g`

### Database (PostgreSQL on Render)
- **10 tables** including lifecycle fields (migrated ✅, 15 orders backfilled ✅)
- GMAIL_SEND_ENABLED=true ✅ — live on Render

### Rules (brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md v1.2)
- **7 order statuses**: needs_payment_link → awaiting_payment → needs_warehouse_order → awaiting_warehouse → needs_bol → awaiting_shipment → complete
- **6 shipment statuses**: needs_order → at_warehouse → needs_bol → ready_ship → shipped → delivered
- **8 AlertsEngine rules** (ORD-A1): all become CRITICAL at 24 biz hours (except delivery at 96)
- **4 trusted customers** who can ship before payment
- **Weight thresholds**: <70 lbs → Shippo, >70 lbs → R+L LTL
- **Customer markup**: R+L quote + $50
- **Freight class**: 85 (always for RTA cabinets)

---

## ORDER LIFECYCLE SYSTEM ✅ DEPLOYED

### Lifecycle Timeline (CURRENT — updated Mar 3)
Clock starts from last customer email activity.

| Calendar Day | Action |
|-------------|--------|
| Day 7 | Move order → **Inactive** tab |
| Day 14 | Send cancellation warning email |
| Day 21 | Auto-cancel via B2BWave API |

**Clock rules:**
- Reminder emails do NOT reset clock
- Customer response adds +7 days to all timers
- "Cancel" keyword in customer email → immediate cancellation flow

---

## PHASE COMPLETION STATUS

| Phase | Status | Notes |
|-------|--------|-------|
| Phase 1: Cleanup | ✅ DONE | |
| Phase 2: RL-Quote | ✅ DONE | MCP v2.6, 12 warehouses, LI zip fixed |
| Phase 3A: AlertsEngine | ✅ DEPLOYED | 8 rules, tz bug fixed (commit c051048) |
| Phase 3B: Lifecycle | ✅ DEPLOYED | DB migrated, 15 orders backfilled |
| Phase 3C: Frontend Alerts | ✅ DONE | Bell badge, dropdown, resolve/dismiss |
| Phase 4: Email Comms | ✅ DEPLOYED | GMAIL_SEND_ENABLED=true live |
| **Phase 5: Backend Hardening** | **🔥 IN PROGRESS — 5B DONE (main.py → ~175 lines), 5C DONE (api.js sha 0c498013). NEXT: sandbox verify → rate limiting (5B slowapi) → JWT rotation.** | main.py decomposition, JWT, CORS |
| Phase 6: Frontend Redesign | ✅ DONE | App.jsx v7.2.2 live |
| Phase 7: Production Promotion | NOT STARTED | After Phase 5 complete |

---

## PHASE 5: BACKEND HARDENING (NEXT)

### Code Quality
1. **main.py decomposition** — ✅ DONE (Phase 5B). main.py = ~175 lines. All route modules live: detection_routes.py, sync_routes.py, migration_routes.py, checkout_routes.py. See SESSION_HANDOFF_ORDERS.md for full module list.
2. Config consolidation — checkout.py and gmail_sync.py bypass config.py
3. Fix bare except clauses (2 found)
4. Update Anthropic API version in ai_summary.py
5. Delete dead files — main2.py, main4.py, main7.py, main8.py, desktop.ini
6. Delete unused frontend components — StatusBar.jsx, OrderCard.jsx, OrderComments.jsx

### Security
7. JWT authentication — api.js centralized ✅ (sha 0c498013). Token rotation = one-line change in api.js. Full JWT (Option C) is next.
8. CORS whitelist — only sandbox/production frontends
9. Rate limiting on public endpoints
10. API key middleware for service-to-service calls

---

## FUTURE ITEMS (noted, not prioritized)
1. Multi-warehouse split shipments
2. Side panel resize behavior (push content, not overlay) — partially done (click-to-close live)
3. Live vs sandbox functionality audit
4. Shippo integration (replace Pirate Ship)

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
