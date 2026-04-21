# WS6_ENVIRONMENT_RISK.md

**Last updated:** 2026-04-21
**Scope:** environmental and cross-system risks for WS6 (CFC Orders). Each entry is an independent risk with its own status and severity. Closes or downgrades only with evidence.

---

## E-001 — Sandbox CFC backend targets production-class B2BWave tenant

**Status:** OPEN (MITIGATED)
**Severity:** HIGH (downgraded from CRITICAL on 2026-04-20 after Option A guardrails went live; email-egress coverage completed 2026-04-21 with G4)
**Opened:** 2026-04-19
**Authority:** WS6_CFC_ORDERS_SOT.md:299, repo + live runtime evidence

### FRAMING
The CFC backend marketed as "sandbox" is pointed at what is operationally a production-class B2BWave host (`https://cabinetsforcontactors.b2bwave.com`). Real customer data flows through this integration.

### CONFIRMED
- Backend `B2BWAVE_URL` is env-driven with no sandbox/prod distinction in code (`config.py:26`).
- Frontend hardcodes a production-class B2BWave URL in `src/config.js:27`; env override via `VITE_B2BWAVE_ORDER_URL` is supported but not set on sandbox Vercel.
- Live order 5554 (2026-04-18) contains real customer PII — confirms the tenant is populated with real data.
- Live `GET /` and `GET /debug/env-readiness` on 2026-04-21 report `email_allowlist_active=true`, `b2bwave_mutations_enabled=false`, `recommended_posture="safe_option_a"` — previously-enumerated side-effect classes are suppressed at the code layer.
- All three code-level email egress paths are now covered:
  - Path A (`email_sender.send_order_email`) — G1 active.
  - Path B (`gmail_sender.send_email`) — DEAD (module absent from repo; imports fail silently).
  - Path C (`checkout_routes._send_gmail_message`) — G4 active as of commit `15fef2cc` (2026-04-21).

### RISK (with current mitigation state)
- Customer-visible email send via `email_sender.send_order_email` — **MITIGATED** by `EMAIL_ALLOWLIST` (G1).
- Customer-visible email send via `_send_gmail_message` (verify-address, commercial-confirm, admin/quote_engine sends) — **MITIGATED** by `EMAIL_ALLOWLIST` (G4, 2026-04-21).
- Lifecycle/quote-reminder email sends via `gmail_sender.send_email` — **MITIGATED** by absence of the `gmail_sender` module (dead path). Revalidate if `gmail_sender.py` is ever introduced.
- B2BWave order mutation (address update) — **MITIGATED** by `B2BWAVE_MUTATIONS_ENABLED=false` (G2a).
- Auto-cancel on day 21 reaching production tenant — **MITIGATED** by `B2BWAVE_MUTATIONS_ENABLED=false` (G2b).
- Square payment link leakage — **UNMITIGATED** at code layer; relies on `SQUARE_ENVIRONMENT=sandbox` env posture (not re-verified this session).
- Supplier notifications reaching real suppliers via `supplier_polling_engine` — **PARTIALLY MITIGATED** only where the send passes through `send_order_email` or `_send_gmail_message`; supplier-specific sends through other modules remain unaudited for this session.
- Webhook replay fan-out — **UNMITIGATED**; see E-003.
- PII capture in sandbox logs / UI — **UNMITIGATED**; sandbox DB still holds real customer records until sanitise endpoint is invoked on cutover day.

### RESOLUTION PATH
- Option A (controlled production-integrated testing) — active today; guardrails enforce the boundary at code layer. Fit-state verified live.
- Option B (true sandbox separation) — preparation complete on the repo side: shared prerequisites P1–P4, guardrails G1–G4, readiness endpoint, sanitise endpoint, cutover runbook, and smoke tests are all in place. Cutover gated on external B2BWave sandbox tenant provisioning.

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
- Downstream side effects:
  - Email sends (customer invoice, verify-address, commercial-confirm, internal notifications) — gated by G1 + G4. Replay cannot leak emails while allowlist is active.
  - B2BWave address update / auto-cancel — gated by G2a + G2b. Replay cannot mutate the production tenant while kill-switch is engaged.
- Thus replay today cannot produce customer-visible damage via email or B2BWave write.
- A proposed webhook-idempotency gate (reject or short-circuit when `pending_checkouts.payment_completed_at IS NOT NULL` for the incoming order_id) was specified in the observability-plan turn but NOT implemented.

### RESIDUAL RISK
- Replay can still create or refresh `pending_checkouts` rows for arbitrary `order_id` values.
- Replay can still trigger fresh `fetch_b2bwave_order` calls against the production tenant (read-only, rate-limit-impacting).
- Once G1/G2/G4 are relaxed (e.g. during Option B testing against a real sandbox tenant), replay becomes active again against the sandbox tenant.

### RESOLUTION PATH
- Implement the deferred webhook idempotency gate in `checkout_routes.py:657` (SELECT `payment_completed_at`; early-return when non-null).
- Longer-term: consider a signed-webhook contract between B2BWave and the backend so the endpoint can reject unauthenticated calls.

---

**End of document.** New entries should append as E-NNN with independent status/severity; existing entries should be annotated in place when their status changes.
