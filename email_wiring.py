"""
email_wiring.py
Phase 4 — Wires the Email Communications system into the FastAPI app.

Call wire_email(app) from main.py after the alerts router mount.
This keeps main.py changes minimal (2 lines: import + call).

Usage in main.py:
    from email_wiring import wire_email
    EMAIL_ROUTES_LOADED = wire_email(app)
"""

from fastapi import FastAPI


def wire_email(app: FastAPI) -> bool:
    """
    Mount email communications router on the app.
    
    Returns True if wired successfully, False if modules not available.
    """
    success = False
    
    # Mount email router
    try:
        from email_routes import email_router
        app.include_router(email_router)
        print("[STARTUP] Email Communications loaded (/email/*, /orders/*/send-email)")
        success = True
    except ImportError as e:
        print(f"[STARTUP] email_routes not found: {e}")
    
    return success
