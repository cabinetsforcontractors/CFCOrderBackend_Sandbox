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
    # Log EVERY hit — authorized or not — so "ping never arrived" and
    # "ping rejected" are distinguishable (the 5732 lesson, 2026-07-26).
    print(f"[DOORBELL] hit: id={payload.get('id')!r} "
          f"token_qs={'yes' if request.query_params.get('token') else 'NO'} "
          f"admin_hdr={'yes' if x_admin_token else 'no'}")
    if not _authorized(request, x_admin_token or ""):
        print(f"[DOORBELL] REJECTED (bad/missing token) id={payload.get('id')!r}")
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


@storefront_router.post("/storefront/resend-order-email")
def resend_order_email(request: Request, payload: dict = Body(...),
                       x_admin_token: str = Header(None, alias="X-Admin-Token")):
    """THE RESEND BELL (William 2026-07-29 FYI: the storefront adds a button
    that rings this to have US resend). Same token auth as the doorbell.
    Payload: {order_id, resend_to?, admin_email?}.

    Resends the ORDER-RECEIVED email (our replacement for B2BWave's — the
    fleet notification shutdown means nobody else can), and when the order
    is invoiced-but-unpaid ALSO rebuilds and resends the invoice email with
    the existing Square link (from the payment_link_created event).
    EMAIL_ALLOWLIST governs delivery as always."""
    import json as _json
    print(f"[RESEND-BELL] hit: id={payload.get('order_id')!r}")
    if not _authorized(request, x_admin_token or ""):
        return {"status": "error", "message": "bad or missing token"}
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        return {"status": "error", "message": "order_id required"}
    from db_helpers import get_order_by_id
    order = get_order_by_id(order_id)
    if not order:
        return {"status": "error", "message": f"order {order_id} not found"}
    order["order_id"] = order_id
    to = (str(payload.get("resend_to") or "").strip()
          or (order.get("email") or "").strip())
    if not to:
        return {"status": "error", "message": "no recipient"}

    from email_sender import send_order_email
    out = {"status": "ok", "order_id": order_id, "to": to, "sent": []}
    res = send_order_email(order_id, "order_received", to,
                           triggered_by="storefront_resend")
    out["sent"].append({"order_received": bool(res.get("success")),
                        "detail": res.get("error") or res.get("to")})

    if order.get("payment_link_sent") and not order.get("payment_received"):
        try:
            link_url, link_amount = "", 0.0
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT event_data FROM order_events
                                   WHERE order_id = %s
                                     AND event_type = 'payment_link_created'
                                   ORDER BY created_at DESC LIMIT 1""",
                                (order_id,))
                    row = cur.fetchone()
            if row:
                data = row[0]
                if isinstance(data, str):
                    data = _json.loads(data)
                link_url = (data or {}).get("url") or ""
                link_amount = float((data or {}).get("amount") or 0)
            if link_url:
                total = float(order.get("order_total") or 0)
                tariff = round(total * 0.08, 2)
                shipping = round(float(order.get("shipping_cost") or 0) or
                                 max(0.0, link_amount - total - tariff), 2)
                order_data = dict(order)
                order_data["order_id"] = order_id
                order_data["payment_link"] = link_url
                order_data["shipping_result"] = {
                    "total_items": total, "tariff_amount": tariff,
                    "tariff_rate": 0.08, "total_shipping": shipping,
                    "grand_total": link_amount or round(
                        total + tariff + shipping, 2)}
                res2 = send_order_email(order_id, "payment_link", to,
                                        order_data=order_data,
                                        triggered_by="storefront_resend")
                out["sent"].append(
                    {"invoice": bool(res2.get("success")),
                     "detail": res2.get("error") or res2.get("to")})
        except Exception as e:
            out["sent"].append({"invoice": False, "detail": str(e)[:150]})

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO order_events
                    (order_id, event_type, event_data, source)
                    VALUES (%s, 'resend_requested', %s, 'storefront_webhook')""",
                    (order_id, _json.dumps(
                        {"resend_to": to,
                         "admin_email": payload.get("admin_email") or "",
                         "sent": out["sent"]})))
            conn.commit()
    except Exception:
        pass
    return out
