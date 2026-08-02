"""
store_credits.py — Wave 4 (William 2026-08-02): STORE CREDIT LEDGER.

Born from the substitution price law: when a customer has ALREADY PAID
and the replacement SKU costs less, the difference becomes a store
credit. The credit lives here and APPEARS ON THEIR NEXT INVOICE as a
credit line (after the tariff line — William's placement ruling), which
also lowers the Square link amount.

Mechanics:
  - add_store_credit() writes an OPEN credit row + order event.
  - Every invoice build (auto_invoice + create_invoice_draft) calls
    apply_credit_to_totals(): open credit is consumed up to grand−$1.00
    (Square's minimum charge) and rides shipping_result as
    store_credit_amount / store_credit_note.
  - mark_credits_applied() flips the rows AFTER the invoice actually
    lands (send or draft success) — a failed send never burns credit.
"""

import json
from typing import Dict, List, Tuple

from db_helpers import get_db


def ensure_store_credits_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS store_credits (
                id SERIAL PRIMARY KEY,
                customer_email VARCHAR(200) NOT NULL,
                source_order_id VARCHAR(20),
                amount NUMERIC(10,2) NOT NULL,
                reason TEXT,
                status VARCHAR(20) DEFAULT 'open',
                applied_order_id VARCHAR(20),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                applied_at TIMESTAMPTZ
            )""")
        conn.commit()


def add_store_credit(customer_email: str, source_order_id: str,
                     amount: float, reason: str) -> Dict:
    amount = round(float(amount), 2)
    if amount <= 0:
        return {"status": "error", "message": "amount must be positive"}
    with get_db() as conn:
        ensure_store_credits_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO store_credits
                    (customer_email, source_order_id, amount, reason)
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (customer_email.strip().lower(), str(source_order_id),
                  amount, reason))
            cid = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'store_credit_added', %s, 'store_credits')
            """, (str(source_order_id), json.dumps(
                {"credit_id": cid, "customer_email": customer_email,
                 "amount": amount, "reason": reason})))
            conn.commit()
    return {"status": "ok", "credit_id": cid, "amount": amount}


def open_credit_for(customer_email: str) -> Tuple[float, List[int]]:
    if not (customer_email or "").strip():
        return 0.0, []
    with get_db() as conn:
        ensure_store_credits_table(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, amount FROM store_credits
                           WHERE customer_email = %s AND status = 'open'
                           ORDER BY created_at""",
                        (customer_email.strip().lower(),))
            rows = cur.fetchall()
    return round(sum(float(r[1]) for r in rows), 2), [r[0] for r in rows]


def apply_credit_to_totals(order_data: Dict, grand: float) -> Tuple[float, float, List[int]]:
    """Returns (new_grand, credit_used, credit_ids). Consumes open credit
    down to a $1.00 floor (Square minimum). Mutates
    order_data['shipping_result'] with the credit keys when used."""
    email = (order_data.get("email") or "").strip()
    total, ids = open_credit_for(email)
    if total <= 0 or grand <= 1.00:
        return grand, 0.0, []
    credit_used = round(min(total, grand - 1.00), 2)
    new_grand = round(grand - credit_used, 2)
    sr = order_data.get("shipping_result") or {}
    sr["store_credit_amount"] = credit_used
    sr["store_credit_note"] = "Store credit applied"
    sr["grand_total"] = new_grand
    order_data["shipping_result"] = sr
    return new_grand, credit_used, ids


def mark_credits_applied(credit_ids: List[int], applied_order_id: str,
                         credit_used: float):
    """Flip the consumed rows. Partial use of the LAST row splits it: the
    unused remainder stays open as a fresh row."""
    if not credit_ids:
        return
    with get_db() as conn:
        ensure_store_credits_table(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, customer_email, source_order_id, amount
                           FROM store_credits WHERE id = ANY(%s) AND
                           status = 'open' ORDER BY created_at""",
                        (credit_ids,))
            rows = cur.fetchall()
            remaining = round(float(credit_used), 2)
            for cid, email, src, amt in rows:
                if remaining <= 0:
                    break
                amt = float(amt)
                use = round(min(amt, remaining), 2)
                remaining = round(remaining - use, 2)
                cur.execute("""UPDATE store_credits SET status = 'applied',
                               applied_order_id = %s, applied_at = NOW()
                               WHERE id = %s""", (str(applied_order_id), cid))
                if use < amt:
                    cur.execute("""
                        INSERT INTO store_credits
                            (customer_email, source_order_id, amount, reason)
                        VALUES (%s, %s, %s, %s)
                    """, (email, src, round(amt - use, 2),
                          f"remainder of credit #{cid}"))
            conn.commit()


def list_credits(customer_email: str = "") -> Dict:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_store_credits_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if customer_email:
                cur.execute("""SELECT * FROM store_credits
                               WHERE customer_email = %s
                               ORDER BY created_at DESC LIMIT 100""",
                            (customer_email.strip().lower(),))
            else:
                cur.execute("""SELECT * FROM store_credits
                               ORDER BY created_at DESC LIMIT 100""")
            rows = cur.fetchall()
    return {"status": "ok", "count": len(rows), "credits": rows}
