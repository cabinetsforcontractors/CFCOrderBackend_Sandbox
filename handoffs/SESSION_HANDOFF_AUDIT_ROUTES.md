# SESSION HANDOFF — WS6 Phase 5 Hardening: Rate Limiting + Phase 5C
**Date:** 2026-03-19
**Workstream:** WS6 — CFC Orders
**Session:** S7
**Handoff SHA:** see table below

---

## ✅ What Was Done This Session (S7)

### S7 Goals
1. Smoke test Phase 5B deploy ← run manually (see below)
2. Phase 5C: add missing run-check + reactivate routes ✅
3. Phase 5C: verify endpoints ← run manually (see below)

### Discovery: run-check and reactivate didn't exist yet
Both endpoints were absent from orders_routes.py. Added them in this session:
- `POST /orders/{id}/run-check` → wraps `lifecycle_engine.process_order_lifecycle()`
- `POST /orders/{id}/reactivate` → wraps `lifecycle_engine.extend_deadline()`
- Both require `X-Admin-Token: CFC2025` via `Depends(require_admin)`
- Inline imports inside route functions (no top-level lifecycle_engine import needed)

---

## Phase 5B Smoke Test — Run These Commands

Render auto-deploys from push. Wait for build complete, then:

**Step 1 — GET /health → 200, v6.2.0**
```
curl.exe https://cfcorderbackend-sandbox.onrender.com/health
```
Expected: `{"status":"ok","version":"6.2.0"}`

**Step 2 — GET / → rate_limiting.enabled: true**
```
curl.exe https://cfcorderbackend-sandbox.onrender.com/
```
Expected: `"rate_limiting":{"enabled":true,"default_limit":"200/minute"}`

**Step 3 — POST /audit/log**
```
curl.exe -X POST https://cfcorderbackend-sandbox.onrender.com/audit/log -H "Content-Type: application/json" -H "X-Admin-Token: CFC2025" -d "{\"entity_type\":\"order\",\"entity_id\":\"TEST-001\",\"action\":\"smoke_test\",\"actor\":\"william\"}"
```
Expected: `{"success":true,"id":1}`

**Step 4 — GET /audit/log**
```
curl.exe https://cfcorderbackend-sandbox.onrender.com/audit/log -H "X-Admin-Token: CFC2025"
```
Expected: count:1, entry returned

---

## Phase 5C Verify — Run These Commands

**Step 1 — Get a real order ID**
```
curl.exe "https://cfcorderbackend-sandbox.onrender.com/orders?limit=1" -H "X-Admin-Token: CFC2025"
```
Copy an `order_id` from the response. Use it in Steps 2–4.

**Step 2 — PATCH /orders/{id} → 200**
```
curl.exe -X PATCH "https://cfcorderbackend-sandbox.onrender.com/orders/ORDER_ID_HERE" -H "Content-Type: application/json" -H "X-Admin-Token: CFC2025" -d "{\"notes\":\"Phase 5C smoke test\"}"
```
Expected: `{"status":"ok","message":"Order updated"}`

**Step 3 — POST /orders/{id}/run-check → 200**
```
curl.exe -X POST "https://cfcorderbackend-sandbox.onrender.com/orders/ORDER_ID_HERE/run-check" -H "X-Admin-Token: CFC2025"
```
Expected: `{"status":"ok","order_id":"...","status_changed":false,...}`

**Step 4 — POST /orders/{id}/reactivate → 200**
```
curl.exe -X POST "https://cfcorderbackend-sandbox.onrender.com/orders/ORDER_ID_HERE/reactivate" -H "X-Admin-Token: CFC2025"
```
Expected: `{"status":"ok","success":true,"order_id":"...","new_status":"active",...}`

If any return 401 → check ADMIN_API_KEY env var on Render (should be unset or CFC2025).
If any return 404 → lifecycle_engine import failed; check Render build logs.

---

## Phase 5 Status

| Item | Status |
|------|--------|
| Audit routes smoke test | ✅ DONE (S5) |
| Phase 5B — rate limiting (slowapi) | ✅ DONE (S6) |
| Phase 5B smoke test | 🔲 Run manually |
| Phase 5C — run-check + reactivate added | ✅ DONE (S7) |
| Phase 5C — sandbox verify (3 endpoints → 200) | 🔲 Run manually |
| JWT rotation (Option C) | DEFERRED |

**Phase 5 = DONE once Phase 5B + 5C manual tests pass green.**

---

## Phase 7 — Production Promotion Checklist (Option A)

Option A = repoint prod Render service to sandbox repo/branch. Do NOT start until Phase 5C is green.

### Pre-flight
- [ ] Phase 5C: all 3 endpoints return 200 with CFC2025
- [ ] Phase 5B: `rate_limiting.enabled: true` in GET /
- [ ] Confirm prod Render env vars match sandbox (DATABASE_URL, B2BWAVE_*, GMAIL_*, SQUARE_*)
- [ ] Note any env vars that differ and must be preserved on prod

### Step 1 — Token flip (do this FIRST, before any code change)
- [ ] Render prod: add env var `ADMIN_API_KEY = CFC2026` (overrides default CFC2025)
- [ ] `cfc-orders-frontend:src/api.js`: change `X-Admin-Token: CFC2025` → `CFC2026`
- [ ] Push api.js change to prod frontend branch
- [ ] Verify prod frontend deploys on Vercel with new token

### Step 2 — Repoint prod backend (Option A)
- [ ] Render prod service → Settings → Branch: switch from prod branch to sandbox repo/branch
- [ ] Trigger manual deploy
- [ ] Watch build logs — no errors

### Step 3 — Smoke test prod
```
curl.exe https://cfcorderbackend.onrender.com/health
curl.exe https://cfcorderbackend.onrender.com/
```
- [ ] /health → 200, v6.2.0
- [ ] / → `rate_limiting.enabled: true`
- [ ] PATCH one real order with `X-Admin-Token: CFC2026` → 200

### Step 4 — Monitor
- [ ] Watch Render prod logs 5–10 min
- [ ] Check frontend: load orders list, verify no auth errors
- [ ] Phase 7 DONE → update COMPLETED_LOG.md

### After Phase 7
- R+L end-to-end: POST /rl/test → BOL → pickup schedule → tracking → notify → emails
- This is Phase 8 (Lane D continuation)

---

## Architecture Reference

```
main.py (~243 lines — app init only)
├── rl_quote_proxy.py     — /proxy/*
├── alerts_routes.py      — /alerts/*
├── startup_wiring.py     — lifecycle + email + ai_configure
├── orders_routes.py      — /orders /shipments /warehouse-mapping /trusted-customers
│                            includes /run-check + /reactivate  ← NEW S7
├── shipping_routes.py    — /rl /shippo /rta
├── detection_routes.py   — /parse-email /detect-* /check-payment-alerts
├── sync_routes.py        — /b2bwave/* /gmail/* /square/*
├── migration_routes.py   — /init-db /add-* /fix-* /debug/*
├── checkout_routes.py    — /checkout* /webhook/*
├── invoice_routes.py     — /invoice/scan /status /emails /flags
├── routes/audit.py       — /audit/log (rate limited)
└── rate_limit.py         — shared Limiter instance
```

---

## Key File SHAs

| File | SHA | Notes |
|------|-----|-------|
| `requirements.txt` | `0f27081e` | Added slowapi + limits |
| `rate_limit.py` | `10e3aa8f` | NEW — shared limiter |
| `main.py` | `46d7c63a` | Phase 5B rate limiting wired |
| `routes/audit.py` | `a6a70380` | Rate limited + Request param |
| `orders_routes.py` | `0ac6a8e3` | Phase 5C: run-check + reactivate added |
| `routes/__init__.py` | `b0e12a97` | Unchanged |
| `cfc-orders-frontend:src/api.js` | `0c498013` | X-Admin-Token: CFC2025 (→ CFC2026 at Phase 7 Step 1) |

---

## Critical Reminders
- `api.js` token = CFC2025 — flip to CFC2026 only at Phase 7 Step 1, not before
- Sandbox and production **share the same PostgreSQL DB** — migrations hit both
- Audit log is **in-memory only** — resets on Render restart
- Rate limiter keyed by IP — admin token does not bypass limits
- Phase 7 cannot start until Phase 5B + 5C manual tests pass
- POWERSHELL: one command per block, no &&, use curl.exe not curl for API calls
- NEVER suggest cold start / wake-up — Render is PAID, servers never sleep
