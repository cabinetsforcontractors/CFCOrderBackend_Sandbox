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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

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

@email_router.post("/orders/{order_id}/draft-invoice")
async def draft_invoice(order_id: str, to: str = "", shipping: float = 0.0,
                        square_link: str = "", tariff_rate: float = 0.08,
                        auto_link: bool = True):
    """BEAT 3: render the v4 invoice (template + PDF) into a Gmail DRAFT with
    the PDF attached. Never sends. Empty square_link + auto_link=true (the
    default, 2026-07-27) -> the robot CREATES the Square payment link itself
    (INV-{order_id}, grand total). `to` defaults to the order's customer
    email — pass to=homesupplyplus@gmail.com for test orders."""
    from email_sender import create_invoice_draft
    result = create_invoice_draft(order_id, to_email=to,
                                  shipping_amount=shipping,
                                  square_link=square_link,
                                  tariff_rate=tariff_rate,
                                  auto_link=auto_link)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


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
