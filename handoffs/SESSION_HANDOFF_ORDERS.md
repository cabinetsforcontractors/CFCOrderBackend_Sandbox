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
`cfc-orders-frontend:README.md` (sha 3aa49d5c) — current component state and known issues.

---

## ⚡ START HERE — Next Session

**Fix Gmail OAuth refresh token on sandbox Render — then retest Trigger 1.**

The Gmail refresh token on CFCOrderBackend-Sandbox has expired (`HTTP Error 400: Bad Request` on token refresh). All 4 Lane B triggers are built and deployed, but Trigger 1 + 4 email sends are blocked until this is fixed.

### Steps to regenerate refresh token:
1. Open https://developers.google.com/oauthplayground
2. Click gear icon (top right) → check "Use your own OAuth credentials"
3. Paste `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` from Render env vars
4. In Step 1 panel: find `https://mail.google.com/` → click → "Authorize APIs"
5. Sign in with `cabinetsforcontractors@gmail.com` → allow all permissions
6. Back in playground: click "Exchange authorization code for tokens"
7. Copy the `refresh_token` value from the response
8. Render → `CFCOrderBackend-Sandbox` → Environment → update `GMAIL_REFRESH_TOKEN` → Save → Manual Deploy

### Retest after fix:
```
Set-Content -Path body.json -Value '{"id":"5518","customer_email":"4wprince@gmail.com"}'
Invoke-WebRequest -Uri "https://cfcorderbackend-sandbox.onrender.com/webhook/b2bwave-order" -Method POST -Headers @{"Content-Type"="application/json"} -InFile body.json -UseBasicParsing
```
Expect: `email_sent: true` and payment link email arrives at 4wprince@gmail.com.

---

## Lane A — Shippo End-to-End: COMPLETE ✅

- Checkout routing: weight <70 lbs → Shippo, 70+ lbs → R+L LTL
- `detect_item_dimensions()`: single number ≥84 in name → LONG_PARCEL (98×9×6); X-separated dims → LTL
- 96" removed from OVERSIZED_KEYWORDS — handled by detect_item_dimensions()
- Tested order 5518: `96 SCRIBE MOLDING` → parcel_length=96, USPS Ground Advantage $101.66 ✅
- Tested order 5516: 8 lb scribe molding → small_package, USPS $16.92 ✅

---

## Lane B — Payment Automation Triggers: BUILT, EMAIL BLOCKED

All 4 triggers built and deployed. Email triggers (1 + 4) blocked by expired Gmail refresh token.

| Trigger | Status | Details |
|---------|--------|---------|
| 1 — Webhook → email payment link | ⛔ BLOCKED (Gmail token) | Code live in checkout_routes.py |
| 2 — Payment → auto-create BOL | ✅ BUILT | Code live in payment_triggers.py |
| 3 — Square sync → order status | ✅ DONE | Existing periodic sync, no change needed |
| 4 — Payment → confirmation email | ⛔ BLOCKED (Gmail token) | Code live in payment_triggers.py |

### New files built this session:
- `cfc-orders:payment_triggers.py` — sha 8b6688cb — Triggers 2 + 4 entry point
- `cfc-orders:checkout_routes.py` — sha 3b15c1c3 — Trigger 1 wired into webhook
- `cfc-orders:square_sync.py` — sha c3c6158b — calls run_payment_triggers() on payment match

---

## What Was Done (Phase 5 + Phase 7 Step 1)

### Phase 5 Hardening — COMPLETE
- Phase 5B: slowapi rate limiting wired
- Phase 5C: PATCH/run-check/reactivate all return 200
- Sandbox smoke test: all green

### Phase 7 Step 1 — DONE
- api.js: CFC2025 → CFC2026 — sha 68019f6e
- App.jsx v7.2.3 pushed — sha ce6f739 ✅

---

## Deferred — Production Promotion (Phase 7 Steps 2–8)

Not executing until Lane B is fully verified.

Steps when ready:
1. Render sandbox: ADMIN_API_KEY=CFC2026 ✅ already set
2. Repoint Vercel frontend to CFCOrdersFrontend_Sandbox repo
3. Smoke test
4. DB migrations: /add-rl-fields, /add-weight-column, /backfill-lifecycle
5. Full checklist
6. R+L end-to-end

---

## Key Files

cfc-orders-frontend:README.md — sha 3aa49d5c — ⚠️ READ FIRST
cfc-orders-frontend:src/App.jsx — sha ce6f739 — v7.2.3 ✅
cfc-orders-frontend:src/api.js — sha 68019f6e — X-Admin-Token: CFC2026 ✅
cfc-orders:checkout.py — sha 48608ce6 — detect_item_dimensions, LONG_PARCEL routing
cfc-orders:shippo_rates.py — sha 835c069f — LONG_PARCEL = 98×9×6
cfc-orders:shipping_routes.py — sha f759b973 — /shippo/rates accepts length param
cfc-orders:payment_triggers.py — sha 8b6688cb — Triggers 2+4
cfc-orders:checkout_routes.py — sha 3b15c1c3 — Trigger 1 in webhook
cfc-orders:square_sync.py — sha c3c6158b — Triggers 2+4 called on payment match
cfc-orders:auth.py — sha 795a0a76 — NOT hardcoded — falls back to CFC2025 if env var missing
cfc-orders:handoffs/SANDBOX_VS_PRODUCTION_AUDIT.md — sha a139452f — gap analysis
brain:handoffs/AUDIT_REPORT_C_CFC_ORDERS.md — sha ebcf3134 — full audit findings
brain:workstreams/WS6_CFC_ORDERS.md — sha 6ef6d4c6 — full workstream file

---

## Critical Reminders
- SANDBOX only — do not touch production during sandbox work.
- Shared PostgreSQL DB — migrations affect production data.
- ADMIN_API_KEY=CFC2026 ✅ set on sandbox Render.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- Gmail refresh token expired — regenerate before testing email triggers.
- PS5 curl: NEVER inline quote escapes. Always Set-Content body.json then -d "@body.json".
- NEVER suggest cold start or wake-up — Render is PAID, servers never sleep.
- Lane C (RTA Weight) blocked on WS5 — do not touch.
- Blind R+L shipping = $106/shipment — rejected, do not revisit.
