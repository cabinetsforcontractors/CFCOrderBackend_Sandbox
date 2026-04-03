# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-03 (updated — URLs clarified, SQUARE_ENVIRONMENT deferred)
**Task:** Phase 7 — Production Promotion (Option A execution)

---

## ⚠️ READ BEFORE ANY TASK

**Read the frontend README first:**
`cfc-orders-frontend:README.md` (sha 1247525f) — current component state, known issues, and required fixes before Phase 7 Step 3.

**URL MAP (authoritative):**
- `cfc-backend` → https://cfc-backend-b83s.onrender.com — PRODUCTION, live, working. ADMIN_API_KEY was stripped here — fix separately, not part of Phase 7 sandbox work.
- `CFCOrderBackend-Sandbox` → https://cfcorderbackend-sandbox.onrender.com — SANDBOX, active work target for Phase 7.
- `rl-quote-sandbox` → https://rl-quote-sandbox.onrender.com — R+L quote service.

**Audit corrections from 2026-04-03 (AUDIT_REPORT_C):**
- App.jsx in repo is v5.10.0 — local is v5.10.5, NOT YET PUSHED. Push before Step 3.
- config.js has hardcoded sandbox API_URL and IS_SANDBOX=true — must flip before Step 3.
- /health returns v6.2.0 — NOT v6.0.0 as written below. Smoke test checklists are wrong on this.
- SQUARE_ENVIRONMENT: NOT being set — Square promotion is deferred. square_sync.py hardcodes production Square URL regardless of env — needs a code fix before Square is considered functional.
- ADMIN_API_KEY: NOT hardcoded — os.environ.get("ADMIN_API_KEY", "CFC2025"). Env var was stripped from prod (cfc-backend-b83s) — falls back to CFC2025. Frontend sends CFC2026 → mismatch → 401 on all writes to prod. Sandbox backend (cfcorderbackend-sandbox) status TBD.
- App.jsx sha e020e868 / v7.2.2 referenced in Key Files below is STALE — actual sha is 345b244572 / v5.10.0.

---

## ⚡ START HERE — First Thing This Session

**Active work target: https://cfcorderbackend-sandbox.onrender.com**

**Phase 7 Step 2: Verify/set ADMIN_API_KEY=CFC2026 on the sandbox Render service.**

The frontend already sends `CFC2026` (api.js sha 68019f6e). auth.py falls back to `CFC2025` if env var is absent.

1. Render dashboard → `CFCOrderBackend-Sandbox` → Environment
2. Add/confirm `ADMIN_API_KEY=CFC2026`
3. Save → Manual deploy → watch logs
4. Then continue with Steps 3–6 below

⛔ Do NOT set SQUARE_ENVIRONMENT — deferred.
⛔ Do NOT touch cfc-backend (prod) during sandbox Phase 7 work.

**Do not start Step 3 (Vercel repoint) until Step 2 is confirmed live.**

**Also required before Step 3 (frontend code):**
- Push local App.jsx v5.10.5 to cfc-orders-frontend main
- Edit src/config.js: flip API_URL to https://cfcorderbackend-sandbox.onrender.com + IS_SANDBOX=false, push to main

---

## Endpoint Fixes Applied (2026-03-19)

Two bugs found and patched:

| Fix | Repo | SHA |
|-----|------|-----|
| Added `/api/capture-lead` to v5 backend (frontend called it, endpoint was missing — silent fail) | v5 | `aa9c5909` |
| Added `cfcordersfrontend-sandbox.vercel.app` to CORS whitelist in `main.py` (sandbox frontend was blocked) | cfc-orders | `e4cd70c1` |

---

## What Was Done (Phase 5 + Phase 7 Step 1)

### Phase 5 Hardening — COMPLETE
- Phase 5B: slowapi rate limiting wired (rate_limit.py sha 10e3aa8f, main.py sha e4cd70c1, routes/audit.py sha a6a70380)
- Phase 5C: PATCH /orders/{id}, POST /orders/{id}/run-check, POST /orders/{id}/reactivate all return 200 with CFC2025
- Sandbox smoke test: all green (rate_limiting.enabled:true in GET /)

### Phase 7 Step 1 — DONE
- api.js updated: CFC2025 to CFC2026
- SHA: 68019f6e (cfc-orders-frontend repo, branch main)

### PS5 curl rule (permanent)
Write JSON to body.json with Set-Content, pass with -d "@body.json". Never use backslash-quote escapes in PowerShell 5.x.

---

## Next Steps — Phase 7 Steps 2 through 8

Step 2 — Render sandbox: verify ADMIN_API_KEY ← **DO THIS FIRST**
- Render → CFCOrderBackend-Sandbox (https://cfcorderbackend-sandbox.onrender.com)
- Add/confirm: ADMIN_API_KEY=CFC2026
- Verify present: GMAIL_SEND_ENABLED=true, RL_QUOTE_SANDBOX_URL=https://rl-quote-sandbox.onrender.com, ANTHROPIC_API_KEY, SHIPPO_API_KEY, RL_CARRIERS_API_KEY
- ⛔ Do NOT set SQUARE_ENVIRONMENT=production — deferred
- Manual deploy → watch logs

Step 3 — Frontend code prep (do before Vercel repoint)
- Push local App.jsx v5.10.5 to cfc-orders-frontend main
- Edit src/config.js: API_URL → https://cfcorderbackend-sandbox.onrender.com, IS_SANDBOX → false, push

Step 4 — Vercel: repoint frontend
- cfc-orders-frontend → Settings → Git → disconnect → reconnect to 4wprince/CFCOrdersFrontend_Sandbox, branch main
- Deploy → watch logs

Step 5 — Smoke test
- curl https://cfcorderbackend-sandbox.onrender.com/health (expect v6.2.0)
- curl https://cfcorderbackend-sandbox.onrender.com/ (expect auto_sync.running=true)
- Set-Content -Path body.json -Value '{"current_status":"pending"}'
- curl -X PATCH https://cfcorderbackend-sandbox.onrender.com/orders/YOUR_ID -H "Content-Type: application/json" -H "X-Admin-Token: CFC2026" -d "@body.json" (expect 200)

Step 6 — DB migrations (idempotent)
- POST /add-rl-fields with X-Admin-Token: CFC2026
- POST /add-weight-column with X-Admin-Token: CFC2026
- POST /backfill-lifecycle with X-Admin-Token: CFC2026
- SKIP /add-lifecycle-fields — already done

Step 7 — Full smoke checklist
- /health → v6.2.0 (NOT v6.0.0)
- /alerts/summary → 200
- /lifecycle/summary → 200
- /proxy/health → 200
- /email/templates → 200
- /orders → loads correctly
- / → auto_sync.running=true
- Frontend loads at cfc-orders-frontend.vercel.app

Step 8 — R+L end-to-end (after Step 7 passes)
- /rl/test → /rl/order/{id}/shipments → /rl/order/{id}/create-bol → PDF/labels → pickup → track → notify → emails

---

## Phase Status

Phase 1 through 6: ALL DEPLOYED
Phase 5 Hardening (5B rate limiting + 5C sandbox verify): DONE
Phase 7 Step 1: DONE — api.js sha 68019f6e, token CFC2026
Phase 7 Steps 2 through 7: NEXT (Step 2 is the immediate blocker)
Phase 7 Step 8 R+L e2e: AFTER Steps 2–7 complete

---

## Key Files

cfc-orders-frontend:README.md — sha 1247525f — ⚠️ READ FIRST — current state, issues, Phase 7 readiness checklist
cfc-orders-frontend:src/api.js — sha 68019f6e — X-Admin-Token: CFC2026 ✅
cfc-orders-frontend:src/App.jsx — sha 345b244572 — v5.10.0 in repo / v5.10.5 local (NOT PUSHED)
cfc-orders-frontend:src/config.js — sha d3590688 — ⚠️ hardcoded sandbox URL, must flip before Step 3
cfc-orders:main.py — sha e4cd70c1 — v6.2.0, sandbox CORS fix
cfc-orders:rate_limit.py — sha 10e3aa8f — shared slowapi Limiter
cfc-orders:routes/audit.py — sha a6a70380 — rate-limited audit log endpoints
cfc-orders:orders_routes.py — sha 0ac6a8e3 — run-check + reactivate added
cfc-orders:rl_carriers.py — sha b92c627a — 719 lines R+L API
cfc-orders:auth.py — sha 795a0a76 — NOT hardcoded — os.environ.get("ADMIN_API_KEY", "CFC2025") — env var stripped from prod → fallback CFC2025 → 401
cfc-orders:checkout.py — sha 4e2bfaab — ⚠️ SQUARE_ENVIRONMENT controls Square API base URL
cfc-orders:square_sync.py — sha 10eeee1b — ⚠️ hardcodes connect.squareup.com regardless of SQUARE_ENVIRONMENT
cfc-orders:handoffs/SANDBOX_VS_PRODUCTION_AUDIT.md — sha a139452f — Sandbox vs prod gap analysis
brain:handoffs/AUDIT_REPORT_C_CFC_ORDERS.md — sha ebcf3134 — Full audit findings (2026-04-03)
brain:workstreams/WS6_CFC_ORDERS.md — sha 6ef6d4c6 — Full workstream file

---

## Critical Reminders
- SANDBOX backend: https://cfcorderbackend-sandbox.onrender.com (CFCOrderBackend-Sandbox on Render) — active work target.
- PROD backend: https://cfc-backend-b83s.onrender.com (cfc-backend on Render) — live, working, do not touch during sandbox work. ADMIN_API_KEY stripped here — fix separately.
- Sandbox and prod share the SAME PostgreSQL DB — migrations hit production.
- auth.py is NOT hardcoded — os.environ.get("ADMIN_API_KEY", "CFC2025"). Env var stripped from prod → fallback CFC2025 → 401.
- api.js already has CFC2026 (sha 68019f6e) — do NOT revert.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- Rollback: Render or Vercel one-click revert. DB unchanged.
- /health returns v6.2.0 — NOT v6.0.0.
- square_sync.py hardcodes production Square URL — Square integration deferred.
- Lane C (RTA Weight) blocked on WS5 — do not touch until WS5 complete.
- Blind R+L shipping = $106/shipment — rejected, do not revisit.
- PS5 curl: NEVER inline quote escapes. Always Set-Content body.json then -d "@body.json".
- Audit log is in-memory only — resets on Render restart.
- Rate limiter keyed by IP — admin token does not bypass limits.
- NEVER suggest cold start or wake-up — Render is PAID, servers never sleep.
