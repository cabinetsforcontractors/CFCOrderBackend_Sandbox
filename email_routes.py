"""
email_routes.py
FastAPI router for Email Communications — Phase 4

Endpoints:
    GET  /email/templates              — List available email templates
    GET  /email/templates/{id}/preview — Preview a template with sample data
    POST /orders/{order_id}/send-email — Send email to customer
    POST /orders/{order_id}/preview-email — Preview email without sending
    GET  /orders/{order_id}/email-history — Get email send history

Mount in main.py with:
    from email_routes import email_router
    app.include_router(email_router)
"""

from fastapi import APIRouter, HTTPException, Depends, File, Form, UploadFile
from pydantic import BaseModel
from typing import Optional

from auth import require_admin

from email_templates import get_template_list, render_template_preview, TEMPLATE_REGISTRY
from email_sender import send_order_email, send_email_dry_run, get_email_history
from db_helpers import get_order_by_id


email_router = APIRouter(tags=["email"])


# =============================================================================
# REQUEST MODELS
# =============================================================================

class SendEmailRequest(BaseModel):
    template_id: str
    to_email: Optional[str] = None  # If None, uses order's customer_email
    custom_subject: Optional[str] = None
    triggered_by: str = "manual"  # manual | lifecycle_engine | status_change


# =============================================================================
# IDENTITY
# =============================================================================

@email_router.get("/email/whoami")
async def email_whoami():
    """Which Gmail account does the robot's token belong to, and what From
    is configured? THE verification door for mailbox moves (orders@
    permanent switch, William 2026-07-26)."""
    import os
    from gmail_sync import gmail_api_request
    profile = gmail_api_request("profile") or {}
    return {
        "success": bool(profile.get("emailAddress")),
        "token_account": profile.get("emailAddress", "TOKEN NOT WORKING"),
        "messages_total": profile.get("messagesTotal"),
        "from_address_env": os.environ.get("EMAIL_FROM_ADDRESS", "(unset — Gmail stamps the token account)"),
        "from_name_env": os.environ.get("EMAIL_FROM_NAME", "(unset)"),
        "allowlist_env": os.environ.get("EMAIL_ALLOWLIST", "(unset — no redirect guard)"),
        "internal_notices_env": os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL", "(unset — default cabinetsforcontractors@gmail.com)"),
        "new_order_notify_to": os.environ.get("NEW_ORDER_NOTIFY_TO", "(unset — default orders@cabinetsforcontractors.com)"),
    }


# =============================================================================
# TEMPLATE ENDPOINTS
# =============================================================================

@email_router.get("/email/templates")
async def list_templates():
    """List all available email templates with metadata."""
    templates = get_template_list()
    return {
        "success": True,
        "count": len(templates),
        "templates": templates,
    }


@email_router.get("/email/templates/{template_id}/preview")
async def preview_template(template_id: str):
    """Preview a template with sample data."""
    if template_id not in TEMPLATE_REGISTRY:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template_id}' not found. Available: {list(TEMPLATE_REGISTRY.keys())}",
        )
    
    html = render_template_preview(template_id)
    meta = TEMPLATE_REGISTRY[template_id]
    
    return {
        "success": True,
        "template_id": template_id,
        "name": meta["name"],
        "subject_template": meta["subject"],
        "category": meta["category"],
        "is_lifecycle": meta["is_lifecycle"],
        "html_preview": html,
    }


# =============================================================================
# SEND ENDPOINTS
# =============================================================================

@email_router.post("/email/draft-raw")
async def draft_raw(to: str = Form(...), subject: str = Form(...),
                    html: UploadFile = File(...),
                    attachments: list[UploadFile] = File(None),
                    _: bool = Depends(require_admin)):
    """Land a fully-composed email as a Gmail DRAFT [admin] — html body +
    attachments arrive as multipart uploads (byte-faithful), NOTHING sends.
    The safe carrier for hand-made one-offs (William 2026-07-28, the 5731
    ship-to case): the human composes, the robot's mailbox carries."""
    html_body = (await html.read()).decode("utf-8", errors="replace")
    if not html_body.strip():
        return {"status": "error", "message": "empty html body"}
    atts = []
    for f in (attachments or []):
        data = await f.read()
        if data:
            atts.append({"filename": f.filename, "content": data,
                         "mime": f.content_type or "application/octet-stream"})
    from email_sender import create_gmail_draft
    res = create_gmail_draft(to, subject, html_body, atts)
    if not res.get("success"):
        return {"status": "error", "message": res.get("error")}
    return {"status": "ok", "draft_id": res["draft_id"], "to": to,
            "subject": subject,
            "attachments": [a["filename"] for a in atts]}


@email_router.post("/orders/{order_id}/delivered-draft")
async def delivered_draft(order_id: str, delivered_on: str = "",
                          to: str = "", _: bool = Depends(require_admin)):
    """Render the delivered-confirmation (delivery date + claims block +
    pick list attached) into a Gmail DRAFT [admin] — the manual door for
    deliveries the R+L machinery didn't catch (Daylight, UPS, regens).
    delivered_on takes MM/DD/YYYY; empty = today. Never sends."""
    order = get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order["order_id"] = order_id
    if (delivered_on or "").strip():
        order["delivered_on"] = delivered_on.strip()
    try:
        from claims_routes import claims_form_url
        order["claims_url"] = claims_form_url(order_id)
    except Exception as e:
        print(f"[EMAIL] claims url failed {order_id}: {e}")
    from email_templates import render_template, get_template_subject
    html = render_template("delivery_confirmation", order)
    subject = get_template_subject("delivery_confirmation", order)
    to_email = (to or "").strip() or (order.get("email") or "").strip()
    if not html or not to_email:
        raise HTTPException(status_code=400,
                            detail="render failed or order has no email")
    attachments = []
    try:
        from picklist_pdf import generate_picklist_pdf
        pk = generate_picklist_pdf(order)
        if pk:
            attachments.append({"filename": f"CFC-Picklist-{order_id}.pdf",
                                "content": pk, "mime": "application/pdf"})
    except Exception as e:
        print(f"[EMAIL] delivered-draft picklist failed {order_id}: {e}")
    from email_sender import create_gmail_draft
    res = create_gmail_draft(to_email, subject, html, attachments)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    return {"status": "ok", "draft_id": res["draft_id"], "to": to_email,
            "subject": subject,
            "picklist_attached": bool(attachments)}


@email_router.post("/orders/{order_id}/roc-schedule-draft")
async def roc_schedule_draft(order_id: str, ready_date: str,
                             transit_lo: int = 2, transit_hi: int = 5,
                             to: str = "",
                             _: bool = Depends(require_admin)):
    """ROC-SPECIFIC (William 2026-07-29): ROC's portal states a
    ready/pickup date — the customer gets an email with that pickup date
    and the approximate delivery window (ready date + transit business
    days). Lands as a Gmail DRAFT + PROGRESS DRAFT READY alert; never
    sends. ready_date = YYYY-MM-DD."""
    from datetime import date
    order = get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order["order_id"] = order_id
    try:
        rd = date.fromisoformat(ready_date.strip())
    except Exception:
        raise HTTPException(status_code=400,
                            detail="ready_date must be YYYY-MM-DD")
    from business_days import add_business_days
    arrive_min = add_business_days(rd, max(0, int(transit_lo)))
    arrive_max = add_business_days(rd, max(int(transit_lo), int(transit_hi)))
    from progress_emails import (_first_name, _make_draft, _nice,
                                 INSPECT_NOTE, SIGNATURE)
    body = (
        f"Hey {_first_name(order)},\n\n"
        f"Good news on order #{order_id} - the warehouse has your order in "
        f"the schedule and says it will be ready for carrier pickup on "
        f"{_nice(rd)}.\n\n"
        f"We will arrange the freight pickup for that day, and your "
        f"approximate delivery is between {_nice(arrive_min)} and "
        f"{_nice(arrive_max)}.\n\n"
        f"We will send your tracking information as soon as the carrier "
        f"picks it up.\n\n{INSPECT_NOTE}\n\n"
        f"Any questions, just reply.\n\n{SIGNATURE}")
    subject = f"Order #{order_id} - ready date and estimated delivery"
    to_email = (to or "").strip() or (order.get("email") or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="order has no email")
    draft_id = _make_draft(to_email, subject, body)
    if not draft_id:
        raise HTTPException(status_code=400, detail="draft creation failed")
    try:
        from supplier_orders import _send_email, INTERNAL_ALERT_EMAIL
        _send_email(order_id, INTERNAL_ALERT_EMAIL,
                    f"PROGRESS DRAFT READY - roc-schedule - order #{order_id}",
                    f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                    f"<p>A ROC ready-date/delivery-estimate draft is waiting "
                    f"in Gmail drafts - review and send.</p>"
                    f"<pre style='background:#f5f5f5;padding:12px;"
                    f"white-space:pre-wrap;'>{body}</pre></div>",
                    triggered_by="roc_schedule_draft")
    except Exception as e:
        print(f"[EMAIL] roc-schedule alert failed {order_id}: {e}")
    return {"status": "ok", "draft_id": draft_id, "to": to_email,
            "subject": subject, "ready_date": str(rd),
            "arrive_min": str(arrive_min), "arrive_max": str(arrive_max)}


@email_router.post("/orders/{order_id}/draft-invoice")
async def draft_invoice(order_id: str, to: str = "", shipping: float = 0.0,
                        square_link: str = "", tariff_rate: float = 0.08,
                        auto_link: bool = True, note: str = "",
                        residential: str = ""):
    """BEAT 3: render the v4 invoice (template + PDF + pick list) into a Gmail
    DRAFT with the PDFs attached. Never sends. Empty square_link +
    auto_link=true (the default, 2026-07-27) -> the robot CREATES the Square
    payment link itself (INV-{order_id}, grand total). `to` defaults to the
    order's customer email — pass to=homesupplyplus@gmail.com for test orders.
    `note` (2026-07-28) rides the email body and the PDF as a highlighted
    per-invoice note (the 5731 B09->B09-FH case)."""
    from email_sender import create_invoice_draft
    result = create_invoice_draft(order_id, to_email=to,
                                  shipping_amount=shipping,
                                  square_link=square_link,
                                  tariff_rate=tariff_rate,
                                  auto_link=auto_link,
                                  note=note,
                                  residential=residential)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@email_router.post("/orders/{order_id}/auto-invoice")
async def auto_invoice_now(order_id: str, dry_run: bool = True,
                           force_reinvoice: bool = False):
    """BEAT C door [admin]: run the auto-invoice pipeline for one order.
    dry_run=true (default) computes gates + quote + totals, creates nothing,
    sends nothing. dry_run=false = the real thing (link + SEND, allowlist
    governs delivery). force_reinvoice=true (2026-08-02): re-invoice an
    already-invoiced unpaid order — old Square links die first."""
    from auto_invoice import run_auto_invoice
    return run_auto_invoice(order_id, triggered_by="manual_door",
                            dry_run=dry_run,
                            force_reinvoice=force_reinvoice)


@email_router.post("/orders/{order_id}/send-email")
async def send_email(order_id: str, req: SendEmailRequest):
    """
    Send an email for an order using a template.
    
    CRITICAL BEHAVIOR:
    - Lifecycle templates are tagged source='system_generated' in order_events
    - system_generated emails do NOT reset the lifecycle inactivity clock
    - All sends are logged to order_events regardless of success/failure
    
    Body:
        template_id: One of the 9 template IDs
        to_email: Customer email (optional — uses order's email if omitted)
        custom_subject: Override default subject (optional)
        triggered_by: "manual" | "lifecycle_engine" | "status_change"
    """
    # Validate template
    if req.template_id not in TEMPLATE_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template '{req.template_id}'. Available: {list(TEMPLATE_REGISTRY.keys())}",
        )
    
    # Get order
    order = get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    
    # Determine recipient email
    to_email = req.to_email
    if not to_email:
        # Try to get from order data
        to_email = order.get("customer_email") or order.get("email")
        if not to_email:
            raise HTTPException(
                status_code=400,
                detail=f"No email address found for order {order_id}. Provide to_email in request body.",
            )
    
    # Send the email
    result = send_order_email(
        order_id=order_id,
        template_id=req.template_id,
        to_email=to_email,
        order_data=order,
        custom_subject=req.custom_subject,
        triggered_by=req.triggered_by,
    )
    
    if not result.get("success"):
        status_code = 503 if result.get("dry_run") else 500
        return {**result, "status_code": status_code}
    
    return result


@email_router.post("/orders/{order_id}/preview-email")
async def preview_email(order_id: str, req: SendEmailRequest):
    """
    Preview what an email would look like without sending.
    Returns rendered HTML and subject. No email is sent.
    """
    if req.template_id not in TEMPLATE_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template '{req.template_id}'",
        )
    
    order = get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    
    to_email = req.to_email or order.get("customer_email") or order.get("email") or "preview@example.com"
    
    result = send_email_dry_run(
        order_id=order_id,
        template_id=req.template_id,
        to_email=to_email,
        order_data=order,
    )
    
    return result


# =============================================================================
# HISTORY ENDPOINT
# =============================================================================

@email_router.get("/orders/{order_id}/email-history")
async def email_history(order_id: str):
    """
    Get email send history for an order.
    Returns all email_sent and email_send_failed events from order_events.
    """
    order = get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    
    history = get_email_history(order_id)
    
    return {
        "success": True,
        "order_id": order_id,
        "count": len(history),
        "emails": history,
    }
