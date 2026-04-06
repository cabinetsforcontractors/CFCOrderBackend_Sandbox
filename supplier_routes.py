"""
supplier_routes.py
WS6 Phase 9 — Supplier-facing public HTML endpoints (no login required, token-authenticated)

Flow:
  GET  /supplier/{token}/date-form        — combined date + pickup time form
  POST /supplier/{token}/set-date         — warehouse submits date+time → BOL fires → emailed to warehouse
  GET  /supplier/{token}/confirm-tomorrow — day-before YES → time entry form
  POST /supplier/{token}/set-time         — warehouse submits time → BOL fires
  GET  /supplier/{token}/push-date        — day-before NO → new date form
  POST /supplier/{token}/submit-push-date — warehouse submits new date
  POST /supplier/{shipment_id}/send-poll  — admin re-send poll [admin]
"""

from datetime import date as date_today
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse

from auth import require_admin
from supplier_polling_engine import (
    get_shipment_by_token,
    warehouse_set_date,
    warehouse_confirm_tomorrow,
    warehouse_push_date,
    warehouse_set_pickup_time,
    send_initial_poll,
    _confirm_for_immediate_bol,
)

supplier_router = APIRouter(tags=["supplier"])


def _time_options(selected: str = "") -> str:
    """Generate 15-minute increment time options 7:00 AM – 5:00 PM."""
    options = ['<option value="">— Select pickup time —</option>']
    for hour in range(7, 18):
        for minute in (0, 15, 30, 45):
            if hour == 17 and minute > 0:
                break
            period = "AM" if hour < 12 else "PM"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            t = f"{display_hour}:{minute:02d} {period}"
            sel = ' selected' if t == selected else ''
            options.append(f'<option value="{t}"{sel}>{t}</option>')
    return "\n".join(options)


def _error_page(message: str, title: str = "Error") -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,sans-serif;background:#f5f5f5;padding:40px;text-align:center;}}
.card{{max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:40px;box-shadow:0 2px 8px rgba(0,0,0,.1);}}</style>
</head><body><div class="card"><h2>⚠️ {title}</h2><p>{message}</p>
<p style="font-size:14px;color:#666;">Questions? Call (770) 990-4885</p></div></body></html>"""


def _success_page(title: str, message: str, extra_html: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,sans-serif;background:#f5f5f5;padding:40px;}}
.card{{max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:40px;box-shadow:0 2px 8px rgba(0,0,0,.1);text-align:center;}}
h2{{color:#1a365d;}} .ok{{font-size:52px;margin-bottom:16px;}}
p{{color:#4a5568;font-size:15px;line-height:1.7;}}
.contact{{margin-top:24px;font-size:13px;color:#999;}}</style>
</head><body><div class="card"><div class="ok">✅</div>
<h2>{title}</h2><p>{message}</p>{extra_html}
<div class="contact">Questions? Call (770) 990-4885</div>
</div></body></html>"""


# =============================================================================
# COMBINED DATE + TIME FORM
# =============================================================================

@supplier_router.get("/supplier/{token}/date-form", response_class=HTMLResponse)
def date_form(token: str):
    """Combined ship date + pickup time form."""
    shipment = get_shipment_by_token(token)
    if not shipment:
        return HTMLResponse(_error_page("This link is invalid or has expired."), status_code=404)

    order_id = shipment["order_id"]
    warehouse = shipment["warehouse"]
    customer = shipment.get("company_name") or shipment.get("customer_name") or "Customer"
    order_total = float(shipment.get("order_total") or 0)
    existing_date = shipment.get("pickup_date")
    existing_str = existing_date.strftime("%Y-%m-%d") if hasattr(existing_date, "strftime") else ""
    existing_time = shipment.get("pickup_time") or ""
    today_str = date_today.today().isoformat()
    time_opts = _time_options(existing_time)

    existing_banner = ""
    if existing_str:
        existing_banner = f"<div style='background:#d1fae5;border:1px solid #6ee7b7;border-radius:6px;padding:12px;margin-bottom:16px;font-size:14px;color:#065f46;'>Date on file: <strong>{existing_str}</strong> — you may update below.</div>"

    html = f"""<!DOCTYPE html>
<html><head>
    <title>Order #{order_id} — Ship Date &amp; Time</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        *{{box-sizing:border-box;margin:0;padding:0;}}
        body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:24px;}}
        .card{{max-width:520px;margin:0 auto;background:white;border-radius:10px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.1);}}
        h1{{color:#1a365d;font-size:22px;margin-bottom:6px;}}
        .subtitle{{color:#718096;font-size:14px;margin-bottom:24px;}}
        table{{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:24px;}}
        td{{padding:6px 0;color:#4a5568;}} td:first-child{{color:#718096;width:130px;}}
        label{{display:block;font-weight:600;color:#1a365d;margin-bottom:6px;font-size:15px;}}
        .field{{margin-bottom:20px;}}
        input[type=date],select{{width:100%;padding:12px;border:2px solid #e2e8f0;border-radius:6px;font-size:16px;font-family:inherit;background:white;}}
        input[type=date]:focus,select:focus{{outline:none;border-color:#2563eb;}}
        hr{{border:none;border-top:1px solid #e2e8f0;margin:20px 0;}}
        button{{width:100%;background:#059669;color:white;padding:14px;border:none;border-radius:6px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit;}}
        button:hover{{background:#047857;}}
        .bol-note{{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:10px 14px;font-size:13px;color:#1E40AF;margin-bottom:20px;}}
        .note{{font-size:12px;color:#999;margin-top:16px;text-align:center;}}
    </style>
</head><body>
<div class="card">
    <h1>Order #{order_id}</h1>
    <div class="subtitle">Cabinets For Contractors — Ship Date &amp; Pickup Time</div>
    {existing_banner}
    <table>
        <tr><td>Customer:</td><td><strong>{customer}</strong></td></tr>
        <tr><td>Order Total:</td><td><strong>${order_total:,.2f}</strong></td></tr>
        <tr><td>Warehouse:</td><td>{warehouse}</td></tr>
    </table>
    <div class="bol-note">📄 The Bill of Lading will be emailed to you automatically when you submit.</div>
    <form method="POST" action="/supplier/{token}/set-date">
        <div class="field">
            <label for="pickup_date">Pickup Date</label>
            <input type="date" id="pickup_date" name="pickup_date" value="{existing_str}" required min="{today_str}">
        </div>
        <hr>
        <div class="field">
            <label for="pickup_time">Pickup Ready Time</label>
            <select id="pickup_time" name="pickup_time" required>
                {time_opts}
            </select>
        </div>
        <button type="submit">Confirm &amp; Generate BOL →</button>
    </form>
    <div class="note">Questions? Call (770) 990-4885 or reply to the email you received.</div>
</div>
</body></html>"""
    return HTMLResponse(html)


@supplier_router.post("/supplier/{token}/set-date", response_class=HTMLResponse)
async def set_date(token: str, request: Request):
    """Warehouse submits date + time → stores both, fires BOL, emails BOL to warehouse."""
    try:
        form = await request.form()
        pickup_date_str = form.get("pickup_date", "")
        pickup_time_str = form.get("pickup_time", "")
    except Exception as e:
        return HTMLResponse(_error_page(f"Could not read form: {str(e)}"), status_code=400)

    if not pickup_date_str:
        return HTMLResponse(_error_page("Please enter a pickup date."), status_code=400)
    if not pickup_time_str:
        return HTMLResponse(_error_page("Please select a pickup time."), status_code=400)

    # Store date
    try:
        date_result = warehouse_set_date(token, pickup_date_str)
    except Exception as e:
        return HTMLResponse(_error_page(f"Error saving date: {str(e)}. Call (770) 990-4885."), status_code=500)

    if not date_result.get("success"):
        return HTMLResponse(_error_page(date_result.get("error", "Could not save date. Call (770) 990-4885.")), status_code=400)

    # Fire BOL immediately
    try:
        bol_result = _confirm_for_immediate_bol(token, pickup_time_str)
    except Exception as e:
        return HTMLResponse(_error_page(f"Date saved but BOL failed: {str(e)}. Call (770) 990-4885."), status_code=500)

    try:
        from datetime import datetime
        dt = datetime.strptime(pickup_date_str, "%Y-%m-%d")
        date_display = dt.strftime("%A, %B %d, %Y")
    except Exception:
        date_display = pickup_date_str

    if bol_result.get("success"):
        return HTMLResponse(_success_page(
            title="You're All Set",
            message=f"Pickup confirmed for <strong>{date_display}</strong> at <strong>{pickup_time_str}</strong>.<br><br>"
                    f"The Bill of Lading has been generated and emailed to you. "
                    f"R+L Carriers will arrive for pickup as scheduled.",
        ))
    else:
        # Date saved, BOL failed — still a partial success
        return HTMLResponse(_success_page(
            title="Date Confirmed",
            message=f"Pickup date recorded as <strong>{date_display}</strong> at <strong>{pickup_time_str}</strong>.<br><br>"
                    f"⚠️ The BOL could not be auto-generated ({bol_result.get('error', 'unknown error')}). "
                    f"Please call (770) 990-4885 and we'll send it manually.",
        ))


# =============================================================================
# DAY-BEFORE — YES branch (nightly cron flow)
# =============================================================================

@supplier_router.get("/supplier/{token}/confirm-tomorrow", response_class=HTMLResponse)
def confirm_tomorrow(token: str):
    """Warehouse clicked YES on day-before poll — show time entry form."""
    shipment = get_shipment_by_token(token)
    if not shipment:
        return HTMLResponse(_error_page("This link is invalid or has expired."), status_code=404)

    result = warehouse_confirm_tomorrow(token)
    if not result.get("success"):
        return HTMLResponse(_error_page(result.get("error", "Something went wrong.")), status_code=400)

    order_id = shipment["order_id"]
    pickup_date = shipment.get("pickup_date")
    date_display = pickup_date.strftime("%A, %B %d") if hasattr(pickup_date, "strftime") else "tomorrow"
    time_opts = _time_options()

    html = f"""<!DOCTYPE html>
<html><head>
    <title>Order #{order_id} — Pickup Time</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        *{{box-sizing:border-box;margin:0;padding:0;}}
        body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:24px;}}
        .card{{max-width:520px;margin:0 auto;background:white;border-radius:10px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.1);}}
        h1{{color:#1a365d;font-size:22px;margin-bottom:16px;}}
        .confirmed{{background:#d1fae5;border:1px solid #6ee7b7;border-radius:6px;padding:12px 16px;color:#065f46;font-weight:600;font-size:14px;margin-bottom:20px;}}
        label{{display:block;font-weight:600;color:#1a365d;margin-bottom:6px;font-size:15px;}}
        select{{width:100%;padding:12px;border:2px solid #e2e8f0;border-radius:6px;font-size:16px;font-family:inherit;margin-bottom:20px;background:white;}}
        select:focus{{outline:none;border-color:#2563eb;}}
        button{{width:100%;background:#059669;color:white;padding:14px;border:none;border-radius:6px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit;}}
        .note{{font-size:12px;color:#999;margin-top:16px;text-align:center;}}
    </style>
</head><body>
<div class="card">
    <h1>Order #{order_id} — Pickup Time</h1>
    <div class="confirmed">✅ Confirmed — Order is on track for {date_display}</div>
    <p style="color:#4a5568;font-size:14px;margin-bottom:20px;">
        What time will this order be ready for R+L pickup?
        The Bill of Lading will be emailed to you when you submit.
    </p>
    <form method="POST" action="/supplier/{token}/set-time">
        <label for="pickup_time">Pickup Ready Time</label>
        <select id="pickup_time" name="pickup_time" required>
            {time_opts}
        </select>
        <button type="submit">Generate &amp; Email BOL →</button>
    </form>
    <div class="note">Questions? Call (770) 990-4885.</div>
</div>
</body></html>"""
    return HTMLResponse(html)


@supplier_router.post("/supplier/{token}/set-time", response_class=HTMLResponse)
async def set_time(token: str, request: Request):
    """Day-before time entry → fires BOL, emails to warehouse."""
    try:
        form = await request.form()
        pickup_time_str = form.get("pickup_time", "")
    except Exception as e:
        return HTMLResponse(_error_page(f"Could not read form: {str(e)}"), status_code=400)

    if not pickup_time_str:
        return HTMLResponse(_error_page("Please select a pickup time."), status_code=400)

    try:
        result = warehouse_set_pickup_time(token, pickup_time_str)
    except Exception as e:
        return HTMLResponse(_error_page(f"Unexpected error: {str(e)}. Call (770) 990-4885."), status_code=500)

    if not result.get("success"):
        return HTMLResponse(_error_page(
            f"Could not generate BOL: {result.get('error', 'Unknown error')}. Call (770) 990-4885."
        ), status_code=500)

    return HTMLResponse(_success_page(
        title="BOL Created — You're All Set",
        message=f"Pickup confirmed at <strong>{pickup_time_str}</strong>.<br><br>"
                f"The Bill of Lading has been emailed to you. "
                f"R+L Carriers will arrive for pickup as scheduled.",
    ))


# =============================================================================
# DAY-BEFORE — NO branch
# =============================================================================

@supplier_router.get("/supplier/{token}/push-date", response_class=HTMLResponse)
def push_date_form(token: str):
    """Warehouse clicked NO — show new date form."""
    shipment = get_shipment_by_token(token)
    if not shipment:
        return HTMLResponse(_error_page("This link is invalid or has expired."), status_code=404)

    order_id = shipment["order_id"]
    pickup_date = shipment.get("pickup_date")
    old_display = pickup_date.strftime("%A, %B %d") if hasattr(pickup_date, "strftime") else "the scheduled date"
    today_str = date_today.today().isoformat()

    html = f"""<!DOCTYPE html>
<html><head>
    <title>Order #{order_id} — New Date</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
        *{{box-sizing:border-box;margin:0;padding:0;}}
        body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:24px;}}
        .card{{max-width:520px;margin:0 auto;background:white;border-radius:10px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.1);}}
        h1{{color:#1a365d;font-size:22px;margin-bottom:16px;}}
        .notice{{background:#fffbeb;border:1px solid #fcd34d;border-radius:6px;padding:12px 16px;color:#92400e;font-size:14px;margin-bottom:20px;}}
        label{{display:block;font-weight:600;color:#1a365d;margin-bottom:6px;font-size:15px;}}
        input[type=date]{{width:100%;padding:12px;border:2px solid #e2e8f0;border-radius:6px;font-size:16px;font-family:inherit;margin-bottom:20px;}}
        input[type=date]:focus{{outline:none;border-color:#2563eb;}}
        button{{width:100%;background:#DC2626;color:white;padding:14px;border:none;border-radius:6px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit;}}
        .note{{font-size:12px;color:#999;margin-top:16px;text-align:center;}}
    </style>
</head><body>
<div class="card">
    <h1>Order #{order_id} — New Pickup Date</h1>
    <div class="notice">⚠️ Order will not be ready on <strong>{old_display}</strong>. Please enter a new date.</div>
    <form method="POST" action="/supplier/{token}/submit-push-date">
        <label for="new_date">New Expected Pickup Date</label>
        <input type="date" id="new_date" name="new_date" required min="{today_str}">
        <button type="submit">Submit New Date →</button>
    </form>
    <div class="note">Questions? Call (770) 990-4885.</div>
</div>
</body></html>"""
    return HTMLResponse(html)


@supplier_router.post("/supplier/{token}/submit-push-date", response_class=HTMLResponse)
async def submit_push_date(token: str, request: Request):
    try:
        form = await request.form()
        new_date_str = form.get("new_date", "")
    except Exception as e:
        return HTMLResponse(_error_page(f"Could not read form: {str(e)}"), status_code=400)

    if not new_date_str:
        return HTMLResponse(_error_page("Please enter a new date."), status_code=400)

    try:
        result = warehouse_push_date(token, new_date_str)
    except Exception as e:
        return HTMLResponse(_error_page(f"Unexpected error: {str(e)}."), status_code=500)

    if not result.get("success"):
        return HTMLResponse(_error_page(result.get("error", "Something went wrong.")), status_code=400)

    try:
        from datetime import datetime
        date_display = datetime.strptime(new_date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")
    except Exception:
        date_display = new_date_str

    extra = ""
    if result.get("cfc_alerted"):
        extra = "<p style='font-size:13px;color:#D97706;margin-top:12px;'>The Cabinets For Contractors team has been notified of the date change.</p>"

    return HTMLResponse(_success_page(
        title="New Date Recorded",
        message=f"Updated pickup date to <strong>{date_display}</strong>.<br><br>"
                f"You'll receive a confirmation email the day before.",
        extra_html=extra
    ))


# =============================================================================
# ADMIN: manually send / re-send poll
# =============================================================================

@supplier_router.post("/supplier/{shipment_id}/send-poll")
def admin_send_poll(shipment_id: str, _: bool = Depends(require_admin)):
    result = send_initial_poll(shipment_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return {"status": "ok", **result}
