# WS6 — CFC Orders Session Handoff
**Date:** 2026-04-04 (updated end of day)
**Repo:** CFCOrderBackend_Sandbox (https://cfcorderbackend-sandbox.onrender.com)

⛔ THIS IS THE SANDBOX REPO — NOT PRODUCTION
- Production backend (leave alone): https://cfc-backend-b83s.onrender.com
- Production frontend (leave alone): https://cfc-orders-frontend.vercel.app
- Sandbox frontend: https://cfcordersfrontend-sandbox.vercel.app

---

## CURRENT STATE — Sandbox is feature-complete. Ready for production promotion.

**Next step: Execute Phase 7 (production promotion checklist below).**

---

## Phase 7 — Production Promotion Checklist

Shared PostgreSQL DB — migrations already done ✅ (confirmed 2026-04-04).
ADMIN_API_KEY=CFC2026 set on sandbox ✅. Gmail OAuth working ✅.

### Pre-Promotion Verification
1. Hit `GET /health` on sandbox backend — confirm 200
2. Confirm ADMIN_API_KEY=CFC2026 is NOT yet set on prod Render (to avoid conflict before cutover)
3. No additional DB migrations needed — all columns confirmed present

### Promotion Steps — In Order

**Step 1 — Set ADMIN_API_KEY on prod Render**
On `cfc-backend-b83s` Render dashboard → Environment tab:
```
ADMIN_API_KEY = CFC2026
```
Do NOT redeploy yet.

**Step 2 — Repoint Render production backend**
On Render for `cfc-backend-b83s`:
- Change connected GitHub repo → `4wprince/CFCOrderBackend_Sandbox`
- Branch: `main`
- Deploy

**Step 3 — Repoint Vercel production frontend**
On Vercel for `cfc-orders-frontend`:
- Change connected GitHub repo → `4wprince/CFCOrdersFrontend_Sandbox`
- Branch: `main`
- Redeploy

**Step 4 — Post-deploy smoke tests on production URLs**
- `GET /health` → 200
- Load order list — confirm orders appear (shared DB, instant)
- Send test invoice via "Send Invoice + PDF" — confirm email + PDF received
- Trigger B2BWave webhook → confirm invoice email fires
- Shippo auto-quote on <70 lb order — confirm rate returns
- Square checkout URL generation — confirm URL copies correctly
- PAID badge renders on table row + detail panel
- AI Summary 6-bullet shows on Details tab
- Sync AI button generates summaries
- Confirm no 500s in Render logs for 5 min post-deploy

**Step 5 — Confirm production stable**
- Watch Render logs 5 min post-deploy
- Confirm Gmail OAuth env vars transferred (GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN)

---

## What's Complete in Sandbox ✅

### Lane A — Shippo End-to-End ✅
- <70 lbs → Shippo; 70+ lbs → R+L LTL
- LONG_PARCEL (98×9×6) for items with single number ≥84 in name
- X-separated dimensions → forced LTL
- Shippo ZIP uses short codes (LI=32148, DL=32256, etc.) matching DB warehouse field
- Shippo weight pre-fills from shipment.weight or order.total_weight

### Lane B — Payment Triggers ✅
- Trigger 1: B2BWave webhook → invoice email + PDF ✅ verified 2026-04-04
- Trigger 2: Square payment → auto-create BOL ✅
- Trigger 3: Square sync → order status ✅
- Trigger 4: Square payment → confirmation email ✅

### Invoice / Checkout ✅
- QB-style HTML invoice with line items, 8% tariff, shipping, grand total
- Ship-to address: addr2 on own line, explicit Ship To label, em-dash fixed ✅
- PDF invoice attached to payment email (reportlab)
- Policy agreement popup before Pay Now
- Internal CFC notification on new order

### Frontend UI ✅ (all wired — App.jsx v7.4.0)
- Light theme CSS v7.3.0
- Customer comments inline on table row + detail panel
- Notes add/edit inline in detail panel (saves + triggers AI summary refresh)
- ShipmentRow in detail panel (status, method, tracking, Save & Email / Save Only)
- Profit tracking in table (Ship: $X (+$Y), Total: $Z)
- Alert backgrounds + labels on table rows
- Sync AI button in header → POST /orders/regenerate-summaries
- 6-bullet AI state summary at top of Details tab (auto-generates on sync every 7.5 min)
- Full AI analysis on AI tab (on-demand)
- Refresh AI Summary button in Actions tab
- Manual shipping override in ShippingManager (cost/charge/note → quote_price + customer_price)
- Shippo option in ShippingManager with auto-quote
- PAID badge on table row and detail panel header
- Send Invoice + PDF button in Actions tab

### Backend ✅
- email_templates.py: Ship To label, addr2 fix, &mdash; encoding
- checkout.py: LI = Cabinetry Distribution, 561 Keuka Rd, (615) 410-6775
- orders_routes.py: Shippo in valid_methods
- sync_service.py: 7.5 min interval, auto-refresh AI summaries post-sync
- sync_routes.py: POST /orders/regenerate-summaries
- ai_summary.py: 6-bullet state summary + comprehensive analysis (both working)
- config.py: 7.5 min sync, LI name/zip correct

### DB Migrations ✅
- All 15 RL fields in order_shipments confirmed present (/add-rl-fields run 2026-04-04)
- orders table: all lifecycle, AI, alert, checkpoint columns present
- order_status view: all 10 columns including current_status and days_open
- No further migrations needed

---

## Deferred Items (do NOT block production promotion)

| Item | Priority |
|------|----------|
| Email template formatting polish (subject line â encoding) | Low — next email touch |
| R+L multi-warehouse auto-quote (logic issue) | Phase 8 |
| /proxy/auto-quote endpoint on production | Phase 8 |
| Lane C (RTA Weight) | Blocked on WS5 |

---

## Phase 8 — Shipment Tracking & Notification Engine

Top priority after production promotion. See previous handoff content preserved below.

### Warehouse Shipping Rules
- **LI (Cabinetry Distribution)** — always ships, any length. Watch Gmail for tracking/PRO.
- **LM (Love-Milestone)** — always ships, any length. Watch Gmail for tracking/PRO.
- **DL (DL Cabinetry)** — ships only if long pallet (≥96"). CFC arranges R+L for everything else.
- **All others** — CFC always arranges R+L. Supplier palletizes. CFC pulls PRO from R+L API.

### Track A — Warehouse Ships (LI, LM, DL long pallet)
Day 0: Payment confirmed. Warehouse notified. Pick list PDF (warehouse version) attached.
Day 2: Gmail scan for tracking/PRO. Found → auto-populate. Not found → send warehouse response form.
Form flow: Has it shipped? Yes → PRO entry → R+L polling every 4 business hours → Tracking Confirmed.
No response 24h → email William.

### Track B — CFC Arranges R+L (all others + DL short)
Day 0: Payment confirmed. BOL created. Warehouse notified.
Day 2: Form: "Is it palletized and ready for R+L pickup?"
Yes → R+L pickup ping → poll for PRO → Tracking Confirmed.
No response 24h → email William.

### Tracking Confirmed (both tracks)
1. Customer email: shipped + PRO + ETA
2. Poll R+L daily for ETA + stop number
3. Evening before delivery: "Your delivery is tomorrow"
4. Morning of delivery: Delivery Day email + customer pick sheet PDF + interactive pick sheet link
5. R+L shows delivered: Post-Delivery email

### Interactive Mobile Pick Sheet
URL: `/picksheet/{order_id}?token={token}`
- 4 required photo slots (pallet on truck, boxes laid out, any damage, signed BOL)
- Per-SKU checklist with tap-to-check + flag missing/damaged
- All 4 photos required before submit activates
- Photos sent silently to dedicated Gmail

### Backend Components Needed
- `shipment_state` column/table — per-shipment state machine
- Polling job — every 4 business hours, checks R+L by PRO
- Gmail scan — tied to state machine (LI, LM, DL-shipped only)
- Warehouse form endpoint — one-page response, writes to DB
- Pick sheet endpoint + photo upload endpoint
- Pick list PDF generator — two versions (customer + warehouse)
- Email scheduler — fires templates on state transitions

### Frontend Components Needed
- ShipmentRow: track state, escalation badge, supplier clock (days since order, warning at day 2)
- New: pick sheet page (mobile-first HTML)

---

## Key Files (current SHAs)

| File | SHA |
|------|-----|
| cfc-orders:email_templates.py | e66c7ba2 |
| cfc-orders:checkout.py | 76ff33fa |
| cfc-orders:orders_routes.py | 27ece593 |
| cfc-orders:ai_summary.py | 15a85caf |
| cfc-orders:sync_service.py | 240c2219 |
| cfc-orders:sync_routes.py | dfaa5ea7 |
| cfc-orders:config.py | 7475f384 |
| cfc-orders:invoice_pdf.py | 17582f15 |
| cfc-orders:email_sender.py | 12bed3e8 |
| cfc-orders:checkout_routes.py | ffc0c8a8 |
| cfc-orders:payment_triggers.py | 8b6688cb |
| cfc-orders:square_sync.py | c3c6158b |
| cfc-orders:shippo_rates.py | 835c069f |
| cfc-orders:shipping_routes.py | f759b973 |
| cfc-orders-frontend:src/App.jsx | 74531425 (v7.4.0) |
| cfc-orders-frontend:src/index.css | ba6be644 (v7.3.0) |
| cfc-orders-frontend:src/components/ShippingManager.jsx | ea4de1bf (v5.9.8) |
| cfc-orders-frontend:src/components/ShipmentRow.jsx | 398b287a (v5.9.4) |

---

## Critical Reminders

- SANDBOX only — do not touch production until Phase 7 promotion steps complete.
- Shared PostgreSQL DB — migrations affect production data.
- ADMIN_API_KEY=CFC2026 ✅ set on sandbox Render.
- Gmail OAuth ✅ working — refresh token regenerated 2026-04-04.
- Do NOT set SQUARE_ENVIRONMENT — deferred.
- PS5 PowerShell: NEVER use &&. One command per block.
- NEVER suggest cold start — Render is PAID.
- Lane C (RTA Weight) blocked on WS5 — do not touch.
- WS17 FILE LOCK still active — no WS17 files without explicit consent.
