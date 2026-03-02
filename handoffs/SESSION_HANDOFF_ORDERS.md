# SESSION HANDOFF — CFC Orders (General)

**Last Updated:** 2026-03-02 (Session 8 — Critical Bug Fixes)
**Last Session:** Mar 2, 2026 — Created startup_wiring.py, verified freight class status, audit cleanup
**Session Before That:** Mar 2, 2026 — AI Config Panel (Connie's NLP UI customizer) + sandbox link

---

## WHAT HAPPENED THIS SESSION (Mar 2 — Session 8)

### Context
Picked up from Session 7's 47-issue audit. Chat crashed once (context overflow on 119KB main.py). Sandbox/filesystem was down, so could only use MCP tools — no bash, no file creation. This limited our ability to modify main.py directly.

### Findings & Actions

| Task | Status | Notes |
|------|--------|-------|
| Wire AlertsEngine into main.py | ✅ ALREADY DONE | Session 7 handled this |
| Wire lifecycle_routes into main.py | 🔧 startup_wiring.py CREATED | Needs 2-line import in main.py |
| Fix duplicate POST endpoint | ✅ NOT A BUG | POST/GET/DELETE are different HTTP methods on same path — valid |
| Fix freight_class 70→85 | 🔧 PARTIALLY DONE | checkout.py + rl_carriers.py already "85". Only main.py has "70" (3 places) |
| Fix requirements.txt | ✅ ALREADY DONE | Session 7 updated to 9 packages |
| Created startup_wiring.py | ✅ COMMITTED | Wires lifecycle + email + AI configure in one call |

### Files Committed This Session
| File | Action | Purpose |
|------|--------|---------|
| `startup_wiring.py` | NEW | One-call wiring for lifecycle, email, AI configure routers |
| `handoffs/SESSION_HANDOFF_ORDERS.md` | UPDATED | This file |

### Key Discovery: Freight Class Status
| File | freight_class | Status |
|------|---------------|--------|
| rl_carriers.py | "85" | ✅ Already correct |
| rl_quote_proxy.py | "85" | ✅ Already correct |
| checkout.py | "85" | ✅ Already correct |
| main.py line 598 | **"70"** | ❌ /rl/quote endpoint default |
| main.py line 675 | **"70"** | ❌ RLBolRequest model default |
| main.py line 1079 | **"70"** | ❌ rl_create_order_bol hardcoded |

**Impact**: Every R+L quote requested through the /rl/quote endpoint or BOL creation through main.py sends class 70 instead of 85. Class 85 is correct for RTA cabinets. The rl-quote microservice (rl_quote_proxy.py) already uses "85" so quotes going through /proxy/* are correct. Only the legacy direct endpoints in main.py are wrong.

---

## WIRING INSTRUCTIONS FOR WILLIAM

### Step 1: Wire ALL routers via startup_wiring.py (2 lines in main.py)

Find this block in main.py (around line 175):
```python
# Phase 3A: AlertsEngine endpoints (/alerts/*)
if ALERTS_ENGINE_LOADED:
    app.include_router(alerts_router)
```

Add AFTER it:
```python
# Phase 3B+: Wire lifecycle, email, AI configure
from startup_wiring import wire_all
WIRING_STATUS = wire_all(app)
```

This replaces the previous 3 separate wiring instructions (lifecycle, email, AI configure).

### Step 2: Fix freight class bug (3 locations in main.py)

Find-and-replace in main.py:

**Location 1** — `/rl/quote` endpoint (~line 598):
```
freight_class: str = "70"
```
→ Change to:
```
freight_class: str = "85"
```

**Location 2** — `RLBolRequest` model (~line 675):
```
freight_class: str = "70"
```
→ Change to:
```
freight_class: str = "85"
```

**Location 3** — `rl_create_order_bol` function (~line 1079):
```
freight_class="70",
```
→ Change to:
```
freight_class="85",
```

### Step 3: Update root endpoint (main.py root() function)

After the `"alerts_engine"` dict in the root() return, add:
```python
        "lifecycle_engine": {
            "enabled": WIRING_STATUS.get("lifecycle", False) if 'WIRING_STATUS' in dir() else False
        },
        "email_routes": {
            "enabled": WIRING_STATUS.get("email", False) if 'WIRING_STATUS' in dir() else False
        },
        "ai_configure": {
            "enabled": WIRING_STATUS.get("ai_configure", False) if 'WIRING_STATUS' in dir() else False
        }
```

### Step 4: Git push sandbox backend

```
cd C:\dev\CFCOrderBackend_Sandbox
git pull
git add -A
git commit -m "Session 8: Wire startup_wiring + fix freight class 70->85"
git push
```

### Step 5: Run DB migration (after deploy)

```
POST https://cfcorderbackend-sandbox.onrender.com/add-lifecycle-fields
POST https://cfcorderbackend-sandbox.onrender.com/backfill-lifecycle
```

### Step 6: Add sandbox link to PRODUCTION frontend (from Session 7)

See Session 7 handoff for the production frontend sandbox link instructions.

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
| 7 | Duplicate endpoint | ✅ NOT A BUG — POST/GET/DELETE are different HTTP methods |
| 8 | Freight class bug | **3 LINES IN main.py** — Steps 2 above |
| 9 | No authentication | OPEN — Phase 5 |
| 10 | Lifecycle not wired | **2 LINES IN main.py** — Step 1 above (startup_wiring.py ready) |
| 11 | AI config not wired | **2 LINES IN main.py** — Step 1 above (startup_wiring.py ready) |

## BATTLE PLAN STATUS

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Cleanup & Hygiene | ✅ DONE |
| 2 | RL-Quote Integration | ✅ DONE |
| Audit | Full-stack audit + UI mockup | ✅ DONE |
| 3A | AlertsEngine | ✅ DONE — wired in main.py |
| 3B | Order Lifecycle | ✅ CODE COMPLETE — startup_wiring.py ready, needs 2 lines in main.py |
| — | AI Config Panel | ✅ CODE COMPLETE — startup_wiring.py ready, needs 2 lines in main.py |
| 4 | Customer Communications | **NEXT** |
| 5 | Backend Hardening | NOT STARTED |
| 6 | Frontend Redesign | NOT STARTED |
| 7 | Production Promotion | NOT STARTED |

## NEXT SESSION SHOULD

1. **Wire everything locally** — Run Steps 1-5 above (William local, ~5 min, just find/replace)
2. **Build lifecycle_engine.py** — Phase 3B: the actual engine that processes lifecycle rules
   - 7d→Inactive, 30d→Archived, 45d→Auto-cancel
   - "cancel" keyword = immediate cancel
   - Reminders at days 6/29/44
   - Reply adds 7d extension
3. **Test lifecycle endpoints** — POST /lifecycle/check-all, GET /lifecycle/summary
4. **Test AI Config Panel** — Open sandbox frontend, click 🤖 button
5. **Consider Phase 4** — Customer Communications (email templates, lifecycle emails)

## KEY REFERENCE FILES

- **startup_wiring.py**: One-call wiring for all pending routers
- **Battle plan**: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md
- **Rules**: brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- **AI configure**: cfc-orders:ai_configure.py + ai_configure_wiring.py
- **AI config panel**: cfc-orders-frontend:src/components/AiConfigPanel.jsx
- **Lifecycle engine**: cfc-orders:lifecycle_engine.py
- **Lifecycle routes**: cfc-orders:lifecycle_routes.py
- **Lifecycle wiring**: cfc-orders:lifecycle_wiring.py

## REPOS

- Sandbox backend: github.com/4wprince/CFCOrderBackend_Sandbox (v6.0.0)
- Sandbox frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox (v5.10.0)
- Prod frontend: github.com/4wprince/CFCOrdersFrontend (NOT in MCP)
- RL quote sandbox: github.com/4wprince/rl-quote-sandbox (MCP alias: `rl-quote`)

## DEPLOY URLS

- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- RL-quote sandbox: rl-quote-sandbox.onrender.com
- Sandbox frontend: cfcordersfrontend-sandbox.vercel.app
- Prod frontend: cfc-orders-frontend.vercel.app

## LOCAL REPOS

- `C:\dev\CFCOrderBackend_Sandbox` — backend
- `C:\dev\CFCOrdersFrontend_Sandbox` — sandbox frontend
- `C:\dev\CFCOrdersFrontend` — production frontend
