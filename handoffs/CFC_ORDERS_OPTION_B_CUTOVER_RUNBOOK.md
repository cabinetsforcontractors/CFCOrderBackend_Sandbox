# CFC Orders — Option B Cutover Runbook

**Audience:** CFC operator executing the cutover with no prior session context
**Authority:** WS6_CFC_ORDERS_SOT.md, WS6_CURRENT_STATE.md, WS6_ENVIRONMENT.md, WS6_ENVIRONMENT_RISK.md
**Last updated:** 2026-04-21

---

## 1. FRAMING

This runbook executes the cutover of the CFC Orders sandbox from **Option A (production-integrated, guardrails ON)** to **Option B (true sandbox separation)**. At the end of the procedure the sandbox CFC backend and frontend will be pointed at a B2BWave sandbox tenant with synthetic data, guardrails can be relaxed, and the production B2BWave tenant will be untouched by any sandbox action.

The runbook is a single-operator-session procedure — all steps are reversible up to and including Step 4.3 (DB sanitise), after which rollback requires a DB restore from the backup taken in Step 4.1.

**Endpoint readiness as of 2026-04-21:**
- `GET /debug/env-readiness` — LIVE, admin-gated.
- `POST /debug/sanitise-sandbox-db` — LIVE-DEPLOYED, admin-gated, UN-INVOKED. Requires header `X-Allow-Destructive: yes`.

---

## 2. PRECONDITIONS

**Do not begin the cutover until every precondition is satisfied.** Each is a hard gate.

### 2.1 — B2BWave sandbox credentials
- A separate B2BWave **sandbox tenant** has been provisioned by B2BWave.
- The sandbox tenant has at least one synthetic test customer with a known `customer_id`.
- Three credential values are available and stored in a secure location:
  - **`B2BWAVE_URL`** — the sandbox tenant API host (e.g. `https://cfc-sandbox.b2bwave.com`). **Must not** equal the current production-class value (`https://cabinetsforcontactors.b2bwave.com`).
  - **`B2BWAVE_USERNAME`** — sandbox API username.
  - **`B2BWAVE_API_KEY`** — sandbox API key.

### 2.2 — Render access
- Admin access to the sandbox Render service `cfcorderbackend-sandbox` (service ID per `handoffs/CFC_ORDERS_BATTLE_PLAN.md`: `srv-d4tu1e24d50c73b6952g`).
- Ability to edit environment variables and trigger a redeploy.
- The admin API token (`X-Admin-Token: CFC2026` per WS6_ENVIRONMENT.md) is available.

### 2.3 — Vercel access
- Admin access to `cfcordersfrontend-sandbox` Vercel project.
- Ability to set project env var `VITE_B2BWAVE_ORDER_URL` and trigger a rebuild.

### 2.4 — DB backup capability
- Render Postgres snapshot feature enabled on the sandbox DB, OR the ability to run `pg_dump` against the sandbox DB from an operator machine.
- A target location with sufficient space to hold a full backup of the sandbox DB is identified and writable.

### 2.5 — Endpoint readiness (confirmed live on 2026-04-21)
- `GET /debug/env-readiness` is live on sandbox Render.
- `POST /debug/sanitise-sandbox-db` is live on sandbox Render, admin-gated, and protected by the `X-Allow-Destructive: yes` header. **Never invoked yet.**
- If either endpoint returns 404 when called with the admin token, stop and confirm the backend is running the expected commit (`migration_routes.py` sha `cc2526a7` or later).

### 2.6 — Current guardrail state
- `GET /` must report `email_allowlist_active: true` AND `b2bwave_mutations_enabled: false`. If either is not in that state, do not proceed — fix Option A posture first.

---

## 3. CURRENT STATE (baseline)

Verify before any changes that the starting point matches the expected baseline.

- **Backend service**: `https://cfcorderbackend-sandbox.onrender.com` — running, responding 200 on `/` and `/health`.
- **Frontend service**: `https://cfcordersfrontend-sandbox.vercel.app` — running, admin UI loads.
- **Backend points at production-class B2BWave**: `b2bwave_target` in `GET /` shows `https://cabinetsforcontactors.b2bwave.com` (per WS6_ENVIRONMENT_RISK.md E-001 and E-002).
- **Guardrails active (Option A)**:
  - `email_allowlist_active: true` — no customer emails leak from sandbox (covers both `email_sender.send_order_email` path and `checkout_routes._send_gmail_message` path per G1 + G4).
  - `b2bwave_mutations_enabled: false` — `update_b2bwave_order_address` and `cancel_order_on_b2bwave` are killed.
- **Functional baseline (positive control)**: order `5554` returns fully populated freight order with 2 shipments (confirms Option A guardrails have not broken order ingestion or display paths).
- **Sandbox DB**: populated with real customer PII sourced from the production-class B2BWave tenant (see WS6_ENVIRONMENT_RISK.md E-001 §RISK).
- **Admin auth**: `X-Admin-Token: CFC2026` per sandbox Render `ADMIN_API_KEY`.

---

## 4. CUTOVER STEPS (ordered, exact)

Execute in order. Do not skip. Each step has a check — if the check fails, stop and go to §7 Rollback.

### 4.1 — Backup the sandbox Postgres DB

**Reason:** the populated sandbox DB contains real customer PII which will be destroyed in Step 4.3. This is the only reversal point for that step.

**Action:**
- On Render dashboard, take a manual snapshot of the sandbox Postgres instance, OR run `pg_dump --format=custom --file=cfc-orders-sandbox-pre-cutover-$(date +%Y%m%d-%H%M%S).dump` from an operator machine.
- Confirm the snapshot/dump file is accessible and non-empty.
- Record the snapshot ID or file path in the session log.
- **Record the pre-cutover `B2BWAVE_URL`, `B2BWAVE_USERNAME`, `B2BWAVE_API_KEY` values** in the session log before §4.2 — required for §7.1 rollback.

**Check:** backup file exists and is > 1 MB (populated DB is larger than an empty schema).

### 4.2 — Set sandbox Render env vars (backend)

**Reason:** swap the B2BWave tenant pointer from production-class to sandbox.

**Action (Render dashboard → `cfcorderbackend-sandbox` → Environment):**
- Set `B2BWAVE_URL` = sandbox tenant host from §2.1.
- Set `B2BWAVE_USERNAME` = sandbox tenant username from §2.1.
- Set `B2BWAVE_API_KEY` = sandbox tenant API key from §2.1.
- Leave `EMAIL_ALLOWLIST`, `INTERNAL_SAFETY_EMAIL`, `B2BWAVE_MUTATIONS_ENABLED` **unchanged** at this step (guardrails stay on until validation passes).
- Trigger redeploy.

**Check:** Render deployment completes green; service returns 200 on `/health`.

### 4.3 — Sanitise the sandbox DB

**Reason:** remove all production-sourced PII from `orders`, `order_shipments`, `order_events`, `order_email_snippets`, `order_alerts`, `order_line_items`, `pending_checkouts`. This is **destructive** and **irreversible without the Step 4.1 backup**.

**Action:**
```
POST https://cfcorderbackend-sandbox.onrender.com/debug/sanitise-sandbox-db
Headers:
  X-Admin-Token: CFC2026
  X-Allow-Destructive: yes
```

**Expected response (HTTP 200):**
```json
{
  "status": "ok",
  "truncated": [
    "order_shipments",
    "order_email_snippets",
    "order_events",
    "order_alerts",
    "order_line_items",
    "orders",
    "pending_checkouts"
  ],
  "seeded": []
}
```

**Intentional behaviour:**
- `seeded: []` is expected — this endpoint does NOT auto-re-seed `warehouse_mapping` or `trusted_customers` (by design; see §4.3a below).
- No `CASCADE` is used; all 7 tables are truncated in one multi-table statement.

**Check:** response is 200 with exactly the 7 tables listed above in `truncated`. Any other shape, any non-200, or any Postgres error → stop and go to §7.

### 4.3a — Manually clear `trusted_customers` PII

**Reason:** `trusted_customers` is intentionally not auto-truncated by the sanitise endpoint. The canonical seed in `schema.py` contains real customer names (PII) which must not persist on a true-sandbox DB.

**Action (admin-gated via any psql or Render Postgres console):**
```sql
DELETE FROM trusted_customers;
```

**Check:** `SELECT COUNT(*) FROM trusted_customers;` returns 0.

### 4.3b — Confirm `warehouse_mapping` preserved

**Reason:** `warehouse_mapping` is operational configuration (SKU prefix → warehouse routing) required for order ingestion. The sanitise endpoint intentionally leaves it untouched. If the new B2BWave sandbox tenant uses identical SKU prefixes, no action is needed. If it uses different prefixes, manually insert new rows before enabling webhook traffic.

**Check:** `SELECT COUNT(*) FROM warehouse_mapping;` returns ~66 rows (canonical count).

### 4.4 — Verify readiness endpoint reports sandbox posture

**Reason:** confirm the env-var swap is effective in the running process before relaxing guardrails.

**Action:**
```
GET https://cfcorderbackend-sandbox.onrender.com/debug/env-readiness
Headers:
  X-Admin-Token: CFC2026
```

**Expected JSON:**
```json
{
  "b2bwave_target": "<sandbox host from §2.1>",
  "matches_production_literal": false,
  "matches_sandbox_pattern": true,
  "email_allowlist_active": true,
  "b2bwave_mutations_enabled": false,
  "recommended_posture": "ready_for_guardrail_relaxation"
}
```

**Check:** `matches_production_literal` is `false` AND `matches_sandbox_pattern` is `true` AND `recommended_posture` is `"ready_for_guardrail_relaxation"`. If any is wrong → §7.

### 4.5 — Set sandbox Vercel env var (frontend)

**Reason:** the admin UI renders per-order links to B2BWave; point them at the sandbox tenant so operators do not click through to production by accident.

**Action (Vercel dashboard → `cfcordersfrontend-sandbox` → Settings → Environment Variables):**
- Set `VITE_B2BWAVE_ORDER_URL` = the sandbox tenant order listing URL (derived from §2.1 host — typically `<B2BWAVE_URL>/orders`).
- Trigger redeploy.

**Check:** the Vercel redeploy completes; loading an order in the admin UI shows a per-order link whose host matches the sandbox tenant.

### 4.6 — Relax Option A guardrails (optional, only if desired)

**Reason:** with the sandbox tenant confirmed, the production-safety guardrails (`EMAIL_ALLOWLIST`, `B2BWAVE_MUTATIONS_ENABLED=false`) are no longer strictly required to prevent production impact. An operator may choose to relax them for full Option B testing fidelity.

**Action (Render dashboard → backend env):**
- Optionally unset `EMAIL_ALLOWLIST` (or keep it on — synthetic-customer-only testing is fine either way).
- Optionally set `B2BWAVE_MUTATIONS_ENABLED=true` to allow address updates and auto-cancel to reach the sandbox tenant.
- Redeploy.

**Check:** subsequent `GET /` reflects the new posture.

---

## 5. VALIDATION CHECKS

All calls use `X-Admin-Token: CFC2026` unless stated otherwise. Run all six checks in order after Step 4.5 (or 4.6 if guardrails were relaxed).

### 5.1 — Service up
```
GET https://cfcorderbackend-sandbox.onrender.com/health
Expected: 200 {"status":"ok","version":"6.5.0"}
```

### 5.2 — Env readiness
```
GET https://cfcorderbackend-sandbox.onrender.com/debug/env-readiness
Expected: matches_sandbox_pattern=true, matches_production_literal=false
```

### 5.3 — Root reports sandbox posture
```
GET https://cfcorderbackend-sandbox.onrender.com/
Expected: b2bwave_target equals sandbox host set in §2.1
```

### 5.4 — DB is clean
```
GET https://cfcorderbackend-sandbox.onrender.com/debug/orders-columns
Expected: all nine baseline tables still present, all address_* columns on pending_checkouts still present (structure unchanged by sanitise)

GET https://cfcorderbackend-sandbox.onrender.com/orders?limit=5
Expected: empty list OR only synthetic test orders (no real PII)
```

### 5.5 — Webhook round-trip against sandbox tenant
- From B2BWave sandbox console, submit a test order with a synthetic customer.
- Within ~3 minutes, query `GET /orders?limit=5` — expect the synthetic order to appear.
- Query `GET /orders/<new_id>/events` — expect at least one `b2bwave_sync` event.
- **Check:** synthetic order materialises end-to-end without touching the production tenant.

### 5.6 — Frontend admin UI
- Load `https://cfcordersfrontend-sandbox.vercel.app`, log in (`APP_PASSWORD=cfc2025`).
- Open the synthetic order from 5.5.
- **Check:** the per-order B2BWave link points at the sandbox tenant host, not production.

---

## 6. FAILURE CONDITIONS

Any of the following indicates the cutover has failed and must be rolled back per §7.

- §4.2 redeploy fails or `/health` returns non-200.
- §4.3 sanitise endpoint returns non-200 or its response shows any table missing from `truncated`.
- §4.3a `DELETE FROM trusted_customers` fails or the row count does not drop to 0.
- §4.4 readiness returns `matches_production_literal: true` — env swap did not take effect.
- §4.4 readiness returns `matches_sandbox_pattern: false` — env var was set to a wrong value.
- §5.4 `/orders?limit=5` returns any row with real customer PII after sanitise (indicates partial or failed truncate).
- §5.5 the synthetic B2BWave order never appears in `/orders` after 10 minutes (sync path broken).
- §5.5 `GET /orders/<new_id>/events` shows an event against a host that is not the §2.1 sandbox host (sync still talking to production).
- §5.6 the admin-UI per-order link points at the production B2BWave host after the Vercel redeploy (frontend env swap did not take effect).
- Any `[B2BWAVE-WARN] LIVE mutation committed` log line in Render stdout referencing a host other than the §2.1 sandbox host after §4.6.

---

## 7. ROLLBACK PLAN

Rollback complexity depends on how far into §4 the cutover progressed when it failed.

### 7.1 — Failure before §4.3 (DB sanitise not yet run)

**Impact:** env-var level only. Sandbox DB still contains production data.

**Steps:**
- Render → backend env: restore `B2BWAVE_URL`, `B2BWAVE_USERNAME`, `B2BWAVE_API_KEY` to the production-class values recorded in the §4.1 session log.
- Redeploy.
- Confirm `GET /` reports the production-class `b2bwave_target` again.
- Vercel (if §4.5 ran): unset `VITE_B2BWAVE_ORDER_URL` so the frontend falls back to the hardcoded production literal; redeploy.
- Stop. Investigate root cause before re-attempting cutover.

### 7.2 — Failure at or after §4.3 (DB sanitise has run)

**Impact:** sandbox DB has been truncated. Data must be restored from the §4.1 backup.

**Steps:**
- Render → sandbox Postgres: restore from the snapshot/dump captured in §4.1 (Render dashboard "Restore" feature, or `pg_restore` from the operator machine).
- Render → backend env: restore `B2BWAVE_URL`, `B2BWAVE_USERNAME`, `B2BWAVE_API_KEY` to the pre-§4.2 production-class values.
- Confirm `EMAIL_ALLOWLIST`, `INTERNAL_SAFETY_EMAIL`, `B2BWAVE_MUTATIONS_ENABLED=false` are all still set — Option A guardrails must be reinstated before any further work.
- Redeploy.
- Vercel (if §4.5 ran): unset `VITE_B2BWAVE_ORDER_URL`; redeploy.
- Confirm `GET /` returns the Option A baseline posture (production-class `b2bwave_target`, guardrails ON).
- Confirm `GET /orders?limit=5` returns the pre-sanitise data (post-restore).
- Stop. Do not retry cutover without first understanding and fixing the root cause.

---

## 8. POST-CUTOVER STATE

When cutover completes successfully (all §5 checks pass), the system should present as follows.

- **`GET /` returns:**
  - `b2bwave_target` = sandbox tenant host from §2.1 (not the production-class literal).
  - `email_allowlist_active` = `true` (if §4.6 was skipped) or `false` (if relaxed).
  - `b2bwave_mutations_enabled` = `false` (if §4.6 was skipped) or `true` (if relaxed).
- **`GET /debug/env-readiness` returns:**
  - `matches_production_literal: false`
  - `matches_sandbox_pattern: true`
  - `recommended_posture: "ready_for_guardrail_relaxation"` or `"unknown"` depending on §4.6 outcome.
- **Sandbox DB:** contains only synthetic data originating from the B2BWave sandbox tenant. Real customer PII from the production-class tenant has been removed. `trusted_customers` is empty. `warehouse_mapping` is preserved.
- **Sandbox frontend:** admin-UI per-order links point at the sandbox tenant.
- **Render logs (stdout):** any `[B2BWAVE-WARN] LIVE mutation committed ...` lines reference only the sandbox tenant host.
- **Production B2BWave tenant:** unchanged, untouched by any sandbox activity from cutover moment onward.
- **WS6_ENVIRONMENT_RISK.md E-001:** may be closed once post-cutover state has held for one full business day without any production-tenant traffic observed in `[B2BWAVE-WARN]` logs.
- **Next-step owner:** operator updates WS6_CURRENT_STATE.md to reflect Option B live state and notes the cutover completion in the session log.

---

**End of runbook.**
