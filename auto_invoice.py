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

2026-07-30 FIRING LAW (step 2a): every event here rides
fire_log.record_fire — full payload + seq + diff-vs-previous. Every quote
records a freight_quoted fire — INCLUDING the per-leg R+L quote numbers
(the quote-number-on-BOL law: the BOL carries the original quote number
so the quoted price HOLDS).
"""

import json
import os
from typing import Dict

from db_helpers import get_db, get_order_by_id

INTERNAL_ALERT = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL",
                                "orders@cabinetsforcontractors.com").strip()


def _event(order_id: str, event_type: str, data: Dict):
    """FIRING LAW: route through fire_log (seq + diff embedded); the old
    raw insert stays as the fallback so an auto-invoice never dies over
    bookkeeping."""
    try:
        from fire_log import record_fire
        record_fire(order_id, event_type, data, "auto_invoice")
        return
    except Exception as e:
        print(f"[AUTO-INVOICE] fire_log failed {order_id}: {e} - falling back")
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


def _pickup_too_far(order_id: str, order: Dict):
    """⚖️ 75-MILE PICKUP RULE (William 8/4): a pickup order whose
    destination sits farther than PICKUP_MAX_MILES from its warehouse
    ships anyway — shipping is added automatically. Returns
    {'miles', 'warehouse'} when over the line, None when pickup stands
    (or when distance can't be determined — never guess)."""
    try:
        from config import (WAREHOUSE_COORDS, PICKUP_MAX_MILES,
                            RL_QUOTE_SANDBOX_URL, miles_between)
        coords = [(w, WAREHOUSE_COORDS.get((order.get(f"warehouse_{i}")
                                            or "").strip()))
                  for i, w in ((1, order.get("warehouse_1")),
                               (2, order.get("warehouse_2")),
                               (3, order.get("warehouse_3")),
                               (4, order.get("warehouse_4")))]
        coords = [(w, c) for w, c in coords if w and c]
        if not coords:
            return None
        import json as _json
        import urllib.request
        body = _json.dumps({
            "street": order.get("street") or "",
            "street2": order.get("street2") or "",
            "city": order.get("city") or "",
            "state": order.get("state") or "",
            "zip_code": (order.get("zip_code") or "").split("-")[0][:5],
        }).encode()
        req = urllib.request.Request(
            f"{RL_QUOTE_SANDBOX_URL}/validate-address", data=body,
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            v = _json.loads(resp.read().decode())
        addr = v.get("address") or {}
        lat, lon = addr.get("latitude"), addr.get("longitude")
        if lat is None or lon is None:
            return None
        best = min(((w, miles_between(lat, lon, c[0], c[1]))
                    for w, c in coords), key=lambda x: x[1])
        if best[1] > PICKUP_MAX_MILES:
            return {"miles": round(best[1]), "warehouse": best[0],
                    "limit": PICKUP_MAX_MILES}
    except Exception as e:
        print(f"[AUTO-INVOICE] pickup-distance check failed {order_id}: {e}")
    return None


def auto_shipping(order_id: str, order: Dict) -> Dict:
    """BEAT A. Returns {'ok': True, 'shipping': X, 'detail': ...} or
    {'ok': False, 'reason': ...}."""
    if order.get("is_pickup"):
        far = _pickup_too_far(order_id, order)
        if not far:
            return {"ok": True, "shipping": 0.0,
                    "detail": "warehouse pickup"}
        _event(order_id, "pickup_distance_override", far)
        print(f"[AUTO-INVOICE] {order_id}: pickup marked but customer is "
              f"{far['miles']} mi from {far['warehouse']} — 75-MILE RULE, "
              f"shipping added")
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
    # QUOTE-NUMBER LAW (2026-07-30): capture every leg's R+L quote number —
    # the BOL builder reuses it so the quoted price HOLDS.
    quote_numbers = {}
    for leg in (q.get("legs") or []):
        qn = ((leg.get("alternatives") or {}).get("R+L") or {}).get("quote_number")
        if qn:
            quote_numbers[leg.get("warehouse")] = qn
    # FIRING LAW: the quote itself is a fire — a later requote diffs
    # against this one instead of silently replacing it.
    _event(order_id, "freight_quoted",
           {"shipping_charged_basis": ship,
            "carrier_total": round(float(q["order_shipping_total"]), 2),
            "carrier_base_total": round(base_total, 2),
            "markup_pct": markup,
            "carriers": carriers,
            "quote_numbers": quote_numbers,
            "residential": q.get("residential"),
            "residential_source": q.get("residential_source"),
            "legs": len(q.get("legs") or [])})
    return {"ok": True, "shipping": ship,
            "residential": q.get("residential"),
            "detail": (f"carriers {carriers}, residential="
                       f"{q.get('residential')} ({q.get('residential_source')})"
                       + (f", markup {markup}% on carrier base" if markup else ""))}


def run_auto_invoice(order_id: str, triggered_by: str = "new_order",
                     dry_run: bool = False,
                     force_reinvoice: bool = False) -> Dict:
    """BEAT C. Gate-check, quote, link, SEND. dry_run computes everything
    but creates no link and sends nothing.
    force_reinvoice (William 2026-08-02, the substitution price law): an
    ALREADY-INVOICED unpaid order re-invoices — old Square links die first
    so the stale amount can never be paid, then the updated invoice sends."""
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
    if order.get("payment_link_sent") and not force_reinvoice:
        out.update(status="skipped", reason="already invoiced")
        return out
    if order.get("payment_received"):
        out.update(status="skipped", reason="already paid")
        return out
    if force_reinvoice and not dry_run:
        try:
            killed = kill_order_links(order_id)
            out["links_killed"] = killed
        except Exception as e:
            out["links_killed"] = {"error": str(e)}
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

    # WILLIAM'S COMMENTS GATE (2026-07-28, "Beat C proceed" — the 5731
    # lesson): a customer comment can contradict the order (ship-to override,
    # special instructions). ANY non-empty comment = a human reads it first.
    comments = (order.get("comments") or "").strip()
    if comments:
        return _needs_human(out, order_id,
                            "customer comments on the order — read them before "
                            "invoicing: “" + comments[:400] + "”")

    # SUBSTITUTION INTAKE TRIGGER (William 2026-07-28, "yes"): lines that
    # don't exist in the supplier's real catalog (the B09 case) get a
    # substitution proposal (customer approves the swap) — the invoice waits.
    try:
        from catalog_check import phantom_sku_check
        phantom = phantom_sku_check(order_id).get("flagged") or []
    except Exception as e:
        print(f"[AUTO-INVOICE] catalog check failed {order_id}: {e}")
        phantom = []
    if phantom:
        bits = []
        for f in phantom:
            if f.get("candidate_sku") and not dry_run:
                try:
                    from substitutions import create_substitution_proposal
                    r = create_substitution_proposal(
                        order_id, f["sku"], f["candidate_sku"],
                        reason="catalog_error")
                    f["proposal"] = r.get("status")
                except Exception as e:
                    f["proposal"] = f"error: {e}"
            bits.append(
                f"{f['sku']}: supplier catalog has no {f['token']}"
                + (f" — substitution proposal ({f['candidate_sku']}): "
                   f"{f.get('proposal', 'dry-run, none sent')}"
                   if f.get("candidate_sku") else " — no clear substitute"))
        out["phantom_lines"] = phantom
        return _needs_human(
            out, order_id,
            "catalog check — " + "; ".join(bits) +
            f". Once resolved, re-run POST /orders/{order_id}/auto-invoice"
            f"?dry_run=false")

    ship = auto_shipping(order_id, order)
    if not ship["ok"]:
        return _needs_human(out, order_id, f"freight quote failed — "
                            f"{ship['reason']}")

    tariff_rate = 0.08
    tariff = round(total * tariff_rate, 2)
    grand = round(total + tariff + ship["shipping"], 2)
    # pay-by-check accounts (William 2026-07-29, Nationwide): invoice goes
    # out WITHOUT a Square link — check notice replaces the pay button.
    from config import pays_by_check
    check_account = pays_by_check(order)
    out.update(totals={"total_items": total, "tariff_amount": tariff,
                       "tariff_rate": tariff_rate,
                       "total_shipping": ship["shipping"],
                       "grand_total": grand},
               shipping_detail=ship["detail"], to=email,
               pay_by_check=check_account)
    # STORE CREDIT (William 2026-08-02): open credit rides this invoice as
    # a credit line after tariff and lowers the Square amount; rows flip
    # only after the send lands.
    credit_used, credit_ids = 0.0, []
    try:
        from store_credits import apply_credit_to_totals
        _od = dict(order)
        _od["shipping_result"] = out["totals"]
        grand, credit_used, credit_ids = apply_credit_to_totals(_od, grand)
        out["totals"] = _od["shipping_result"]
        if credit_used:
            out["store_credit_applied"] = credit_used
    except Exception as e:
        print(f"[AUTO-INVOICE] store-credit lookup failed {order_id}: {e}")
    if dry_run:
        out.update(status="dry_run")
        return out

    link = None
    if not check_account:
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
    order_data["payment_link"] = link["url"] if link else "#"
    order_data["pay_by_check"] = check_account
    # classification box mirrors the actual quote (William 2026-07-28)
    if not order.get("is_pickup"):
        order_data["quoted_residential"] = ship.get("residential")
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
                            f"send failed — {res.get('error')}"
                            + (f" (link {link['url']} already exists; "
                               f"reuse it)" if link else ""))
    if credit_used and credit_ids:
        try:
            from store_credits import mark_credits_applied
            mark_credits_applied(credit_ids, order_id, credit_used)
        except Exception as e:
            print(f"[AUTO-INVOICE] store-credit apply-mark failed {order_id}: {e}")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE orders SET payment_link_sent = TRUE,
                           payment_link_sent_at = NOW(), updated_at = NOW()
                           WHERE order_id = %s""", (order_id,))
            conn.commit()
    _event(order_id, "invoice_auto_sent",
           {"to": res.get("to") or email, "grand_total": grand,
            "total_items": total, "tariff_amount": tariff,
            "total_shipping": ship["shipping"],
            "link_id": link["id"] if link else None,
            "pay_by_check": check_account,
            "redirected": (res.get("to") or email) != email})
    try:
        from b2bwave_status import on_payment_link_sent
        on_payment_link_sent(order_id)
    except Exception:
        pass
    out.update(status="sent", sent_to=res.get("to") or email,
               payment_link=link["url"] if link else None,
               payment_link_id=link["id"] if link else None)
    print(f"[AUTO-INVOICE] SENT order {order_id} -> {out['sent_to']} "
          f"grand ${grand:,.2f} "
          f"{'pay-by-check' if check_account else 'link ' + link['id']}")
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
