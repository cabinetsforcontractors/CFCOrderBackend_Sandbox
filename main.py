"""
CFC Order Workflow Backend - v6.2.0
Phase 5B Backend Hardening: main.py fully decomposed into route modules.
WS17: invoice_routes added (/invoice/scan /status /emails /flags).
Phase 5 Hardening: routes/audit.py added (/audit/log POST + GET).
Phase 5B Rate Limiting: slowapi wired (200/min global default).

Module Map:
  orders_routes.py    — /orders /shipments /warehouse-mapping /trusted-customers
  shipping_routes.py  — /rl /shippo /rta
  alerts_routes.py    — /alerts/*
  detection_routes.py — /parse-email /detect-* /check-payment-alerts
  sync_routes.py      — /b2bwave/* /gmail/* /square/*
  migration_routes.py — /init-db /add-* /fix-* /debug/orders-columns
  checkout_routes.py  — /checkout* /checkout-ui/* /webhook/*
  invoice_routes.py   — /invoice/scan /invoice/status /invoice/emails /invoice/flags
  routes/audit.py     — /audit/log (POST write, GET read)
  auth.py             — require_admin Depends() — X-Admin-Token or Bearer JWT
  rate_limit.py       — shared slowapi Limiter instance

CORS: whitelist only (no wildcard). Add origins via CORS_ORIGINS env var.
Auth: set ADMIN_JWT_SECRET on Render to enable signed JWTs.
      Generate token: python -c "from auth import create_admin_token; print(create_admin_token())"
Rate limits: global default 200/minute per IP. Override per-route with @limiter.limit().
"""

import os
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from rate_limit import limiter

# =============================================================================
# CONFIG + DB
# =============================================================================

from config import (
    AUTO_SYNC_INTERVAL_MINUTES,
)

from db_helpers import get_db  # noqa: F401  (keep import so other modules can resolve)

# =============================================================================
# OPTIONAL SERVICE MODULES
# =============================================================================

try:
    from sync_service import start_auto_sync_thread, get_sync_status
    SYNC_SERVICE_LOADED = True
except ImportError:
    SYNC_SERVICE_LOADED = False
    print("[STARTUP] sync_service module not found")

    def get_sync_status():
        return {
            "enabled": False,
            "interval_minutes": AUTO_SYNC_INTERVAL_MINUTES,
            "last_sync": None,
            "running": False,
        }

try:
    from gmail_sync import run_gmail_sync, gmail_configured
except ImportError:
    print("[STARTUP] gmail_sync module not found, email sync disabled")

    def run_gmail_sync(conn, hours_back=2):
        return {"status": "disabled", "reason": "module_not_found"}

    def gmail_configured():
        return False


try:
    from square_sync import run_square_sync, square_configured
except ImportError:
    print("[STARTUP] square_sync module not found, payment sync disabled")

    def run_square_sync(conn, hours_back=24):
        return {"status": "disabled", "reason": "module_not_found"}

    def square_configured():
        return False


# =============================================================================
# ROUTE MODULE IMPORTS
# =============================================================================

# Phase 2: RL-Quote proxy (/proxy/*)
from rl_quote_proxy import router as rl_proxy_router

# Phase 3A: Alerts (/alerts/*)
try:
    from alerts_routes import alerts_router
    ALERTS_ENGINE_LOADED = True
except ImportError:
    ALERTS_ENGINE_LOADED = False
    print("[STARTUP] alerts_routes module not found, AlertsEngine disabled")

# Phase 3B + 4: Lifecycle + Email + AI Config
from startup_wiring import wire_all

# Phase 5A: Orders + Shipping
from orders_routes import orders_router
from shipping_routes import shipping_router

# Phase 5B: Detection, Sync, Migrations, Checkout
from detection_routes import detection_router
from sync_routes import sync_router
from migration_routes import migration_router
from checkout_routes import checkout_router

# WS17: Invoice Intelligence (/invoice/*)
try:
    from invoice_routes import invoice_router
    INVOICE_LOADED = True
except ImportError:
    INVOICE_LOADED = False
    print("[STARTUP] invoice_routes module not found, Invoice Intelligence disabled")

# Phase 5 Hardening: Audit Log (/audit/log)
try:
    from routes.audit import audit_router
    AUDIT_LOADED = True
except ImportError:
    AUDIT_LOADED = False
    print("[STARTUP] routes.audit module not found, audit log disabled")


# =============================================================================
# FASTAPI APP — CORS whitelist (no wildcard)
# =============================================================================

_cors_env = os.environ.get("CORS_ORIGINS", "")
_extra_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else []

ALLOWED_ORIGINS = [
    "https://cfc-orders-frontend.vercel.app",
    "https://cfcorderbackend-sandbox.onrender.com",   # checkout self-reference
    "https://brain-backend-6uhk.onrender.com",        # Brain UI proxy
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
] + _extra_origins

app = FastAPI(title="CFC Order Workflow", version="6.2.0")

# Phase 5B: Rate limiting — must be wired before routers are mounted
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# MOUNT ROUTERS
# =============================================================================

app.include_router(rl_proxy_router)           # Phase 2: /proxy/*

if ALERTS_ENGINE_LOADED:
    app.include_router(alerts_router)         # Phase 3A: /alerts/*

WIRING_STATUS = wire_all(app)                 # Phase 3B+4: lifecycle + email + ai_configure

app.include_router(orders_router)             # Phase 5A: /orders /shipments /warehouse-mapping /trusted-customers
app.include_router(shipping_router)           # Phase 5A: /rl /shippo /rta

app.include_router(detection_router)          # Phase 5B: /parse-email /detect-* /check-payment-alerts
app.include_router(sync_router)               # Phase 5B: /b2bwave/* /gmail/* /square/*
app.include_router(migration_router)          # Phase 5B: /init-db /add-* /fix-* /debug/orders-columns
app.include_router(checkout_router)           # Phase 5B: /checkout* /checkout-ui/* /webhook/*

if INVOICE_LOADED:
    app.include_router(invoice_router)        # WS17: /invoice/scan /status /emails /flags

if AUDIT_LOADED:
    app.include_router(audit_router)          # Phase 5: /audit/log


# =============================================================================
# STARTUP EVENT
# =============================================================================

@app.on_event("startup")
def start_auto_sync():
    """Start background sync thread on app startup."""
    if SYNC_SERVICE_LOADED:
        start_auto_sync_thread(run_gmail_sync, run_square_sync)
    else:
        print("[AUTO-SYNC] sync_service not loaded, auto-sync disabled")


# =============================================================================
# ROOT / HEALTH
# =============================================================================

@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "CFC Order Workflow",
        "version": "6.2.0",
        "auto_sync": get_sync_status(),
        "gmail_sync": {"enabled": gmail_configured()},
        "square_sync": {"enabled": square_configured()},
        "alerts_engine": {"enabled": ALERTS_ENGINE_LOADED},
        "lifecycle_engine": {"enabled": WIRING_STATUS.get("lifecycle", False)},
        "email_engine": {"enabled": WIRING_STATUS.get("email", False)},
        "ai_configure": {"enabled": WIRING_STATUS.get("ai_configure", False)},
        "invoice_intel": {"enabled": INVOICE_LOADED},
        "audit_log": {"enabled": AUDIT_LOADED},
        "rate_limiting": {"enabled": True, "default_limit": "200/minute"},
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": "6.2.0"}


# =============================================================================
# SERVER STARTUP
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
