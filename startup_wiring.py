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

    # Task board (/tasks) — Gmail-sweep needs-you board + note box (2026-07-25)
    try:
        from task_board import task_router
        app.include_router(task_router)
        results["task_board"] = True
    except ImportError as e:
        results["task_board"] = False
        print(f"[STARTUP] task_board not found: {e}")

    # Test-order registry (/test-orders) — one source of truth for test rows (2026-07-25)
    try:
        from test_registry import test_registry_router
        app.include_router(test_registry_router)
        results["test_registry"] = True
    except ImportError as e:
        results["test_registry"] = False
        print(f"[STARTUP] test_registry not found: {e}")

    # New-order watcher (/new-order-watch) — cloned admin New Order email (Beat 2)
    try:
        from new_order_notifier import new_order_router
        app.include_router(new_order_router)
        results["new_order_watch"] = True
    except ImportError as e:
        results["new_order_watch"] = False
        print(f"[STARTUP] new_order_notifier not found: {e}")

    # Storefront doorbell (/storefront/order-submitted) — ping + read-back (2026-07-26)
    try:
        from storefront_webhook import storefront_router
        app.include_router(storefront_router)
        results["storefront_webhook"] = True
    except ImportError as e:
        results["storefront_webhook"] = False
        print(f"[STARTUP] storefront_webhook not found: {e}")

    # Firing-order law (/fire-log/* + /orders/{id}/fires) — full-payload
    # append-only fires + diff-on-write (William ruled 2026-07-30)
    try:
        from fire_log import fire_log_router
        app.include_router(fire_log_router)
        results["fire_log"] = True
    except ImportError as e:
        results["fire_log"] = False
        print(f"[STARTUP] fire_log not found: {e}")

    # Order dossier + supplier playbooks (/orders/{id}/dossier, /playbooks)
    # — the generated mini-md per order (William ruled 2026-07-30)
    try:
        from dossier import dossier_router
        app.include_router(dossier_router)
        results["dossier"] = True
    except ImportError as e:
        results["dossier"] = False
        print(f"[STARTUP] dossier not found: {e}")

    # Reply composer (/reply/compose + /reply/send) — intent in,
    # William-voiced reply out; PREVIEW LAW (William ruled 2026-07-30)
    try:
        from reply_composer import reply_router
        app.include_router(reply_router)
        results["reply_composer"] = True
    except ImportError as e:
        results["reply_composer"] = False
        print(f"[STARTUP] reply_composer not found: {e}")

    # Queue backend (/auto-settle/run + /queue/money-strip) — flags die
    # when their cause dies + the money strip (William ruled 2026-07-30)
    try:
        from queue_api import queue_router
        app.include_router(queue_router)
        results["queue_api"] = True
    except ImportError as e:
        results["queue_api"] = False
        print(f"[STARTUP] queue_api not found: {e}")

    # Learning machine (/learn/*) — read-only @gmail connection + the
    # reply harvester (William ruled 2026-07-31: "build a machine that
    # will learn... sweep the @gmail account and see my replies")
    try:
        from learn_gmail import learn_router
        app.include_router(learn_router)
        results["learn_gmail"] = True
    except ImportError as e:
        results["learn_gmail"] = False
        print(f"[STARTUP] learn_gmail not found: {e}")
    try:
        from learn_harvest import harvest_router
        app.include_router(harvest_router)
        results["learn_harvest"] = True
    except ImportError as e:
        results["learn_harvest"] = False
        print(f"[STARTUP] learn_harvest not found: {e}")

    # The distiller (/learn/distill) — harvest pairs -> plain-English
    # lessons in the playbooks (William ruled 2026-07-31 "run the distiller")
    try:
        from learn_distill import distill_router
        app.include_router(distill_router)
        results["learn_distill"] = True
    except ImportError as e:
        results["learn_distill"] = False
        print(f"[STARTUP] learn_distill not found: {e}")

    # The fallback law (/consensus/drill) — two checkers must agree
    # (William blessed 2026-07-31)
    try:
        from verify_consensus import consensus_router
        app.include_router(consensus_router)
        results["verify_consensus"] = True
    except ImportError as e:
        results["verify_consensus"] = False
        print(f"[STARTUP] verify_consensus not found: {e}")

    # Full Analysis (POST /orders/{id}/comprehensive-summary — the app's
    # Generate button) + GET /orders/{id}/last-exchange (William 2026-07-31)
    try:
        from order_analysis import analysis_router
        app.include_router(analysis_router)
        results["order_analysis"] = True
    except ImportError as e:
        results["order_analysis"] = False
        print(f"[STARTUP] order_analysis not found: {e}")

    # THE DO-BOX (/do/preview + /do/execute) — typed actions fire real
    # machinery from the tasks tab, preview-then-fire, stated overrides
    # (William law change 2026-08-01)
    try:
        from do_box import do_router
        app.include_router(do_router)
        results["do_box"] = True
    except ImportError as e:
        results["do_box"] = False
        print(f"[STARTUP] do_box not found: {e}")

    loaded = sum(1 for v in results.values() if v)
    print(f"[STARTUP] startup_wiring: {loaded}/{len(results)} modules loaded")
    
    return results
