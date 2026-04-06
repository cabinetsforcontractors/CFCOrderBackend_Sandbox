"""
supplier_routes.py
WS6 Phase 9 — Supplier-facing public HTML endpoints

Flow:
  GET  /supplier/{token}/date-form        — date + ready time + close time form
  POST /supplier/{token}/set-date         — BOL + Pickup Request fired; two emails scheduled:
                                            1. _delayed_bol_email → supplier (BOL PDF, ~10min)
                                            2. _send_customer_pickup_scheduled_email → customer
                                               (pickup confirmed, tracking coming when moving)
  GET  /supplier/{token}/confirm-tomorrow — day-before YES → time + close time form
  POST /supplier/{token}/set-time         — BOL + Pickup Request fired; same two emails
  GET  /supplier/{token}/push-date        — day-before NO → new date
  POST /supplier/{token}/submit-push-date — store new date
  POST /supplier/{shipment_id}/send-poll  — admin re-send [admin]
"""

from datetime import date as date_today
from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from fastapi.responses import HTMLResponse

from auth import require_admin
from supplier_polling_engine import (
    get_shipment_by_token,
    warehouse_set_date,
    warehouse_confirm_tomorrow,
    warehouse_push_date,
    warehouse_set_pickup_time,
    send_initial_poll,
    process_bol_and_pickup,
    _delayed_bol_email,
    _send_customer_pickup_scheduled_email,
)

supplier_router = APIRouter(tags=["supplier"])

CFC_EMAIL = "orders@cabinetsforcontractors.net"


def _time_options(selected: str = "", start_hour: int = 7, end_hour: int = 17) -> str:
    opts = ['<option value="">— Select time —</option>']
    for hour in range(start_hour, end_hour + 1):
        for minute in (0, 15, 30, 45):
            if hour == end_hour and minute > 0:
                break
            period = "AM" if hour < 12 else "PM"
            dh = hour if hour <= 12 else hour - 12
            if dh == 0:
                dh = 12
            t = f"{dh}:{minute:02d} {period}"
            sel = ' selected' if t == selected else ''
            opts.append(f'<option value="{t}"{sel}>{t}</option>')
    return "\n".join(opts)


def _error_page(message: str, title: str = "Error") -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,sans-serif;background:#f5f5f5;padding:40px;text-align:center;}}
.card{{max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:40px;box-shadow:0 2px 8px rgba(0,0,0,.1);}}</style>
</head><body><div class="card"><h2>⚠️ {title}</h2><p>{message}</p>
<p style="font-size:14px;color:#666;">Questions? <a href="mailto:{CFC_EMAIL}">{CFC_EMAIL}</a></p></div></body></html>"""


def _success_page(title: str, message: str, extra_html: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,sans-serif;background:#f5f5f5;padding:40px;}}
.card{{max-width:480px;margin:0 auto;background:white;border-radius:8px;padding:40px;box-shadow:0 2px 8px rgba(0,0,0,.1);text-align:center;}}
h2{{color:#1a365d;}} .ok{{font-size:52px;margin-bottom:16px;}}
p{{color:#4a5568;font-size:15px;line-height:1.7;}}
.email-note{{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:12px;margin:16px 0;font-size:14px;color:#1E40AF;}}
.contact{{margin-top:24px;font-size:13px;color:#999;}}</style>
</head><body><div class="card"><div class="ok">✅</div>
<h2>{title}</h2><p>{message}</p>{extra_html}
<div class="contact">Questions? <a href="mailto:{CFC_EMAIL}" style="color:#1a365d;">{CFC_EMAIL}</a></div>
</div></body></html>"""


_FORM_STYLE = """
    *{box-sizing:border-box;margin:0;padding:0;}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:24px;}
    .card{max-width:560px;margin:0 auto;background:white;border-radius:10px;padding:36px;box-shadow:0 2px 12px rgba(0,0,0,.1);}
    h1{color:#1a365d;font-size:22px;margin-bottom:6px;}
    .subtitle{color:#718096;font-size:14px;margin-bottom:24px;}
    label{display:block;font-weight:600;color:#1a365d;margin-bottom:6px;font-size:14px;}
    .field{margin-bottom:18px;}
    .field-row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px;}
    input[type=date],select{width:100%;padding:11px 12px;border:2px solid #e2e8f0;border-radius:6px;font-size:15px;font-family:inherit;background:white;}
    input[type=date]:focus,select:focus{outline:none;border-color:#2563eb;}
    hr{border:none;border-top:1px solid #e2e8f0;margin:18px 0;}
    .bol-note{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:10px 14px;font-size:13px;color:#1E40AF;margin-bottom:18px;}
    button{width:100%;background:#059669;color:white;padding:14px;border:none;border-radius:6px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit;display:flex;align-items:center;justify-content:center;gap:10px;}
    button:hover{background:#047857;}
    button:disabled{background:#6b7280;cursor:not-allowed;}
    .spinner{display:none;width:18px;height:18px;border:3px solid rgba(255,255,255,.4);border-top-color:white;border-radius:50%;animation:spin .7s linear infinite;}
    @keyframes spin{to{transform:rotate(360deg)}}
    .note{font-size:12px;color:#999;margin-top:16px;text-align:center;}
    .contact{font-size:12px;color:#999;margin-top:12px;text-align:center;}
"""

_SPINNER_JS = """
<script>
document.querySelectorAll('form').forEach(function(form) {
  form.addEventListener('submit', function() {
    var btn = form.querySelector('button[type=submit]');
    if (btn) {
      btn.disabled = true;
      var spinner = btn.querySelector('.spinner');
      if (spinner) spinner.style.display = 'block';
      var label = btn.querySelector('.btn-label');
      if (label) label.textContent = 'Processing...';
    }
  });
});
</script>
"""


# =============================================================================
# COMBINED DATE + READY TIME + CLOSE TIME FORM
# =============================================================================

@supplier_router.get("/supplier/{token}/date-form", response_class=HTMLResponse)
def date_form(token: str):
    shipment = get_shipment_by_token(token)
    if not shipment:
        return HTMLResponse(_error_page("This link is invalid or has expired."), status_code=404)

    order_id = shipment["order_id"]
    existing_date = shipment.get("pickup_date")
    existing_str = existing_date.strftime("%Y-%m-%d") if hasattr(existing_date, "strftime") else ""
    existing_time = shipment.get("pickup_time") or ""
    existing_close = shipment.get("close_time") or ""
    today_str = date_today.today().isoformat()

    existing_banner = ""
    if existing_str:
        existing_banner = f"<div style='background:#d1fae5;border:1px solid #6ee7b7;border-radius:6px;padding:10px;margin-bottom:16px;font-size:13px;color:#065f46;'>Date on file: <strong>{existing_str}</strong> — you may update below.</div>"

    html = f"""<!DOCTYPE html>
<html><head>
    <title>Order #{order_id} — Pickup Schedule</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>{_FORM_STYLE}</style>
</head><body>
<div class="card">
    <h1>Order #{order_id}</h1>
    <div class="subtitle">Cabinets For Contractors — Pickup Schedule</div>
    {existing_banner}
    <div class="bol-note">📄 R+L Carriers pickup will be scheduled automatically. The BOL will be emailed to you — allow up to 10 minutes.</div>
    <form method="POST" action="/supplier/{token}/set-date">
        <div class="field">
            <label for="pickup_date">Pickup Date</label>
            <input type="date" id="pickup_date" name="pickup_date" value="{existing_str}" required min="{today_str}">
        </div>
        <hr>
        <div class="field-row">
            <div>
                <label for="pickup_time">Ready Time <span style="font-weight:400;color:#718096;">(when ready)</span></label>
                <select id="pickup_time" name="pickup_time" required>
                    {_time_options(existing_time)}
                </select>
            </div>
            <div>
                <label for="close_time">Close Time <span style="font-weight:400;color:#718096;">(latest R+L can arrive)</span></label>
                <select id="close_time" name="close_time" required>
                    {_time_options(existing_close)}
                </select>
            </div>
        </div>
        <button type="submit"><span class="btn-label">Confirm &amp; Schedule Pickup →</span><span class="spinner"></span></button>
    </form>
    <div class="contact">Questions? <a href="mailto:{CFC_EMAIL}" style="color:#1a365d;">{CFC_EMAIL}</a></div>
</div>
{_SPINNER_JS}
</body></html>"""
    return HTMLResponse(html)


@supplier_router.post("/supplier/{token}/set-date", response_class=HTMLResponse)
async def set_date(token: str, request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        pickup_date_str = form.get("pickup_date", "")
        pickup_time_str = form.get("pickup_time", "")
        close_time_str  = form.get("close_time", "")
    except Exception as e:
        return HTMLResponse(_error_page(f"Could not read form: {str(e)}"), status_code=400)

    if not pickup_date_str:
        return HTMLResponse(_error_page("Please enter a pickup date."), status_code=400)
    if not pickup_time_str:
        return HTMLResponse(_error_page("Please select a ready time."), status_code=400)
    if not close_time_str:
        return HTMLResponse(_error_page("Please select a close time."), status_code=400)

    date_result = warehouse_set_date(token, pickup_date_str)
    if not date_result.get("success"):
        return HTMLResponse(_error_page(date_result.get("error", "Could not save date.")), status_code=400)

    result = process_bol_and_pickup(token, pickup_time_str, close_time_str)

    try:
        from datetime import datetime
        date_display = datetime.strptime(pickup_date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")
    except Exception:
        date_display = pickup_date_str

    if not result.get("success"):
        return HTMLResponse(_success_page(
            title="Date Confirmed",
            message=f"Pickup date recorded as <strong>{date_display}</strong> at <strong>{pickup_time_str}</strong>.<br><br>"
                    f"⚠️ BOL could not be auto-generated: {result.get('error', 'unknown')}. "
                    f"Please email us.",
        ))

    # Background task 1: Email BOL PDF to supplier (~10 min delay for R+L to process)
    background_tasks.add_task(
        _delayed_bol_email,
        to_email=result["supplier_email"],
        warehouse_name=result["warehouse_name"],
        order_id=result["order_id"],
        pro_number=result["pro_number"],
        pickup_date=result["pickup_date"] or pickup_date_str,
        pickup_time=pickup_time_str,
        bol_pdf_url=result.get("bol_pdf_url", ""),
        shipment=result["shipment"],
    )

    # Background task 2: Email customer that pickup is scheduled
    # No PRO shared — tracking email fires separately when R+L shows first scan
    if result.get("customer_email"):
        background_tasks.add_task(
            _send_customer_pickup_scheduled_email,
            to_email=result["customer_email"],
            customer_name=result.get("customer_name", ""),
            order_id=result["order_id"],
            pickup_date=result["pickup_date"] or pickup_date_str,
            pickup_time=pickup_time_str,
            close_time=close_time_str,
        )

    pickup_confirmation = result.get("pickup_confirmation")
    pickup_note = ""
    if pickup_confirmation:
        pickup_note = f"<p style='font-size:13px;color:#059669;margin-top:4px;'>R+L Pickup ID: <strong>{pickup_confirmation}</strong></p>"
    else:
        pickup_err = result.get("pickup_error", "no error returned")
        pickup_note = f"<p style='font-size:12px;color:#DC2626;margin-top:4px;'>⚠️ Pickup scheduling failed: {pickup_err}</p>"

    return HTMLResponse(_success_page(
        title="You're All Set",
        message=f"Pickup confirmed for <strong>{date_display}</strong>.<br>"
                f"Ready: <strong>{pickup_time_str}</strong> — Close: <strong>{close_time_str}</strong><br><br>"
                f"R+L Carriers has been notified.",
        extra_html=f"""{pickup_note}
        <div class="email-note">
            📧 The Bill of Lading will be emailed to you within <strong>10 minutes</strong>.
            Please check your inbox (and spam folder) shortly.
        </div>"""
    ))


# =============================================================================
# DAY-BEFORE — YES branch
# =============================================================================

@supplier_router.get("/supplier/{token}/confirm-tomorrow", response_class=HTMLResponse)
def confirm_tomorrow(token: str):
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
<html><head>
    <title>Order #{order_id} — Pickup Times</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>{_FORM_STYLE}</style>
</head><body>
<div class="card">
    <h1>Order #{order_id} — Confirm Pickup Times</h1>
    <div style="background:#d1fae5;border:1px solid #6ee7b7;border-radius:6px;padding:12px;color:#065f46;font-weight:600;font-size:14px;margin-bottom:20px;">
        ✅ Confirmed — on track for {date_display}
    </div>
    <div class="bol-note">📄 R+L Carriers pickup will be scheduled automatically when you submit.</div>
    <form method="POST" action="/supplier/{token}/set-time">
        <div class="field-row">
            <div>
                <label for="pickup_time">Ready Time</label>
                <select id="pickup_time" name="pickup_time" required>
                    {_time_options()}
                </select>
            </div>
            <div>
                <label for="close_time">Close Time</label>
                <select id="close_time" name="close_time" required>
                    {_time_options()}
                </select>
            </div>
        </div>
        <button type="submit"><span class="btn-label">Schedule Pickup &amp; Generate BOL →</span><span class="spinner"></span></button>
    </form>
    <div class="contact">Questions? <a href="mailto:{CFC_EMAIL}" style="color:#1a365d;">{CFC_EMAIL}</a></div>
</div>
{_SPINNER_JS}
</body></html>"""
    return HTMLResponse(html)


@supplier_router.post("/supplier/{token}/set-time", response_class=HTMLResponse)
async def set_time(token: str, request: Request, background_tasks: BackgroundTasks):
    try:
        form = await request.form()
        pickup_time_str = form.get("pickup_time", "")
        close_time_str  = form.get("close_time", "5:00 PM")
    except Exception as e:
        return HTMLResponse(_error_page(f"Could not read form: {str(e)}"), status_code=400)

    if not pickup_time_str:
        return HTMLResponse(_error_page("Please select a ready time."), status_code=400)

    result = process_bol_and_pickup(token, pickup_time_str, close_time_str)

    if not result.get("success"):
        return HTMLResponse(_error_page(
            f"Could not generate BOL: {result.get('error', 'Unknown error')}. Please email us."
        ), status_code=500)

    # Background task 1: Email BOL PDF to supplier
    background_tasks.add_task(
        _delayed_bol_email,
        to_email=result["supplier_email"],
        warehouse_name=result["warehouse_name"],
        order_id=result["order_id"],
        pro_number=result["pro_number"],
        pickup_date=result.get("pickup_date", ""),
        pickup_time=pickup_time_str,
        bol_pdf_url=result.get("bol_pdf_url", ""),
        shipment=result["shipment"],
    )

    # Background task 2: Email customer that pickup is scheduled
    if result.get("customer_email"):
        background_tasks.add_task(
            _send_customer_pickup_scheduled_email,
            to_email=result["customer_email"],
            customer_name=result.get("customer_name", ""),
            order_id=result["order_id"],
            pickup_date=result.get("pickup_date", ""),
            pickup_time=pickup_time_str,
            close_time=close_time_str,
        )

    pickup_confirmation = result.get("pickup_confirmation")
    pickup_note = ""
    if pickup_confirmation:
        pickup_note = f"<p style='font-size:13px;color:#059669;'>R+L Pickup ID: <strong>{pickup_confirmation}</strong></p>"
    else:
        pickup_err = result.get("pickup_error", "no error returned")
        pickup_note = f"<p style='font-size:12px;color:#DC2626;'>⚠️ Pickup scheduling failed: {pickup_err}</p>"

    return HTMLResponse(_success_page(
        title="BOL Created — You're All Set",
        message=f"Pickup confirmed at <strong>{pickup_time_str}</strong> (close: {close_time_str}).<br><br>"
                f"R+L Carriers has been notified.",
        extra_html=f"""{pickup_note}
        <div class="email-note">
            📧 The Bill of Lading will be emailed to you within <strong>10 minutes</strong>.
        </div>"""
    ))


# =============================================================================
# DAY-BEFORE — NO branch
# =============================================================================

@supplier_router.get("/supplier/{token}/push-date", response_class=HTMLResponse)
def push_date_form(token: str):
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
    <style>{_FORM_STYLE}</style>
</head><body>
<div class="card">
    <h1>Order #{order_id} — New Pickup Date</h1>
    <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:6px;padding:12px;color:#92400e;font-size:14px;margin-bottom:20px;">
        ⚠️ Order will not be ready on <strong>{old_display}</strong>. Please enter a new date.
    </div>
    <form method="POST" action="/supplier/{token}/submit-push-date">
        <div class="field">
            <label for="new_date">New Expected Pickup Date</label>
            <input type="date" id="new_date" name="new_date" required min="{today_str}">
        </div>
        <button type="submit" style="background:#DC2626;"><span class="btn-label">Submit New Date →</span><span class="spinner"></span></button>
    </form>
    <div class="contact">Questions? <a href="mailto:{CFC_EMAIL}" style="color:#1a365d;">{CFC_EMAIL}</a></div>
</div>
{_SPINNER_JS}
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

    result = warehouse_push_date(token, new_date_str)
    if not result.get("success"):
        return HTMLResponse(_error_page(result.get("error", "Something went wrong.")), status_code=400)

    try:
        from datetime import datetime
        date_display = datetime.strptime(new_date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")
    except Exception:
        date_display = new_date_str

    extra = ""
    if result.get("cfc_alerted"):
        extra = "<p style='font-size:13px;color:#D97706;margin-top:12px;'>The team has been notified of the date change.</p>"

    return HTMLResponse(_success_page(
        title="New Date Recorded",
        message=f"Updated pickup date to <strong>{date_display}</strong>.<br><br>"
                f"You'll receive a confirmation email the day before.",
        extra_html=extra
    ))


# =============================================================================
# ADMIN: re-send poll
# =============================================================================

@supplier_router.post("/supplier/{shipment_id}/send-poll")
def admin_send_poll(shipment_id: str, _: bool = Depends(require_admin)):
    result = send_initial_poll(shipment_id)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return {"status": "ok", **result}
