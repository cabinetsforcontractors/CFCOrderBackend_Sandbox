# WS6 — CFC Orders Session Handoff
**Date:** 2026-03-04
**Task:** Phase 5B Backend Hardening — main.py full decomposition + auth wiring

## ✅ What Was Done This Session

### Phase 5B COMPLETE — main.py fully decomposed

| File | Lines | What It Contains |
|------|-------|-----------------|
| `main.py` v6.1.0 | ~175 | App init, CORS, router mounts, startup, health |
| `migration_routes.py` | ~130 | All 9 DB migration endpoints + `/debug/orders-columns` + `/init-db` — all admin-gated |
| `sync_routes.py` | ~140 | B2BWave test/sync/order + Gmail sync + Square sync/status — all admin-gated |
| `detection_routes.py` | ~220 | `/parse-email`, `/detect-payment-link`, `/detect-payment-received`, `/detect-rl-quote`, `/detect-pro-number`, `/check-payment-alerts` — all admin-gated |
| `checkout_routes.py` | ~290 | Full checkout flow + debug endpoints (admin-gated) + public webhook/checkout UI |

**main.py shrank from 1,233 → ~175 lines.**

### Auth Wiring Applied
`require_admin` (from `auth.py`) is now wired into:
- ALL migration endpoints (destructive DB ops)
- ALL B2BWave/Gmail/Square sync endpoints
- ALL email parsing + payment detection endpoints
- ALL checkout debug endpoints (`/debug/*`, `/checkout-status`)

Public endpoints (no auth required):
- `GET /` and `GET /health`
- `GET /square/status`
- `POST /webhook/b2bwave-order`
- `GET/POST /checkout/*` (token-gated instead)
- `GET /checkout-ui/*` (token-gated instead)

## 🏗️ Architecture Summary (Phase 5 Complete)

```
main.py (175 lines — app entry only)
├── rl_quote_proxy.py     — /proxy/*
├── alerts_routes.py      — /alerts/*
├── startup_wiring.py     — lifecycle + email + ai_configure
├── orders_routes.py      — /orders /shipments /warehouse-mapping /trusted-customers
├── shipping_routes.py    — /rl /shippo /rta
├── detection_routes.py   — /parse-email /detect-* /check-payment-alerts  [NEW Phase 5B]
├── sync_routes.py        — /b2bwave/* /gmail/* /square/*                  [NEW Phase 5B]
├── migration_routes.py   — /init-db /add-* /fix-* /debug/*               [NEW Phase 5B]
└── checkout_routes.py    — /checkout* /checkout-ui/* /webhook/*           [NEW Phase 5B]
```

## What's Next

### Phase 5C — ✅ DONE (2026-03-04) — api.js centralized all 29 fetch() calls (sha 0c498013)
Phase 5 remaining: sandbox verify (PATCH /orders/{id} + Run Check + Reactivate Order
→ 200 not 401) → rate limiting 5B (slowapi) → JWT rotation (Option C).

### Phase 7 — Production Promotion (Sandbox → Prod)
After Phase 5C, sandbox is clean and ready. Promote to prod.
- Set `ADMIN_API_KEY` env var on Render (change from default CFC2025)
- Optionally set `ADMIN_JWT_SECRET` for rotating tokens
- Update frontend `VITE_API_URL` to prod backend URL
- Set `CORS_ORIGINS` on prod if different domain

### R+L Test Harness (Priority #4 globally)
- `cfc-orders:tests/rl_test_harness.py` (sha 3fd9f79, 521 lines)
- POC: 5 orders ±5% variance → scale to 100
- William has real orders CSV ready

## Key Files (Phase 5 Complete)
- `cfc-orders:main.py` (v6.1.0, ~175 lines)
- `cfc-orders:auth.py` (built, HS256 JWT + API key fallback)
- `cfc-orders:migration_routes.py` (sha 0edbfef)
- `cfc-orders:sync_routes.py` (sha e7abb56)
- `cfc-orders:detection_routes.py` (sha cb17813)
- `cfc-orders:checkout_routes.py` (sha c9edfeb)
- `cfc-orders-frontend:src/App.jsx` (v7.2.2 — no changes this session)
- `cfc-orders-frontend:src/api.js` | 0c498013 | Phase 5C — apiFetch() wrapper, injects X-Admin-Token: CFC2025 on every request. Token rotation = one-line change here.
