"""
rl_delivered.py — R+L DELIVERED notice -> customer delivered-email DRAFT
(William 2026-07-28: delivered emails YES, draft-first; "look at rl delivered
emails that rl has sent and use as a template").

R+L's Shipment Tracing notification is both the trigger AND the data source:
    sender   WebNotificationSVC.PRD@rlcarriers.com
    subject  "R+L Carriers PRO {PRO} has been Delivered"
    body     "This shipment was delivered on time on MM/DD/YYYY" plus a
             REFERENCE NUMBERS table whose MPO row carries OUR order number
             (proof: PRO IAH3136257 -> MPO 5697).

Flow (rides the ledger cycle right after ingest — the read-once guarantee):
  new inbox ledger rows from rlcarriers with a Delivered subject
    -> match the order two ways: PRO vs orders/shipments pro_number, and the
       MPO number from the body. Both found and DISAGREEING -> NEEDS-A-HUMAN
       alert, nothing stamped, no draft (a mislabeled BOL would otherwise
       email the wrong customer).
    -> stamp shipments.delivered_at only-if-empty + an rl_delivered event
    -> render the delivery_confirmation template into a Gmail DRAFT to the
       customer (DRAFT-FIRST: William reviews and sends) + a
       "DELIVERED DRAFT READY" internal alert.
  Idempotent per Gmail message id via the rl_delivered order_event.
"""

import json
import re
from typing import Dict

from db_helpers import get_db, get_order_by_id

_SUBJ_RE = re.compile(r"PRO\s+([A-Z]{0,3}\d{6,12})\s+has been Delivered",
                      re.IGNORECASE)
_DATE_RE = re.compile(r"delivered(?:\s+on\s+time)?\s+on\s+(\d{2}/\d{2}/\d{4})",
                      re.IGNORECASE)

INTERNAL_ALERT = "orders@cabinetsforcontractors.com"


def _mpo_from_body(body: str):
    """Our order number from R+L's REFERENCE NUMBERS table (MPO row)."""
    i = (body or "").upper().find("MPO")
    if i < 0:
        return None
    m = re.search(r"(\d{4,5})", body[i:i + 500])
    return m.group(1) if m else None


def _orders_for_pro(conn, pro: str):
    hits = set()
    with conn.cursor() as cur:
        cur.execute("""SELECT order_id FROM orders
                       WHERE pro_number = %s OR tracking ILIKE %s""",
                    (pro, f"%{pro}%"))
        hits.update(str(r[0]) for r in cur.fetchall())
        cur.execute("SELECT order_id FROM order_shipments WHERE pro_number = %s",
                    (pro,))
        hits.update(str(r[0]) for r in cur.fetchall())
    return sorted(hits)


def _already_handled(conn, message_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM order_events
                       WHERE event_type IN ('rl_delivered',
                                            'rl_delivered_mismatch')
                         AND event_data::text LIKE %s LIMIT 1""",
                    (f"%{message_id}%",))
        return bool(cur.fetchone())


def _alert(order_id, subject: str, html: str):
    try:
        from supplier_orders import _send_email
        _send_email(str(order_id or ""), INTERNAL_ALERT, subject, html,
                    triggered_by="rl_delivered")
    except Exception as e:
        print(f"[RL-DELIVERED] alert failed: {e}")


def process_rl_delivered(hours_back: int = 48, dry_run: bool = False) -> Dict:
    """Scan recent ledger rows for R+L Delivered notices and act on the
    unhandled ones. dry_run reports decisions without stamping/drafting."""
    out = {"status": "ok", "checked": 0, "drafted": [], "mismatched": [],
           "unmatched": [], "already": 0, "errors": []}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT message_id, subject FROM email_ledger
                WHERE folder = 'inbox'
                  AND from_addr ILIKE '%%rlcarriers%%'
                  AND subject ILIKE '%%has been Delivered%%'
                  AND email_date > NOW() - make_interval(hours => %s)
            """, (int(hours_back),))
            rows = cur.fetchall()

        for mid, subject in rows:
            out["checked"] += 1
            try:
                m = _SUBJ_RE.search(subject or "")
                if not m:
                    continue
                pro = m.group(1).upper()
                if _already_handled(conn, mid):
                    out["already"] += 1
                    continue

                from email_ledger import _fetch_message
                email = _fetch_message(mid) or {}
                body = email.get("body") or ""
                mpo = _mpo_from_body(body)
                dm = _DATE_RE.search(body)
                delivered_on = dm.group(1) if dm else ""

                pro_orders = _orders_for_pro(conn, pro)
                # decide the order
                order_id = None
                if mpo and pro_orders and mpo not in pro_orders:
                    out["mismatched"].append(
                        {"message_id": mid, "pro": pro, "mpo": mpo,
                         "pro_orders": pro_orders})
                    if not dry_run:
                        with conn.cursor() as cur:
                            cur.execute("""INSERT INTO order_events
                                (order_id, event_type, event_data, source)
                                VALUES (%s, 'rl_delivered_mismatch', %s,
                                        'rl_delivered')""",
                                (pro_orders[0], json.dumps(
                                    {"message_id": mid, "pro": pro,
                                     "mpo": mpo,
                                     "pro_orders": pro_orders})))
                            conn.commit()
                        _alert(
                            pro_orders[0],
                            f"R+L DELIVERED NEEDS A HUMAN - PRO {pro}",
                            f"<p>R+L says PRO <strong>{pro}</strong> was "
                            f"delivered on {delivered_on or '?'} — but their "
                            f"reference table says our order is "
                            f"<strong>{mpo}</strong> while our records tie "
                            f"the PRO to <strong>{', '.join(pro_orders)}"
                            f"</strong>.</p><p>Someone needs to rule which "
                            f"order this shipment belongs to before a "
                            f"delivered email goes anywhere.</p>")
                    continue
                order_id = (pro_orders[0] if pro_orders else mpo)
                if not order_id or not get_order_by_id(order_id):
                    out["unmatched"].append({"message_id": mid, "pro": pro,
                                             "mpo": mpo})
                    if not dry_run:
                        _alert(
                            "",
                            f"R+L DELIVERED NEEDS A HUMAN - PRO {pro}",
                            f"<p>R+L reports PRO <strong>{pro}</strong> "
                            f"delivered ({delivered_on or 'date unknown'}) "
                            f"but no order matches it (MPO on the notice: "
                            f"{mpo or 'none'}).</p>")
                    continue

                if dry_run:
                    out["drafted"].append({"order_id": order_id, "pro": pro,
                                           "dry_run": True})
                    continue

                # stamp delivered_at only-if-empty on the matching shipment
                with conn.cursor() as cur:
                    cur.execute("""UPDATE order_shipments
                                   SET delivered_at = NOW()
                                   WHERE order_id = %s AND delivered_at IS NULL
                                     AND (pro_number = %s
                                          OR pro_number IS NULL)""",
                                (order_id, pro))
                    cur.execute("""INSERT INTO order_events
                        (order_id, event_type, event_data, source)
                        VALUES (%s, 'rl_delivered', %s, 'rl_delivered')""",
                        (order_id, json.dumps(
                            {"message_id": mid, "pro": pro, "mpo": mpo,
                             "delivered_on": delivered_on})))
                    conn.commit()

                # the customer draft (DRAFT-FIRST — never sends)
                order = get_order_by_id(order_id) or {}
                order["order_id"] = order_id
                order["pro_number"] = pro
                from email_templates import render_template, \
                    get_template_subject
                html = render_template("delivery_confirmation", order)
                subj = get_template_subject("delivery_confirmation", order)
                to_email = (order.get("email") or "").strip()
                draft_id = None
                if html and to_email:
                    # the pick list rides the delivered email too (William
                    # 2026-07-29: "ref the pick list and ask that they
                    # download it and check off each item")
                    attachments = []
                    try:
                        from picklist_pdf import generate_picklist_pdf
                        pk = generate_picklist_pdf(order)
                        if pk:
                            attachments.append(
                                {"filename": f"CFC-Picklist-{order_id}.pdf",
                                 "content": pk, "mime": "application/pdf"})
                    except Exception as e:
                        print(f"[RL-DELIVERED] picklist failed {order_id}: {e}")
                    from email_sender import create_gmail_draft
                    res = create_gmail_draft(to_email, subj, html,
                                             attachments=attachments)
                    draft_id = res.get("draft_id")
                _alert(
                    order_id,
                    f"DELIVERED DRAFT READY - order #{order_id} "
                    f"(R+L PRO {pro})",
                    f"<p>R+L delivered order <strong>#{order_id}</strong> "
                    f"on {delivered_on or 'today'} (PRO {pro}"
                    + (f", their MPO ref {mpo}" if mpo else "") + ").</p>"
                    f"<p>The customer delivered-confirmation is waiting in "
                    f"Gmail drafts — review and send.</p>"
                    if draft_id else
                    f"<p>R+L delivered order #{order_id} (PRO {pro}) but "
                    f"the draft could not be created — draft it by hand.</p>")
                out["drafted"].append({"order_id": order_id, "pro": pro,
                                       "draft_id": draft_id})
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"{mid}: {e}")
    return out
