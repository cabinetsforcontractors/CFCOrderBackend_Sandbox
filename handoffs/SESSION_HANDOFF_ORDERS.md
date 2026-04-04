# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-04
**Repo:** CFCOrderBackend_Sandbox (https://cfcorderbackend-sandbox.onrender.com)

⛔ THIS IS THE SANDBOX REPO — NOT PRODUCTION
- Production backend (leave alone): https://cfc-backend-b83s.onrender.com
- Production frontend (leave alone): https://cfc-orders-frontend.vercel.app
- Sandbox frontend: https://cfcordersfrontend-sandbox.vercel.app

---

## PRIORITY ORDER — DO IN THIS SEQUENCE

1. ✅ Complete sandbox UI wiring (in progress — see Current Work below)
2. Promote to production (Phase 7)
3. Build Phase 8 — Shipment Tracking & Notification Engine
4. Phase 9 — Full Customer Portal
5. Phase 10 — Full Warehouse Portal
6. Phase 11 — SMS
7. Phase 12 — Mobile App

---

## Current Work — Sandbox UI Wiring (IN PROGRESS)

Still needed before production promotion:

- Notes add/edit inline in detail panel
- Customer comments (B2BWave `comments` field) shown in detail panel
- ShipmentRow inline in table — status, method, tracking, Save & Email / Save Only
- Supplier email clock on ShipmentRow (Phase 8 dependency — see below)
- Profit tracking per order (shipping charge vs cost)
- AI Summary snippet on detail panel Details tab
- Alert background + label on table rows
- Light theme CSS (match production)
- Manual shipping override in ShippingManager
- Shippo ZIP auto-fill fix (warehouse name key mismatch)
- Fix StatusBar Sync AI to use apiFetch not raw fetch
- Ship-to address in invoice email template
- Email template minor formatting polish

---

## What's Complete ✅

### Lane A — Shippo End-to-End ✅
- <70 lbs → Shippo; 70+ lbs → R+L LTL
- LONG_PARCEL (98×9×6) for items with single number ≥84 in name
- X-separated dimensions → forced LTL

### Lane B — Payment Triggers ✅
- Trigger 1: B2BWave webhook → invoice email + PDF ✅ verified 2026-04-04
- Trigger 2: Square payment → auto-create BOL ✅
- Trigger 3: Square sync → order status ✅
- Trigger 4: Square payment → confirmation email ✅

### Invoice / Checkout ✅
- QB-style HTML invoice with line items, 8% tariff, shipping, grand total
- PDF invoice attached to payment email (reportlab)
- Policy agreement popup before Pay Now
- Internal CFC notification on new order
- Checkout UI shows tariff + shipping breakdown

### Frontend UI (partial) ✅
- PAID badge on table row and detail panel header
- Payment status in detail panel (received, amount, paid at)
- Send Invoice + PDF button in Actions tab
- Checkout URL copy in Actions tab
- Shippo option in ShippingManager with auto-quote
- AI Summary tab working

---

## Deferred Minor Fixes (before production promotion)

- Ship-to address missing from invoice email template
- Email template formatting polish
- LI warehouse: name = Cabinetry Distribution, address = 561 Keuka Rd, Interlachen FL 32148, phone = (615) 410-6775

---

## PHASE 8 — Shipment Tracking & Notification Engine

This is the top priority after sandbox UI wiring and production promotion.
Phase 8 absorbs what were previously Phase 8 (supplier email), Phase 9 (customer portal foundation), and Phase 10 (warehouse portal foundation).

### Warehouse Shipping Rules
- **LI (Cabinetry Distribution)** — always ships, any length. Watch Gmail for tracking/PRO.
- **LM (Love-Milestone)** — always ships, any length. Watch Gmail for tracking/PRO.
- **DL (DL Cabinetry)** — ships only if long pallet (≥96"). CFC arranges R+L for everything else. Watch Gmail for DL-shipped items.
- **All others** — CFC always arranges R+L. Supplier palletizes and gets it out. CFC pulls PRO from R+L.

### DL Length Detection
Already built in checkout.py via `detect_item_dimensions()`. Shipment router reads the long pallet flag to assign Track A or Track B.

### Track A — Warehouse Ships (LI, LM, DL long pallet)

Day 0: Payment confirmed. Warehouse notified. Pick list PDF (warehouse version, no customer info) attached to notification email.

Day 2: Gmail scan for tracking/PRO from that warehouse.
- Found → auto-populate shipment, skip to Tracking Confirmed
- Not found → send warehouse response form link

Form: "Has Order #XXXX shipped?"
- Yes → text field: enter tracking/PRO → submit → R+L polling starts every 4 business hours
  - R+L confirms → Tracking Confirmed
  - 12 hours, no R+L confirmation → "R+L has no record, confirm it went out?" Yes/No
    - Yes again → poll 4 more hours → still nothing → email William to call
    - No → fall to "When will it ship?" flow
- No → "When will it ship?" dropdown:
  - Today → poll R+L next morning → no pickup → email William
  - Tomorrow → poll R+L day after → no pickup → email William
  - 2+ days → email William immediately
  - Not sure → email William immediately
- No response in 24 hours → email William

### Track B — CFC Arranges R+L (all others + DL short)

Day 0: Payment confirmed. BOL created. Warehouse notified. Pick list PDF (warehouse version) attached.

Day 2: Send warehouse form: "Is Order #XXXX palletized and ready for R+L pickup?"
- Yes, ready → backend pings R+L to confirm/schedule pickup → poll every 4 business hours for PRO
  - PRO confirmed → Tracking Confirmed
  - 12 hours, no confirmation → email William
- No → "When will it be ready?" dropdown (same options as Track A)
- No response in 24 hours → email William

Note: Supplier never sees the PRO. CFC pulls PRO from R+L API and sends to customer.

### Tracking Confirmed (both tracks)

1. Customer email: "Your order has shipped" + PRO + estimated delivery date from R+L API
2. Poll R+L API daily for ETA and stop number
3. Evening before delivery: "Your delivery is tomorrow — someone must be present"
4. Morning of delivery: Delivery Day email (see below) + customer pick sheet PDF attached + interactive pick sheet link
5. R+L shows delivered: Post-Delivery email

### Delivery Day Email
- You are stop #X of Y on today's route — use this to estimate arrival time
- ⚠️ Claims cannot be honored without: (1) digital pick sheet completed, (2) minimum 4 delivery photos submitted through the pick sheet link
- Take photos of every box BEFORE the driver leaves — even if everything appears fine
- Check that ALL SKUs on your order are present on the pallet before the driver leaves
- Do NOT sign the BOL until you have physically inspected every box and noted any visible damage directly on the BOL in writing
- If damage: do not refuse delivery, note on BOL, complete replacement request at cabinetsforcontractors.net/pages/5-replacement-request
- Policy: no returns on assembled/installed cabinets, 20% restocking on undamaged items in original packaging, damage must be reported within 48 hours

### Post-Delivery Email
- R+L confirms delivered
- Reiterate: damage within 48 hours, replacement form link
- Reiterate: no returns on assembled/installed cabinets
- "Hope everything arrived perfectly — reply or call (770) 990-4885"
- Future: SMS version

### Interactive Mobile Pick Sheet
URL: `/picksheet/{order_id}?token={token}` — same token pattern as checkout-ui

Content:
- Order #, customer name, delivery date, warehouse
- Per line item: SKU + description, quantity, ✅ tap to check off, flag as missing/damaged
- 4 required photo slots with labels:
  1. Full pallet on truck before unloading
  2. All boxes laid out after unloading
  3. Any visible damage (or "no damage" tap)
  4. BOL with signature and any damage notes written on it
- All 4 photos required before submit activates
- "All items present + photos taken" → submit → logs completion timestamp
- "Report missing items" → flagged SKUs fire immediate alert to William

Photos sent silently to dedicated Gmail account (to be set up).
Subject format: "Order #XXXX — Delivery Photos — [Customer Name] — [Date]"
Body includes order ID, customer name, delivery address, any damage flags.

### Pick List PDF — Two Versions (generated via reportlab, same pattern as invoice_pdf.py)

Customer version: CFC header, order #, customer name/address/phone/email, date, line items table (SKU, description, qty, notes), policy agreement text, pick sheet link, claim language.

Warehouse version: CFC header only, order #, warehouse name, line items table, internal notes. No customer info.

When sent:
- Warehouse version → attached to warehouse notification email at payment confirmation
- Customer version → attached to delivery day morning email

### Email Templates Needed (Phase 8)
1. Warehouse — pick list + payment notification (warehouse version PDF attached)
2. Warehouse — Track A form: "Has it shipped?"
3. Warehouse — Track B form: "Is it palletized and ready?"
4. Warehouse — R+L no record, confirm again
5. Customer — order has shipped (PRO + ETA)
6. Customer — delivery tomorrow
7. Customer — delivery day (stop #, pick sheet link, photo requirement, BOL language)
8. Customer — delivered confirmation
9. William — escalation (various triggers)

### Backend Components Needed
- `shipment_state` column or table — tracks per-shipment state machine state
- Polling job — runs every 4 business hours, checks R+L API by PRO number
- Gmail scan — tied to shipment state machine (LI, LM, DL-shipped items only)
- Warehouse form endpoint — one-page response form, writes to DB
- Pick sheet endpoint — `/picksheet/{order_id}?token={token}`
- Photo upload endpoint — receives 4 images, sends silent email to dedicated Gmail
- Pick list PDF generator — `picklist_pdf.py`, two versions
- Email scheduler — fires correct template on state transitions

### Frontend Components Needed
- ShipmentRow: show current track state, escalation badge
- ShipmentRow: supplier clock indicator (days since order, warning at day 2)
- New: pick sheet page (mobile-first HTML)

---

## PHASE 9 — Full Customer Portal (after Phase 8)

Builds on Phase 8 foundation (pick sheet page, delivery day page, tracking status).

- Customer-facing order status page (progress, tracking history, past orders)
- Light auth: last 4 phone + shipping ZIP
- Shows: current status, all shipments, tracking, estimated delivery, BOL link
- Future: browser extension
- Future: mobile app (Phase 12)

---

## PHASE 10 — Full Warehouse Portal (after Phase 8)

Builds on Phase 8 foundation (warehouse response form).

- Full warehouse login (last 4 phone + warehouse ZIP)
- View all open orders assigned to their warehouse
- Download BOL PDF
- Schedule R+L pickup
- CFC notification when pickup scheduled

---

## PHASE 11 — SMS (after Phase 8)

SMS slots into every Phase 8 notification point:
- Payment confirmed
- Order shipped
- Delivery tomorrow
- Delivery today (with stop number)
- Delivered

---

## PHASE 12 — Mobile App (after Phase 9)

Customer-specific version of Phase 9 portal as a native app.

---

## Key Files

cfc-orders:checkout.py               sha 635ad071
cfc-orders:invoice_pdf.py            sha 17582f15
cfc-orders:email_templates.py        sha 96ca2b7f
cfc-orders:email_sender.py           sha 12bed3e8
cfc-orders:checkout_routes.py        sha ffc0c8a8
cfc-orders:payment_triggers.py       sha 8b6688cb
cfc-orders:square_sync.py            sha c3c6158b
cfc-orders:shippo_rates.py           sha 835c069f
cfc-orders:shipping_routes.py        sha f759b973
cfc-orders:requirements.txt          sha 6d27a806
cfc-orders:handoffs/SESSION_HANDOFF_ORDERS.md  sha (this file)
cfc-orders-frontend:src/App.jsx      sha 72b0251e — v7.2.4
cfc-orders-frontend:src/components/ShippingManager.jsx  sha a8baafeb — v5.9.5
cfc-orders-frontend:README.md        sha 927706cc

---

## Critical Reminders

- SANDBOX only — do not touch production.
- Shared PostgreSQL DB — migrations affect production data.
- ADMIN_API_KEY=CFC2026 ✅ set on sandbox Render.
- Gmail OAuth ✅ working — refresh token regenerated 2026-04-04.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- PS5 PowerShell: NEVER use &&. One command per block. Set-Content body.json then -InFile.
- NEVER suggest cold start — Render is PAID.
- Lane C (RTA Weight) blocked on WS5 — do not touch.
- LI warehouse name/address correction pending — deferred until next file touch.
- WS17 FILE LOCK still active — no WS17 files without explicit consent.
