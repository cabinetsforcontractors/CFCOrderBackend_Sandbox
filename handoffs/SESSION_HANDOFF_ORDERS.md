# WS6 — CFC Orders Session Handoff
**Date:** 2026-03-19
**Task:** Phase 7 — Production Promotion (Option A execution)

---

## ⚡ START HERE — First Thing This Session

**Phase 7 Step 2: Set ADMIN_API_KEY=CFC2026 on prod Render.**

The frontend already sends `CFC2026` (api.js sha 68019f6e). The prod backend still has
`ADMIN_API_KEY=CFC2025`. Every write endpoint is rejecting requests until this is flipped.

1. Render dashboard → `cfc-backend-b83s.onrender.com` → Environment
2. Change `ADMIN_API_KEY` from `CFC2025` → `CFC2026`
3. Save → Manual deploy → watch logs
4. Then continue with Steps 3–6 below

**Do not start Step 3 (Vercel repoint) until Step 2 is confirmed live.**

---

## Endpoint Fixes Applied This Session (2026-03-19)

Two bugs found and patched during endpoint audit:

| Fix | Repo | SHA |
|-----|------|-----|
| Added `/api/capture-lead` to v5 backend (frontend called it, endpoint was missing — silent fail) | v5 | `aa9c5909` |
| Added `cfcordersfrontend-sandbox.vercel.app` to CORS whitelist in `main.py` (sandbox frontend was blocked) | cfc-orders | `e4cd70c1` |

---

## What Was Done (Phase 5 + Phase 7 Step 1)

### Phase 5 Hardening — COMPLETE
- Phase 5B: slowapi rate limiting wired (rate_limit.py sha 10e3aa8f, main.py sha 46d7c63a, routes/audit.py sha a6a70380)
- Phase 5C: PATCH /orders/{id}, POST /orders/{id}/run-check, POST /orders/{id}/reactivate all return 200 with CFC2025
- Sandbox smoke test: all green (rate_limiting.enabled:true in GET /)

### Phase 7 Step 1 — DONE
- api.js updated: CFC2025 to CFC2026
- SHA: 68019f6e (cfc-orders-frontend repo, branch main)

### PS5 curl rule (permanent)
Write JSON to body.json with Set-Content, pass with -d "@body.json". Never use backslash-quote escapes in PowerShell 5.x.

---

## Next Steps — Phase 7 Steps 2 through 6

Step 2 — Render: env var and repoint backend ← **DO THIS FIRST**
- Render → cfc-backend-b83s.onrender.com
- Add env var: ADMIN_API_KEY=CFC2026
- Verify: GMAIL_SEND_ENABLED=true, RL_QUOTE_SANDBOX_URL=https://rl-quote-sandbox.onrender.com, ANTHROPIC_API_KEY, SHIPPO_API_KEY, RL_CARRIERS_API_KEY
- Settings → Build and Deploy → change repo to 4wprince/CFCOrderBackend_Sandbox, branch main
- Manual deploy → watch logs

Step 3 — Vercel: repoint frontend
- cfc-orders-frontend → Settings → Git → disconnect → reconnect to 4wprince/CFCOrdersFrontend_Sandbox, branch main
- Deploy → watch logs

Step 4 — Smoke test
- curl https://cfc-backend-b83s.onrender.com/health (expect v6.0.0)
- curl https://cfc-backend-b83s.onrender.com/ (expect auto_sync.running=true)
- Set-Content -Path body.json -Value '{"current_status":"pending"}'
- curl -X PATCH .../orders/YOUR_ID -H "Content-Type: application/json" -H "X-Admin-Token: CFC2026" -d "@body.json" (expect 200)

Step 5 — DB migrations (idempotent)
- POST /add-rl-fields with X-Admin-Token: CFC2026
- POST /add-weight-column with X-Admin-Token: CFC2026
- POST /backfill-lifecycle with X-Admin-Token: CFC2026
- SKIP /add-lifecycle-fields — already done

Step 6 — Full smoke checklist
- /health → v6.0.0
- /alerts/summary → 200
- /lifecycle/summary → 200
- /proxy/health → 200
- /email/templates → 200
- /orders → loads correctly
- / → auto_sync.running=true
- Frontend loads at cfc-orders-frontend.vercel.app — dark theme, all 8 tabs, alerts bell

Step 7 — R+L end-to-end (after Step 6 passes)
- /rl/test → /rl/order/{id}/shipments → /rl/order/{id}/create-bol → PDF/labels → pickup → track → notify → emails

---

## Phase Status

Phase 1 through 6: ALL DEPLOYED
Phase 5 Hardening (5B rate limiting + 5C sandbox verify): DONE
Phase 7 Step 1: DONE — api.js sha 68019f6e, token CFC2026
Phase 7 Steps 2 through 6: NEXT (Step 2 is the immediate blocker)
Phase 7 Step 7 R+L e2e: AFTER Steps 2 through 6 complete

---

## Key Files

cfc-orders-frontend:src/api.js — sha 68019f6e — X-Admin-Token: CFC2026 (updated 2026-03-19)
cfc-orders-frontend:src/App.jsx — sha e020e868 — v7.2.2 dark theme
cfc-orders:main.py — sha e4cd70c1 — v6.2.0, sandbox CORS fix (updated 2026-03-19)
cfc-orders:rate_limit.py — sha 10e3aa8f — shared slowapi Limiter
cfc-orders:routes/audit.py — sha a6a70380 — rate-limited audit log endpoints
cfc-orders:orders_routes.py — sha 0ac6a8e3 — run-check + reactivate added
cfc-orders:rl_carriers.py — sha b92c627a — 719 lines R+L API
cfc-orders:handoffs/SANDBOX_VS_PRODUCTION_AUDIT.md — sha a139452f — Sandbox vs prod gap analysis
brain:workstreams/WS6_CFC_ORDERS.md — sha 6ef6d4c6 — Full 14-lane workstream file (updated 2026-03-19)
v5:app.py — sha aa9c5909 — /api/capture-lead added (updated 2026-03-19)

---

## Critical Reminders
- Prod backend: cfc-backend-b83s.onrender.com. Prod frontend: cfc-orders-frontend.vercel.app.
- Sandbox and prod share the SAME PostgreSQL DB — migrations hit production.
- Set ADMIN_API_KEY=CFC2026 on Render BEFORE repointing the branch.
- api.js already has CFC2026 (sha 68019f6e) — do NOT revert, frontend deploy picks it up automatically.
- Rollback: Render or Vercel one-click revert. DB unchanged.
- Lane C (RTA Weight) blocked on WS5 — do not touch until WS5 complete.
- Blind R+L shipping = $106/shipment — rejected, do not revisit.
- PS5 curl: NEVER inline quote escapes. Always Set-Content body.json then -d "@body.json".
- Audit log is in-memory only — resets on Render restart.
- Rate limiter keyed by IP — admin token does not bypass limits.
- NEVER suggest cold start or wake-up — Render is PAID, servers never sleep.
