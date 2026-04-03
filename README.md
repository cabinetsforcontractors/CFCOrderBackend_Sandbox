# CFC Order Workflow Backend — Sandbox

**Version:** 6.2.0
**Service URL:** https://cfcorderbackend-sandbox.onrender.com
**Frontend:** https://cfcordersfrontend-sandbox.vercel.app

## ⛔ Session Rules — ALL Claude Sessions
- READ this README before doing any work in this repo
- DO NOT write or rewrite any file unless William explicitly says to in that session
- Report only and stop unless William says otherwise

## Overview

FastAPI backend for managing CFC (Cabinets For Contractors) wholesale order workflow — from B2BWave order ingestion through payment, warehouse fulfillment, freight quoting, and delivery.

## Architecture

- **main.py** — FastAPI app with 84 endpoints (order CRUD, sync, shipping, checkout)
- **16 modules** — each handling a specific domain:

| Module | Purpose |
|--------|---------|
| `config.py` | Environment variables, constants, warehouse/supplier info |
| `db_helpers.py` | PostgreSQL connection management |
| `db_migrations.py` | Schema migrations (shipments, checkouts, columns) |
| `schema.py` | SQL schema definitions |
| `sync_service.py` | B2BWave auto-sync scheduler + Gmail/Square sync |
| `b2bwave_api.py` | B2BWave REST API client |
| `gmail_sync.py` | Gmail scanning for payment links, payments, tracking |
| `square_sync.py` | Square payment API sync |
| `email_parser.py` | B2BWave order email parsing |
| `detection.py` | Payment link, RL quote, PRO number detection |
| `ai_summary.py` | Anthropic Claude API for order summaries |
| `checkout.py` | Checkout flow: shipping calc, Square payment links |
| `shippo_rates.py` | Shippo API for small package rates (UPS/FedEx/USPS) |
| `rl_carriers.py` | R+L Carriers LTL freight: quotes, BOL, pickup, tracking |
| `load_rta_data.py` | Load RTA cabinet database from Excel |
| `rta_database.py` | SKU weight lookup and order weight calculation |

## Database

PostgreSQL on Render with tables: `orders`, `order_line_items`, `order_events`, `order_alerts`, `order_email_snippets`, `order_shipments`, `pending_checkouts`, `warehouse_mapping`, `trusted_customers`, and the `order_status` view.

## Order Workflow

```
needs_payment_link → awaiting_payment → needs_warehouse_order → awaiting_warehouse → needs_bol → awaiting_shipment → complete
```

## Shipping Rules

- **< 70 lbs** → Shippo (UPS/FedEx/USPS small package)
- **≥ 70 lbs** → R+L Carriers LTL freight
- **Customer markup:** R+L quote + $50
- **Freight class:** 85 (always for RTA cabinets)

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `B2BWAVE_URL` | Yes | B2BWave API base URL |
| `B2BWAVE_USERNAME` | Yes | B2BWave API username |
| `B2BWAVE_API_KEY` | Yes | B2BWave API key |
| `ANTHROPIC_API_KEY` | Yes | Claude AI for order summaries |
| `SHIPPO_API_KEY` | Yes | Shippo shipping rates |
| `SQUARE_ACCESS_TOKEN` | Yes | Square payment processing |
| `SQUARE_LOCATION_ID` | Yes | Square location |
| `CHECKOUT_BASE_URL` | Yes | Base URL for checkout links |
| `CHECKOUT_SECRET` | Yes | HMAC secret for checkout tokens |
| `GMAIL_CLIENT_ID` | Optional | Gmail OAuth for email sync |
| `GMAIL_CLIENT_SECRET` | Optional | Gmail OAuth |
| `GMAIL_REFRESH_TOKEN` | Optional | Gmail OAuth |
| `GMAIL_SEND_ENABLED` | Optional | Enable sending emails (default: false) |
| `RL_CARRIERS_API_KEY` | Optional | R+L Carriers direct API |

## Deploy

Deployed on Render. Push to `main` branch triggers auto-deploy.

```bash
# Local development
pip install -r requirements.txt
uvicorn main:app --reload --port 10000
```

## Related Services

- **rl-quote-sandbox** — Separate R+L freight quoting microservice at https://rl-quote-sandbox.onrender.com
- **Production backend** — Older version at separate Render service
- **Production frontend** — https://cfc-orders-frontend.vercel.app
