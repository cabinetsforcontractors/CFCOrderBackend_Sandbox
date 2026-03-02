# SESSION HANDOFF - CFC Orders (General)

**Last Updated:** 2026-03-02 (Session 15 - Pre-wiring audit, exact edit locations mapped)
**Latest Session:** Session 15 - Read main.py (3,088 lines), mapped all 3 edit locations precisely
**Session Before:** Session 14 - Phase 3C Frontend alerts bell badge, dropdown, per-order alerts
**Session Before:** Session 13 - Fixed rl_quote_proxy.py: POST not GET (405 resolved)

---

## CURRENT STATE SUMMARY

### What's DONE (all code committed, in repo)
| Component | File(s) | Status |
|-----------|---------|--------|
| AlertsEngine (Phase 3A) | alerts_engine.py, alerts_routes.py | WIRED in main.py |
| Lifecycle Engine (Phase 3B) | lifecycle_engine.py, lifecycle_routes.py, lifecycle_wiring.py | CODE COMPLETE - needs startup_wiring import |
| Frontend Alerts (Phase 3C) | App.jsx v7.1, index.css v7.1 | COMMITTED |
| Email Templates (Phase 4) | email_templates.py (9 templates) | COMMITTED |
| Email Send (Phase 4) | email_sender.py, email_routes.py, email_wiring.py | COMMITTED - needs wiring |
| AI Config Panel | ai_configure.py, ai_configure_wiring.py | COMMITTED - needs startup_wiring |
| Startup Wiring | startup_wiring.py | COMMITTED - wires lifecycle + email + AI config |
| RL Quote Proxy | rl_quote_proxy.py (276 lines) | WORKING |
| Freight Class | checkout.py, rl_carriers.py, rl_quote_proxy.py | All 85 - main.py still has 3x 70 |
| Frontend v7.1 | App.jsx, index.css, BrainChat.jsx v2.0 | DARK THEME + ALERTS UI LIVE |

### What's STILL NEEDED
| # | Task | Effort | Details |
|---|------|--------|---------|
| 1 | main.py startup_wiring import | 3 lines | After alerts mount ~line 174 |
| 2 | main.py freight class 70 to 85 | 3 replacements | Lines ~598, ~675, ~1079 |
| 3 | main.py root() endpoint update | 9 lines | Add lifecycle/email/ai status |
| 4 | git push | 1 command | or MCP repo_write_file |
| 5 | DB migration | 2 POST calls | /add-lifecycle-fields + /backfill-lifecycle |
| 6 | GMAIL_SEND_ENABLED=true | Render env var | Flip when ready |
| 7 | Test alerts end-to-end | Manual | root then summary then check-all then bell badge |

---

## EXACT MAIN.PY EDITS (Session 15 verified all locations)

### EDIT 1: startup_wiring import (after line ~174)

Find this block:
```python
# Phase 3A: AlertsEngine endpoints (/alerts/*)
if ALERTS_ENGINE_LOADED:
    app.include_router(alerts_router)
```

Add immediately after:
```python
# Phase 3B+4: Lifecycle + Email + AI Config (one-call wiring)
from startup_wiring import wire_all
WIRING_STATUS = wire_all(app)
```

### EDIT 2: Freight class 70 to 85 (3 spots)

Spot 1 line ~598 (rl_quote endpoint default):
`freight_class: str = "70"` change to `freight_class: str = "85"`

Spot 2 line ~675 (RLBolRequest model default):
`freight_class: str = "70"` change to `freight_class: str = "85"`

Spot 3 line ~1079 (rl_create_order_bol hardcoded):
`freight_class="70",` change to `freight_class="85",`

Verify: After edits grep main.py for "70" and zero freight_class refs should remain.

### EDIT 3: root() endpoint (~line 260)

Find end of root return dict:
```python
        "alerts_engine": {
            "enabled": ALERTS_ENGINE_LOADED
        }
    }
```

Replace with:
```python
        "alerts_engine": {
            "enabled": ALERTS_ENGINE_LOADED
        },
        "lifecycle_engine": {
            "enabled": WIRING_STATUS.get("lifecycle", False)
        },
        "email_engine": {
            "enabled": WIRING_STATUS.get("email", False)
        },
        "ai_configure": {
            "enabled": WIRING_STATUS.get("ai_configure", False)
        }
    }
```

---

## CONTEXT WINDOW WARNING

main.py is 3,088 lines / ~120KB. DO NOT read entire file into conversation.
Use repo_search_content to find exact lines around each edit.
Build targeted replacements from EDIT instructions above.
Alternative: William makes edits locally (they are small) and just pushes.

---

## POST-PUSH STEPS

### DB Migration (after Render redeploys ~2-3 min)
POST https://cfcorderbackend-sandbox.onrender.com/add-lifecycle-fields
POST https://cfcorderbackend-sandbox.onrender.com/backfill-lifecycle

### Test Sequence
1. GET / verify lifecycle_engine email_engine ai_configure all true
2. GET /alerts/summary
3. POST /alerts/check-all
4. Frontend bell badge verify count shows
5. Click order then Details then verify alert section
6. Resolve alert then verify count decrements

---

## BATTLE PLAN STATUS

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Cleanup | DONE |
| 2 | RL-Quote Integration | DONE |
| 3A | AlertsEngine | WIRED |
| 3B | Order Lifecycle | CODE COMPLETE needs main.py wiring |
| 3C | Frontend Alerts | DONE |
| 4 | Email Comms | CODE COMPLETE needs main.py wiring + GMAIL flip |
| 5 | Backend Hardening | NOT STARTED |
| 6 | Frontend Redesign | DONE v7.1 |
| 7 | Production Promotion | NOT STARTED |

---

## PHASE 5 PREVIEW (after wiring is live)

Phase 5 Backend Hardening scope:
- main.py decomposition 3,088 lines into route group modules
- JWT auth on all endpoints except / and /health
- CORS whitelist replace allow_origins=* with specific domains
- Dead file cleanup remove old components no longer imported
- Error handling consistent format across all endpoints
- Recommend dedicating full session to Phase 5 planning before executing

---

## KEY FILES

| Repo | File | Purpose |
|------|------|---------|
| cfc-orders | main.py (3,088 lines) | App factory needs 3 edits |
| cfc-orders | startup_wiring.py | Wires lifecycle + email + AI config |
| cfc-orders | alerts_engine.py / alerts_routes.py | Phase 3A |
| cfc-orders | lifecycle_engine.py / lifecycle_routes.py / lifecycle_wiring.py | Phase 3B |
| cfc-orders | email_templates.py / email_sender.py / email_routes.py / email_wiring.py | Phase 4 |
| cfc-orders | rl_quote_proxy.py | R+L proxy |
| cfc-orders-frontend | src/App.jsx (v7.1) | Dark theme + alerts UI |
| cfc-orders-frontend | src/components/BrainChat.jsx (v2.0) | Brain chat |

## REPOS
- Backend: github.com/4wprince/CFCOrderBackend_Sandbox
- Frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox
- RL quote: github.com/4wprince/rl-quote-sandbox (MCP alias rl-quote)

## DEPLOY URLS
- Backend: cfcorderbackend-sandbox.onrender.com
- RL-quote: rl-quote-sandbox.onrender.com
- Frontend: cfcordersfrontend-sandbox.vercel.app

## LOCAL REPOS
- C:\dev\CFCOrderBackend_Sandbox
- C:\dev\CFCOrdersFrontend_Sandbox
