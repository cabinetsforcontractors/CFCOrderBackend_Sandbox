# CFC Order Workflow Backend — Sandbox

**Version:** 6.5.0
**Last Updated:** 2026-04-07
**Service URL:** https://cfcorderbackend-sandbox.onrender.com
**Frontend:** https://cfcordersfrontend-sandbox.vercel.app

## ⛔ Session Rules — ALL Claude Sessions
- READ this README before doing any work in this repo
- DO NOT write or rewrite any file unless William explicitly says to in that session
- Report only and stop unless William says otherwise
- ⛔ DO NOT restore supplier emails — `wpjob1@gmail.com` for LI and Love-Milestone until William explicitly says to change

## Overview

FastAPI backend for managing CFC (Cabinets For Contractors) wholesale order workflow — from B2BWave order ingestion through payment, warehouse fulfillment, freight quoting, and delivery. Supports both freight (R+L LTL) and warehouse pickup order flows.

## Architecture

| Module | Purpose |
|--------|---------|
| `main.py` | FastAPI app entry point — mounts all routers |
| `config.py` | Env vars, constants, warehouse/supplier info |
| `db_helpers.py` | PostgreSQL connection management |
| `db_migrations.py` | Schema migrations |
| `schema.py` | SQL schema definitions |
| `orders_routes.py` | Order CRUD, shipment management, checkpoint handler |
| `checkout_routes.py` | B2BWave webhook, checkout flow, pickup handler |
| `checkout.py` | Shipping calc, Square payment links, pickup detection |
| `supplier_routes.py` | Supplier-facing HTML forms (freight + pickup) |
| `supplier_polling_engine.py` | Freight poll engine — BOL, pickup request, tracking |
| `pickup_polling_engine.py` | Pickup poll engine — ready date, customer notify, confirm |
| `alerts_routes.py` | Cron endpoints: tracking check, pickup confirm |
| `alerts_engine.py` | Alert rule evaluation |
| `lifecycle_engine.py` | Inactivity detection: day 7 inactive, day 14 warn, day 21 cancel |
| `lifecycle_routes.py` | Lifecycle cron endpoint + quote reminders |
| `ai_summary.py` | Anthropic Claude for 6-bullet summaries + full analysis |
| `rl_carriers.py` | R+L Carriers API: quotes, BOL, pickup, tracking |
| `shippo_rates.py` | Shippo for small package rates |
| `bol_routes.py` | BOL admin routes |
| `bol_template.py` | Fallback BOL PDF generator |
| `migration_routes.py` | DB migration endpoints + debug endpoints |
| `sync_service.py` | B2BWave auto-sync scheduler |
| `gmail_sync.py` | Gmail scanning for payments and tracking |
| `square_sync.py` | Square payment sync |

## Order Flows

### Freight Flow
```
B2BWave order → webhook → Smarty address validation → R+L quote → invoice email
→ customer pays → admin sends to warehouse → supplier date-form
→ BOL created → R+L pickup scheduled → BOL PDF emailed to supplier
→ customer pickup-scheduled email → tracking cron → customer tracking email on first scan
→ complete
```

### Warehouse Pickup Flow
```
B2BWave order (shipping_option_id == 2) → webhook → $0 invoice sent
→ customer pays → admin clicks per-warehouse "Send Pickup Poll"
→ supplier pickup-ready-form → supplier enters date/time
→ customer "Order Ready" email with warehouse address
→ pickup confirm cron (after ready date) → supplier "Has customer picked up?"
→ Yes → order complete / No → CFC escalation alert
```

### Lifecycle Flow (automated inactivity)
```
Day 0: order active
Day 7: move to Inactive tab + email customer "order moved to inactive"
Day 14: email customer "order will be canceled in 7 days"
Day 21: auto-cancel on B2BWave + mark complete + cancel confirmation email
Customer response at any point → reset clock to day 0
"Cancel" keyword in customer email → immediate cancel
```

## Render Cron Jobs

| Cron Name | Schedule | Endpoint | Purpose |
|-----------|----------|---------|---------|
| `cfc-pickup-cron` | `0 10 * * *` (10am UTC) | `POST /alerts/pickup/check-confirmations` | Ask supplier if customer picked up |
| `cfc-tracking-cron` | `0 */3 * * *` (every 3hrs) | `POST /alerts/tracking/check-all` | Poll R+L tracking, send customer email on first scan |
| `cfc-lifecycle-cron` | `0 8 * * *` (8am UTC) | `POST /lifecycle/check-all` | Inactivity engine + quote reminders |

All crons authenticate with `X-Admin-Token: CFC2026`.

## Key Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /webhook/b2bwave-order` | B2BWave order webhook — detects pickup vs freight |
| `POST /supplier/{shipment_id}/send-poll` | Admin: send to warehouse (routes by pickup_type) |
| `PATCH /orders/{id}/checkpoint` | Mark payment_received, warehouse_confirmed, is_complete |
| `PATCH /orders/{id}/set-status` | Correct way to set order status |
| `POST /orders/{id}/send-tracking` | Save tracking + email customer + log event (prevents cron dupe) |
| `GET /checkout-ui/{id}?view=quote` | Read-only quote view — no Pay button |
| `POST /lifecycle/check-all` | Daily lifecycle check + quote reminders |
| `POST /alerts/tracking/check-all` | R+L tracking poll |
| `POST /alerts/pickup/check-confirmations` | Pickup confirm poll |
| `GET /debug/shipment/{order_id}` | Debug: shipment state for an order |
| `POST /debug/insert-pickup-shipment/{order_id}` | Debug: test pickup INSERT |
| `GET /debug/orders-columns` | Debug: show all DB columns |

## Database

PostgreSQL on Render. Key tables:
- `orders` — master order record with all checkpoints + lifecycle fields + is_pickup
- `order_shipments` — per-warehouse shipment records with pickup_type, supplier_token, pickup fields
- `order_events` — full event log (polls sent, BOLs, emails, tracking, lifecycle)
- `pending_checkouts` — B2BWave orders awaiting payment
- `order_alerts` — unresolved alert flags
- `order_email_snippets` — email history for AI analysis
- `warehouse_mapping` — SKU prefix → warehouse routing
- `trusted_customers` — pre-approved customers

## Known Technical Notes

- **DDL rollback bug** in `add_ws6_supplier_workflow_fields()`: quote_number ADD COLUMN
  is rolled back by later "already exists" exceptions. Fixed by removing quote_number from
  pickup INSERTs. Future multi-column migrations: use one `with get_db()` per column.
- **quote_number** may not be present in order_shipments for sandbox (migration bug).
  Freight BOL reads it with `.get()` fallback — safe.
- **Pickup vs freight routing**: `sent_to_warehouse` checkpoint checks `pickup_type`
  on each shipment and routes to `send_pickup_ready_poll` or `send_initial_poll` accordingly.

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `B2BWAVE_URL` | Yes | B2BWave API base URL |
| `B2BWAVE_USERNAME` | Yes | B2BWave API username |
| `B2BWAVE_API_KEY` | Yes | B2BWave API key |
| `ANTHROPIC_API_KEY` | Yes | Claude AI for order summaries |
| `SQUARE_ACCESS_TOKEN` | Yes | Square payment processing |
| `SQUARE_LOCATION_ID` | Yes | Square location |
| `CHECKOUT_BASE_URL` | Yes | Base URL for checkout links |
| `CHECKOUT_SECRET` | Yes | HMAC secret for checkout tokens |
| `RL_QUOTE_API_URL` | Yes | rl-quote microservice URL |
| `GMAIL_CLIENT_ID` | Optional | Gmail OAuth |
| `GMAIL_CLIENT_SECRET` | Optional | Gmail OAuth |
| `GMAIL_REFRESH_TOKEN` | Optional | Gmail OAuth |
| `GMAIL_SEND_ENABLED` | Optional | Enable sending emails (default: false) |
| `RL_CARRIERS_API_KEY` | Optional | R+L Carriers direct API |
| `WAREHOUSE_NOTIFICATION_EMAIL` | Optional | Internal alert email |

## Deploy

Push to `main` → Render auto-deploys.

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 10000
```

## Related Services

- **rl-quote-sandbox** — R+L freight microservice: https://rl-quote-sandbox.onrender.com
- **Frontend sandbox** — https://cfcordersfrontend-sandbox.vercel.app
- **Production backend** — Separate Render service (not yet promoted)
