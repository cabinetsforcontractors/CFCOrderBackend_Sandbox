"""
add_supplier_polling_columns.py
Standalone migration: add supplier polling columns to order_shipments.
Run via POST /add-supplier-poll-columns

Columns added:
    supplier_token          VARCHAR(64) UNIQUE  — tokenized link for warehouse emails
    pickup_date             DATE                — expected ship date entered by warehouse
    pickup_time             VARCHAR(20)         — confirmed pickup time (triggers BOL)
    supplier_poll_sent_count INTEGER DEFAULT 0  — 1, 2, or 3 polls sent
    supplier_poll_1_sent_at TIMESTAMP           — initial poll sent at
    supplier_poll_2_sent_at TIMESTAMP           — 24hr escalation
    supplier_poll_3_sent_at TIMESTAMP           — 48hr critical escalation
    day_before_poll_sent_at TIMESTAMP           — confirmation poll sent night before pickup
    day_before_confirmed    BOOLEAN DEFAULT FALSE — warehouse said YES to day-before poll
"""

from db_helpers import get_db


def add_supplier_polling_columns() -> dict:
    """Add warehouse polling columns to order_shipments. Safe to run multiple times."""
    results = []
    cols = [
        ("supplier_token",          "VARCHAR(64) UNIQUE"),
        ("pickup_date",             "DATE"),
        ("pickup_time",             "VARCHAR(20)"),
        ("supplier_poll_sent_count","INTEGER DEFAULT 0"),
        ("supplier_poll_1_sent_at", "TIMESTAMP WITH TIME ZONE"),
        ("supplier_poll_2_sent_at", "TIMESTAMP WITH TIME ZONE"),
        ("supplier_poll_3_sent_at", "TIMESTAMP WITH TIME ZONE"),
        ("day_before_poll_sent_at", "TIMESTAMP WITH TIME ZONE"),
        ("day_before_confirmed",    "BOOLEAN DEFAULT FALSE"),
    ]
    with get_db() as conn:
        with conn.cursor() as cur:
            for col_name, col_def in cols:
                try:
                    cur.execute(f"ALTER TABLE order_shipments ADD COLUMN {col_name} {col_def}")
                    results.append(f"{col_name}: added")
                except Exception as e:
                    if "already exists" in str(e):
                        results.append(f"{col_name}: already exists")
                    else:
                        results.append(f"{col_name}: ERROR — {str(e)}")
                    conn.rollback()
                    continue
            conn.commit()
    return {
        "status": "ok",
        "message": "Supplier polling columns added to order_shipments",
        "results": results,
    }
