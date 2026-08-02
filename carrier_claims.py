"""
carrier_claims.py — Wave 3 build J (William 2026-08-02): CARRIER CLAIM
PACKET + THE 9-MONTH CLOCK.

Industry law: LTL claims must be filed within 9 months of delivery, and
late/limp filings are the #1 denial reason. Claims land in our Claims
tab (replacement_requests) but nothing filed with the carrier or
watched the clock — now:

  - POST /claims/requests/{id}/carrier-claim builds the claim packet as
    a Gmail DRAFT to the carrier's claims box (PRO, delivery date, short/
    damaged lines, amount, the customer's photos attached) and opens a
    carrier_claims row with deadline = delivered + 270 days. DRAFT-FIRST:
    William reviews and sends the filing.
  - The clock check rides the ledger cycle: deadline minus 30 days with
    the claim still unfiled -> alarm; filed with no movement for 30 days
    -> follow-up nudge. Each alarm fires once (event-deduped).

RL_CLAIMS_EMAIL env = the carrier claims box (default
cargoclaims@rlcarriers.com — CONFIRM with R+L before the first real
filing; in the test lane the allowlist redirects it anyway).
"""

import json
import os
from typing import Dict, Optional

from db_helpers import get_db, get_order_by_id

INTERNAL_ALERT_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "orders@cabinetsforcontractors.com").strip()
RL_CLAIMS_EMAIL = os.environ.get("RL_CLAIMS_EMAIL",
                                 "cargoclaims@rlcarriers.com").strip()

CLAIM_STATUSES = ("draft", "filed", "acknowledged", "paid", "denied", "closed")
MAX_PHOTOS = 6


def ensure_carrier_claims_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS carrier_claims (
                id SERIAL PRIMARY KEY,
                order_id VARCHAR(20) NOT NULL,
                request_id INTEGER,
                carrier VARCHAR(30) DEFAULT 'R+L',
                pro_number VARCHAR(40),
                amount NUMERIC(10,2),
                delivered_at DATE,
                deadline DATE,
                status VARCHAR(20) DEFAULT 'draft',
                draft_id TEXT,
                note TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                filed_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        conn.commit()


def _shipment_facts(conn, order_id: str) -> Dict:
    """PRO + delivered date, best effort across orders/order_shipments."""
    facts = {"pro": None, "delivered": None}
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(tracking, pro_number)
                       FROM orders WHERE order_id = %s""", (order_id,))
        r = cur.fetchone()
        if r and r[0]:
            facts["pro"] = r[0]
        try:
            cur.execute("""SELECT pro_number, delivered_at FROM order_shipments
                           WHERE order_id = %s
                           ORDER BY id DESC LIMIT 1""", (order_id,))
            r = cur.fetchone()
            if r:
                facts["pro"] = facts["pro"] or r[0]
                facts["delivered"] = r[1]
        except Exception:
            conn.rollback()
    return facts


def open_carrier_claim(request_id: int, amount: float,
                       pro_number: str = "", note: str = "") -> Dict:
    """Build the packet draft + open the claim row. Draft-first."""
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_carrier_claims_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT * FROM replacement_requests WHERE id = %s""",
                        (request_id,))
            req = cur.fetchone()
            if not req:
                return {"status": "error",
                        "message": f"replacement request {request_id} not found"}
            cur.execute("""SELECT filename, mime, content
                           FROM replacement_request_photos
                           WHERE request_id = %s LIMIT %s""",
                        (request_id, MAX_PHOTOS))
            photos = cur.fetchall()
        order_id = str(req["order_id"])
        facts = _shipment_facts(conn, order_id)

    pro = (pro_number or facts["pro"] or "").strip()
    delivered = facts["delivered"]
    order = get_order_by_id(order_id) or {}

    try:
        lines = req.get("lines")
        if isinstance(lines, str):
            lines = json.loads(lines)
    except Exception:
        lines = None
    lines_html = ""
    if lines:
        lines_html = "".join(
            f"<tr><td style='padding:3px 10px;'>{ln.get('qty', ln.get('quantity', ''))}</td>"
            f"<td style='padding:3px 10px;'>{ln.get('sku', '')}</td>"
            f"<td style='padding:3px 10px;'>{ln.get('issue', ln.get('issue_type', ''))}</td></tr>"
            for ln in lines)
        lines_html = (f"<table style='border-collapse:collapse;font-size:13px;'>"
                      f"<tr style='background:#f2f2f2;'><th style='padding:3px 10px;'>Qty</th>"
                      f"<th style='padding:3px 10px;'>Item</th>"
                      f"<th style='padding:3px 10px;'>Issue</th></tr>{lines_html}</table>")

    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
            f"<p>To the R+L Carriers Claims Department,</p>"
            f"<p>We are filing a freight claim on the following shipment:</p>"
            f"<p>PRO number: <strong>{pro or 'PRO UNKNOWN - FILL IN'}</strong><br>"
            f"Delivered: <strong>{delivered or 'DATE UNKNOWN - FILL IN'}</strong><br>"
            f"Consignee: {order.get('company_name') or order.get('customer_name') or ''}<br>"
            f"Claim amount: <strong>${float(amount):,.2f}</strong></p>"
            f"{lines_html}"
            f"<p>{(req.get('description') or '').strip()}</p>"
            f"<p>Photos of the affected freight are attached. Please confirm "
            f"receipt of this claim and the assigned claim number.</p>"
            f"<p>--<br>William Prince<br>Cabinets For Contractors<br>"
            f"(770) 990-4885</p></div>")

    attachments = [{"filename": p["filename"] or f"photo{i + 1}.jpg",
                    "content": bytes(p["content"]),
                    "mime": p["mime"] or "image/jpeg"}
                   for i, p in enumerate(photos) if p.get("content")]

    from email_sender import create_gmail_draft
    draft = create_gmail_draft(
        RL_CLAIMS_EMAIL,
        f"Freight claim - PRO {pro or '(fill in)'} - Cabinets For Contractors",
        html, attachments=attachments)
    if not draft.get("success"):
        return {"status": "error", "message": f"draft failed: {draft.get('error')}"}

    with get_db() as conn:
        ensure_carrier_claims_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO carrier_claims
                    (order_id, request_id, pro_number, amount, delivered_at,
                     deadline, draft_id, note)
                VALUES (%s, %s, %s, %s, %s,
                        CASE WHEN %s::date IS NOT NULL
                             THEN %s::date + INTERVAL '270 days' END,
                        %s, %s)
                RETURNING id, deadline
            """, (order_id, request_id, pro or None, amount,
                  delivered, delivered, delivered,
                  draft.get("draft_id"), note or None))
            cid, deadline = cur.fetchone()
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'carrier_claim_opened', %s, 'carrier_claims')
            """, (order_id, json.dumps(
                {"claim_id": cid, "request_id": request_id, "pro": pro,
                 "amount": amount, "deadline": str(deadline) if deadline else None,
                 "draft_id": draft.get("draft_id"),
                 "photos_attached": len(attachments)}, default=str)))
            conn.commit()

    from supplier_orders import _send_email
    _send_email(order_id, INTERNAL_ALERT_EMAIL,
                f"CARRIER CLAIM DRAFT READY - order #{order_id} "
                f"(${float(amount):,.2f})",
                f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                f"<p>The freight-claim packet for order #{order_id} is waiting "
                f"in Gmail drafts (claim #{cid}, {len(attachments)} photos "
                f"attached, to {RL_CLAIMS_EMAIL}). Review and send — the "
                f"9-month clock {'ends ' + str(deadline) if deadline else 'has no delivery date yet: FILL IT IN'}."
                f"</p></div>",
                triggered_by="carrier_claim_opened")
    return {"status": "ok", "claim_id": cid, "draft_id": draft.get("draft_id"),
            "to": RL_CLAIMS_EMAIL, "pro": pro or None,
            "deadline": str(deadline) if deadline else None,
            "photos_attached": len(attachments)}


def set_claim_status(claim_id: int, status: str, note: str = "") -> Dict:
    if status not in CLAIM_STATUSES:
        return {"status": "error",
                "message": f"status must be one of {CLAIM_STATUSES}"}
    with get_db() as conn:
        ensure_carrier_claims_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE carrier_claims SET
                    status = %s,
                    note = COALESCE(NULLIF(%s, ''), note),
                    filed_at = CASE WHEN %s = 'filed' AND filed_at IS NULL
                                    THEN NOW() ELSE filed_at END,
                    updated_at = NOW()
                WHERE id = %s RETURNING order_id
            """, (status, note, status, claim_id))
            row = cur.fetchone()
            if not row:
                return {"status": "error", "message": f"claim {claim_id} not found"}
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'carrier_claim_status', %s, 'carrier_claims')
            """, (row[0], json.dumps({"claim_id": claim_id, "status": status,
                                      "note": note or None})))
            conn.commit()
    return {"status": "ok", "claim_id": claim_id, "now": status}


def list_carrier_claims() -> Dict:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_carrier_claims_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT *,
                                  (deadline - CURRENT_DATE) AS days_to_deadline
                           FROM carrier_claims
                           ORDER BY created_at DESC LIMIT 200""")
            rows = cur.fetchall()
    return {"status": "ok", "count": len(rows), "claims": rows}


def _clock_alert_once(conn, order_id: str, key: str) -> bool:
    """True when this alarm key has NOT fired yet (and records it)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM order_events
                       WHERE event_type = 'carrier_claim_clock'
                         AND event_data::text ILIKE %s LIMIT 1""",
                    (f"%{key}%",))
        if cur.fetchone():
            return False
        cur.execute("""
            INSERT INTO order_events (order_id, event_type, event_data, source)
            VALUES (%s, 'carrier_claim_clock', %s, 'carrier_claims')
        """, (order_id, json.dumps({"key": key})))
        conn.commit()
    return True


def check_claim_clocks() -> Dict:
    """Rides the ledger cycle. DB-only unless an alarm is due; every alarm
    fires exactly once per condition."""
    out = {"checked": 0, "alarms": 0, "errors": []}
    from psycopg2.extras import RealDictCursor
    try:
        with get_db() as conn:
            ensure_carrier_claims_table(conn)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""SELECT * FROM carrier_claims
                               WHERE status NOT IN ('paid', 'denied', 'closed')""")
                claims = cur.fetchall()
            from supplier_orders import _send_email
            for c in claims:
                out["checked"] += 1
                cid, oid = c["id"], str(c["order_id"])
                # unfiled and inside 30 days of the deadline
                if c["status"] == "draft" and c["deadline"]:
                    days = (c["deadline"] - __import__("datetime").date.today()).days
                    if days <= 30 and _clock_alert_once(conn, oid,
                                                        f"deadline30:{cid}"):
                        _send_email(oid, INTERNAL_ALERT_EMAIL,
                                    f"CLAIM DEADLINE - {days} DAYS LEFT - "
                                    f"order #{oid} claim #{cid}",
                                    f"<p>The freight claim for order #{oid} is "
                                    f"STILL A DRAFT and the 9-month filing "
                                    f"deadline is {c['deadline']} — "
                                    f"{days} days away. Send the filing.</p>",
                                    triggered_by="carrier_claim_clock")
                        out["alarms"] += 1
                # filed but silent for 30+ days
                if c["status"] in ("filed", "acknowledged") and c["filed_at"]:
                    import datetime as _dt
                    age = (_dt.datetime.now(_dt.timezone.utc)
                           - c["updated_at"]).days
                    if age >= 30:
                        bucket = age // 30
                        if _clock_alert_once(conn, oid,
                                             f"followup{bucket}:{cid}"):
                            _send_email(oid, INTERNAL_ALERT_EMAIL,
                                        f"CLAIM FOLLOW-UP DUE - order #{oid} "
                                        f"claim #{cid} quiet {age} days",
                                        f"<p>Freight claim #{cid} "
                                        f"(order #{oid}, "
                                        f"${float(c['amount'] or 0):,.2f}) has "
                                        f"had no movement for {age} days — "
                                        f"nudge R+L claims.</p>",
                                        triggered_by="carrier_claim_clock")
                            out["alarms"] += 1
    except Exception as e:
        out["errors"].append(str(e))
    return out
