# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-04
**Repo:** CFCOrderBackend_Sandbox (https://cfcorderbackend-sandbox.onrender.com)

⛔ THIS IS THE SANDBOX REPO — NOT PRODUCTION
- Production backend (leave alone): https://cfc-backend-b83s.onrender.com
- Production frontend (leave alone): https://cfc-orders-frontend.vercel.app
- Sandbox frontend: https://cfcordersfrontend-sandbox.vercel.app

---

## Current State — What's Complete ✅

### Lane A — Shippo End-to-End ✅
- <70 lbs → Shippo; 70+ lbs → R+L LTL
- `detect_item_dimensions()`: single number ≥84 → LONG_PARCEL (98×9×6); X-dims → LTL
- Tested: order 5518 → $101.66 USPS, order 5516 → $16.92 USPS

### Lane B — Payment Automation Triggers ✅
- Trigger 1: B2BWave webhook → invoice email + PDF ✅ verified 2026-04-04
- Trigger 2: Square payment → auto-create BOL (LTL only) ✅ built
- Trigger 3: Square periodic sync → order status ✅ existing
- Trigger 4: Square payment → confirmation email ✅ built

### Invoice / Checkout Flow ✅
- QB-style HTML invoice with line items (SKU, name, qty, unit price, total)
- 8% tariff on items subtotal
- Shipping cost from Shippo or R+L
- Grand total = items + tariff + shipping
- PDF invoice (reportlab) attached to payment email
- Policy agreement popup before Pay Now
- Internal order notification email to CFC on new order
- Checkout UI shows full breakdown with tariff line

---

## Deferred — Minor Fixes (do before production promotion)

- Ship-to address missing from invoice email template
- Email template minor formatting polish
- LI warehouse correction: name = Cabinetry Distribution, address = 561 Keuka Rd, Interlachen FL 32148, phone = (615) 410-6775

---

## Future Scope

### Phase 8 — Supplier Email / CSV Upload
- Cross-ref website SKUs with supplier SKUs (William building mapping file)
- `96` in product description = long trim → UPS routing indicator
- Auto-email supplier on payment received with order details
- CSV upload to supplier website — deferred, needs investigation

### Phase 9 — Customer Order Portal
- In-house first: customer-facing order status page
- Show: order progress, tracking number, estimated delivery, BOL link
- Light auth: last 4 phone + shipping ZIP
- Future: browser extension
- Future: mobile app

### Phase 10 — Warehouse Portal
- Warehouse login: last 4 phone + warehouse ZIP
- View orders, download BOL PDF, schedule R+L pickup
- CFC notification when warehouse schedules pickup

---

## Next Steps (before production promotion)

1. Fix ship-to address in invoice email template
2. Email template formatting polish
3. Fix LI warehouse name/address in checkout.py
4. Repoint Vercel frontend to CFCOrdersFrontend_Sandbox
5. Smoke test production URLs
6. DB migrations: /add-rl-fields, /add-weight-column, /backfill-lifecycle
7. Full production checklist
8. R+L end-to-end test with real order

---

## Key Files

cfc-orders-frontend:README.md — sha 927706cc
cfc-orders-frontend:src/App.jsx — sha ce6f739 — v7.2.3 ✅
cfc-orders-frontend:src/api.js — sha 68019f6e — CFC2026 ✅
cfc-orders:checkout.py — sha 635ad071 — field mapping fixed, 8% tariff, LONG_PARCEL routing
cfc-orders:invoice_pdf.py — sha 17582f15 — QB-style PDF generator
cfc-orders:email_templates.py — sha 96ca2b7f — full invoice template
cfc-orders:email_sender.py — sha 12bed3e8 — PDF attachment for payment_link
cfc-orders:checkout_routes.py — sha ffc0c8a8 — policy popup, internal notification, shipping calc in webhook
cfc-orders:payment_triggers.py — sha 8b6688cb — Triggers 2+4
cfc-orders:square_sync.py — sha c3c6158b — calls run_payment_triggers() on payment
cfc-orders:shippo_rates.py — sha 835c069f — LONG_PARCEL 98×9×6
cfc-orders:shipping_routes.py — sha f759b973 — /shippo/rates accepts length
cfc-orders:requirements.txt — sha 6d27a806 — reportlab added

---

## Critical Reminders
- SANDBOX only — do not touch production.
- Shared PostgreSQL DB — migrations affect production data.
- ADMIN_API_KEY=CFC2026 ✅ set on sandbox Render.
- Gmail OAuth ✅ working — refresh token regenerated 2026-04-04.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- PS5 curl: Set-Content body.json then -InFile. Never inline escapes.
- NEVER suggest cold start — Render is PAID.
- Lane C (RTA Weight) blocked on WS5 — do not touch.
- LI warehouse name/address correction pending — deferred.
