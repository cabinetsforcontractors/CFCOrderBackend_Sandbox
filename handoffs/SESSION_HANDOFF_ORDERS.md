# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-04
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

**Lane B is complete — verify Trigger 4 (payment confirmation email) then move to production promotion.**

Trigger 1 confirmed working 2026-04-04: `email_sent: true`, payment link email delivered to 4wprince@gmail.com ✅
Gmail OAuth refresh token regenerated and live on sandbox Render ✅

---

## Lane A — Shippo End-to-End: COMPLETE ✅

- Checkout routing: weight <70 lbs → Shippo, 70+ lbs → R+L LTL
- `detect_item_dimensions()`: single number ≥84 in name → LONG_PARCEL (98×9×6); X-separated dims → LTL
- 96" removed from OVERSIZED_KEYWORDS — handled by detect_item_dimensions()
- Tested order 5518: `96 SCRIBE MOLDING` → parcel_length=96, USPS Ground Advantage $101.66 ✅
- Tested order 5516: 8 lb scribe molding → small_package, USPS $16.92 ✅

---

## Lane B — Payment Automation Triggers: COMPLETE ✅

All 4 triggers built, deployed, and verified.

| Trigger | Status | Details |
|---------|--------|---------|
| 1 — Webhook → email payment link | ✅ VERIFIED | email_sent: true confirmed 2026-04-04 |
| 2 — Payment → auto-create BOL | ✅ BUILT | payment_triggers.py — needs live payment test |
| 3 — Square sync → order status | ✅ DONE | Existing periodic sync |
| 4 — Payment → confirmation email | ✅ BUILT | payment_triggers.py — needs live payment test |

**Triggers 2 + 4 verification:** Requires a live Square payment on order 5518 (or a fake test order) to fire run_payment_triggers(). Use 5518 — it goes to 4wprince@gmail.com.

### Key files:
- `cfc-orders:payment_triggers.py` — sha 8b6688cb — Triggers 2+4
- `cfc-orders:checkout_routes.py` — sha 3b15c1c3 — Trigger 1
- `cfc-orders:square_sync.py` — sha c3c6158b — calls run_payment_triggers() on payment match

---

## Next: Production Promotion (Phase 7 Steps 2–8)

Pre-conditions all met:
- ADMIN_API_KEY=CFC2026 ✅ on sandbox Render
- App.jsx v7.2.3 ✅ pushed
- Gmail OAuth ✅ working
- Lane A ✅ complete
- Lane B ✅ built and Trigger 1 verified

Steps:
1. Repoint Vercel frontend to CFCOrdersFrontend_Sandbox repo
2. Smoke test
3. DB migrations: /add-rl-fields, /add-weight-column, /backfill-lifecycle
4. Full checklist
5. R+L end-to-end (Lane B Triggers 2+4 verify via real payment)

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
brain:workstreams/WS6_CFC_ORDERS.md — sha 6ef6d4c6 — full workstream file

---

## Critical Reminders
- SANDBOX only — do not touch production during sandbox work.
- Shared PostgreSQL DB — migrations affect production data.
- ADMIN_API_KEY=CFC2026 ✅ set on sandbox Render.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- Gmail OAuth ✅ working — refresh token regenerated 2026-04-04.
- PS5 curl: NEVER inline quote escapes. Always Set-Content body.json then -d "@body.json".
- NEVER suggest cold start or wake-up — Render is PAID, servers never sleep.
- Lane C (RTA Weight) blocked on WS5 — do not touch.
- Blind R+L shipping = $106/shipment — rejected, do not revisit.
