"""
storefront_webhook.py — THE DOORBELL (William 2026-07-26: "we need to parse
that out now and get it hooked up").

The storefront's checkout fire-and-forgets a thin ping at ORDERS_WEBHOOK_URL:
    POST {"id": <order number>, "customer_email": "..."}
The ping says "come look" — it never carries the order. We come look:
    1. read the FULL order back from B2BWave (during the parallel run the
       storefront has already pushed/flipped it there — B2BWave stays the
       number-giver and source of truth; post-cutover the same URL points at
       their B2BWave-compatible API and NOTHING here changes),
    2. ingest it (sync_order_from_b2bwave — same dance as the cycle sync),
    3. run the new-order watcher immediately (COM- orders get the cloned
       "New Order" admin email within seconds instead of next cycle),
    4. log an order_event. The invoice flow rides the normal machinery.

AUTH: the storefront's ping carries no headers, so the shared secret rides
the URL: ...?token=<STOREFRONT_WEBHOOK_TOKEN>. The door stays CLOSED until
William sets that env on Render. X-Admin-Token also opens it (drills).

Give the storefront lane exactly:
  ORDERS_WEBHOOK_URL = https://cfcorderbackend-sandbox.onrender.com/storefront/order-submitted?token=<value>
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header, Request

from db_helpers import get_db

storefront_router = APIRouter(tags=["storefront"])

ADMIN_TOKEN = os.environ.get("ADMIN_API_KEY", "").strip() or "CFC2026"


def _authorized(request: Request, x_admin_token: str) -> bool:
    secret = os.environ.get("STOREFRONT_WEBHOOK_TOKEN", "").strip()
    if x_admin_token and x_admin_token == ADMIN_TOKEN:
        return True
    if not secret:
        return False                      # door closed until William arms it
    return request.query_params.get("token", "") == secret


@storefront_router.post("/storefront/order-submitted")
def order_submitted(request: Request, payload: dict = Body(...),
                    x_admin_token: str = Header(None, alias="X-Admin-Token")):
    if not _authorized(request, x_admin_token or ""):
        return {"status": "error", "message": "bad or missing token"}

    order_id = str(payload.get("id") or "").strip()
    if not order_id or not order_id.isdigit():
        return {"status": "error", "message": "payload needs a numeric id"}

    out = {"status": "ok", "order_id": order_id,
           "at": datetime.now(timezone.utc).isoformat()}

    # 1+2. read back the full order and ingest it
    try:
        from sync_service import b2bwave_api_request, sync_order_from_b2bwave
        data = b2bwave_api_request("orders", {"id_eq": order_id})
        if not data:
            out["synced"] = False
            out["note"] = ("order not visible on B2BWave yet — the cycle sync "
                           "will catch it")
        else:
            order_data = data[0] if isinstance(data, list) else data
            sync_order_from_b2bwave(order_data)
            out["synced"] = True
    except Exception as e:
        out["synced"] = False
        out["sync_error"] = str(e)[:200]

    # 3. immediate new-order pass (cloned admin email for COM- orders)
    try:
        from new_order_notifier import run_new_order_watch
        with get_db() as conn:
            nw = run_new_order_watch(conn)
        out["notified"] = [n.get("order_id") for n in nw.get("notified", [])]
    except Exception as e:
        out["notify_error"] = str(e)[:200]

    # 4. record the ping
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    SELECT %s, 'storefront_ping', %s, 'storefront_webhook'
                    WHERE EXISTS (SELECT 1 FROM orders WHERE order_id = %s)
                """, (order_id,
                      '{"customer_email": "%s"}' % (payload.get("customer_email") or ""),
                      order_id))
            conn.commit()
    except Exception:
        pass

    return out
