# SESSION HANDOFF — CFC Orders Phase 5: Backend Hardening

**Created:** 2026-03-03
**Status:** NOT STARTED — ready to begin
**Prerequisite:** Phases 3-4 fully deployed ✅ (Mar 3)
**Estimated Effort:** 1-2 full sessions

---

## GOAL

Decompose main.py (3,101 lines) into focused route modules, add JWT auth, lock down CORS, and clean up dead files. This is the last major backend task before production promotion (Phase 7).

---

## SCOPE

### 1. main.py Decomposition (3,101 → ~200 lines + route modules)

main.py currently contains ALL route definitions inline. Goal: extract into route group modules that main.py imports and mounts, similar to how `alerts_routes.py` and `startup_wiring.py` already work.

**Proposed route modules:**

| Module | Routes | Approx Lines |
|--------|--------|-------------|
| `routes_orders.py` | /orders/*, CRUD, checkpoints, set-status, events | ~400 |
| `routes_shipments.py` | /shipments/*, /orders/{id}/shipments | ~250 |
| `routes_b2bwave.py` | /b2bwave/*, /b2bwave/sync, /b2bwave/order/{id} | ~150 |
| `routes_email_parse.py` | /parse-email, /detect-*, /detect-rl-quote, /detect-pro-number | ~250 |
| `routes_rl_carriers.py` | /rl/*, /rl/bol/*, /rl/pickup/*, /rl/order/* | ~500 |
| `routes_checkout.py` | /checkout/*, /webhook/*, /checkout-ui/*, payment-complete | ~350 |
| `routes_shippo.py` | /shippo/* | ~60 |
| `routes_rta.py` | /rta/* | ~60 |
| `routes_debug.py` | /debug/*, /checkout-status | ~80 |
| `routes_misc.py` | /warehouse-mapping, /trusted-customers, /check-payment-alerts, /status/summary | ~150 |

**main.py after decomposition (~200 lines):**
- Imports + app creation
- Middleware (CORS)
- Router mounting (one `app.include_router()` per module)
- Startup event
- Root + health endpoints
- `if __name__` block

**Strategy:**
- Each route module uses `APIRouter(prefix=..., tags=[...])` 
- Shared dependencies (get_db, models, config) imported directly
- Do NOT change any endpoint paths or behavior — pure structural refactor
- Test after each module extraction: hit affected endpoints to verify

### 2. JWT Auth

Add JWT authentication to all endpoints except:
- `GET /` (health/status)
- `GET /health`
- `POST /webhook/b2bwave-order` (external webhook)
- `GET /checkout-ui/{order_id}` (customer-facing)
- `GET /checkout/{order_id}` (uses its own token)

**Implementation:**
- New file: `auth.py`
- Use `python-jose` or `PyJWT` for token validation
- Environment vars: `JWT_SECRET`, `JWT_ALGORITHM` (default HS256)
- FastAPI `Depends()` on a `get_current_user` function
- Apply via router-level dependency on protected routers
- Frontend gets token via login endpoint or env-injected

### 3. CORS Whitelist

Replace `allow_origins=["*"]` with specific domains:
```python
allow_origins=[
    "https://cfcordersfrontend-sandbox.vercel.app",
    "https://brain-ui-iota.vercel.app",
    "http://localhost:3000",
    "http://localhost:5173",
]
```

### 4. Dead File Cleanup

Files to audit and potentially remove:
- `main.py.bak` — old backup, remove
- `main_OLD.py` — old backup, remove
- `patch_main.py` — one-time patch script, remove
- Any other `.bak` or `_OLD` files

### 5. Error Handling Standardization

Ensure all endpoints return consistent error format:
```json
{"status": "error", "message": "...", "detail": "..."}
```

Currently mixed between `HTTPException`, `{"status": "error", ...}`, and bare exception strings.

---

## EXECUTION ORDER

1. **Dead file cleanup** — quick win, remove .bak/.OLD files
2. **Extract route modules** — one at a time, test after each:
   - Start with smallest (routes_shippo, routes_rta) to establish pattern
   - Then medium (routes_debug, routes_misc, routes_b2bwave)
   - Then large (routes_orders, routes_shipments, routes_rl_carriers)
   - Last: routes_checkout, routes_email_parse
3. **CORS whitelist** — one-line change after decomposition
4. **JWT auth** — add after routes are modular (easier to apply per-router)
5. **Error handling** — sweep through each route module

---

## CONTEXT WINDOW WARNING

main.py is 3,101 lines / ~120KB. DO NOT read entire file into conversation.
Use `repo_search_content` to find route boundaries.
Extract one module at a time using targeted reads.

---

## KEY FILES

| File | Purpose |
|------|---------|
| `main.py` (3,101 lines) | Monolith to decompose |
| `startup_wiring.py` | Reference pattern for router mounting |
| `alerts_routes.py` | Reference pattern for APIRouter module |
| `config.py` | Shared env vars — all route modules import from here |
| `db_helpers.py` | Shared DB access — all route modules import from here |

---

## DEPLOY STRATEGY

- Push each route module + updated main.py together
- Render auto-redeploys on push
- Verify GET / still returns all engines true after each push
- Run POST /alerts/check-all after major changes to confirm nothing broke
