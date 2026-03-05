# Payments & Checkout — Session Handoff
**Last Updated:** 2026-03-05
**Workstream:** CFC Orders
**Status:** Paused — needs Render health check

## What This Lane Covers
Square payment link generation, payment sync/matching against orders, the checkout flow for contractors, and Gmail-based payment detection. Everything from "contractor places order" to "payment confirmed."

## Current State
- **Square checkout flow** built — checkout.py (620 lines), generates payment links
- **Square payment sync** built — square_sync.py (329 lines), matches payments to orders
- **Payment detection** works — detection.py identifies payment confirmations in email content
- **Gmail-based detection** works — gmail_sync.py scans emails for payment signals
- **Customer tracking emails** NOT enabled — GMAIL_SEND_ENABLED=false, template not built

## Key Files
- `CFCOrderBackend_Sandbox/checkout.py` — 620 lines, Square payment checkout flow
- `CFCOrderBackend_Sandbox/square_sync.py` — 329 lines, Square payment matching
- `CFCOrderBackend_Sandbox/detection.py` — 191 lines, payment/quote/PRO detection
- `CFCOrderBackend_Sandbox/gmail_sync.py` — 425 lines, email scanning and parsing
- `CFCOrderBackend_Sandbox/config.py` — 155 lines, env vars including GMAIL_SEND_ENABLED

## Active Bugs / Blockers
- GMAIL_SEND_ENABLED=true ✅ live on Render (flipped Phase 4 deploy, Mar 4).
- Email template for payment confirmations not built
- No automated payment reminder system
- Phase 5B complete — main.py decomposed to ~175 lines; checkout_routes.py is now the separate module (~290 lines, sha c9edfeb).

## Next Steps
1. Confirm Square integration still connected (services are on paid Render plan, do NOT cold start).
2. Enable GMAIL_SEND_ENABLED and build payment confirmation template
3. Build automated payment reminder workflow
4. Promote checkout module to production

## Rules & Decisions
- Square is the payment processor — non-negotiable
- Payment links are generated per-order, not per-session
- Gmail scanning is the payment detection mechanism (not webhooks)
- Sandbox is source of truth for all checkout logic
