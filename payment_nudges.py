"""
payment_nudges.py — THE PAYMENT NUDGE CADENCE (William's ruling 8/4:
"payment nudge at 3 business days after invoice, lands as a DRAFT for
your send, repeats every 3 business days, Nationwide exempt").

Born from the 5714 finding: the lifecycle reminder engine existed but
was wired to NOTHING — no order ever got a payment nudge. This module
rides the ledger cycle instead:

  - Orders invoiced (payment_link_sent) and UNPAID, active, not
    complete, real money, not test-registry.
  - EXEMPT: by-check / terms accounts (config.pays_by_check — Nationwide
    class). Never nag a customer who pays on their monthly cycle.
  - First nudge: 3 BUSINESS days after the invoice went out.
  - Repeats: every 3 business days after the previous nudge draft.
  - DRAFT-FIRST: each nudge lands as a Gmail DRAFT in orders@ for
    William's send (the 5707 wording that got Gerald to pay same-day).
    The drafts sweep shows it on the board; sending it is his hand.
  - Idempotent via payment_nudge_drafted events (FK law: real order id).
"""

import json
from typing import Dict

from db_helpers import get_db


def run_payment_nudges() -> Dict:
    out = {"drafted": [], "skipped_terms": 0, "errors": []}
    try:
        from test_registry import test_order_ids
        tests = test_order_ids()
    except Exception:
        tests = set()
    try:
        from oos_detect import business_hours_between
        from business_days import business_days_since
    except Exception as e:
        return {"errors": [f"clock import: {e}"]}
    from config import pays_by_check

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.order_id, o.customer_name, o.company_name, o.email,
                       o.order_total, o.payment_link_sent_at,
                       GREATEST(
                           (SELECT MAX(e.created_at) FROM order_events e
                            WHERE e.order_id = o.order_id
                              AND e.event_type = 'payment_nudge_drafted'),
                           -- HAND-SENT NUDGES count (the 5714 case:
                           -- William nudged by hand before this module
                           -- existed - the sent ledger is the anchor)
                           (SELECT MAX(l.email_date) FROM email_ledger l
                            WHERE l.folder = 'sent'
                              AND l.subject ILIKE
                                  'Order #' || o.order_id ||
                                  ' - checking in%'))
                FROM orders o
                WHERE COALESCE(o.payment_link_sent, FALSE) = TRUE
                  AND COALESCE(o.payment_received, FALSE) = FALSE
                  AND COALESCE(o.is_complete, FALSE) = FALSE
                  AND COALESCE(o.lifecycle_status, 'active') = 'active'
                  AND COALESCE(o.order_total, 0) > 0
                  AND o.payment_link_sent_at IS NOT NULL
            """)
            rows = cur.fetchall()

    for (oid, cust, company, email, total, invoiced_at, last_nudge) in rows:
        if str(oid) in tests or not email:
            continue
        order_like = {"customer_name": cust or "", "company_name": company or ""}
        if pays_by_check(order_like):
            out["skipped_terms"] += 1
            continue
        anchor = last_nudge or invoiced_at
        try:
            if business_days_since(anchor) < 3:
                continue
        except Exception as e:
            out["errors"].append(f"{oid} clock: {e}")
            continue
        first_name = (cust or "").split(" ")[0].title() or "there"
        inv_date = invoiced_at.strftime("%B %d") if invoiced_at else "recently"
        subject = f"Order #{oid} - checking in on your invoice"
        html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;"
                f"color:#333'><p>Hey {first_name},</p>"
                f"<p>Just checking in on order <b>#{oid}</b> — the invoice "
                f"went out on {inv_date} and I don't see a payment on it "
                f"yet. The total is <b>${float(total):,.2f}</b>.</p>"
                f"<p>The payment link in the invoice email is still live — "
                f"if it's easier, just reply here and I'll resend it. And "
                f"if anything on the order needs adjusting first, tell me "
                f"and we'll sort it.</p>"
                f"<p>Thank you,<br>--<br>William Prince<br>"
                f"Cabinets For Contractors<br>"
                f"www.CabinetsForContractors.net<br>(770) 990-4885</p></div>")
        try:
            from email_sender import create_gmail_draft
            res = create_gmail_draft(email, subject, html)
            if not res.get("success"):
                out["errors"].append(f"{oid}: {res.get('error')}")
                continue
        except Exception as e:
            out["errors"].append(f"{oid} draft: {e}")
            continue
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO order_events
                            (order_id, event_type, event_data, source)
                        VALUES (%s, 'payment_nudge_drafted', %s,
                                'payment_nudges')
                    """, (str(oid), json.dumps({
                        "subject": subject, "to": email,
                        "draft_id": res.get("draft_id") or res.get("id"),
                        "total": float(total)})))
                    conn.commit()
        except Exception as e:
            out["errors"].append(f"{oid} event: {e}")
        out["drafted"].append(str(oid))
    return out
