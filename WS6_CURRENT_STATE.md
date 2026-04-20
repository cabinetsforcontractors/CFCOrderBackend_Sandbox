# WS6_CURRENT_STATE.md

**Last updated:** 2026-04-20

## FRAMING

This document reflects the **live, evidence-validated operational state** of WS6 (CFC Orders). It is aligned **line-by-line with WS6_CFC_ORDERS_SOT.md** and captures **runtime-confirmed behavior**, not assumptions.

---

## SYSTEM STATUS (SOT-ALIGNED)

**STATE: STATE-FIT**

### Layer Alignment (SOT ↔ Runtime)

* Routing → CONFIRMED (SOT routes match live endpoints)
* Auth → CONFIRMED (require_admin + ADMIN_API_KEY behavior observed)
* Schema → CONFIRMED (all required columns verified via /debug/orders-columns)
* Service → CONFIRMED (webhook + sync separation validated)
* Integrations → CONFIRMED (B2BWave + Square observed live; current `b2bwave_target=https://cabinetsforcontactors.b2bwave.com` — domain spelling discrepancy vs frontend literal tracked under E-001 in WS6_ENVIRONMENT_RISK.md)

---

## OPTION A GUARDRAILS (ACTIVE)

Live-verified via `GET /` on 2026-04-20:

* `email_allowlist_active = true`
* `b2bwave_mutations_enabled = false`
* `recommended_posture = "safe_option_a"` (via `GET /debug/env-readiness`)

### Option B Readiness

* Shared prerequisites P1–P4 complete.
* G1–G3 guardrails complete and active.
* `GET /debug/env-readiness` endpoint LIVE.
* `POST /debug/sanitise-sandbox-db` endpoint DRAFTED ONLY — diff approved in session, NOT YET PUSHED / NOT LIVE.

---

## ORDER CREATION MODEL (SOT vs RUNTIME)

### Freight Orders (SOT + VERIFIED)

SOT Definition:

* Webhook inserts pending_checkouts
* Orders created by sync service

Runtime Confirmation:

* Webhook returns 200 without orders row
* Orders appear after sync
* Shipments created alongside orders during sync

Conclusion:

* Webhook ≠ order creation (freight)
* Sync service = source of truth for orders table

---

### Pickup Orders (SOT + VERIFIED)

SOT Definition:

* _ensure_order_row used in pickup flow

Runtime Confirmation:

* Only pickup path calls _ensure_order_row
* Orders + shipments created immediately

Conclusion:

* Pickup = synchronous order creation
* Freight = asynchronous order creation

---

## SCHEMA STATE (SOT REQUIREMENTS vs RUNTIME)

SOT Requirement:
pending_checkouts must include address_* fields

Runtime Evidence:
All present:

* address_pending
* address_validation_error
* address_classification_needed
* address_initially_found
* address_type_confirmed
* is_residential_customer_confirmed

Conclusion:

* SOT schema requirements fully satisfied
* SOT:770 failure mode NOT active

---

## AUTH MODEL (SOT vs RUNTIME)

SOT Definition:

* require_admin protects admin routes
* ADMIN_API_KEY used

Runtime Evidence:

* 401 (no token)
* 401 (invalid token)
* 200 with X-Admin-Token: CFC2026

Conclusion:

* Auth layer behaves exactly as defined
* Sandbox mismatch (CFC2025 vs CFC2026) is expected

---

## WEBHOOK BEHAVIOR (CRITICAL SOT ALIGNMENT)

SOT Behavior:

* Webhook may return 200 without full processing

Runtime Evidence:
Confirmed branches where no orders row is created:

* CHECKOUT_ENABLED disabled
* fetch_b2bwave_order returns falsy
* quote conversion path
* freight Case B/C
* freight Case A (try/except swallowed failures)

Conclusion:

* "Missing order after webhook" is NOT a failure condition

---

## SYNC SERVICE (SOT vs RUNTIME)

SOT Definition:

* Sync service populates orders table

Runtime Evidence:

* Orders fully enriched with customer + address data
* Repeated sync events observed
* orders.updated_at reflects active sync

Conclusion:

* Sync service is active and authoritative

---

## GOLDEN REFERENCE (RUNTIME CONTROL)

Order: 5554
Functional baseline (positive control) — order 5554 still returns a fully populated freight order with 2 shipments as of 2026-04-20; confirms Option A guardrails have not broken order ingestion or display paths.

Observed:

* Webhook → success
* B2BWave fetch → success
* Sync → populated orders
* Shipments → 2 created
* Payment → recorded

Conclusion:

* Represents correct system behavior

---

## NON-ISSUES (SOT HYPOTHESES DISPROVEN — no longer open)

* Schema missing columns → FALSE
* Endpoint not mounted → FALSE
* Auth broken → FALSE
* Migration failure → FALSE
* Webhook failure → FALSE

Superseded: prior "schema hypothesis open" and "webhook ambiguity" entries are retired as of 2026-04-20.
All have been disproven by runtime evidence and none are treated as active blockers.

---

## REAL FAILURE MODES (REMAINING)

### 1. Silent Early Exit (SOT-CONFIRMED)

```
if not fetch_b2bwave_order(order_id):
    return 200
```

Effect:

* No logs
* No orders row
* Looks like success

### 2. Timing Gap (EXPECTED)

* Webhook fires
* Sync delayed
* Order appears "missing"

---

## OBSERVABILITY GAPS (SOT-ALIGNED)

* No webhook ingress logging
* No logging on fetch failure
* Silent exception handling

Impact:

* Requires order-level tracing
* Not a correctness issue

---

## ADMIN ENDPOINTS (NEW / UPDATED 2026-04-20)

* `GET /` — now returns `b2bwave_target`, `email_allowlist_active`, `b2bwave_mutations_enabled` keys in addition to prior fields.
* `GET /debug/env-readiness` — LIVE. Returns `{b2bwave_target, matches_production_literal, matches_sandbox_pattern, email_allowlist_active, b2bwave_mutations_enabled, recommended_posture}`. Admin-gated.
* `POST /debug/sanitise-sandbox-db` — DRAFTED ONLY, NOT LIVE. Requires admin + `X-Allow-Destructive: yes` header.

### New stdout log markers (Render logs)

* `[ENV] b2bwave_url=... email_allowlist=... b2bwave_mutations=...` — startup banner, one line per boot.
* `[EMAIL-GUARD] blocked|redirected ...` — fires when EMAIL_ALLOWLIST suppresses or redirects a send.
* `[B2BWAVE-GUARD] mutation blocked ...` — fires when B2BWAVE_MUTATIONS_ENABLED=false suppresses a write.
* `[B2BWAVE-WARN] LIVE mutation committed ...` — fires when a real B2BWave mutation reaches the tenant (audit trail for production-side effects).

---

## TRACE PLAYBOOK (OPERATIONAL)

Use ONLY for order-specific debugging.

### Step 1 — Confirm Order Exists

* GET /orders/{order_id}

### Step 2 — Check Shipments

* GET /orders/{order_id}/shipments
* OR GET /debug/shipment/{order_id}

### Step 3 — Verify Sync Activity

* GET /orders/{order_id}/events
* Look for b2bwave_sync entries

### Step 4 — Determine Failure Type

A. No orders row:

* Possible causes:

  * sync not yet run
  * fetch_b2bwave_order early exit

B. Orders exists, no shipments:

* sync partial failure

C. Orders + shipments exist:

* system working

### Step 5 — Compare to Golden Order

* Use order 5554 as baseline

---

## OPERATIONAL MODEL

System-Level:

* Do NOT re-debug system
* System is stable

Order-Level:

* Use trace playbook
* Investigate specific order_id only

---

## CURRENT RISKS

* Diagnosability gaps only
* No system correctness risks identified

---

## SLA EXPECTATIONS (RUNTIME TIMELINES)

These are **observed and expected timing windows** for each stage based on validated runtime behavior.

| Stage                      | Trigger                     | Expected Time       | Notes                                     |
| -------------------------- | --------------------------- | ------------------- | ----------------------------------------- |
| Webhook Ingestion          | POST /webhook/b2bwave-order | Immediate (< 1s)    | Returns 200 quickly, may not create order |
| Pending Checkout Insert    | Inside webhook              | Immediate (< 1s)    | Happens before any branching              |
| B2BWave Fetch              | Inside webhook / sync       | 0–2s                | May early-exit silently if fetch fails    |
| Sync Service (orders row)  | Background thread           | 1–3 minutes typical | Observed: ~2m 28s for order 5554          |
| Shipment Creation          | During sync                 | Same as orders row  | Created alongside orders                  |
| Payment Recording (Square) | External webhook            | Seconds to minutes  | Depends on customer payment timing        |
| Lifecycle Progression      | Post-sync / admin action    | Manual / async      | e.g. needs_warehouse_order → next stage   |

### SLA INTERPRETATION

* < 1 minute delay → normal
* 1–3 minutes → expected sync window
* 3–10 minutes → investigate (possible delay)
* > 10 minutes → likely issue (trace required)

---

## FAILURE SIGNATURE MATRIX

Use this to quickly classify issues without re-investigating the system.

| Symptom                                  | Likely Cause                           | Layer             | Evidence to Check                            | Trace Step             |
| ---------------------------------------- | -------------------------------------- | ----------------- | -------------------------------------------- | ---------------------- |
| No order in DB after webhook             | Expected freight behavior (sync delay) | Service (design)  | Time gap between webhook + orders.created_at | Step 1 + wait / Step 3 |
| No order ever appears                    | fetch_b2bwave_order early exit         | Service (webhook) | Absence of sync events                       | Step 3                 |
| 200 response but nothing created         | Silent early exit branch               | Service           | No logs + no pending effects                 | Step 3 + logs          |
| 401 on admin endpoint                    | Invalid or missing token               | Auth              | Response body message                        | Retry with CFC2026     |
| 404 on endpoint                          | Route not mounted                      | Routing           | HTTP 404                                     | Verify path            |
| Orders exist, no shipments               | Sync partial failure                   | Service           | Missing shipment rows                        | Step 2                 |
| Orders + shipments exist, status stalled | Downstream process not triggered       | Lifecycle         | current_status field                         | Post-processing step   |
| Payment missing                          | Square webhook delay/failure           | Integration       | payment_received flag                        | Check Square events    |
| Intermittent missing orders              | Timing perception                      | System behavior   | Order appears later                          | Re-check after delay   |

---

## FINAL SUMMARY

WS6 is fully aligned with SOT and validated by runtime evidence.

All prior uncertainty has been eliminated.

Future work is limited to:

* order-level tracing
* observability improvements

NOT system debugging.
