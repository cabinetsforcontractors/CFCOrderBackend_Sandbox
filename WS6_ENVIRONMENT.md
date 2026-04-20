# WS6_ENVIRONMENT.md

**Last updated:** 2026-04-20

## LOCAL DEVELOPMENT ENVIRONMENT

### Repo Location
C:\dev\CFCOrderBackend_Sandbox

### Primary Branch
main

### Git Remote
https://github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox.git

### Key Files
- SOT: WS6_CFC_ORDERS_SOT.md
- Current State: WS6_CURRENT_STATE.md

### Deployment
- Sandbox Base URL:
  https://cfcorderbackend-sandbox.onrender.com

### Admin Auth (SANDBOX ONLY)
- Header: X-Admin-Token
- Known working value: CFC2026
- `CFC2026` remains valid as of 2026-04-20.
- `ADMIN_API_KEY` env has NOT been rotated this session.

### Single Source of Truth (Tenant)
- Backend reads `B2BWAVE_URL` exclusively via `from config import B2BWAVE_URL`.
- Duplicate env reads in `checkout.py` and `lifecycle_engine.py` were removed by P1 (2026-04-20).
- `config.py:26` is the one switch point for any future tenant swap.

### Backend env vars (sandbox Render)
- `B2BWAVE_URL` — currently resolves to `https://cabinetsforcontactors.b2bwave.com`.
  - Flag: spelling discrepancy vs frontend literal `cabinetsforcontractors.b2bwave.com` — tracked under E-001 / E-002 in WS6_ENVIRONMENT_RISK.md.
- `B2BWAVE_USERNAME` / `B2BWAVE_API_KEY` — set (values not inspected).
- `EMAIL_ALLOWLIST` — ACTIVE (non-empty); gates all `send_order_email` recipients.
- `B2BWAVE_MUTATIONS_ENABLED` — set to `false`; kills address-update and auto-cancel writes.
- `INTERNAL_SAFETY_EMAIL` — UNKNOWN (not verified this session).
- `ADMIN_API_KEY` — set to a value accepted as `CFC2026`.
- `ADMIN_JWT_SECRET` — UNKNOWN (optional, JWT fallback path).
- `CHECKOUT_SECRET`, `CHECKOUT_BASE_URL`, Square creds, RL_QUOTE_API_URL — presumed set (not re-verified).

### Frontend env (sandbox Vercel)
- `VITE_B2BWAVE_ORDER_URL` — NOT SET. Frontend still resolves to hardcoded production literal `https://cabinetsforcontractors.b2bwave.com/orders` (P3 fallback path).

### New root response keys (as of 2026-04-20)
- `b2bwave_target`
- `email_allowlist_active`
- `b2bwave_mutations_enabled`

### New admin endpoint
- `GET /debug/env-readiness` — returns structured posture verdict including `recommended_posture`.

### Notes
- This file is environment-specific
- Do not treat as system logic
- Used to eliminate path discovery and command friction
