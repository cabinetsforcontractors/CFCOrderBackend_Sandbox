"""
shortages.py — Wave 3 build H (William 2026-08-02): SHORT-SHIP / SPLIT
FULFILLMENT gets a home.

The 5696 story: Milestone shipped short (2× SA-DS-SB30/VS30 missing),
sourced the makeup from their North Carolina location under a separate
ref (#POFL05489) — and the whole thing lived only in email memory. Now a
shortage is a RECORD: the short lines, where the makeup ships from,
their ref, the ETA, and a status that has to be walked to resolved.

DESIGN: humans decide everything (record/update are admin doors); the
robot's contribution is the record, one internal alert on creation, an
optional DRAFT-FIRST customer note ("part of your order ships
separately"), and surfacing in GET /shortages until resolved.
"""

import json
import os
from typing import Dict, List, Optional

from db_helpers import get_db, get_order_by_id

INTERNAL_ALERT_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "orders@cabinetsforcontractors.com").strip()

STATUSES = ("open", "makeup_arranged", "resolved")


def ensure_shortages_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supplier_shortages (
                id SERIAL PRIMARY KEY,
                order_id VARCHAR(20) NOT NULL,
                warehouse VARCHAR(60) NOT NULL,
                short_items TEXT NOT NULL,
                source_location TEXT,
                supplier_ref VARCHAR(80),
                eta DATE,
                status VARCHAR(20) DEFAULT 'open',
                note TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        conn.commit()


def record_shortage(order_id: str, warehouse: str, items: List[Dict],
                    source_location: str = "", supplier_ref: str = "",
                    eta: str = "", note: str = "",
                    customer_draft: bool = True) -> Dict:
    """items = [{"sku": ..., "qty": ...}]. Records the shortage, alerts the
    humans, and (default) lands a DRAFT-FIRST customer note in Gmail drafts.
    Nothing sends to the customer without William's finger."""
    if not items:
        return {"status": "error", "message": "items required"}
    with get_db() as conn:
        ensure_shortages_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO supplier_shortages
                    (order_id, warehouse, short_items, source_location,
                     supplier_ref, eta, note)
                VALUES (%s, %s, %s, %s, %s, NULLIF(%s, '')::date, %s)
                RETURNING id
            """, (order_id, warehouse, json.dumps(items),
                  source_location or None, supplier_ref or None,
                  eta or "", note or None))
            sid = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'shortage_recorded', %s, 'shortages')
            """, (order_id, json.dumps(
                {"shortage_id": sid, "warehouse": warehouse, "items": items,
                 "source_location": source_location or None,
                 "supplier_ref": supplier_ref or None, "eta": eta or None},
                default=str)))
            conn.commit()

    items_txt = ", ".join(f"{i.get('qty')}x {i.get('sku')}" for i in items)
    from supplier_orders import _send_email
    _send_email(order_id, INTERNAL_ALERT_EMAIL,
                f"SHORT SHIP recorded - order #{order_id} ({warehouse})",
                f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                f"<p><strong>Shortage #{sid} recorded on order #{order_id} "
                f"({warehouse}).</strong></p>"
                f"<p>Short: {items_txt}</p>"
                f"<p>Makeup source: {source_location or '(not set)'} &middot; "
                f"their ref: {supplier_ref or '(not set)'} &middot; "
                f"ETA: {eta or '(not set)'}</p>"
                f"<p>It stays on GET /shortages until resolved.</p></div>",
                triggered_by="shortage_recorded")

    draft = None
    if customer_draft:
        try:
            order = get_order_by_id(order_id) or {}
            to = (order.get("email") or "").strip()
            first = (order.get("customer_name") or "").split(" ")[0] or "there"
            if to:
                from email_sender import create_gmail_draft
                eta_line = (f" We expect it around <strong>{eta}</strong>."
                            if eta else "")
                draft = create_gmail_draft(
                    to,
                    f"Order #{order_id} - part of your order ships separately",
                    f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                    f"<p>Hey {first},</p>"
                    f"<p>A heads-up on order #{order_id}: "
                    f"<strong>{items_txt}</strong> is coming in a separate "
                    f"shipment from the rest of your order.{eta_line}</p>"
                    f"<p>Everything else is on its way as planned. Reply here "
                    f"or call (770) 990-4885 with any questions.</p>"
                    f"<p>Thank you,<br>William<br>Cabinets For Contractors</p>"
                    f"</div>")
        except Exception as e:
            draft = {"success": False, "error": str(e)}
    return {"status": "ok", "shortage_id": sid, "customer_draft": draft}


def update_shortage(shortage_id: int, status: str = "", eta: str = "",
                    supplier_ref: str = "", source_location: str = "",
                    note: str = "") -> Dict:
    if status and status not in STATUSES:
        return {"status": "error",
                "message": f"status must be one of {STATUSES}"}
    with get_db() as conn:
        ensure_shortages_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE supplier_shortages SET
                    status = COALESCE(NULLIF(%s, ''), status),
                    eta = COALESCE(NULLIF(%s, '')::date, eta),
                    supplier_ref = COALESCE(NULLIF(%s, ''), supplier_ref),
                    source_location = COALESCE(NULLIF(%s, ''), source_location),
                    note = COALESCE(NULLIF(%s, ''), note),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING order_id, status
            """, (status, eta, supplier_ref, source_location, note,
                  shortage_id))
            row = cur.fetchone()
            if not row:
                return {"status": "error",
                        "message": f"shortage {shortage_id} not found"}
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'shortage_updated', %s, 'shortages')
            """, (row[0], json.dumps(
                {"shortage_id": shortage_id, "status": row[1],
                 "eta": eta or None, "supplier_ref": supplier_ref or None,
                 "note": note or None}, default=str)))
            conn.commit()
    return {"status": "ok", "shortage_id": shortage_id, "now": row[1]}


def list_shortages(include_resolved: bool = False) -> Dict:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_shortages_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if include_resolved:
                cur.execute("""SELECT * FROM supplier_shortages
                               ORDER BY created_at DESC LIMIT 200""")
            else:
                cur.execute("""SELECT * FROM supplier_shortages
                               WHERE status != 'resolved'
                               ORDER BY created_at DESC LIMIT 200""")
            rows = cur.fetchall()
    for r in rows:
        try:
            r["short_items"] = json.loads(r["short_items"])
        except Exception:
            pass
    return {"status": "ok", "count": len(rows), "shortages": rows}
