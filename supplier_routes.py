"""
supplier_routes.py
WS6 Phase 9 — Supplier-facing public HTML endpoints (no login required, token-authenticated)

All endpoints are public — the warehouse receives a link via email and clicks it.
Token is stored on order_shipments.supplier_token (generated at send-to-warehouse time).

Endpoints:
    GET  /supplier/{token}/date-form        — HTML form: "When will this ship?"
    POST /supplier/{token}/set-date         — Warehouse submits expected date
    GET  /supplier/{token}/confirm-tomorrow — Day-before YES link (warehouse confirms)
    GET  /supplier/{token}/time-form        — HTML form: "What time will it be ready?"
    POST /supplier/{token}/set-time         — Warehouse submits pickup time → BOL fires
    GET  /supplier/{token}/push-date        — Day-before NO link (warehouse enters new date)
    POST /supplier/{token}/submit-push-date — Warehouse submits new date after pushing

Admin endpoints:
    POST /supplier/{shipment_id}/send-poll  — Manually re-send poll to warehouse [admin]
"""

from datetime import date as date_today
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional

from auth import require_admin
from supplier_polling_engine import (
    get_shipment_by_token,
    warehouse_set_date,
    warehouse_confirm_tomorrow,
    warehouse_push_date,
    warehouse_set_pickup_time,
    send_initial_poll,
)

supplier_router = APIRouter(tags=["supplier"])


def _error_page(message: str, title: str = "Error") -> str:
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,sans-serif;background:#f5f5f5;padding:40px;text-align:center;}}
.card{{max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:40px;box-shadow:0 2px 8px rgba(0,0,0,.1);}}</style>
</head>
<body><div class="card"><h2>⚠️ {title}</h2><p>{message}</p>
<p style="font-size:14px;color:#666;">Questions? Call (770) 990-4885</p></div></body></html>"""


def _success_page(title: str, message: str, extra_html: str = "") -> str:
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,sans-serif;background:#f5f5f5;padding:40px;}}
.card{{max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:40px;box-shadow:0 2px 8px rgba(0,0,0,.1);text-align:center;}}
h2{{color:#1a365d;}} .ok{{font-size:52px;margin-bottom:16px;}}
p{{color:#4a5568;font-size:15px;line-height:1.7;}}
.contact{{margin-top:24px;font-size:13px;color:#999;}}</style>
</head>
<body><div class="card"><div class="ok">✅</div>
<h2>{title}</h2><p>{message}</p>
{extra_html}
<div class="contact">Questions? Call (770) 990-4885</div>
</div></body></html>"""


# =============================================================================
# DATE FORM — warehouse enters expected ship date
# =============================================================================

@supplier_router.get("/supplier/{token}/date-form", response_class=HTMLResponse)
def date_form(token: str):
    """Serve the ship date entry form to the warehouse."""
    shipment = get_shipment_by_token(token)
    if not shipment:
        return HTMLResponse(_error_page("This link is invalid or has expired."), status_code=404)

    order_id = shipment["order_id"]
    warehouse = shipment["warehouse"]
    customer = shipment.get("company_name") or shipment.get("customer_name") or "Customer"
    order_total = float(shipment.get("order_total") or 0)
    existing_date = shipment.get("pickup_date")
    existing_str = existing_date.strftime("%Y-%m-%d") if hasattr(existing_date, "strftime") else ""
    today_str = date_today.today().isoformat()

    existing_banner = ""
    if existing_str:
        existing_banner = f"<div style='background:#d1fae5;border:1px solid #6ee7b7;border-radius:6px;padding:12px;margin-bottom:16px;font-size:14px;color:#065f46;'>Current date on file: <strong>{existing_str}</strong> — you may update it below.</div>"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Order #{order_id} — Ship Date</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        * {{ box-sizing:border-box;margin:0;padding:0; }}
        body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:24px; }}
        .card {{ max-width:520px;margin:0 auto;background:white;border-radius:10px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.1); }}
        h1 {{ color:#1a365d;font-size:22px;margin-bottom:6px; }}
        .subtitle {{ color:#718096;font-size:14px;margin-bottom:24px; }}
        table {{ width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px; }}
        td {{ padding:6px 0;color:#4a5568; }}
        td:first-child {{ color:#718096;width:130px; }}
        label {{ display:block;font-weight:600;color:#1a365d;margin-bottom:6px;font-size:15px; }}
        input[type=date] {{ width:100%;padding:12px;border:2px solid #e2e8f0;border-radius:6px;
                           font-size:16px;font-family:inherit;margin-bottom:20px; }}
        input[type=date]:focus {{ outline:none;border-color:#2563eb; }}
        button {{ width:100%;background:#1a365d;color:white;padding:14px;border:none;border-radius:6px;
                 font-size:16px;font-weight:700;cursor:pointer;font-family:inherit; }}
        button:hover {{ background:#153059; }}
        .note {{ font-size:12px;color:#999;margin-top:16px;text-align:center; }}
    </style>
</head>
<body>
<div class="card">
    <h1>Order #{order_id} — Ship Date</h1>
    <div class="subtitle">Cabinets For Contractors</div>
    {existing_banner}
    <table>
        <tr><td>Customer:</td><td><strong>{customer}</strong></td></tr>
        <tr><td>Order Total:</td><td><strong>${order_total:,.2f}</strong></td></tr>
        <tr><td>Warehouse:</td><td>{warehouse}</td></tr>
    </table>
    <form method="POST" action="/supplier/{token}/set-date">
        <label for="pickup_date">When will this order be ready for R+L pickup?</label>
        <input type="date" id="pickup_date" name="pickup_date" value="{existing_str}" required min="{today_str}">
        <button type="submit">Confirm Ship Date →</button>
    </form>
    <div class="note">Questions? Call (770) 990-4885 or reply to the email you received.</div>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@supplier_router.post("/supplier/{token}/set-date", response_class=HTMLResponse)
async def set_date(token: str, request: Request):
    """Warehouse submits expected ship date."""
    form = await request.form()
    pickup_date_str = form.get("pickup_date", "")

    if not pickup_date_str:
        return HTMLResponse(_error_page("Please enter a date."), status_code=400)

    result = warehouse_set_date(token, pickup_date_str)

    if not result.get("success"):
        return HTMLResponse(_error_page(result.get("error", "Something went wrong.")), status_code=400)

    try:
        from datetime import datetime
        dt = datetime.strptime(pickup_date_str, "%Y-%m-%d")
        date_display = dt.strftime("%A, %B %d, %Y")
    except Exception:
        date_display = pickup_date_str

    return HTMLResponse(_success_page(
        title="Ship Date Confirmed",
        message=f"Thank you. We've recorded the pickup date as <strong>{date_display}</strong>.<br><br>"
                f"R+L Carriers will be scheduled to pick up on that date. "
                f"You'll receive a confirmation email the day before with the Bill of Lading."
    ))


# =============================================================================
# DAY-BEFORE — YES branch (warehouse confirms, then enters time)
# =============================================================================

@supplier_router.get("/supplier/{token}/confirm-tomorrow", response_class=HTMLResponse)
def confirm_tomorrow(token: str):
    """Warehouse clicked YES on day-before poll. Show time entry form."""
    shipment = get_shipment_by_token(token)
    if not shipment:
        return HTMLResponse(_error_page("This link is invalid or has expired."), status_code=404)

    result = warehouse_confirm_tomorrow(token)
    if not result.get("success"):
        return HTMLResponse(_error_page(result.get("error", "Something went wrong.")), status_code=400)

    order_id = shipment["order_id"]
    pickup_date = shipment.get("pickup_date")
    date_display = pickup_date.strftime("%A, %B %d") if hasattr(pickup_date, "strftime") else "tomorrow"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Order #{order_id} — Pickup Time</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        * {{ box-sizing:border-box;margin:0;padding:0; }}
        body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:24px; }}
        .card {{ max-width:520px;margin:0 auto;background:white;border-radius:10px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.1); }}
        h1 {{ color:#1a365d;font-size:22px;margin-bottom:16px; }}
        .confirmed {{ background:#d1fae5;border:1px solid #6ee7b7;border-radius:6px;padding:12px 16px;
                     color:#065f46;font-weight:600;font-size:14px;margin-bottom:20px; }}
        label {{ display:block;font-weight:600;color:#1a365d;margin-bottom:6px;font-size:15px; }}
        select {{ width:100%;padding:12px;border:2px solid #e2e8f0;border-radius:6px;
                 font-size:16px;font-family:inherit;margin-bottom:20px;background:white; }}
        select:focus {{ outline:none;border-color:#2563eb; }}
        button {{ width:100%;background:#059669;color:white;padding:14px;border:none;border-radius:6px;
                 font-size:16px;font-weight:700;cursor:pointer;font-family:inherit; }}
        button:hover {{ background:#047857; }}
        .note {{ font-size:12px;color:#999;margin-top:16px;text-align:center; }}
    </style>
</head>
<body>
<div class="card">
    <h1>Order #{order_id} — Pickup Time</h1>
    <div class="confirmed">✅ Confirmed — Order is on track for {date_display}</div>
    <p style="color:#4a5568;font-size:14px;margin-bottom:20px;">
        One more step: what time will this order be ready for R+L pickup?
        When you submit, we'll create the Bill of Lading and email it to you.
    </p>
    <form method="POST" action="/supplier/{token}/set-time">
        <label for="pickup_time">Pickup Ready Time</label>
        <select id="pickup_time" name="pickup_time" required>
            <option value="">— Select time —</option>
            <option value="7:00 AM">7:00 AM</option>
            <option value="8:00 AM">8:00 AM</option>
            <option value="9:00 AM">9:00 AM</option>
            <option value="10:00 AM">10:00 AM</option>
            <option value="11:00 AM">11:00 AM</option>
            <option value="12:00 PM">12:00 PM (Noon)</option>
            <option value="1:00 PM">1:00 PM</option>
            <option value="2:00 PM">2:00 PM</option>
            <option value="3:00 PM">3:00 PM</option>
            <option value="4:00 PM">4:00 PM</option>
            <option value="5:00 PM">5:00 PM</option>
        </select>
        <button type="submit">Generate Bill of Lading →</button>
    </form>
    <div class="note">This will create the BOL and schedule R+L pickup automatically.</div>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@supplier_router.post("/supplier/{token}/set-time", response_class=HTMLResponse)
async def set_time(token: str, request: Request):
    """Warehouse submits pickup time → fires BOL."""
    form = await request.form()
    pickup_time_str = form.get("pickup_time", "")

    if not pickup_time_str:
        return HTMLResponse(_error_page("Please select a pickup time."), status_code=400)

    result = warehouse_set_pickup_time(token, pickup_time_str)

    if not result.get("success"):
        return HTMLResponse(_error_page(
            f"BOL could not be created: {result.get('error', 'Unknown error')}. Please call (770) 990-4885."
        ), status_code=500)

    pro_number = result.get("pro_number", "")

    return HTMLResponse(_success_page(
        title="Bill of Lading Created",
        message=f"The Bill of Lading has been created and R+L Carriers has been notified.<br><br>"
                f"Pickup is scheduled for <strong>{pickup_time_str}</strong>.",
        extra_html=f"""<div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;
                       padding:16px;margin:16px 0;">
            <div style="font-size:12px;color:#666;margin-bottom:4px;">PRO Number (R+L Tracking)</div>
            <div style="font-size:22px;font-weight:700;font-family:monospace;color:#059669;">{pro_number}</div>
        </div>
        <p style="font-size:14px;color:#4a5568;">A copy of the BOL details has been emailed to you.</p>"""
    ))


# =============================================================================
# DAY-BEFORE — NO branch (warehouse pushes date)
# =============================================================================

@supplier_router.get("/supplier/{token}/push-date", response_class=HTMLResponse)
def push_date_form(token: str):
    """Warehouse clicked NO on day-before poll. Show new date form."""
    shipment = get_shipment_by_token(token)
    if not shipment:
        return HTMLResponse(_error_page("This link is invalid or has expired."), status_code=404)

    order_id = shipment["order_id"]
    pickup_date = shipment.get("pickup_date")
    old_date_display = pickup_date.strftime("%A, %B %d") if hasattr(pickup_date, "strftime") else "the scheduled date"
    today_str = date_today.today().isoformat()

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Order #{order_id} — New Date</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        * {{ box-sizing:border-box;margin:0;padding:0; }}
        body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:24px; }}
        .card {{ max-width:520px;margin:0 auto;background:white;border-radius:10px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.1); }}
        h1 {{ color:#1a365d;font-size:22px;margin-bottom:16px; }}
        .notice {{ background:#fffbeb;border:1px solid #fcd34d;border-radius:6px;padding:12px 16px;
                  color:#92400e;font-size:14px;margin-bottom:20px; }}
        label {{ display:block;font-weight:600;color:#1a365d;margin-bottom:6px;font-size:15px; }}
        input[type=date] {{ width:100%;padding:12px;border:2px solid #e2e8f0;border-radius:6px;
                           font-size:16px;font-family:inherit;margin-bottom:20px; }}
        input[type=date]:focus {{ outline:none;border-color:#2563eb; }}
        button {{ width:100%;background:#DC2626;color:white;padding:14px;border:none;border-radius:6px;
                 font-size:16px;font-weight:700;cursor:pointer;font-family:inherit; }}
        .note {{ font-size:12px;color:#999;margin-top:16px;text-align:center; }}
    </style>
</head>
<body>
<div class="card">
    <h1>Order #{order_id} — New Pickup Date</h1>
    <div class="notice">⚠️ You indicated the order will not be ready on <strong>{old_date_display}</strong>.
    Please enter the new expected date below.</div>
    <form method="POST" action="/supplier/{token}/submit-push-date">
        <label for="new_date">New Expected Pickup Date</label>
        <input type="date" id="new_date" name="new_date" required min="{today_str}">
        <button type="submit">Submit New Date →</button>
    </form>
    <div class="note">Questions? Call (770) 990-4885.</div>
</div>
</body>
</html>"""
    return HTMLResponse(html)


@supplier_router.post("/supplier/{token}/submit-push-date", response_class=HTMLResponse)
async def submit_push_date(token: str, request: Request):
    """Warehouse submits a new date after pushing."""
    form = await request.form()
    new_date_str = form.get("new_date", "")

    if not new_date_str:
        return HTMLResponse(_error_page("Please enter a new date."), status_code=400)

    result = warehouse_push_date(token, new_date_str)

    if not result.get("success"):
        return HTMLResponse(_error_page(result.get("error", "Something went wrong.")), status_code=400)

    try:
        from datetime import datetime
        dt = datetime.strptime(new_date_str, "%Y-%m-%d")
        date_display = dt.strftime("%A, %B %d, %Y")
    except Exception:
        date_display = new_date_str

    cfc_alerted = result.get("cfc_alerted", False)
    extra = ""
    if cfc_alerted:
        extra = "<p style='font-size:13px;color:#D97706;margin-top:12px;'>The Cabinets For Contractors team has been notified of the date change.</p>"

    return HTMLResponse(_success_page(
        title="New Date Recorded",
        message=f"Thank you. We've updated the pickup date to <strong>{date_display}</strong>.<br><br>"
                f"You'll receive a confirmation email the day before.",
        extra_html=extra
    ))


# =============================================================================
# ADMIN: manually send / re-send poll
# =============================================================================

@supplier_router.post("/supplier/{shipment_id}/send-poll")
def admin_send_poll(shipment_id: str, _: bool = Depends(require_admin)):
    """Admin manually sends or re-sends the initial poll to the warehouse."""
    result = send_initial_poll(shipment_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return {"status": "ok", **result}
