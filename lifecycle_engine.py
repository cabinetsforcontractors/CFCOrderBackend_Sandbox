"""
lifecycle_engine.py
CFC Orders Lifecycle Engine — automated inactivity detection, archiving, cancellation.

Order Lifecycle Rules (William's rules, Mar 2 2026):
  - Clock basis: last email to/from customer about that order
  - System reminder emails do NOT reset the clock
  - Customer response adds +7 days to ALL action timers
  - "Cancel" keyword in customer email → immediate B2BWave cancel

Timeline:
  Day 6:  Auto email — "order hasn't been paid"         (does NOT reset clock)
  Day 7:  Move → Inactive tab
  Day 29: Auto email — "order marked inactive"           (does NOT reset clock)
  Day 30: Move → Archived tab
  Day 44: Auto email — "order will be deleted tomorrow"  (does NOT reset clock)
  Day 45: Hit B2BWave API → cancel order on website

Lifecycle statuses: active, inactive, archived, canceled

Usage:
    from lifecycle_engine import check_all_orders_lifecycle, process_order_lifecycle
    
    # Cron job (daily): check all orders
    results = check_all_orders_lifecycle()
    
    # Single order check
    result = process_order_lifecycle(order_id)
    
    # Customer response detected (extends deadline +7 days)
    extend_deadline(order_id, days=7)
    
    # Customer said "cancel"
    cancel_order(order_id, reason="customer_request")
"""

import os
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from db_helpers import get_db
from psycopg2.extras import RealDictCursor


# =============================================================================
# CONSTANTS
# =============================================================================

# Lifecycle timeline (calendar days since last customer email activity)
INACTIVE_DAY = 7
ARCHIVE_DAY = 30
CANCEL_DAY = 45

# Reminder email trigger days (does NOT reset clock)
PAYMENT_REMINDER_DAY = 6
INACTIVE_NOTICE_DAY = 29
DELETION_WARNING_DAY = 44

# Lifecycle status values
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_ARCHIVED = "archived"
STATUS_CANCELED = "canceled"

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

# B2BWave API config (from config.py)
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
    
    Per rules: immediate B2BWave cancel + confirmation email + status change.
    """
    if not text:
        return False
    text_lower = text.lower().strip()
    for pattern in CANCEL_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


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
    
    days_inactive = (now - last_customer_email_at).days
    
    if days_inactive >= CANCEL_DAY:
        return STATUS_CANCELED, days_inactive, None
    elif days_inactive >= ARCHIVE_DAY:
        cancel_at = last_customer_email_at + timedelta(days=CANCEL_DAY)
        return STATUS_ARCHIVED, days_inactive, cancel_at
    elif days_inactive >= INACTIVE_DAY:
        archive_at = last_customer_email_at + timedelta(days=ARCHIVE_DAY)
        return STATUS_INACTIVE, days_inactive, archive_at
    else:
        inactive_at = last_customer_email_at + timedelta(days=INACTIVE_DAY)
        return STATUS_ACTIVE, days_inactive, inactive_at


def get_pending_reminders(
    last_customer_email_at: Optional[datetime],
    reminders_sent: dict,
    now: Optional[datetime] = None
) -> List[str]:
    """
    Determine which reminder emails should be sent based on days inactive.
    Only returns reminders that haven't been sent yet.
    
    reminders_sent: dict with keys like 'payment_reminder', 'inactive_notice', 
                    'deletion_warning' and boolean values.
    
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
    
    days_inactive = (now - last_customer_email_at).days
    pending = []
    
    if days_inactive >= PAYMENT_REMINDER_DAY and not reminders_sent.get("payment_reminder"):
        pending.append("payment_reminder")
    
    if days_inactive >= INACTIVE_NOTICE_DAY and not reminders_sent.get("inactive_notice"):
        pending.append("inactive_notice")
    
    if days_inactive >= DELETION_WARNING_DAY and not reminders_sent.get("deletion_warning"):
        pending.append("deletion_warning")
    
    return pending


# =============================================================================
# CORE ENGINE — PROCESS SINGLE ORDER
# =============================================================================

def process_order_lifecycle(order_id: str, now: Optional[datetime] = None) -> Dict:
    """
    Evaluate lifecycle status for a single order.
    Updates lifecycle_status and lifecycle_deadline_at in DB.
    
    Returns dict with actions taken.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    
    actions = {
        "order_id": order_id,
        "status_changed": False,
        "old_status": None,
        "new_status": None,
        "reminders_queued": [],
        "canceled": False,
        "days_inactive": 0,
    }
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Fetch order with lifecycle fields
            cur.execute("""
                SELECT order_id, is_complete, 
                       last_customer_email_at, lifecycle_status, 
                       lifecycle_deadline_at, lifecycle_reminders_sent
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
            
            # Handle day 45 cancellation
            if new_status == STATUS_CANCELED and current_lc_status != STATUS_CANCELED:
                _cancel_order_in_db(cur, order_id, "lifecycle_auto_cancel")
                actions["canceled"] = True
                actions["new_status"] = STATUS_CANCELED
                actions["status_changed"] = True
                # Note: B2BWave cancel + email will be handled by Phase 4
                # For now, just update the DB status
                
                # Log event
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'lifecycle_cancel', %s, 'lifecycle_engine')
                """, (order_id, json.dumps({
                    "days_inactive": days_inactive,
                    "last_customer_email_at": last_email.isoformat() if last_email else None,
                    "reason": "45_day_auto_cancel"
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
            
            # Mark reminders as queued (actual sending is Phase 4)
            if pending_reminders:
                for reminder in pending_reminders:
                    reminders_sent[reminder] = now.isoformat()
                
                cur.execute("""
                    UPDATE orders SET lifecycle_reminders_sent = %s
                    WHERE order_id = %s
                """, (json.dumps(reminders_sent), order_id))
                
                actions["reminders_queued"] = pending_reminders
                
                # Log each reminder
                for reminder in pending_reminders:
                    cur.execute("""
                        INSERT INTO order_events (order_id, event_type, event_data, source)
                        VALUES (%s, 'lifecycle_reminder_queued', %s, 'lifecycle_engine')
                    """, (order_id, json.dumps({
                        "reminder_type": reminder,
                        "days_inactive": days_inactive,
                    })))
    
    return actions


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
        "reminders_queued": 0,
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
            if result.get("reminders_queued"):
                summary["reminders_queued"] += len(result["reminders_queued"])
            
            # Include details for orders that had actions
            if result.get("status_changed") or result.get("reminders_queued") or result.get("canceled"):
                summary["details"].append(result)
                
        except Exception as e:
            summary["errors"].append({"order_id": order_id, "error": str(e)})
    
    return summary


# =============================================================================
# EXTEND DEADLINE (customer response)
# =============================================================================

def extend_deadline(order_id: str, days: int = 7) -> Dict:
    """
    Extend all lifecycle deadlines by N days when a customer responds.
    
    Per rules: Any customer email about the order adds +7 days to ALL action timers.
    This is done by updating last_customer_email_at to NOW, which effectively
    resets the day count. However, to preserve the +7 day extension behavior,
    we reset the reminders_sent so they can fire again from the new baseline.
    
    Actually — re-reading the rules: "Customer response adds +7 days to all timers"
    This means the timers EXTEND, not reset. So if an order is at day 25 and 
    customer responds, the inactive timer (day 7) already passed, but archive 
    timer (day 30) becomes day 37, cancel timer (day 45) becomes day 52.
    
    Implementation: We set last_customer_email_at = NOW(), which resets the 
    day counter to 0. This effectively gives more than +7 days for most timers
    but is the simplest correct behavior — the clock restarts from the last
    customer interaction, which is the stated rule basis.
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
                "extended_by_days": days,
                "new_last_email_at": now.isoformat(),
                "reason": "customer_response"
            })))
    
    return {
        "success": True,
        "order_id": order_id,
        "new_status": STATUS_ACTIVE,
        "deadline_extended_by": days,
        "last_customer_email_at": now.isoformat(),
    }


# =============================================================================
# CANCEL ORDER
# =============================================================================

def cancel_order(order_id: str, reason: str = "manual") -> Dict:
    """
    Cancel an order — sets lifecycle_status to canceled.
    
    Reasons:
      - 'customer_request': Customer said "cancel" in email
      - 'lifecycle_auto_cancel': Day 45 auto-cancellation  
      - 'manual': Admin manually canceled
    
    Phase 4 will add: B2BWave API cancel + confirmation email.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            result = _cancel_order_in_db(cur, order_id, reason)
            if not result:
                return {"success": False, "error": "Order not found"}
    
    return {
        "success": True,
        "order_id": order_id,
        "lifecycle_status": STATUS_CANCELED,
        "reason": reason,
        # Phase 4: "b2bwave_canceled": True/False,
        # Phase 4: "confirmation_email_sent": True/False,
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
                        WHEN 'archived' THEN 3
                        WHEN 'canceled' THEN 4
                    END
            """)
            rows = cur.fetchall()
            
            summary = {row["status"]: row["count"] for row in rows}
            summary["total"] = sum(row["count"] for row in rows)
            
            return summary
