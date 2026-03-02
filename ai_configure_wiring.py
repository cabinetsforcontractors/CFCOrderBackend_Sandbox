"""
ai_configure_wiring.py
Mounts the AI configure router into the FastAPI app.
Usage in main.py:
    from ai_configure_wiring import wire_ai_configure
    wire_ai_configure(app)

Session 7 — Mar 2, 2026
"""

def wire_ai_configure(app):
    """Mount AI configure router onto the FastAPI app."""
    from ai_configure import router as ai_configure_router
    app.include_router(ai_configure_router)
    print("[WIRED] AI Configure router mounted at /ai/*")
