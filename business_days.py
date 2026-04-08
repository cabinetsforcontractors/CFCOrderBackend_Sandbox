"""
business_days.py
Business day calculator for CFC Orders lifecycle and quote timers.
Counts Mon-Fri only. No holiday calendar -- weekends only.
"""

from datetime import date, datetime, timedelta, timezone


def business_days_since(start: datetime) -> int:
    """Count business days (Mon-Fri) between start datetime and now."""
    if not start:
        return 0
    if hasattr(start, 'tzinfo') and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc).date()
    start_date = start.date() if hasattr(start, 'date') else start
    count = 0
    current = start_date
    while current < now:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Mon=0 Fri=4
            count += 1
    return count


def add_business_days(start: date, n: int) -> date:
    """Return the date n business days after start."""
    current = start
    added = 0
    while added < n:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def is_business_day(d: date = None) -> bool:
    """Return True if d (default: today) is Mon-Fri."""
    d = d or date.today()
    return d.weekday() < 5
