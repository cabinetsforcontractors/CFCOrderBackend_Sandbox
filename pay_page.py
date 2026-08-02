"""
pay_page.py — THE PAY-SCREEN (item 6 of William's build-1-7, 2026-07-29;
the target workflow ruled this morning: "you get the payload from .net or
.com, you present that screen and the user pays for the order right then
and there").

Flow:
  GET  /pay/{order_id}            — tokenless landing (this is the URL the
                                    storefront "proceed with order" button
                                    and, later, the B2BWave email template
                                    can carry — only the order number
                                    needed). Asks the customer to verify
                                    with the billing ZIP or the email on
                                    the order.
  POST /pay/{order_id}/verify     — checks ZIP/email; on match hands back
                                    the tokened invoice-page URL.
  GET  /pay/{order_id}/invoice    — token-gated INVOICE SCREEN: the exact
                                    v4 invoice (same renderer as the email
                                    — beats 1+2 by construction: totals via
                                    auto_invoice's quote path, Square link
                                    via square_links INV-{id}, pay-by-check
                                    variant included). Already-paid orders
                                    see a receipt note; a failed quote shows
                                    "being prepared" and alerts a human.

Numbers/link reuse: when a payment_link_created event exists the page
reuses that link + amounts (page and email can never disagree); otherwise
it quotes live and mints the link itself (event-registered so cancel-kill
and square_sync know it).
"""

import json
import os
from typing import Optional

from fastapi import APIRouter, Body
from fastapi.responses import HTMLResponse

from db_helpers import get_db, get_order_by_id
from checkout import generate_checkout_token, verify_checkout_token

pay_router = APIRouter(tags=["pay-page"])

BASE_URL = (os.environ.get("CHECKOUT_BASE_URL", "").strip()
            or "https://cfcorderbackend-sandbox.onrender.com")

_SHELL_CSS = """
body{font-family:'Open Sans',Helvetica,Arial,sans-serif;background:#f4f6f8;
margin:0;color:#333}
.wrap{max-width:720px;margin:20px auto;padding:0 10px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;
padding:24px 26px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
h1{font-size:21px;color:#1a365d;margin:0 0 6px}
.sub{color:#718096;font-size:13px;margin-bottom:16px}
input{width:100%;box-sizing:border-box;padding:11px 12px;font-size:15px;
border:1px solid #cbd5e0;border-radius:8px}
.btn{display:inline-block;background:#1D4ED8;color:#fff;border:none;
border-radius:8px;padding:12px 30px;font-size:15px;font-weight:700;
cursor:pointer;margin-top:12px}
.err{color:#c0392b;font-size:13px;margin-top:8px;display:none}
"""


def _shell(title: str, inner: str) -> str:
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,"
            f"initial-scale=1'><title>{title}</title>"
            f"<style>{_SHELL_CSS}</style></head><body>"
            f"<div class='wrap'>{inner}</div></body></html>")


@pay_router.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_landing(order_id: str):
    order = get_order_by_id(order_id)
    if not order:
        return HTMLResponse(_shell("Order not found",
                                   "<div class='card'><h1>We couldn't find "
                                   "that order</h1><p>Please check the link, "
                                   "or reply to any of our emails.</p></div>"),
                            status_code=404)
    return HTMLResponse(_shell(
        f"Order #{order_id} — Cabinets For Contractors",
        f"""<div class='card'>
<h1>Order #{order_id}</h1>
<div class='sub'>Cabinets For Contractors &bull; quick check that it's you</div>
<p style='font-size:14px'>Enter the <strong>billing ZIP code</strong> or the
<strong>email address</strong> on this order:</p>
<input id='v' placeholder='ZIP code or email' autocomplete='off'>
<div class='err' id='e'>That doesn't match this order — try again or reply
to any of our emails.</div>
<button class='btn' onclick='go()'>Continue to your invoice &rarr;</button>
<script>
function go(){{
fetch('{BASE_URL}/pay/{order_id}/verify',{{method:'POST',
headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{value:document.getElementById('v').value}})}})
.then(function(r){{return r.json()}})
.then(function(d){{if(d.url){{window.location=d.url}}else{{
document.getElementById('e').style.display='block'}}}})
.catch(function(){{document.getElementById('e').style.display='block'}});
}}
document.getElementById('v').addEventListener('keydown',
function(ev){{if(ev.key==='Enter')go()}});
</script></div>"""))


@pay_router.post("/pay/{order_id}/verify")
def pay_verify(order_id: str, payload: dict = Body(...)):
    order = get_order_by_id(order_id)
    if not order:
        return {"ok": False}
    value = str(payload.get("value") or "").strip().lower()
    if not value:
        return {"ok": False}
    zip_code = str(order.get("zip_code") or "").strip().lower()
    email = str(order.get("email") or "").strip().lower()
    ok = bool(value) and (value == zip_code or value == email)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO order_events
                    (order_id, event_type, event_data, source)
                    VALUES (%s, 'pay_page_verify', %s, 'pay_page')""",
                    (order_id, json.dumps({"ok": ok})))
            conn.commit()
    except Exception:
        pass
    if not ok:
        return {"ok": False}
    tok = generate_checkout_token(str(order_id), long_lived=True)
    return {"ok": True,
            "url": f"{BASE_URL}/pay/{order_id}/invoice?token={tok}"}


def _existing_link(order_id: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT event_data FROM order_events
                           WHERE order_id = %s
                             AND event_type = 'payment_link_created'
                           ORDER BY created_at DESC LIMIT 1""", (order_id,))
            row = cur.fetchone()
    if not row:
        return None
    data = row[0]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None
    return data if (data or {}).get("url") else None


@pay_router.get("/pay/{order_id}/invoice", response_class=HTMLResponse)
def pay_invoice(order_id: str, token: str = "", quote: str = ""):
    if not verify_checkout_token(str(order_id), token or ""):
        return HTMLResponse(_shell("Link expired",
                                   "<div class='card'><h1>This link has "
                                   "expired</h1><p>Go back and verify again, "
                                   "or reply to any of our emails.</p></div>"),
                            status_code=403)
    order = get_order_by_id(order_id)
    if not order:
        return HTMLResponse(_shell("Not found",
                                   "<div class='card'><h1>Order not found"
                                   "</h1></div>"), status_code=404)
    order["order_id"] = order_id

    if order.get("payment_received"):
        return HTMLResponse(_shell(
            f"Order #{order_id} — paid",
            f"<div class='card'><h1>Order #{order_id} is paid &#9989;</h1>"
            f"<p style='font-size:14px'>Payment received — your receipt was "
            f"emailed, and progress updates follow as your order moves. "
            f"Any questions, just reply to any of our emails.</p></div>"))

    from config import pays_by_check
    check_account = pays_by_check(order)

    total = float(order.get("order_total") or 0)
    tariff_rate = 0.08
    link = _existing_link(order_id)
    if link:
        grand = float(link.get("amount") or 0)
        tariff = round(total * tariff_rate, 2)
        shipping = round(max(0.0, grand - total - tariff), 2)
        pay_url = link.get("url")
    else:
        # QUOTE SPINNER (William 2026-08-02: "if it takes 100 seconds then
        # it takes 100 seconds — put a spinning something and a message").
        # First visit shows the spinner instantly; the meta-refresh request
        # runs the real quote while the spinner stays on screen.
        if quote != "1":
            next_url = (f"{BASE_URL}/pay/{order_id}/invoice"
                        f"?token={token}&quote=1")
            return HTMLResponse(_shell(
                f"Order #{order_id} — pricing your freight",
                f"<div class='card' style='text-align:center'>"
                f"<h1>Pricing your freight&hellip;</h1>"
                f"<div style='margin:26px auto;width:52px;height:52px;"
                f"border:6px solid #e2e8f0;border-top-color:#1D4ED8;"
                f"border-radius:50%;animation:spin 1s linear infinite'></div>"
                f"<style>@keyframes spin{{to{{transform:rotate(360deg)}}}}"
                f"</style>"
                f"<p style='font-size:14px'>We're getting your real freight "
                f"quote from the carriers right now — this can take a minute "
                f"or two. Please don't close this page; your invoice will "
                f"appear the moment the quote lands.</p>"
                f"<script>setTimeout(function(){{window.location.href="
                f"'{next_url}';}},800);</script>"
                f"</div>"))
        from auto_invoice import auto_shipping
        ship = auto_shipping(order_id, order)
        if not ship.get("ok"):
            try:
                from supplier_orders import _send_email
                _send_email(order_id, "orders@cabinetsforcontractors.com",
                            f"PAY PAGE NEEDS A HUMAN - order #{order_id}",
                            f"<p>The customer opened the pay screen for "
                            f"order <strong>#{order_id}</strong> but the "
                            f"freight quote failed: {ship.get('reason')}. "
                            f"They were told the invoice is being prepared "
                            f"— finish it by hand.</p>",
                            triggered_by="pay_page")
            except Exception:
                pass
            return HTMLResponse(_shell(
                f"Order #{order_id} — preparing",
                f"<div class='card'><h1>Your invoice is being prepared</h1>"
                f"<p style='font-size:14px'>Order #{order_id} needs a quick "
                f"human touch on the shipping quote — you'll receive the "
                f"invoice by email from orders@cabinetsforcontractors.com "
                f"shortly. Nothing needed from you right now.</p></div>"))
        shipping = float(ship["shipping"])
        tariff = round(total * tariff_rate, 2)
        grand = round(total + tariff + shipping, 2)
        pay_url = "#"
        if not check_account:
            try:
                from square_links import create_payment_link
                info = create_payment_link(order_id, grand)
                pay_url = info["url"]
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""INSERT INTO order_events
                            (order_id, event_type, event_data, source)
                            VALUES (%s, 'payment_link_created', %s,
                                    'pay_page')""",
                            (order_id, json.dumps(
                                {"link_id": info["id"], "url": info["url"],
                                 "amount": grand, "auto": True})))
                    conn.commit()
            except Exception as e:
                print(f"[PAY-PAGE] link creation failed {order_id}: {e}")

    order_data = dict(order)
    order_data["order_id"] = order_id
    order_data["payment_link"] = pay_url or "#"
    order_data["pay_by_check"] = check_account
    order_data["shipping_result"] = {
        "total_items": total, "tariff_amount": tariff,
        "tariff_rate": tariff_rate, "total_shipping": shipping,
        "grand_total": grand}
    if not order.get("is_pickup"):
        order_data.setdefault("quoted_residential", None)
    try:
        ctok = generate_checkout_token(str(order_id), long_lived=True)
        order_data["confirm_commercial_url"] = (
            f"{BASE_URL}/checkout/{order_id}/confirm-commercial?token={ctok}")
    except Exception:
        pass

    from email_templates import render_template
    invoice_html = render_template("payment_link", order_data) or ""
    return HTMLResponse(_shell(
        f"Invoice INV-{order_id} — Cabinets For Contractors",
        f"<div class='card' style='padding:6px'>{invoice_html}</div>"))
