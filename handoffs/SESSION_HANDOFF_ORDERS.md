# SESSION HANDOFF — CFC Orders (General)

**Last Updated:** 2026-03-02 (Session 6)
**Last Session:** Mar 2, 2026 — Full-stack audit (backend + frontend + UI/UX) + order lifecycle rules defined
**Session Before That:** Mar 2, 2026 — RL-Quote API testing + MCP bridge rl-quote repo added

---

## WHAT HAPPENED THIS SESSION (Mar 2 — Session 6)

### Full-Stack System Audit
Complete audit of cfc-orders backend, cfc-orders-frontend, and rl-quote repos. Deliverable: `CFC_Orders_Full_Stack_Audit.docx` (provided to William).

**47 issues identified across 5 severity levels:**

| Severity | Count | Key Examples |
|----------|-------|-------------|
| CRITICAL | 6 | No auth, duplicate endpoint, freight class bug, dead code w/ API key |
| HIGH | 8 | Monolith main.py (3,151 lines), missing requirements.txt deps, StatusBar bug |
| MEDIUM | 15 | STATUS_MAP duplicated 3x, unused OrderComments.jsx, no search/filter |
| LOW | 12 | No accessibility, no loading skeletons, no keyboard shortcuts |
| ENHANCEMENT | 6 | Customer portal, tasks/todo, analytics, kanban, email automation |

**Critical Backend Findings:**
1. `POST /rl/pickup/pro/{pro_number}` defined TWICE (lines 821 + 917) — second overwrites first
2. Freight class hardcoded as "70" in multiple places — should be "85" — every R+L quote is WRONG
3. ZERO authentication — CORS `allow_origins=["*"]`, no middleware, no API keys, no rate limiting
4. requirements.txt lists 4 of 12+ actual dependencies — fresh deploy would fail
5. ~484KB dead files: main2.py, main4.py, main7.py, main8.py, desktop.ini
6. AlertsEngine: 8 rules designed (ORD-A1) but ZERO code exists

**Critical Frontend Findings:**
1. STATUS_MAP + STATUS_OPTIONS duplicated in App.jsx, OrderCard.jsx, StatusBar.jsx
2. OrderComments.jsx (157 lines) fully built but NEVER imported/used
3. StatusBar `onRefresh` prop never passed from App.jsx — "Sync AI" button does nothing
4. node_modules (2,242 files) + dist committed to git — needs .gitignore
5. No routing, no search, no bulk actions, no error boundaries
6. 100+ lines of inline JSX for order detail modal in App.jsx

**Competitive Analysis:**
- Researched ShipStation, Ordoro, modern B2B order management UIs (2025 patterns)
- Key gaps: No dashboard view, no kanban, no customer portal, no email hub, no analytics
- UI redesign proposed: dashboard-first layout, side panel detail, component tree by feature domain

### NEW: Order Lifecycle & Inactivity System (William's Rules)

William defined a new automated order lifecycle system with inactivity detection, auto-archiving, and auto-cancellation. This is a MAJOR new feature that integrates with email parsing, B2BWave API, and the alert/reminder system.

#### Tabs
- **All** — existing tab, stays as-is, shows all active orders
- **Inactive** — NEW: orders with no email activity (to/from customer) for 7+ days
- **Archived** — NEW: orders inactive for 30+ days (read-only, can be reactivated)

#### Lifecycle Timeline (from last customer email activity)
| Day | Action | Type |
|-----|--------|------|
| 6 | Send reminder email: "Order hasn't been paid" | Auto email (does NOT reset clock) |
| 7 | Move order to Inactive tab | Auto status change |
| 29 | Send reminder email: "Order marked inactive" | Auto email (does NOT reset clock) |
| 30 | Move order to Archived tab | Auto status change |
| 44 | Send reminder email: "Order will be deleted tomorrow" | Auto email (does NOT reset clock) |
| 45 | Hit B2BWave API → cancel order on website | Auto cancellation |

#### Clock Rules
- **Clock basis:** Last email to/from customer regarding that specific order
- **Reminder emails do NOT reset the clock** — only customer-initiated communication counts
- **Customer response adds +7 days** — if customer sends ANY email about the order, add 7 days to all action timers
- **"Cancel" keyword detection:** If customer email contains the word "cancel" (fuzzy match), immediately:
  1. Ping B2BWave API to mark order canceled
  2. Send customer confirmation email that order has been canceled
  3. Move order to Canceled status

#### Email Templates Needed
1. **Day 6 reminder:** "Your order #{order_id} hasn't been paid yet. [Payment link]"
2. **Day 29 reminder:** "Your order #{order_id} has been marked inactive due to no activity."
3. **Day 44 reminder:** "Your order #{order_id} will be canceled tomorrow if no action is taken."
4. **Cancel confirmation:** "Your order #{order_id} has been canceled per your request."

#### Backend Implementation Notes
- New fields on orders table: `last_customer_email_at`, `lifecycle_status` (active/inactive/archived/canceled), `lifecycle_deadline_at`
- New cron job: `POST /lifecycle/check-all` — runs daily, checks all orders against timeline
- B2BWave cancel endpoint: needs API integration for order cancellation
- Email parser enhancement: detect "cancel" keyword in customer emails
- Gmail sync enhancement: track last customer email timestamp per order

#### Frontend Implementation Notes
- Tab bar: All | Inactive | Archived (with counts)
- Inactive tab shows: days since last activity, next action date, "Reactivate" button
- Archived tab shows: read-only order view, "Reactivate" button, days until deletion
- Visual indicators: countdown badges, color coding by urgency

---

## UI REDESIGN — HTML MOCKUP REQUESTED

William requested an HTML mockup of the proposed UI changes including:
- Dashboard with metric cards
- All / Inactive / Archived tabs
- Order lifecycle indicators
- Modernized layout based on competitive analysis

**Status:** Building in this session

---

## BLOCKER STATUS (updated Mar 2)

| # | Blocker | Status |
|---|---------|--------|
| 1 | rl-quote-sandbox private | ✅ RESOLVED — MCP bridge v2.6 has `rl-quote` alias |
| 2 | Render services dead | ✅ RESOLVED — paid tier, no sleep |
| 3 | PostgreSQL expired | ✅ RESOLVED |
| 4 | Hardcoded API key | ✅ RESOLVED |
| 5 | Frontend junk in repo | OPEN — needs William local git rm commands |
| 6 | Warehouse data wrong | OPEN — fix models.py in rl-quote repo (6 warehouses + correct LI zip) |
| 7 | Duplicate endpoint | OPEN — POST /rl/pickup/pro/{pro_number} defined twice |
| 8 | Freight class bug | OPEN — hardcoded "70" in multiple places, should be "85" |
| 9 | No authentication | OPEN — CORS wide open, no API keys, no JWT |

## BATTLE PLAN STATUS

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Cleanup & Hygiene | ✅ DONE (local git cleanup remaining) |
| 2 | RL-Quote Integration | ✅ DONE — deployed and tested |
| 3 | AlertsEngine + Order Lifecycle | NOT STARTED (expanded scope with lifecycle rules) |
| 4 | Customer Communications | NOT STARTED (expanded with lifecycle emails) |
| 5 | Backend Hardening | NOT STARTED |
| 6 | Frontend Redesign | NOT STARTED (expanded with dashboard + tabs + mockup) |
| 7 | Production Promotion | NOT STARTED |

## REMAINING CLEANUP (Phase 1 leftover)

Frontend repo still has committed junk (node_modules, dist, cfc-frontend.zip):
```
cd C:\dev\CFCOrdersFrontend_Sandbox
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

## NEXT SESSION SHOULD

1. **Fix warehouse data** — edit `backend/models.py` in rl-quote repo to add all 6 warehouses with correct zips
2. **Fix critical bugs** — duplicate endpoint, freight class 70→85, requirements.txt
3. **Start Phase 3** — AlertsEngine + Order Lifecycle system (the two are tightly coupled)
4. **Read rules:** brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md for ORD-A1 spec
5. **Read battle plan:** cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md (Phase 3 section)
6. **Reference audit:** The full audit doc covers all 47 issues with severity and fix recommendations

## KEY REFERENCE FILES

- **Audit report**: CFC_Orders_Full_Stack_Audit.docx (delivered to William Mar 2)
- **Battle plan**: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md
- **Original upgrade plan**: cfc-orders:handoffs/CFC_ORDERS_PLAN.md
- **Rules**: brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- **Master status**: brain:MASTER_STATUS.md (Workstream 6)

## REPOS

- Sandbox backend: github.com/4wprince/CFCOrderBackend_Sandbox (v6.0.0)
- Sandbox frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox (v5.10.1)
- RL quote sandbox: github.com/4wprince/rl-quote-sandbox (MCP alias: `rl-quote`)
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
