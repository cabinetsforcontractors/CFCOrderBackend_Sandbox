# Order Lifecycle — Session Handoff
**Last Updated:** 2026-02-28
**Workstream:** CFC Orders
**Status:** Paused — blocked on repo access + Render health

## What This Lane Covers
The 7-step order workflow from B2BWave sync through delivery. Status tracking across the full lifecycle, AI-powered order summaries via Claude API, and the planned AlertsEngine for proactive notifications.

## Current State
- **B2BWave sync** works — sync_service.py pulls orders, auto-scheduler runs on interval
- **AI summaries** work — ai_summary.py generates 6-section analysis in ~2 seconds via Claude API
- **Detection module** works — payment/quote/PRO number detection from email content
- **AlertsEngine** NOT built — 8 rules defined in ORDERS_BRAIN/rules.md (ORD-A1) but cron never implemented
- **Status tracking** is basic — no automated state machine or lifecycle transitions

## Key Files
- `CFCOrderBackend_Sandbox/main.py` — 3,144 lines, FastAPI, 60+ endpoints, all lifecycle logic
- `CFCOrderBackend_Sandbox/sync_service.py` — B2BWave order sync + auto-scheduler
- `CFCOrderBackend_Sandbox/ai_summary.py` — Claude API order summaries (6-section)
- `CFCOrderBackend_Sandbox/detection.py` — Payment/quote/PRO detection
- `CFCOrderBackend_Sandbox/email_parser.py` — Email content parsing
- `CFCOrderBackend_Sandbox/gmail_sync.py` — Email scanning and parsing
- `brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md` — v1.2, 7 golden examples, AlertsEngine rules ⚠️ pending migration from WILLIAM_BRAIN
- `brain:WILLIAM_BRAIN/ORDERS_BRAIN/state.md` — Domain state (stale — Feb 1) ⚠️ pending migration from WILLIAM_BRAIN

## Active Bugs / Blockers
- AlertsEngine cron never built (8 rules defined but no implementation)
- state.md is stale (last updated Feb 1)
- No lifecycle state machine — orders don't auto-transition between stages
- Production backend is monolithic and 2 months behind sandbox

## Next Steps
1. Build AlertsEngine cron job implementing the 8 rules from ORD-A1
2. Design lifecycle state machine (order stages + transition triggers)
3. Update state.md to reflect current system
4. Consider event-driven architecture for status changes

## Rules & Decisions
- AI summaries use Claude API (not GPT) — decided and working
- 7-step lifecycle is the canonical model: Sync → Confirm → Pick → Ship → Track → Deliver → Close
- AlertsEngine rules are defined in ORD-A1 — these are non-negotiable requirements
- Sandbox is source of truth — production gets promoted FROM sandbox, never the reverse
