"""
auto_invoice.py — Beats A + C of the 1-2-3-4 (William 2026-07-27:
"proceed to beat A and beat C, lets make it real").

BEAT A — the freight quote fires itself: cheapest carrier for every leg via
freight_router.carrier_quote_order (Smarty residential auto-detect, R+L vs
Daylight, accessorials, pallet fees), plus SHIPPING_MARKUP_PCT (env, default
0). Pickup orders ship at $0.

BEAT C — the invoice sends itself: totals + tariff + auto Square link +
the locked v4 invoice (policies + residential blocks, PDF attached),
straight to the customer the cycle the order is first seen.
EMAIL_ALLOWLIST still governs actual delivery — it is the beta ramp switch:
while set, sends redirect to the safety inbox; remove it and the machine is
fully live. William's ruling: live today, imperfection accepted, fix fast.

GATES (all must pass, else NO send + a NEEDS-A-HUMAN alert to orders@):
  AUTO_INVOICE_ENABLED env (default true) · not a test-registry order ·
  customer email present · order_total > 0 · line items synced · not
  already invoiced (payment_link_sent) · not already paid · freight quote
  complete (or pickup).
ON SUCCESS: payment_link_sent stamped, B2BWave -> Awaiting Payment
checkpoint, payment_link_created + invoice_auto_sent events (the link id
in the event is what lets a cancel KILL the link).
"""

import json
import os
from typing import Dict

from db_helpers import get_db, get_order_by_id

INTERNAL_ALERT = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL",
                                "orders@cabinetsforcontractors.com").strip()


def _event(order_id: str, event_type: str, data: Dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO order_events
                               (order_id, event_type, event_data, source)
                               VALUES (%s, %s, %s, 'auto_invoice')""",
                            (order_id, event_type,
                             json.dumps(data, default=str)[:4000]))
                conn.commit()
    except Exception as e:
        print(f"[AUTO-INVOICE] event log failed {order_id}: {e}")


def auto_shipping(order_id: str, order: Dict) -> Dict:
    """BEAT A. Returns {'ok': True, 'shipping': X, 'detail': ...} or
    {'ok': False, 'reason': ...}."""
    if order.get("is_pickup"):
        return {"ok": True, "shipping": 0.0, "detail": "warehouse pickup"}
    try:
        from freight_router import carrier_quote_order
        q = carrier_quote_order(order_id)
    except Exception as e:
        return {"ok": False, "reason": f"quote engine error: {e}"}
    if q.get("status") != "ok":
        return {"ok": False, "reason": q.get("message", "quote failed")}
    if not q.get("all_legs_quoted") or q.get("order_shipping_total") is None:
        notes = [n for leg in (q.get("legs") or []) for n in leg.get("notes", [])]
        return {"ok": False,
                "reason": "not all legs quoted: " + ("; ".join(notes[:4]) or "?")}
    # William ruling 2026-07-28 ("B"): the markup rides the CARRIER BASE only —
    # pallet pass-throughs and accessorial fees are added flat, never marked up.
    markup = float(os.environ.get("SHIPPING_MARKUP_PCT", "0") or 0)
    base_total = sum(float(leg.get("carrier_base") or 0)
                     for leg in (q.get("legs") or []))
    ship = round(float(q["order_shipping_total"]) + base_total * (markup / 100.0), 2)
    carriers = {leg.get("warehouse"): leg.get("carrier")
                for leg in (q.get("legs") or [])}
    return {"ok": True, "shipping": ship,
            "detail": (f"carriers {carriers}, residential="
                       f"{q.get('residential')} ({q.get('residential_source')})"
                       + (f", markup {markup}% on carrier base" if markup else ""))}


def run_auto_invoice(order_id: str, triggered_by: str = "new_order",
                     dry_run: bool = False) -> Dict:
    """BEAT C. Gate-check, quote, link, SEND. dry_run computes everything
    but creates no link and sends nothing."""
    out = {"order_id": order_id, "triggered_by": triggered_by,
           "dry_run": dry_run}

    if os.environ.get("AUTO_INVOICE_ENABLED", "true").lower() != "true":
        out.update(status="disabled")
        return out
    from test_registry import test_order_ids
    if str(order_id) in test_order_ids():
        out.update(status="skipped", reason="test-registry order")
        return out
    order = get_order_by_id(order_id)
    if not order:
        out.update(status="error", reason="order not found")
        return out
    if order.get("payment_link_sent"):
        out.update(status="skipped", reason="already invoiced")
        return out
    if order.get("payment_received"):
        out.update(status="skipped", reason="already paid")
        return out
    email = (order.get("email") or "").strip()
    total = float(order.get("order_total") or 0)
    if not email or "@" not in email:
        return _needs_human(out, order_id, "no customer email on the order")
    if total <= 0:
        return _needs_human(out, order_id, "order total is zero")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM order_line_items WHERE order_id = %s",
                        (order_id,))
            if not cur.fetchone()[0]:
                return _needs_human(out, order_id, "no line items synced yet")

    ship = auto_shipping(order_id, order)
    if not ship["ok"]:
        return _needs_human(out, order_id, f"freight quote failed — "
                            f"{ship['reason']}")

    tariff_rate = 0.08
    tariff = round(total * tariff_rate, 2)
    grand = round(total + tariff + ship["shipping"], 2)
    out.update(totals={"total_items": total, "tariff_amount": tariff,
                       "tariff_rate": tariff_rate,
                       "total_shipping": ship["shipping"],
                       "grand_total": grand},
               shipping_detail=ship["detail"], to=email)
    if dry_run:
        out.update(status="dry_run")
        return out

    try:
        from square_links import create_payment_link
        link = create_payment_link(order_id, grand)
    except Exception as e:
        return _needs_human(out, order_id, f"Square link creation failed — {e}")
    _event(order_id, "payment_link_created",
           {"link_id": link["id"], "url": link["url"], "amount": grand,
            "auto": True})

    order_data = dict(order)
    order_data["order_id"] = order_id
    order_data["shipping_result"] = out["totals"]
    order_data["payment_link"] = link["url"]
    try:
        from checkout import generate_checkout_token
        base = os.environ.get("CHECKOUT_BASE_URL", "").strip() or \
            "https://cfcorderbackend-sandbox.onrender.com"
        ctok = generate_checkout_token(str(order_id), long_lived=True)
        order_data["confirm_commercial_url"] = \
            f"{base}/checkout/{order_id}/confirm-commercial?token={ctok}"
    except Exception as e:
        print(f"[AUTO-INVOICE] confirm-commercial url failed {order_id}: {e}")

    from email_sender import send_order_email
    res = send_order_email(order_id, "payment_link", email,
                           order_data=order_data,
                           triggered_by=f"auto_invoice_{triggered_by}")
    if not res.get("success"):
        return _needs_human(out, order_id,
                            f"send failed — {res.get('error')} "
                            f"(link {link['url']} already exists; reuse it)")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE orders SET payment_link_sent = TRUE,
                           payment_link_sent_at = NOW(), updated_at = NOW()
                           WHERE order_id = %s""", (order_id,))
            conn.commit()
    _event(order_id, "invoice_auto_sent",
           {"to": res.get("to") or email, "grand_total": grand,
            "link_id": link["id"], "redirected": (res.get("to") or email) != email})
    try:
        from b2bwave_status import on_payment_link_sent
        on_payment_link_sent(order_id)
    except Exception:
        pass
    out.update(status="sent", sent_to=res.get("to") or email,
               payment_link=link["url"], payment_link_id=link["id"])
    print(f"[AUTO-INVOICE] SENT order {order_id} -> {out['sent_to']} "
          f"grand ${grand:,.2f} link {link['id']}")
    return out


def _needs_human(out: Dict, order_id: str, reason: str) -> Dict:
    """Gate failed: no send. Alert orders@ with the exact manual next step.
    Dry runs report the reason but never email or log — drills stay silent."""
    out.update(status="needs_human", reason=reason)
    if out.get("dry_run"):
        return out
    _event(order_id, "auto_invoice_needs_human", {"reason": reason})
    try:
        from supplier_orders import _send_email
        _send_email(order_id, INTERNAL_ALERT,
                    f"AUTO-INVOICE NEEDS A HUMAN: order #{order_id}",
                    f"<p>Auto-invoice could not run for order "
                    f"<strong>#{order_id}</strong>.</p>"
                    f"<p><strong>Reason:</strong> {reason}</p>"
                    f"<p>Manual path: POST /orders/{order_id}/draft-invoice"
                    f"?shipping=X (the draft lands with an auto Square link).</p>",
                    triggered_by="auto_invoice_gate")
    except Exception as e:
        print(f"[AUTO-INVOICE] alert failed {order_id}: {e}")
    return out


def kill_order_links(order_id: str) -> Dict:
    """CANCELED ORDER -> THE LINK DIES TOO (William 2026-07-27). Deletes every
    payment link ever created for the order (from payment_link_created
    events)."""
    from psycopg2.extras import RealDictCursor
    killed, errors = [], []
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT event_data FROM order_events
                           WHERE order_id = %s
                           AND event_type = 'payment_link_created'""",
                        (order_id,))
            rows = cur.fetchall()
    from square_links import delete_payment_link
    for r in rows:
        data = r["event_data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                continue
        lid = (data or {}).get("link_id")
        if not lid:
            continue
        try:
            delete_payment_link(lid)
            killed.append(lid)
        except Exception as e:
            errors.append(f"{lid}: {e}")
    if killed:
        _event(order_id, "payment_link_deleted",
               {"killed": killed, "on": "order cancel"})
    return {"status": "ok", "killed": killed, "errors": errors}
