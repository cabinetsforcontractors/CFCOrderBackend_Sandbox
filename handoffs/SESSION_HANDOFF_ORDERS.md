# SESSION HANDOFF — CFC Orders (General)

**Last Updated:** 2026-03-02 (Session 7 — Phase 3B Lifecycle Engine Wiring)
**Last Session:** Mar 2, 2026 — Phase 3B verification + wiring helper + bug documentation
**Session Before That:** Mar 2, 2026 — Full-stack audit + order lifecycle rules + UI mockup + AlertsEngine Phase 3A

---

## WHAT HAPPENED THIS SESSION (Mar 2 — Session 7)

### Phase 3B Status Assessment — ALL CODE ALREADY EXISTS
Discovered that Phase 3B lifecycle engine code was already built in a prior session (Session 6 continuation). 
All 4 modules committed:

| File | Lines | Status |
|------|-------|--------|
| `lifecycle_engine.py` | 536 | ✅ COMMITTED — full engine with 7/30/45 day rules |
| `lifecycle_routes.py` | 189 | ✅ COMMITTED — 6 FastAPI endpoints |
| `gmail_sync.py` | 611 | ✅ COMMITTED — enhanced with lifecycle tracking + cancel detection |
| `db_migrations.py` | 374 | ✅ COMMITTED — add_lifecycle_fields() + backfill_lifecycle_from_emails() |
| `lifecycle_wiring.py` | 55 | ✅ COMMITTED (Session 7) — mounts router + migration endpoints |

### lifecycle_engine.py — What's Built
- `process_order_lifecycle(order_id)` — evaluate single order against 7/30/45 day rules
- `check_all_orders_lifecycle()` — daily cron: checks all non-complete orders
- `extend_deadline(order_id, days=7)` — resets clock when customer responds
- `cancel_order(order_id, reason)` — marks canceled, logs event (B2BWave API cancel deferred to Phase 4)
- `detect_cancel_keyword(text)` — fuzzy regex match for "cancel" variants
- `calculate_lifecycle_status(last_email_at, status, now)` — pure function, returns (status, days, deadline)
- `get_pending_reminders(last_email_at, sent_dict, now)` — determines which day 6/29/44 reminders to queue
- `get_lifecycle_summary()` — dashboard counts by status

### lifecycle_routes.py — Endpoints
- `POST /lifecycle/check-all` — daily cron trigger
- `POST /lifecycle/check/{order_id}` — single order check
- `POST /lifecycle/extend/{order_id}` — manual extend deadline
- `POST /lifecycle/cancel/{order_id}` — manual cancel
- `GET /lifecycle/summary` — dashboard counts (active/inactive/archived/canceled)
- `GET /lifecycle/orders?status=inactive` — list orders by lifecycle status

### gmail_sync.py — Phase 3B Enhancements
- `is_system_generated_email(subject)` — detects system reminder subjects, prevents clock reset
- `is_customer_email(from, to)` — classifies direction: from_customer / to_customer / internal
- `update_last_customer_email(conn, order_id, date)` — tracks lifecycle clock basis
- `check_cancel_keyword(conn, order_id, body, subject)` — imports from lifecycle_engine, triggers cancel
- Pass #5 in `run_gmail_sync()` — scans all customer emails for lifecycle tracking

### db_migrations.py — Lifecycle Migration
- `add_lifecycle_fields()` — adds 4 columns + 2 indexes:
  - `last_customer_email_at` TIMESTAMP WITH TIME ZONE
  - `lifecycle_status` VARCHAR(20) DEFAULT 'active'
  - `lifecycle_deadline_at` TIMESTAMP WITH TIME ZONE
  - `lifecycle_reminders_sent` JSONB DEFAULT '{}'
  - Indexes on lifecycle_status and last_customer_email_at
- `backfill_lifecycle_from_emails()` — populates from order_email_snippets table

### lifecycle_wiring.py — NEW (Session 7)
Clean wiring helper to avoid modifying the 3,088-line main.py heavily:
```python
from lifecycle_wiring import wire_lifecycle
wire_lifecycle(app)
```
Mounts lifecycle_router + adds /add-lifecycle-fields and /backfill-lifecycle endpoints.

### Bugs Documented (Pending Local Fix)
| Bug | Location | Fix |
|-----|----------|-----|
| Freight class "70" | main.py lines 598, 675, 1079 | Change to "85" |
| No lifecycle wiring | main.py ~line 175 | Add 2 lines (see instructions below) |
| Root endpoint missing lifecycle | main.py root() function | Add lifecycle_engine to info dict |

---

## WIRING INSTRUCTIONS FOR WILLIAM

### Step 1: Wire lifecycle into main.py (2 lines)

Add these 2 lines AFTER the alerts router mount (around line 175):

```python
# Phase 3B: Lifecycle Engine wiring
from lifecycle_wiring import wire_lifecycle
wire_lifecycle(app)
```

### Step 2: Fix freight class bug (3 locations in main.py)

Line 598: Change `freight_class: str = "70"` → `freight_class: str = "85"`
Line 675: Change `freight_class: str = "70"` → `freight_class: str = "85"` 
Line 1079: Change `freight_class="70",` → `freight_class="85",`

### Step 3: Add lifecycle to root endpoint (main.py root() function)

After the `"alerts_engine"` dict, add:
```python
        "lifecycle_engine": {
            "enabled": True
        }
```

### Step 4: Run DB migration (after deploy)

```
POST https://cfcorderbackend-sandbox.onrender.com/add-lifecycle-fields
POST https://cfcorderbackend-sandbox.onrender.com/backfill-lifecycle
```

### Step 5: Git push
```
cd C:\dev\CFCOrderBackend_Sandbox
git add -A
git commit -m "Phase 3B: Wire lifecycle engine + fix freight class"
git push
```

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
| 8 | Freight class bug | **DOCUMENTED** — 3 lines in main.py, fix instructions above |
| 9 | No authentication | OPEN — Phase 5 |
| 10 | Lifecycle not wired | **DOCUMENTED** — 2 lines in main.py, wire instructions above |

## BATTLE PLAN STATUS

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Cleanup & Hygiene | ✅ DONE |
| 2 | RL-Quote Integration | ✅ DONE |
| Audit | Full-stack audit + UI mockup | ✅ DONE |
| 3A | AlertsEngine | ✅ DONE — wired in main.py |
| 3B | Order Lifecycle | ✅ CODE COMPLETE — needs main.py wiring (2 lines) |
| 4 | Customer Communications | **NEXT** — lifecycle emails + B2BWave cancel API |
| 5 | Backend Hardening | NOT STARTED |
| 6 | Frontend Redesign | NOT STARTED |
| 7 | Production Promotion | NOT STARTED |

## NEXT SESSION SHOULD

1. **Wire lifecycle** — Run the 5 steps above (William local, ~5 min)
2. **Run DB migration** — POST /add-lifecycle-fields + POST /backfill-lifecycle
3. **Test lifecycle endpoints** — POST /lifecycle/check-all, GET /lifecycle/summary
4. **Start Phase 4** — Customer Communications:
   - Email template engine (HTML templates with order data injection)
   - B2BWave cancel API integration (day 45 auto-cancel)
   - Enable GMAIL_SEND_ENABLED=true on Render
   - Wire lifecycle reminder sending (day 6/29/44 emails)
   - Tag outgoing system emails so they don't reset clock
5. Read: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md (Phase 4 section)

## KEY REFERENCE FILES

- **Battle plan**: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md
- **Rules**: brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- **Master status**: brain:MASTER_STATUS.md (Workstream 6)
- **Lifecycle engine**: cfc-orders:lifecycle_engine.py
- **Lifecycle routes**: cfc-orders:lifecycle_routes.py
- **Lifecycle wiring**: cfc-orders:lifecycle_wiring.py
- **Gmail sync (enhanced)**: cfc-orders:gmail_sync.py
- **DB migrations**: cfc-orders:db_migrations.py

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
