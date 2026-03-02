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
    
    loaded = sum(1 for v in results.values() if v)
    print(f"[STARTUP] startup_wiring: {loaded}/{len(results)} modules loaded")
    
    return results
