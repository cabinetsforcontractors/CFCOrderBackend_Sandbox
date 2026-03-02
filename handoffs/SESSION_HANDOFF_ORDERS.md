# SESSION HANDOFF — CFC Orders (General)

**Last Updated:** 2026-03-02 (Session 6)
**Last Session:** Mar 2, 2026 — Full-stack audit + order lifecycle rules + UI mockup + admin AI textbox concept
**Session Before That:** Mar 2, 2026 — RL-Quote API testing + MCP bridge rl-quote repo added

---

## WHAT HAPPENED THIS SESSION (Mar 2 — Session 6)

### Full-Stack System Audit — COMPLETE
Complete audit of cfc-orders backend, cfc-orders-frontend, and rl-quote repos.
- **Deliverable:** `CFC_Orders_Full_Stack_Audit.docx` (delivered to William)
- **47 issues** across 5 severity levels (6 CRITICAL, 8 HIGH, 15 MEDIUM, 12 LOW, 6 ENHANCEMENT)
- Competitive analysis: ShipStation, Ordoro, modern B2B UI patterns
- Full component architecture redesign proposed

**Critical bugs found:**
1. Duplicate endpoint: `POST /rl/pickup/pro/{pro_number}` (lines 821 + 917)
2. Freight class hardcoded "70" — should be "85" — every R+L quote is WRONG
3. ZERO authentication — CORS `allow_origins=["*"]`, no API keys
4. requirements.txt only 4 of 12+ deps
5. ~484KB dead files (main2-8.py)
6. AlertsEngine: designed but ZERO code

### Order Lifecycle System — RULES DEFINED
William defined automated inactivity/archiving/cancellation system:

| Day | Action |
|-----|--------|
| 6 | Auto email: "order hasn't been paid" (does NOT reset clock) |
| 7 | Move → Inactive tab |
| 29 | Auto email: "order marked inactive" (does NOT reset clock) |
| 30 | Move → Archived tab |
| 44 | Auto email: "order will be deleted tomorrow" (does NOT reset clock) |
| 45 | Hit B2BWave API → cancel order on website |

**Clock rules:**
- Based on last email to/from customer about that order
- System reminder emails do NOT reset clock
- Customer response adds +7 days to all timers
- "Cancel" keyword in customer email → immediate B2BWave cancel + confirmation email

### UI Mockup — DELIVERED
Interactive HTML mockup delivered: `CFC_Orders_UI_Mockup.html`
- Dark theme dashboard with metric cards, alerts banner
- All / Inactive / Archived tabs with lifecycle indicators
- Sortable orders table with bulk actions
- Slide-in detail panel (replaces modal)
- Lifecycle progress bars and countdown badges
- William approved general direction

### Admin AI Textbox — CONCEPT APPROVED
William wants a natural language admin panel for his wife Connie (the admin):
- Text input where Connie types commands like "make awaiting payment pink"
- Pings Anthropic API, Claude returns config changes (CSS vars, labels, display prefs)
- Changes apply live and persist via storage
- Scope: visual theming (colors, fonts, labels) + behavioral (filters, sort, show/hide columns)
- Needs guardrails so layout can't accidentally break
- Build as part of Phase 6 (Frontend Redesign)

---

## BLOCKER STATUS

| # | Blocker | Status |
|---|---------|--------|
| 1 | rl-quote-sandbox private | ✅ RESOLVED |
| 2 | Render services dead | ✅ RESOLVED |
| 3 | PostgreSQL expired | ✅ RESOLVED |
| 4 | Hardcoded API key | ✅ RESOLVED |
| 5 | Frontend junk in repo | OPEN — needs William local git rm |
| 6 | Warehouse data wrong | OPEN — fix models.py (6 warehouses) |
| 7 | Duplicate endpoint | OPEN — merge POST /rl/pickup/pro |
| 8 | Freight class bug | OPEN — 70→85 |
| 9 | No authentication | OPEN |

## BATTLE PLAN STATUS

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Cleanup & Hygiene | ✅ DONE (local git cleanup remaining) |
| 2 | RL-Quote Integration | ✅ DONE |
| Audit | Full-stack audit + UI mockup | ✅ DONE |
| 3 | AlertsEngine + Order Lifecycle | **NEXT** |
| 4 | Customer Communications (lifecycle emails) | NOT STARTED |
| 5 | Backend Hardening (main.py decomp, security) | NOT STARTED |
| 6 | Frontend Redesign (dashboard, tabs, admin AI textbox) | NOT STARTED |
| 7 | Production Promotion | NOT STARTED |

## NEXT SESSION SHOULD

1. **Fix critical bugs first** — duplicate endpoint, freight class 70→85, requirements.txt
2. **Fix warehouse data** — rl-quote repo models.py (6 warehouses + correct LI zip)
3. **Start Phase 3** — AlertsEngine + Order Lifecycle system:
   - New DB fields: `last_customer_email_at`, `lifecycle_status`, `lifecycle_deadline_at`
   - `lifecycle_engine.py` module with daily cron
   - `alerts_engine.py` module with 8 ORD-A1 rules
   - Gmail sync enhancement for tracking last customer email
   - "Cancel" keyword detection in email parser
4. Read: brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (ORD-A1 spec)
5. Read: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md (Phase 3 section)

## KEY REFERENCE FILES

- **Audit report**: CFC_Orders_Full_Stack_Audit.docx (delivered Mar 2)
- **UI Mockup**: CFC_Orders_UI_Mockup.html (delivered Mar 2)
- **Battle plan**: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md
- **Rules**: brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- **Master status**: brain:MASTER_STATUS.md (Workstream 6)

## REPOS

- Sandbox backend: github.com/4wprince/CFCOrderBackend_Sandbox (v6.0.0)
- Sandbox frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox (v5.10.1)
- RL quote sandbox: github.com/4wprince/rl-quote-sandbox (MCP alias: `rl-quote`)

## DEPLOY URLS

- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- RL-quote sandbox: rl-quote-sandbox.onrender.com
- Sandbox frontend: cfcordersfrontend-sandbox.vercel.app

## LOCAL REPOS

- `C:\dev\CFCOrderBackend_Sandbox` — backend
- `C:\dev\CFCOrdersFrontend_Sandbox` — frontend
