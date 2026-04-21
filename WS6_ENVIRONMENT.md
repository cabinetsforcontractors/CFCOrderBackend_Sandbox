# WS6_ENVIRONMENT.md

**Last updated:** 2026-04-21

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
- Risk register: WS6_ENVIRONMENT_RISK.md
- Option B cutover runbook: handoffs/CFC_ORDERS_OPTION_B_CUTOVER_RUNBOOK.md (LIVE in repo as of 2026-04-21, commit `785ba956`)
- Email-guard smoke test: test_ws6_email_guard.py — asserts `recommended_posture=safe_option_a`; does NOT send email
- Pre-existing sandbox smoke test: test_ws6_sandbox.py

### Deployment
- Sandbox Base URL:
  https://cfcorderbackend-sandbox.onrender.com

### Admin Auth (SANDBOX ONLY)
- Header: X-Admin-Token
- Known working value: CFC2026
- `CFC2026` remains valid as of 2026-04-21.
- `ADMIN_API_KEY` env has NOT been rotated this session.

### Single Source of Truth (Tenant)
- Backend reads `B2BWAVE_URL` exclusively via `from config import B2BWAVE_URL`.
- Duplicate env reads in `checkout.py` and `lifecycle_engine.py` were removed by P1 (2026-04-20).
- `config.py:26` is the one switch point for any future tenant swap.

### Backend env vars (sandbox Render)
- `B2BWAVE_URL` — currently resolves to `https://cabinetsforcontactors.b2bwave.com`.
  - Flag: spelling discrepancy vs frontend literal `cabinetsforcontractors.b2bwave.com` — tracked under E-001 / E-002 in WS6_ENVIRONMENT_RISK.md.
- `B2BWAVE_USERNAME` / `B2BWAVE_API_KEY` — set (values not inspected).
- `EMAIL_ALLOWLIST` — ACTIVE (non-empty); gates all email sends via `email_sender.send_order_email` (G1) AND `checkout_routes._send_gmail_message` (G4, live as of 2026-04-21).
- `B2BWAVE_MUTATIONS_ENABLED` — set to `false`; kills address-update and auto-cancel writes.
- `INTERNAL_SAFETY_EMAIL` — UNKNOWN (not verified this session).
- `ADMIN_API_KEY` — set to a value accepted as `CFC2026`.
- `ADMIN_JWT_SECRET` — UNKNOWN (optional, JWT fallback path).
- `CHECKOUT_SECRET`, `CHECKOUT_BASE_URL`, Square creds, RL_QUOTE_API_URL — presumed set (not re-verified).

### Frontend env (sandbox Vercel)
- `VITE_B2BWAVE_ORDER_URL` — NOT SET. Frontend still resolves to hardcoded production literal `https://cabinetsforcontractors.b2bwave.com/orders` (P3 fallback path).

### Email sender paths (current coverage)
- Path A — `email_sender.send_order_email` — guarded by G1 (`EMAIL_ALLOWLIST`).
- Path B — `gmail_sender.send_email` — DEAD PATH. `gmail_sender.py` module does not exist in repo; import fails silently in `lifecycle_engine.py`.
- Path C — `checkout_routes._send_gmail_message` — guarded by G4 (`EMAIL_ALLOWLIST`), live as of 2026-04-21 commit `15fef2cc`.

### Root response keys (as of 2026-04-20)
- `b2bwave_target`
- `email_allowlist_active`
- `b2bwave_mutations_enabled`

### Admin endpoints (current live state)
- `GET /debug/env-readiness` — LIVE. Returns structured posture verdict including `recommended_posture`. Current live value: `safe_option_a`.
- `POST /debug/sanitise-sandbox-db` — LIVE, UN-INVOKED. Admin-gated + `X-Allow-Destructive: yes` header required. Truncates customer-data tables only; `warehouse_mapping` preserved by design; `trusted_customers` preserved (manual `DELETE` required on cutover day per runbook §4.3a).

### Notes
- This file is environment-specific
- Do not treat as system logic
- Used to eliminate path discovery and command friction
