"""
pickup_window.py — WILLIAM'S PICKUP LAW (ruled 2026-07-27):
pickup requests run Mon-Thu 9:00-16:00 and Fri 9:00-15:00 Eastern,
NEVER Saturday or Sunday; a same-day request inside the final 2 hours
of the window (or after close) rolls to the NEXT business morning.

Before this module, BOL/pickup dates defaulted to plain "today" — a
Sunday-created pickup literally requested a Sunday pickup.
"""

from datetime import datetime, timedelta, timezone


_CLOSE_HOUR = {0: 16, 1: 16, 2: 16, 3: 16, 4: 15}  # Mon-Thu 4pm, Fri 3pm


def _now_eastern() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=4)


def next_pickup_date(now: datetime = None) -> str:
    """Earliest LAWFUL pickup date, ISO (YYYY-MM-DD). Same-day only when the
    request lands more than 2 hours before that day's close; weekends and
    late requests roll forward to the next business day."""
    now = now or _now_eastern()
    d = now
    for _ in range(8):
        close = _CLOSE_HOUR.get(d.weekday())
        if close is not None:
            if d.date() != now.date():
                return d.date().isoformat()
            if (now.hour + now.minute / 60.0) < (close - 2):
                return d.date().isoformat()
        d = d + timedelta(days=1)
    return d.date().isoformat()


def next_pickup_date_mmddyyyy(now: datetime = None) -> str:
    """Same law, R+L's MM/DD/YYYY shape."""
    y, m, dd = next_pickup_date(now).split("-")
    return f"{m}/{dd}/{y}"
