# WS6 — CFC Orders Session Handoff
**Date:** 2026-03-07
**Task:** Scope audit — discovered 9 missing lanes from chat history; all added to WS6_CFC_ORDERS.md

---

## ✅ What Was Done This Session

### Scope Audit Complete
Searched 15+ past chats (Dec 2025 → Mar 2026) and found 9 lanes built or spec'd but never tracked in WS6. All added to `brain:workstreams/WS6_CFC_ORDERS.md` (sha c51bb0ba).

| Lane | Priority | Finding |
|------|----------|---------|
| A — Shippo Small Package | P1 | BUILT + LIVE — untested in checkout flow |
| B — Payment Automation Triggers | P1 | Partial — triggers not wired |
| C — RTA Weight Integration | P2 | ⛔ BLOCKED on WS5 canonical master |
| D — Production Promotion (Phase 7) | P2 | Full checklist written — Option A chosen |
| E — Warehouse Portal | P3 | Spec'd, not built |
| F — Customer Tracking Portal | P3 | Spec'd, not built |
| G — Multi-Warehouse Unified Checkout | P3 | Design agreed, not built |
| H — DYLT / CA Carrier | P4 | Noted, not started |
| I — Frontend UX Bugs | P4 | 2 bugs noted, may already be fixed |

### Key Decisions Made This Session
| Decision | Detail |
|----------|--------|
| Phase 7 strategy | **Option A** — repoint production Render/Vercel to sandbox repos (`4wprince/CFCOrderBackend_Sandbox` + `4wprince/CFCOrdersFrontend_Sandbox`) |
| Production API key | **`ADMIN_API_KEY=CFC2026`** — set on Render prod; update `api.js` X-Admin-Token from CFC2025 → CFC2026 before frontend deploy |
| Lane C blocked | Depends on WS5 canonical master cleanup — do not touch until WS5 signals complete |

---

## What's Next (priority order)

1. **Phase 5 sandbox verify** — PATCH /orders/{id}, Run Check POST, Reactivate POST all → 200 not 401
2. **Phase 5B** — rate limiting (slowapi)
3. **R+L end-to-end test** — /rl/test → /rl/order/{id}/shipments → /rl/order/{id}/create-bol → PDF/labels → pickup → track → notify → emails
4. **Phase 7 (Lane D)** — execute Option A checklist in dedicated session after Phase 5 complete
5. **Lane A (Shippo)** — test full checkout flow for a <70 lb order
6. **Lane B (Payment Automation)** — verify all 4 triggers fire in sandbox

---

## Phase Completion Summary

| Phase | Status | Key Deliverables |
|-------|--------|----------------|
| Phase 1: Cleanup | ✅ DONE | Dead files removed |
| Phase 2: RL-Quote | ✅ DONE | MCP v2.6, 12 warehouses, LI zip fixed |
| Phase 3A: AlertsEngine | ✅ DEPLOYED | 8 rules, tz bug fixed |
| Phase 3B: Lifecycle | ✅ DEPLOYED | DB migrated, 15 orders backfilled, 7/14/21 days |
| Phase 3C: Frontend Alerts | ✅ DONE | Bell badge, dropdown, resolve/dismiss |
| Phase 4: Email Comms | ✅ DEPLOYED | 9 templates, GMAIL_SEND_ENABLED=true |
| Phase 5B: Decompose main.py | ✅ DONE | 1,233 → 175 lines, 4 route modules |
| Phase 5C: api.js | ✅ DONE | All 29 fetch() centralized, sha 0c498013 |
| Phase 5 Hardening | 🔥 IN PROGRESS | Sandbox verify + rate limiting + JWT rotation |
| Phase 6: Frontend Redesign | ✅ DONE | App.jsx v7.2.2 dark theme live |
| Phase 7: Production Promotion | NOT STARTED | Option A + CFC2026 key — awaiting Phase 5 completion |

---

## Architecture (Phase 5 Complete)

```
main.py (~175 lines — app init only)
├── rl_quote_proxy.py     — /proxy/*
├── alerts_routes.py      — /alerts/*
├── startup_wiring.py     — lifecycle + email + ai_configure
├── orders_routes.py      — /orders /shipments /warehouse-mapping /trusted-customers
├── shipping_routes.py    — /rl /shippo /rta
├── detection_routes.py   — /parse-email /detect-*          [Phase 5B]
├── sync_routes.py        — /b2bwave/* /gmail/* /square/*   [Phase 5B]
├── migration_routes.py   — /init-db /add-* /fix-* /debug/* [Phase 5B]
└── checkout_routes.py    — /checkout* /webhook/*            [Phase 5B]
```

---

## Key Files

| File | SHA | Purpose |
|------|-----|---------|
| `cfc-orders-frontend:src/api.js` | 0c498013 | apiFetch() — X-Admin-Token: CFC2025 (→ CFC2026 at Phase 7) |
| `cfc-orders-frontend:src/App.jsx` | e020e868 | v7.2.2 dark theme |
| `cfc-orders:main.py` | 93db3a0b | ~175 lines app init |
| `cfc-orders:tests/rl_test_harness.py` | 3fd9f79 | 521 lines — R+L validation harness |
| `cfc-orders:handoffs/SANDBOX_VS_PRODUCTION_AUDIT.md` | a139452f | Full sandbox vs prod gap analysis |
| `brain:workstreams/WS6_CFC_ORDERS.md` | c51bb0ba | Full 14-lane WS6 workstream file |

---

## Critical Reminders
- `api.js` token is still `CFC2025` in sandbox — must flip to `CFC2026` as part of Phase 7 Step 3, not before
- Sandbox and production **share the same PostgreSQL DB** — migrations in sandbox hit production
- Do NOT point real orders at production until Phase 7 checklist is complete
- Lane C (RTA Weight) is blocked on WS5 — do not attempt to re-test until WS5 signals complete
- Blind shipping via R+L = $106/shipment — rejected, do not revisit
