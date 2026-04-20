# WS6_ENVIRONMENT_RISK.md

**Last updated:** 2026-04-20
**Scope:** environmental and cross-system risks for WS6 (CFC Orders). Each entry is an independent risk with its own status and severity. Closes or downgrades only with evidence.

---

## E-001 — Sandbox CFC backend targets production-class B2BWave tenant

**Status:** OPEN (MITIGATED)
**Severity:** HIGH (downgraded from CRITICAL on 2026-04-20 after Option A guardrails went live)
**Opened:** 2026-04-19
**Authority:** WS6_CFC_ORDERS_SOT.md:299, repo + live runtime evidence

### FRAMING
The CFC backend marketed as "sandbox" is pointed at what is operationally a production-class B2BWave host (`https://cabinetsforcontactors.b2bwave.com`). Real customer data flows through this integration.

### CONFIRMED
- Backend `B2BWAVE_URL` is env-driven with no sandbox/prod distinction in code (`config.py:26`).
- Frontend hardcodes a production-class B2BWave URL in `src/config.js:27`; env override via `VITE_B2BWAVE_ORDER_URL` is supported but not set on sandbox Vercel.
- Live order 5554 (2026-04-18) contains real customer PII — confirms the tenant is populated with real data.
- Live `GET /` on 2026-04-20 reports `email_allowlist_active=true` and `b2bwave_mutations_enabled=false` — previously-enumerated side-effect classes are currently suppressed at the code layer.

### RISK (with current mitigation state)
- Customer-visible email send — **MITIGATED** by `EMAIL_ALLOWLIST` (G1).
- B2BWave order mutation (address update) — **MITIGATED** by `B2BWAVE_MUTATIONS_ENABLED=false` (G2a).
- Auto-cancel on day 21 reaching production tenant — **MITIGATED** by `B2BWAVE_MUTATIONS_ENABLED=false` (G2b).
- Square payment link leakage — **UNMITIGATED** at code layer; relies on `SQUARE_ENVIRONMENT=sandbox` env posture (not re-verified this session).
- Supplier notifications reaching real suppliers — **PARTIALLY MITIGATED** only by `EMAIL_ALLOWLIST` if supplier emails pass through `send_order_email`; supplier-specific override in `config.py SUPPLIER_INFO` is still active.
- Webhook replay fan-out — **UNMITIGATED**; see E-003.
- PII capture in sandbox logs / UI — **UNMITIGATED**; sandbox DB still holds real customer records until sanitise endpoint is live and run.

### RESOLUTION PATH
- Option A (controlled production-integrated testing) — active today, guardrails enforce the boundary at code layer.
- Option B (true sandbox separation) — preparation in progress; cutover requires a B2BWave sandbox tenant (external) plus running `POST /debug/sanitise-sandbox-db` (drafted-only).

---

## E-002 — `B2BWAVE_URL` host spelling mismatch

**Status:** OPEN
**Severity:** LOW–MEDIUM
**Opened:** 2026-04-20

### FRAMING
The backend `B2BWAVE_URL` resolves to `https://cabinetsforcontactors.b2bwave.com` (note: `cont**a**ctors`, missing the second `r`). The frontend hardcoded literal is `https://cabinetsforcontractors.b2bwave.com/orders` (correct spelling). Live order 5554 proves the backend host resolves and serves real data, so the mis-spelled host is a valid B2BWave endpoint of some kind — it is not a broken URL.

### UNKNOWNS
- Whether the mis-spelled host is an intentional alternate B2BWave tenant, a customer-owned domain alias, or a latent typo in the sandbox Render env var.
- Whether admin-UI per-order links (pointed at the correct-spelling host) return valid order pages when the backend is writing against the mis-spelled host.

### RESOLUTION PATH
- Confirm with B2BWave operations whether `cabinetsforcontactors.b2bwave.com` and `cabinetsforcontractors.b2bwave.com` are the same tenant, aliases, or distinct.
- If a typo: correct the sandbox Render `B2BWAVE_URL` env value.
- If intentional alternate: align the frontend literal (or `VITE_B2BWAVE_ORDER_URL` override) so admin-UI links match the backend tenant.

---

## E-003 — `POST /webhook/b2bwave-order` is unauthenticated and replayable

**Status:** OPEN
**Severity:** MEDIUM
**Opened:** 2026-04-20

### FRAMING
The B2BWave order webhook at `checkout_routes.py:635` has no authentication. Any captured payload can be replayed against sandbox or production; the public admin UI is itself documented at SOT:421 as a caller of this endpoint. The first DB write inside the handler (unguarded INSERT into `pending_checkouts`) is not idempotent against replays beyond the `ON CONFLICT (order_id) DO UPDATE` semantic; downstream side effects are not gated against replay at all in the un-patched flow.

### CURRENT MITIGATION
- Downstream side effects (email, B2BWave address update, supplier notifications, auto-cancel) are all gated by the G1/G2 guardrails today, so replay today cannot produce customer-visible damage.
- A proposed webhook-idempotency gate (reject or short-circuit when `pending_checkouts.payment_completed_at IS NOT NULL` for the incoming order_id) was specified in the observability-plan turn but NOT implemented.

### RESIDUAL RISK
- Replay can still create or refresh `pending_checkouts` rows for arbitrary `order_id` values.
- Replay can still trigger fresh `fetch_b2bwave_order` calls against the production tenant (read-only, rate-limit-impacting).
- Once G1/G2 are relaxed (e.g. during Option B testing against a real sandbox tenant), replay becomes active again.

### RESOLUTION PATH
- Implement the deferred webhook idempotency gate in `checkout_routes.py:657` (SELECT `payment_completed_at`; early-return when non-null).
- Longer-term: consider a signed-webhook contract between B2BWave and the backend so the endpoint can reject unauthenticated calls.

---

**End of document.** New entries should append as E-NNN with independent status/severity; existing entries should be annotated in place when their status changes.
