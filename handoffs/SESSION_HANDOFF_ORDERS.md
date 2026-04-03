# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-03
**Repo:** CFCOrderBackend_Sandbox (https://cfcorderbackend-sandbox.onrender.com)

⛔ THIS IS THE SANDBOX REPO — NOT PRODUCTION
- Production backend (leave alone): https://cfc-backend-b83s.onrender.com
- Production frontend (leave alone): https://cfc-orders-frontend.vercel.app
- Sandbox frontend: https://cfcordersfrontend-sandbox.vercel.app

Production promotion is DEFERRED. All active work is sandbox-only.

---

## ⚠️ READ BEFORE ANY TASK

**Read the frontend README first:**
`cfc-orders-frontend:README.md` (sha 0573008a) — current component state and known issues.

**Current state corrections (2026-04-03):**
- App.jsx in repo is v5.10.0 — local is v5.10.5, NOT YET PUSHED.
- /health returns v6.2.0.
- ADMIN_API_KEY: NOT hardcoded — `os.environ.get("ADMIN_API_KEY", "CFC2025")`. If env var is absent, falls back to CFC2025. Frontend sends CFC2026 → mismatch → 401 on all writes. Fix: add ADMIN_API_KEY=CFC2026 to sandbox Render service env vars.
- SQUARE_ENVIRONMENT: deferred — do NOT set.
- square_sync.py hardcodes production Square URL — deferred code fix.
- App.jsx sha e020e868 / v7.2.2 in Key Files is STALE — actual sha is 345b244572 / v5.10.0.

---

## ⚡ Active Sandbox Work

**Lane A (P1) — Shippo end-to-end test**
Test full checkout flow for a <70 lb order through Shippo end-to-end in sandbox.
- Under 70 lbs → Shippo (UPS/USPS); 70 lbs+ → R+L LTL
- `shippo_rates.py` is live; flow has NOT been tested end-to-end

**Lane B (P1) — Payment automation trigger verification**
Verify each trigger fires in sandbox with a test order:
- Webhook → auto-send checkout URL to customer email (not confirmed live)
- Payment received → auto-create BOL (logic exists, trigger not confirmed)
- Square webhook → auto-update order status (sync exists, event-driven trigger not verified)
- Payment confirmation email → auto-send on payment (templates built, trigger not wired)

---

## What Was Done

### Phase 5 Hardening — COMPLETE
- Phase 5B: slowapi rate limiting wired (rate_limit.py sha 10e3aa8f, main.py sha e4cd70c1, routes/audit.py sha a6a70380)
- Phase 5C: PATCH /orders/{id}, POST /orders/{id}/run-check, POST /orders/{id}/reactivate all return 200
- Sandbox smoke test: all green (rate_limiting.enabled:true in GET /)

### Phase 7 Step 1 — DONE
- api.js updated: CFC2025 → CFC2026
- SHA: 68019f6e (cfc-orders-frontend repo, branch main)

### Endpoint Fixes (2026-03-19)
| Fix | Repo | SHA |
|-----|------|-----|
| Added `/api/capture-lead` to v5 backend | v5 | `aa9c5909` |
| Added `cfcordersfrontend-sandbox.vercel.app` to CORS whitelist | cfc-orders | `e4cd70c1` |

---

## Deferred — Production Promotion (Phase 7 Steps 2–8)

Not executing until sandbox lanes A and B are complete.

Steps when ready:
1. Render sandbox: add ADMIN_API_KEY=CFC2026 (⛔ do NOT set SQUARE_ENVIRONMENT)
2. Push local App.jsx v5.10.5 to cfc-orders-frontend main
3. Repoint Vercel frontend to CFCOrdersFrontend_Sandbox repo
4. Smoke test
5. DB migrations (idempotent): /add-rl-fields, /add-weight-column, /backfill-lifecycle
6. Full checklist
7. R+L end-to-end

---

## Key Files

cfc-orders-frontend:README.md — sha 0573008a — ⚠️ READ FIRST
cfc-orders-frontend:src/api.js — sha 68019f6e — X-Admin-Token: CFC2026 ✅
cfc-orders-frontend:src/App.jsx — sha 345b244572 — v5.10.0 in repo / v5.10.5 local (NOT PUSHED)
cfc-orders-frontend:src/config.js — sha d3590688 — sandbox URL hardcoded (correct for sandbox)
cfc-orders:main.py — sha e4cd70c1 — v6.2.0
cfc-orders:rate_limit.py — sha 10e3aa8f — shared slowapi Limiter
cfc-orders:routes/audit.py — sha a6a70380 — rate-limited audit log endpoints
cfc-orders:orders_routes.py — sha 0ac6a8e3 — run-check + reactivate added
cfc-orders:rl_carriers.py — sha b92c627a — 719 lines R+L API
cfc-orders:auth.py — sha 795a0a76 — NOT hardcoded — falls back to CFC2025 if env var missing
cfc-orders:checkout.py — sha 4e2bfaab — Square checkout + Shippo + R+L routing + weight logic
cfc-orders:square_sync.py — sha 10eeee1b — ⚠️ hardcodes production Square URL — deferred fix
cfc-orders:handoffs/SANDBOX_VS_PRODUCTION_AUDIT.md — sha a139452f — Sandbox vs prod gap analysis
brain:handoffs/AUDIT_REPORT_C_CFC_ORDERS.md — sha ebcf3134 — Full audit findings (2026-04-03)
brain:workstreams/WS6_CFC_ORDERS.md — sha 6ef6d4c6 — Full workstream file

---

## Critical Reminders
- SANDBOX only — do not touch production during sandbox work.
- Shared PostgreSQL DB — migrations affect production data.
- ADMIN_API_KEY not hardcoded — env var stripped from sandbox Render → fallback CFC2025 → 401. Add CFC2026 to sandbox env before testing writes.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- api.js has CFC2026 (sha 68019f6e) — do NOT revert.
- PS5 curl: NEVER inline quote escapes. Always Set-Content body.json then -d "@body.json".
- Audit log is in-memory only — resets on Render restart.
- Rate limiter keyed by IP — admin token does not bypass limits.
- NEVER suggest cold start or wake-up — Render is PAID, servers never sleep.
- Lane C (RTA Weight) blocked on WS5 — do not touch.
- Blind R+L shipping = $106/shipment — rejected, do not revisit.
