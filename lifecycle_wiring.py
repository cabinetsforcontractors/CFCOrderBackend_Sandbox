"""
lifecycle_wiring.py
Phase 3B — Wires the Lifecycle Engine into the FastAPI app.

Call wire_lifecycle(app) from main.py after the alerts router mount.
This keeps main.py changes minimal (2 lines: import + call).

Usage in main.py:
    from lifecycle_wiring import wire_lifecycle
    wire_lifecycle(app)
"""

from fastapi import FastAPI


def wire_lifecycle(app: FastAPI) -> bool:
    """
    Mount lifecycle router and migration endpoints on the app.
    
    Returns True if wired successfully, False if modules not available.
    """
    success = False
    
    # Mount lifecycle router
    try:
        from lifecycle_routes import lifecycle_router
        app.include_router(lifecycle_router)
        print("[STARTUP] Lifecycle Engine loaded (/lifecycle/*)")
        success = True
    except ImportError as e:
        print(f"[STARTUP] lifecycle_routes not found: {e}")
    
    # Add migration endpoints
    try:
        from db_migrations import (
            add_lifecycle_fields as _add_lifecycle_fields,
            backfill_lifecycle_from_emails as _backfill_lifecycle
        )
        
        @app.post("/add-lifecycle-fields")
        def add_lifecycle_fields_endpoint():
            """Add lifecycle columns to orders table (Phase 3B)."""
            return _add_lifecycle_fields()
        
        @app.post("/backfill-lifecycle")
        def backfill_lifecycle_endpoint():
            """Backfill last_customer_email_at from existing email snippets."""
            return _backfill_lifecycle()
        
        print("[STARTUP] Lifecycle migration endpoints loaded")
    except ImportError as e:
        print(f"[STARTUP] lifecycle migration endpoints not loaded: {e}")
    
    return success
