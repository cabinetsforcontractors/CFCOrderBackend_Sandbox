"""
alerts_engine.py
CFC Orders AlertsEngine — implements ORD-A1 rules from rules.md v1.2

8 alert rules, all becoming CRITICAL at 24 business hours except delivery (96).
Business hours = Mon-Fri, excluding US federal holidays.
24 business hours ≈ 3 calendar days.
96 business hours ≈ 12 calendar days.

Usage:
    from alerts_engine import check_all_orders, check_order_alerts
    
    # Cron job (daily): check all active orders
    results = check_all_orders()
    
    # Single order check
    alerts = check_order_alerts(order_id)
"""

from datetime import datetime, timedelta, date
from typing import List, Dict, Optional, Tuple
from db_helpers import get_db, get_order_alerts, create_alert, resolve_alert
from psycopg2.extras import RealDictCursor


# =============================================================================
# BUSINESS HOURS CALCULATOR
# =============================================================================

# US Federal Holidays (fixed dates + computed)
# We pre-compute for the current and next year; expand as needed.
def _get_federal_holidays(year: int) -> set:
    """Get US federal holidays for a given year.
    
    Fixed-date holidays:
      - Jan 1: New Year's Day
      - Jun 19: Juneteenth
      - Jul 4: Independence Day
      - Nov 11: Veterans Day
      - Dec 25: Christmas Day
    
    Computed holidays:
      - 3rd Monday in Jan: MLK Day
      - 3rd Monday in Feb: Presidents' Day
      - Last Monday in May: Memorial Day
      - 1st Monday in Sep: Labor Day
      - 2nd Monday in Oct: Columbus Day
      - 4th Thursday in Nov: Thanksgiving
    """
    holidays = set()
    
    # Fixed dates
    holidays.add(date(year, 1, 1))    # New Year's
    holidays.add(date(year, 6, 19))   # Juneteenth
    holidays.add(date(year, 7, 4))    # Independence Day
    holidays.add(date(year, 11, 11))  # Veterans Day
    holidays.add(date(year, 12, 25))  # Christmas
    
    # Nth weekday of month helper
    def nth_weekday(year, month, weekday, n):
        """Get nth occurrence of weekday (0=Mon) in month."""
        first = date(year, month, 1)
        # Days until first occurrence of weekday
        days_ahead = weekday - first.weekday()
        if days_ahead < 0:
            days_ahead += 7
        first_occurrence = first + timedelta(days=days_ahead)
        return first_occurrence + timedelta(weeks=n - 1)
    
    def last_weekday(year, month, weekday):
        """Get last occurrence of weekday (0=Mon) in month."""
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        last_day = next_month - timedelta(days=1)
        days_back = (last_day.weekday() - weekday) % 7
        return last_day - timedelta(days=days_back)
    
    # Computed holidays (weekday 0 = Monday)
    holidays.add(nth_weekday(year, 1, 0, 3))   # MLK Day: 3rd Mon Jan
    holidays.add(nth_weekday(year, 2, 0, 3))   # Presidents' Day: 3rd Mon Feb
    holidays.add(last_weekday(year, 5, 0))      # Memorial Day: last Mon May
    holidays.add(nth_weekday(year, 9, 0, 1))   # Labor Day: 1st Mon Sep
    holidays.add(nth_weekday(year, 10, 0, 2))  # Columbus Day: 2nd Mon Oct
    holidays.add(nth_weekday(year, 11, 3, 4))  # Thanksgiving: 4th Thu Nov
    
    return holidays


# Cache holidays for performance
_holiday_cache = {}

def _is_business_day(d: date) -> bool:
    """Check if a date is a business day (Mon-Fri, not a holiday)."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    year = d.year
    if year not in _holiday_cache:
        _holiday_cache[year] = _get_federal_holidays(year)
    return d not in _holiday_cache[year]


def business_hours_elapsed(since: datetime, now: datetime = None) -> float:
    """Calculate business hours between two datetimes.
    
    Counts 8 business hours per business day.
    A full business day = 8 hours.
    24 business hours = 3 business days.
    96 business hours = 12 business days.
    """
    if now is None:
        now = datetime.now()
    
    if since is None:
        return 0.0
    
    # Strip timezone info BEFORE any comparison to avoid
    # "can't compare offset-naive and offset-aware datetimes"
    if hasattr(since, 'tzinfo') and since.tzinfo:
        since = since.replace(tzinfo=None)
    if hasattr(now, 'tzinfo') and now.tzinfo:
        now = now.replace(tzinfo=None)
    
    if since > now:
        return 0.0
    
    # Count business days between the two dates
    business_days = 0
    current = since.date()
    end = now.date()
    
    while current <= end:
        if _is_business_day(current):
            business_days += 1
        current += timedelta(days=1)
    
    # Convert to hours (8 hours per business day)
    return business_days * 8.0


# =============================================================================
# ALERT RULE DEFINITIONS (ORD-A1)
# =============================================================================

# Each rule: (alert_type, description, threshold_biz_hours, condition_sql, timestamp_column)
ALERT_RULES = [
    {
        "alert_type": "needs_invoice",
        "description": "Order placed, no invoice sent",
        "threshold_hours": 24,
        "condition": "payment_link_sent = FALSE OR payment_link_sent IS NULL",
        "resolved_condition": "payment_link_sent = TRUE",
        "timestamp_col": "COALESCE(order_date, created_at)",
    },
    {
        "alert_type": "awaiting_payment_long",
        "description": "Invoice sent, no payment received",
        "threshold_hours": 24,
        "condition": "payment_link_sent = TRUE AND (payment_received = FALSE OR payment_received IS NULL)",
        "resolved_condition": "payment_received = TRUE",
        "timestamp_col": "payment_link_sent_at",
    },
    {
        "alert_type": "needs_warehouse_order",
        "description": "Payment received, not sent to warehouse",
        "threshold_hours": 24,
        "condition": "payment_received = TRUE AND (sent_to_warehouse = FALSE OR sent_to_warehouse IS NULL)",
        "resolved_condition": "sent_to_warehouse = TRUE",
        "timestamp_col": "payment_received_at",
    },
    {
        "alert_type": "at_warehouse_long",
        "description": "Sent to warehouse, not confirmed",
        "threshold_hours": 24,
        "condition": "sent_to_warehouse = TRUE AND (warehouse_confirmed = FALSE OR warehouse_confirmed IS NULL)",
        "resolved_condition": "warehouse_confirmed = TRUE",
        "timestamp_col": "sent_to_warehouse_at",
    },
    {
        "alert_type": "needs_bol",
        "description": "Warehouse confirmed, no BOL sent",
        "threshold_hours": 24,
        "condition": "warehouse_confirmed = TRUE AND (bol_sent = FALSE OR bol_sent IS NULL)",
        "resolved_condition": "bol_sent = TRUE",
        "timestamp_col": "warehouse_confirmed_at",
    },
    {
        "alert_type": "ready_ship_long",
        "description": "BOL sent, not shipped",
        "threshold_hours": 24,
        "condition": "bol_sent = TRUE AND (tracking IS NULL OR tracking = '')",
        "resolved_condition": "tracking IS NOT NULL AND tracking != ''",
        "timestamp_col": "bol_sent_at",
    },
    {
        "alert_type": "tracking_not_sent",
        "description": "Shipped but no tracking email sent to customer",
        "threshold_hours": 24,
        # This one is tricky — we check if tracking exists but no tracking email event
        "condition": """tracking IS NOT NULL AND tracking != '' 
                       AND NOT EXISTS (
                           SELECT 1 FROM order_events e 
                           WHERE e.order_id = o.order_id 
                           AND e.event_type = 'tracking_email_sent'
                       )""",
        "resolved_condition": """EXISTS (
                           SELECT 1 FROM order_events e 
                           WHERE e.order_id = o.order_id 
                           AND e.event_type = 'tracking_email_sent'
                       )""",
        "timestamp_col": "updated_at",  # When tracking was added
    },
    {
        "alert_type": "delivery_confirm_needed",
        "description": "Shipped, no delivery confirmation",
        "threshold_hours": 96,  # 12 business days
        "condition": """tracking IS NOT NULL AND tracking != '' 
                       AND (is_complete = FALSE OR is_complete IS NULL)""",
        "resolved_condition": "is_complete = TRUE",
        "timestamp_col": "bol_sent_at",  # Approximate ship date
    },
]


# =============================================================================
# CORE ENGINE
# =============================================================================

def check_order_alerts(order_id: str) -> List[Dict]:
    """Check all alert rules for a single order. Returns list of new/existing alerts."""
    alerts_created = []
    alerts_resolved = []
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Fetch the order
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
            if not order:
                return []
            
            # Skip completed or canceled orders
            if order.get("is_complete"):
                # Auto-resolve any remaining alerts
                _resolve_all_for_order(cur, order_id)
                return []
            
            # Check each rule
            for rule in ALERT_RULES:
                result = _evaluate_rule(cur, order, rule)
                if result:
                    if result["action"] == "created":
                        alerts_created.append(result)
                    elif result["action"] == "resolved":
                        alerts_resolved.append(result)
    
    return alerts_created


def check_all_orders() -> Dict:
    """Check all active (non-complete) orders for alerts.
    
    Meant to be called by a daily cron job via POST /alerts/check-all.
    Returns summary of actions taken.
    """
    now = datetime.now()
    created_count = 0
    resolved_count = 0
    orders_checked = 0
    errors = []
    
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get all active orders
            cur.execute("""
                SELECT * FROM orders 
                WHERE (is_complete = FALSE OR is_complete IS NULL)
                ORDER BY order_date DESC
            """)
            orders = cur.fetchall()
            
            for order in orders:
                orders_checked += 1
                order_id = order["order_id"]
                
                try:
                    for rule in ALERT_RULES:
                        result = _evaluate_rule(cur, order, rule, now=now)
                        if result:
                            if result["action"] == "created":
                                created_count += 1
                            elif result["action"] == "resolved":
                                resolved_count += 1
                except Exception as e:
                    errors.append({"order_id": order_id, "error": str(e)})
        
        # Commit happens automatically via context manager
    
    return {
        "checked_at": now.isoformat(),
        "orders_checked": orders_checked,
        "alerts_created": created_count,
        "alerts_resolved": resolved_count,
        "errors": errors,
    }


def get_alert_summary() -> Dict:
    """Get a summary of all unresolved alerts grouped by type."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT alert_type, COUNT(*) as count
                FROM order_alerts 
                WHERE is_resolved = FALSE OR is_resolved IS NULL
                GROUP BY alert_type
                ORDER BY count DESC
            """)
            by_type = {row["alert_type"]: row["count"] for row in cur.fetchall()}
            
            cur.execute("""
                SELECT COUNT(*) as total
                FROM order_alerts 
                WHERE is_resolved = FALSE OR is_resolved IS NULL
            """)
            total = cur.fetchone()["total"]
            
            return {
                "total_unresolved": total,
                "by_type": by_type,
            }


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _evaluate_rule(cur, order: Dict, rule: Dict, now: datetime = None) -> Optional[Dict]:
    """Evaluate a single alert rule against an order.
    
    Returns:
        {"action": "created", ...} if new alert created
        {"action": "resolved", ...} if existing alert resolved
        None if no action needed
    """
    if now is None:
        now = datetime.now()
    
    order_id = order["order_id"]
    alert_type = rule["alert_type"]
    
    # Check if there's already an unresolved alert of this type
    cur.execute("""
        SELECT id FROM order_alerts 
        WHERE order_id = %s AND alert_type = %s 
        AND (is_resolved = FALSE OR is_resolved IS NULL)
        LIMIT 1
    """, (order_id, alert_type))
    existing = cur.fetchone()
    
    # Evaluate whether the alert condition is currently true
    # We do this in Python since the order dict is already loaded
    condition_met = _check_condition(order, rule, cur)
    
    if condition_met:
        # Condition is active — check if threshold is exceeded
        timestamp_col = rule["timestamp_col"]
        
        # Get the relevant timestamp
        # Handle COALESCE-style columns
        if "COALESCE" in timestamp_col:
            # Parse COALESCE(col1, col2)
            cols = timestamp_col.replace("COALESCE(", "").replace(")", "").split(",")
            ts = None
            for col in cols:
                col = col.strip()
                ts = order.get(col)
                if ts:
                    break
        else:
            ts = order.get(timestamp_col)
        
        if ts is None:
            return None  # No timestamp to measure from
        
        # Calculate elapsed business hours
        elapsed = business_hours_elapsed(ts, now)
        
        if elapsed >= rule["threshold_hours"]:
            if not existing:
                # Create new alert
                hours_label = f"{int(elapsed)} biz hrs"
                message = f"{rule['description']} ({hours_label})"
                
                cur.execute("""
                    INSERT INTO order_alerts (order_id, alert_type, alert_message)
                    VALUES (%s, %s, %s)
                    RETURNING id
                """, (order_id, alert_type, message))
                alert_id = cur.fetchone()["id"]
                
                return {
                    "action": "created",
                    "order_id": order_id,
                    "alert_type": alert_type,
                    "alert_id": alert_id,
                    "elapsed_hours": elapsed,
                    "message": message,
                }
        # Threshold not yet exceeded, or alert already exists — no action
        return None
    
    else:
        # Condition no longer met — resolve existing alert if any
        if existing:
            cur.execute("""
                UPDATE order_alerts 
                SET is_resolved = TRUE, resolved_at = NOW()
                WHERE id = %s
            """, (existing["id"],))
            
            return {
                "action": "resolved",
                "order_id": order_id,
                "alert_type": alert_type,
                "alert_id": existing["id"],
            }
        
        return None


def _check_condition(order: Dict, rule: Dict, cur) -> bool:
    """Check if an alert rule's condition is met for this order.
    
    Uses Python-side checks based on order dict fields, with SQL fallback
    for conditions involving subqueries (like tracking_not_sent).
    """
    alert_type = rule["alert_type"]
    
    if alert_type == "needs_invoice":
        return not order.get("payment_link_sent")
    
    elif alert_type == "awaiting_payment_long":
        return (order.get("payment_link_sent") and 
                not order.get("payment_received"))
    
    elif alert_type == "needs_warehouse_order":
        return (order.get("payment_received") and 
                not order.get("sent_to_warehouse"))
    
    elif alert_type == "at_warehouse_long":
        return (order.get("sent_to_warehouse") and 
                not order.get("warehouse_confirmed"))
    
    elif alert_type == "needs_bol":
        return (order.get("warehouse_confirmed") and 
                not order.get("bol_sent"))
    
    elif alert_type == "ready_ship_long":
        tracking = order.get("tracking") or ""
        return order.get("bol_sent") and not tracking.strip()
    
    elif alert_type == "tracking_not_sent":
        tracking = order.get("tracking") or ""
        if not tracking.strip():
            return False
        # Check for tracking_email_sent event via SQL
        cur.execute("""
            SELECT 1 FROM order_events 
            WHERE order_id = %s AND event_type = 'tracking_email_sent'
            LIMIT 1
        """, (order["order_id"],))
        return cur.fetchone() is None
    
    elif alert_type == "delivery_confirm_needed":
        tracking = order.get("tracking") or ""
        return (tracking.strip() and 
                not order.get("is_complete"))
    
    return False


def _resolve_all_for_order(cur, order_id: str):
    """Resolve all unresolved alerts for a completed/canceled order."""
    cur.execute("""
        UPDATE order_alerts 
        SET is_resolved = TRUE, resolved_at = NOW()
        WHERE order_id = %s 
        AND (is_resolved = FALSE OR is_resolved IS NULL)
    """, (order_id,))
