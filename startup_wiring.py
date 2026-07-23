"""
startup_wiring.py
Wires all Phase 3B+ modules into the FastAPI app in one call.

Usage in main.py (add after the alerts_router mount):
    from startup_wiring import wire_all
    wire_all(app)

This keeps main.py changes to 2 lines instead of editing 15+ lines
across multiple import blocks.

Session 8 — Mar 2, 2026
"""

from fastapi import FastAPI


def wire_all(app: FastAPI) -> dict:
    """
    Mount all pending routers and return status dict.
    
    Returns dict with module names as keys, bool loaded status as values.
    Use in root endpoint: status["lifecycle_engine"] = {"enabled": results.get("lifecycle", False)}
    """
    results = {}
    
    # Phase 3B: Lifecycle Engine (/lifecycle/*)
    try:
        from lifecycle_wiring import wire_lifecycle
        results["lifecycle"] = wire_lifecycle(app)
    except ImportError as e:
        results["lifecycle"] = False
        print(f"[STARTUP] lifecycle_wiring not found: {e}")
    
    # Phase 4: Email Communications (/email/*, /orders/*/send-email)
    try:
        from email_wiring import wire_email
        results["email"] = wire_email(app)
    except ImportError as e:
        results["email"] = False
        print(f"[STARTUP] email_wiring not found: {e}")
    
    # AI Configure (/ai/*)
    try:
        from ai_configure_wiring import wire_ai_configure
        wire_ai_configure(app)
        results["ai_configure"] = True
    except ImportError as e:
        results["ai_configure"] = False
        print(f"[STARTUP] ai_configure_wiring not found: {e}")
    
    # Quote Engine (/quotes/*)
    try:
        from quote_routes import quote_router
        app.include_router(quote_router)
        results["quotes"] = True
    except ImportError as e:
        results["quotes"] = False
        print(f"[STARTUP] quote_routes not found: {e}")

    # Freight Plan engine (/freight/*) — pallet plans + fees from freight_logic (2026-07-15)
    try:
        from freight_routes import freight_router
        app.include_router(freight_router)
        results["freight"] = True
    except ImportError as e:
        results["freight"] = False
        print(f"[STARTUP] freight_routes not found: {e}")

    # Carrier routing (/freight/carrier-quote/{order_id}) — Daylight-vs-R+L per leg,
    # all-in with accessorials + supplier pallet fees (freight_router.py, 2026-07-23)
    try:
        from carrier_routes import carrier_router
        app.include_router(carrier_router)
        results["carrier_quote"] = True
    except ImportError as e:
        results["carrier_quote"] = False
        print(f"[STARTUP] carrier_routes not found: {e}")
    
    loaded = sum(1 for v in results.values() if v)
    print(f"[STARTUP] startup_wiring: {loaded}/{len(results)} modules loaded")
    
    return results
