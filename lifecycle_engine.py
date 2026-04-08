"""
lifecycle_engine.py
CFC Orders Lifecycle Engine — automated inactivity detection, archiving, cancellation.

Order Lifecycle Rules (William's rules, Mar 3 2026):
  - Clock basis: last email to/from customer about that order
  - System reminder emails do NOT reset the clock
  - Customer response resets the clock back to day 0 (active)
  - "Cancel" keyword in customer email → immediate B2BWave cancel

Timeline:
  Day 7:  Move → Inactive tab + send email "order moved to inactive"
  Day 14: Send email "order will be canceled in 7 days"
  Day 21: Hit B2BWave API → cancel order, move to Done tab with canceled indicator

Lifecycle statuses: active, inactive, canceled

Usage:
    from lifecycle_engine import check_all_orders_lifecycle, process_order_lifecycle
    
    # Cron job (daily): check all orders
    results = check_all_orders_lifecycle()
    
    # Single order check
    result = process_order_lifecycle(order_id)
    
    # Customer response detected (resets clock)
    extend_deadline(order_id)
    
    # Customer said "cancel"
    cancel_order(order_id, reason="customer_request")
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from db_helpers import get_db
from psycopg2.extras import RealDictCursor
from business_days import business_days_since, add_business_days


# =============================================================================
# CONSTANTS
# =============================================================================

# Lifecycle timeline (business days since last customer email activity)
INACTIVE_DAY = 7
CANCEL_WARNING_DAY = 14
CANCEL_DAY = 21

# Reminder email trigger days (does NOT reset clock)
INACTIVE_NOTICE_DAY = 7     # Same day as move to inactive
CANCEL_WARNING_EMAIL_DAY = 14
# Day 21 = actual cancel (no separate email day, cancel confirmation sent at cancel)

# Lifecycle status values
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_CANCELED = "canceled"
STATUS_ARCHIVED = "archived"

# Cancel keyword patterns (fuzzy match)
CANCEL_PATTERNS = [
    r'\bcancel\b',
    r'\bcancell?\b',           # Common misspelling
    r'\bcancel\s+(?:my|this|the)\s+order\b',
    r'\bcancel\s+order\b',
    r'\bplease\s+cancel\b',
    r'\bwant\s+to\s+cancel\b',
    r'\bneed\s+to\s+cancel\b',
    r'\bwould\s+like\s+to\s+cancel\b',
    r'\bgo\s+ahead\s+and\s+cancel\b',
]

# B2BWave API config
B2BWAVE_URL = os.environ.get("B2BWAVE_URL", "").strip().rstrip('/')
B2BWAVE_USERNAME = os.environ.get("B2BWAVE_USERNAME", "").strip()
B2BWAVE_API_KEY = os.environ.get("B2BWAVE_API_KEY", "").strip()


# =============================================================================
# CANCEL KEYWORD DETECTION
# =============================================================================

def detect_cancel_keyword(text: str) -> bool:
    """
    Check if customer email contains a "cancel" keyword (fuzzy match).
    Returns True if cancel intent detected.
    """
    if not text:
        return False
    text_lower = text.lower().strip()
    for pattern in CANCEL_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


# =============================================================================
# B2BWAVE CANCEL API
# =============================================================================

def cancel_order_on_b2bwave(order_id: str) -> Dict:
    """
    Cancel an order on B2BWave via their API.
    
    Returns dict with success status and details.
    """
    if not B2BWAVE_URL or not B2BWAVE_USERNAME or not B2BWAVE_API_KEY:
        return {"success": False, "error": "B2BWave API not configured"}
    
    try:
        url = f"{B2BWAVE_URL}/api/v1/orders/{order_id}"
        headers = {
            "Content-Type": "application/json",
        }
        auth = (B2BWAVE_USERNAME, B2BWAVE_API_KEY)
        
        # Try PATCH to update order status to canceled
        response = requests.patch(
            url,
            json={"status": "canceled"},
            headers=headers,
            auth=auth,
            timeout=30
        )
        
        if response.status_code in (200, 204):
            return {"success": True, "b2bwave_status": response.status_code}
        else:
            return {
                "success": False,
                "error": f"B2BWave API returned {response.status_code}",
                "response": response.text[:500]
            }
    except Exception as e:
        return {"success": False, "error": f"B2BWave API error: {str(e)}"}


# =============================================================================
# LIFECYCLE STATUS CALCULATOR
# =============================================================================

def calculate_lifecycle_status(
    last_customer_email_at: Optional[datetime],
    current_status: str,
    now: Optional[datetime] = None
) -> Tuple[str, int, Optional[datetime]]:
    """
    Calculate what lifecycle status an order should be in based on
    days since last customer email activity.
    
    Timeline:
      Day 0-6:  active
      Day 7-20: inactive
      Day 21+:  canceled
    
    Returns: (new_status, days_inactive, next_deadline_at)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    
    # If already canceled, stay canceled
    if current_status == STATUS_CANCELED:
        return STATUS_CANCELED, 0, None
    
    # If no customer email tracked yet, use a large number
    # (order will be considered active until first email scan catches up)
    if last_customer_email_at is None:
        return STATUS_ACTIVE, 0, None
    
    # Ensure timezone-aware comparison
    if last_customer_email_at.tzinfo is None:
        last_customer_email_at = last_customer_email_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    
    days_inactive = business_days_since(last_customer_email_at)
    
    if days_inactive >= CANCEL_DAY:
        return STATUS_CANCELED, days_inactive, None
    elif days_inactive >= INACTIVE_DAY:
        cancel_at = datetime.combine(add_business_days(last_customer_email_at.date(), CANCEL_DAY), datetime.min.time(), tzinfo=timezone.utc)
        return STATUS_INACTIVE, days_inactive, cancel_at
    else:
        inactive_at = datetime.combine(add_business_days(last_customer_email_at.date(), INACTIVE_DAY), datetime.min.time(), tzinfo=timezone.utc)
        return STATUS_ACTIVE, days_inactive, inactive_at


def get_pending_reminders(
    last_customer_email_at: Optional[datetime],
    reminders_sent: dict,
    now: Optional[datetime] = None
) -> List[str]:
    """
    Determine which reminder emails should be sent based on days inactive.
    Only returns reminders that haven't been sent yet.
    
    reminders_sent: dict with keys like 'inactive_notice', 'cancel_warning'
                    and datetime string values (when sent).
    
    Returns: list of reminder types to send.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    
    if last_customer_email_at is None:
        return []
    
    if last_customer_email_at.tzinfo is None:
        last_customer_email_at = last_customer_email_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    
    days_inactive = business_days_since(last_customer_email_at)
    pending = []
    
    # Day 7: "Your order has been moved to inactive"
    if days_inactive >= INACTIVE_NOTICE_DAY and not reminders_sent.get("inactive_notice"):
        pending.append("inactive_notice")
    
    # Day 14: "Your order will be canceled in 7 more days"
    if days_inactive >= CANCEL_WARNING_EMAIL_DAY and not reminders_sent.get("cancel_warning"):
        pending.append("cancel_warning")
    
    return pending


# =============================================================================
# CORE ENGINE — PROCESS SINGLE ORDER
# =============================================================================

def process_order_lifecycle(order_id: str, now: Optional[datetime] = None) -> Dict:
    """
    Evaluate lifecycle status for a single order.
    Updates lifecycle_status and lifecycle_deadline_at in DB.
    Sends reminder emails when due.
    Cancels on B2BWave at day 21.
    
    Returns dict with actions taken.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    
    actions = {
        "order_id": order_id,
        "status_changed": False,
        "old_status": None,
        "new_status": None,
        "reminders_sent": [],
        "canceled": False,
        "b2bwave_canceled": False,
        "days_inactive": 0,
    }
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Fetch order with lifecycle fields
            # NOTE: current_status is on order_status table, not orders — do not select it here
            cur.execute("""
                SELECT order_id, is_complete,
                       last_customer_email_at, lifecycle_status, 
                       lifecycle_deadline_at, lifecycle_reminders_sent,
                       email, customer_name, company_name, order_total, order_date
                FROM orders 
                WHERE order_id = %s
            """, (order_id,))
            order = cur.fetchone()
            
            if not order:
                actions["error"] = "Order not found"
                return actions
            
            # Skip completed orders
            if order.get("is_complete"):
                return actions
            
            # Skip already canceled
            current_lc_status = order.get("lifecycle_status") or STATUS_ACTIVE
            if current_lc_status == STATUS_CANCELED:
                return actions
            
            actions["old_status"] = current_lc_status
            
            # Calculate new status
            last_email = order.get("last_customer_email_at")
            new_status, days_inactive, next_deadline = calculate_lifecycle_status(
                last_email, current_lc_status, now
            )
            actions["days_inactive"] = days_inactive
            
            # Check for pending reminders
            reminders_sent = order.get("lifecycle_reminders_sent") or {}
            if isinstance(reminders_sent, str):
                try:
                    reminders_sent = json.loads(reminders_sent)
                except (json.JSONDecodeError, TypeError):
                    reminders_sent = {}
            
            pending_reminders = get_pending_reminders(last_email, reminders_sent, now)
            
            # Handle day 21 cancellation
            if new_status == STATUS_CANCELED and current_lc_status != STATUS_CANCELED:
                _cancel_order_in_db(cur, order_id, "lifecycle_auto_cancel")
                
                # Cancel on B2BWave
                b2b_result = cancel_order_on_b2bwave(order_id)
                actions["b2bwave_canceled"] = b2b_result.get("success", False)
                actions["b2bwave_result"] = b2b_result
                
                # Mark order as complete (moves to Done tab)
                cur.execute("""
                    UPDATE orders SET
                        is_complete = TRUE,
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE order_id = %s
                """, (order_id,))
                
                actions["canceled"] = True
                actions["new_status"] = STATUS_CANCELED
                actions["status_changed"] = True
                
                # Send cancel confirmation email
                _send_lifecycle_email(order, "cancel_confirmation")
                actions["reminders_sent"].append("cancel_confirmation")
                
                # Log event
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'lifecycle_cancel', %s, 'lifecycle_engine')
                """, (order_id, json.dumps({
                    "days_inactive": days_inactive,
                    "last_customer_email_at": last_email.isoformat() if last_email else None,
                    "reason": "21_day_auto_cancel",
                    "b2bwave_canceled": b2b_result.get("success", False),
                })))
                
                return actions
            
            # Update status if changed
            if new_status != current_lc_status:
                cur.execute("""
                    UPDATE orders SET
                        lifecycle_status = %s,
                        lifecycle_deadline_at = %s,
                        updated_at = NOW()
                    WHERE order_id = %s
                """, (new_status, next_deadline, order_id))
                
                actions["status_changed"] = True
                actions["new_status"] = new_status
                
                # Log event
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'lifecycle_status_change', %s, 'lifecycle_engine')
                """, (order_id, json.dumps({
                    "old_status": current_lc_status,
                    "new_status": new_status,
                    "days_inactive": days_inactive,
                })))
            elif next_deadline:
                # Update deadline even if status didn't change
                cur.execute("""
                    UPDATE orders SET lifecycle_deadline_at = %s
                    WHERE order_id = %s
                """, (next_deadline, order_id))
            
            # Send pending reminder emails
            if pending_reminders:
                for reminder in pending_reminders:
                    # Map reminder type to email template
                    template_map = {
                        "inactive_notice": "inactive_notice_day7",
                        "cancel_warning": "cancel_warning_day14",
                    }
                    template_id = template_map.get(reminder)
                    if template_id:
                        email_sent = _send_lifecycle_email(order, template_id)
                        if email_sent:
                            actions["reminders_sent"].append(reminder)
                    
                    reminders_sent[reminder] = now.isoformat()
                
                cur.execute("""
                    UPDATE orders SET lifecycle_reminders_sent = %s
                    WHERE order_id = %s
                """, (json.dumps(reminders_sent), order_id))
                
                # Log each reminder
                for reminder in pending_reminders:
                    cur.execute("""
                        INSERT INTO order_events (order_id, event_type, event_data, source)
                        VALUES (%s, 'lifecycle_reminder_sent', %s, 'lifecycle_engine')
                    """, (order_id, json.dumps({
                        "reminder_type": reminder,
                        "days_inactive": days_inactive,
                    })))
    
    return actions


# =============================================================================
# EMAIL SENDING HELPER
# =============================================================================

def _send_lifecycle_email(order: Dict, template_id: str) -> bool:
    """
    Send a lifecycle email using the email_templates + Gmail API.
    
    Returns True if email was sent successfully.
    """
    try:
        from email_templates import render_template, get_template_subject
        from gmail_sender import send_email
        
        customer_email = order.get("email")
        if not customer_email:
            print(f"[LIFECYCLE] No email for order {order.get('order_id')} — skipping {template_id}")
            return False
        
        order_data = {
            "order_id": order.get("order_id", ""),
            "customer_name": order.get("customer_name", "Valued Customer"),
            "company_name": order.get("company_name", ""),
            "order_total": order.get("order_total", 0),
            "order_date": order.get("order_date", ""),
            "cancel_reason": "inactivity",
        }
        
        html_body = render_template(template_id, order_data)
        if not html_body:
            print(f"[LIFECYCLE] Template {template_id} not found — skipping")
            return False
        
        subject = get_template_subject(template_id, order_data)
        
        result = send_email(
            to=customer_email,
            subject=subject,
            html_body=html_body,
        )
        
        if result.get("success"):
            print(f"[LIFECYCLE] Sent {template_id} to {customer_email} for order {order.get('order_id')}")
            return True
        else:
            print(f"[LIFECYCLE] Failed to send {template_id}: {result.get('error')}")
            return False
            
    except ImportError as e:
        print(f"[LIFECYCLE] Email modules not available: {e}")
        return False
    except Exception as e:
        print(f"[LIFECYCLE] Email error for {template_id}: {e}")
        return False


# =============================================================================
# CORE ENGINE — CHECK ALL ORDERS
# =============================================================================

def check_all_orders_lifecycle() -> Dict:
    """
    Check all active (non-complete, non-canceled) orders for lifecycle actions.
    Meant to be called by a daily cron job via POST /lifecycle/check-all.
    
    Returns summary of actions taken.
    """
    now = datetime.now(timezone.utc)
    summary = {
        "checked_at": now.isoformat(),
        "orders_checked": 0,
        "status_changes": 0,
        "reminders_sent": 0,
        "cancellations": 0,
        "errors": [],
        "details": [],
    }
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get all non-complete, non-canceled orders
            cur.execute("""
                SELECT order_id FROM orders
                WHERE (is_complete = FALSE OR is_complete IS NULL)
                AND (lifecycle_status IS NULL OR lifecycle_status NOT IN ('canceled'))
                ORDER BY order_date ASC
            """)
            orders = cur.fetchall()
    
    for order_row in orders:
        order_id = order_row["order_id"]
        summary["orders_checked"] += 1
        
        try:
            result = process_order_lifecycle(order_id, now=now)
            
            if result.get("status_changed"):
                summary["status_changes"] += 1
            if result.get("canceled"):
                summary["cancellations"] += 1
            if result.get("reminders_sent"):
                summary["reminders_sent"] += len(result["reminders_sent"])
            
            # Include details for orders that had actions
            if result.get("status_changed") or result.get("reminders_sent") or result.get("canceled"):
                summary["details"].append(result)
                
        except Exception as e:
            summary["errors"].append({"order_id": order_id, "error": str(e)})
    
    return summary


# =============================================================================
# EXTEND DEADLINE (customer response)
# =============================================================================

def extend_deadline(order_id: str, days: int = 7) -> Dict:
    """
    Reset lifecycle clock when a customer responds.
    
    Per rules: Any customer email about the order resets the clock.
    Sets last_customer_email_at = NOW(), lifecycle_status = active,
    clears all sent reminders so they can re-fire from the new baseline.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            now = datetime.now(timezone.utc)
            
            cur.execute("""
                UPDATE orders SET
                    last_customer_email_at = %s,
                    lifecycle_status = %s,
                    lifecycle_reminders_sent = %s,
                    updated_at = NOW()
                WHERE order_id = %s
                RETURNING order_id, lifecycle_status
            """, (now, STATUS_ACTIVE, json.dumps({}), order_id))
            
            result = cur.fetchone()
            if not result:
                return {"success": False, "error": "Order not found"}
            
            # Log event
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'lifecycle_deadline_extended', %s, 'lifecycle_engine')
            """, (order_id, json.dumps({
                "new_last_email_at": now.isoformat(),
                "reason": "customer_response"
            })))
    
    return {
        "success": True,
        "order_id": order_id,
        "new_status": STATUS_ACTIVE,
        "last_customer_email_at": now.isoformat(),
    }


# =============================================================================
# CANCEL ORDER
# =============================================================================

def cancel_order(order_id: str, reason: str = "manual") -> Dict:
    """
    Cancel an order — sets lifecycle_status to canceled, marks complete,
    and cancels on B2BWave.
    
    Reasons:
      - 'customer_request': Customer said "cancel" in email
      - 'lifecycle_auto_cancel': Day 21 auto-cancellation  
      - 'manual': Admin manually canceled
    """
    b2b_result = {"success": False, "error": "not attempted"}
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = _cancel_order_in_db(cur, order_id, reason)
            if not result:
                return {"success": False, "error": "Order not found"}
            
            # Mark as complete (moves to Done tab)
            cur.execute("""
                UPDATE orders SET
                    is_complete = TRUE,
                    completed_at = NOW(),
                    updated_at = NOW()
                WHERE order_id = %s
            """, (order_id,))
    
    # Cancel on B2BWave
    b2b_result = cancel_order_on_b2bwave(order_id)
    
    return {
        "success": True,
        "order_id": order_id,
        "lifecycle_status": STATUS_CANCELED,
        "reason": reason,
        "b2bwave_canceled": b2b_result.get("success", False),
        "b2bwave_result": b2b_result,
    }


def _cancel_order_in_db(cur, order_id: str, reason: str) -> bool:
    """Internal: update order to canceled status in database."""
    cur.execute("""
        UPDATE orders SET
            lifecycle_status = %s,
            lifecycle_deadline_at = NULL,
            updated_at = NOW()
        WHERE order_id = %s
        RETURNING order_id
    """, (STATUS_CANCELED, order_id))
    
    result = cur.fetchone()
    if not result:
        return False
    
    cur.execute("""
        INSERT INTO order_events (order_id, event_type, event_data, source)
        VALUES (%s, 'order_canceled', %s, 'lifecycle_engine')
    """, (order_id, json.dumps({"reason": reason})))
    
    return True


# =============================================================================
# LIFECYCLE SUMMARY
# =============================================================================

def get_lifecycle_summary() -> Dict:
    """Get summary counts by lifecycle status for the dashboard."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    COALESCE(lifecycle_status, 'active') as status,
                    COUNT(*) as count
                FROM orders
                WHERE (is_complete = FALSE OR is_complete IS NULL)
                GROUP BY COALESCE(lifecycle_status, 'active')
                ORDER BY 
                    CASE COALESCE(lifecycle_status, 'active')
                        WHEN 'active' THEN 1
                        WHEN 'inactive' THEN 2
                        WHEN 'canceled' THEN 3
                    END
            """)
            rows = cur.fetchall()
            
            summary = {row["status"]: row["count"] for row in rows}
            summary["total"] = sum(row["count"] for row in rows)
            
            return summary


# =============================================================================
# QUOTE REMINDER ENGINE
# =============================================================================

def check_pending_quote_reminders() -> dict:
    """Check for pending quote reminders (quotes sent 3+ days ago with no payment activity)."""
    summary = {"reminders_sent": 0, "errors": [], "details": []}
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT pc.order_id, pc.checkout_url, pc.payment_amount, pc.customer_email,
                           o.customer_name, o.company_name, o.order_total, o.order_date
                    FROM pending_checkouts pc
                    JOIN orders o ON pc.order_id = o.order_id
                    WHERE pc.payment_completed_at IS NULL
                      AND pc.payment_initiated_at IS NULL
                      AND pc.created_at < NOW() - interval '3 days'
                      AND NOT EXISTS (
                          SELECT 1 FROM order_events e
                          WHERE e.order_id = pc.order_id
                          AND e.event_type = 'quote_reminder_sent'
                      )
                """)
                rows = cur.fetchall()

            for row in rows:
                order_id = row["order_id"]
                email = row.get("customer_email") or ""
                if not email:
                    summary["details"].append({"order_id": order_id, "skipped": True, "reason": "no_email"})
                    continue
                try:
                    first_name = (row.get("customer_name") or "Valued Customer").split()[0]
                    amount = row.get("payment_amount") or row.get("order_total") or 0
                    checkout_url = row.get("checkout_url") or ""
                    subject = f"Your Cabinet Quote for Order #{order_id} is Ready"
                    html_body = f"""<p>Hi {first_name},</p>
<p>Your quote for Order <strong>#{order_id}</strong> is still available.</p>
<p><a href="{checkout_url}" style="display:inline-block;padding:12px 24px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;">View Your Quote</a></p>
<p>Total: <strong>${amount:,.2f}</strong></p>
<p>Call <strong>(770) 990-4885</strong> to confirm your order.</p>
<p>Thank you,<br/>Cabinets for Contractors</p>"""

                    from gmail_sender import send_email
                    result = send_email(to=email, subject=subject, html_body=html_body)
                    sent = result.get("success", False)

                    if sent:
                        with get_db() as conn2:
                            with conn2.cursor() as cur2:
                                cur2.execute("""
                                    INSERT INTO order_events (order_id, event_type, event_data, created_at)
                                    VALUES (%s, 'quote_reminder_sent', %s, NOW())
                                """, (order_id, json.dumps({"email": email, "amount": float(amount)})))
                                conn2.commit()
                        summary["reminders_sent"] += 1
                        summary["details"].append({"order_id": order_id, "sent": True, "email": email})
                    else:
                        summary["details"].append({"order_id": order_id, "sent": False, "reason": "send_failed"})
                except Exception as e:
                    summary["errors"].append({"order_id": order_id, "error": str(e)})
    except Exception as e:
        summary["errors"].append({"error": str(e)})
    return summary
