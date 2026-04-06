"""
add_close_time_column.py
Migration: add close_time column to order_shipments.
Run via POST /add-close-time-column

close_time VARCHAR(20) — warehouse close time for R+L pickup request (e.g. "5:00 PM")
"""

from db_helpers import get_db


def add_close_time_column() -> dict:
    results = []
    with get_db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("ALTER TABLE order_shipments ADD COLUMN close_time VARCHAR(20)")
                results.append("close_time: added")
            except Exception as e:
                if "already exists" in str(e):
                    results.append("close_time: already exists")
                else:
                    results.append(f"close_time: ERROR — {str(e)}")
                conn.rollback()
        conn.commit()
    return {"status": "ok", "results": results}
