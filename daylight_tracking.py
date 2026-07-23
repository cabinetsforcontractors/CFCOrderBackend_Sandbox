"""
daylight_tracking.py
Daylight order-integration STEP 2: probill registry + externalTrace delivery
poller. Mirrors the proven R+L delivery-day layer in progress_emails.py.

FLOW:
  1. Register the shipment's PRO (probill) once it's known:
         POST /daylight/probill/{order_id}?probill=...   [admin]
     The PRO is NOT the BOL number (BOL 5673183 was 7 digits; Daylight PROs
     are 8-10 digits, assigned at pickup - the BOL has a "place PRO label
     here" box). Registration stamps orders.tracking ("Daylight Transport
     PRO {n}") ONLY when tracking is empty, which arms the existing progress
     sweep to draft the customer tracking email (draft-first law - William
     reviews and sends). stamp_tracking=false registers without stamping
     (drills / hand-tracked orders).
  2. The poller rides every progress sweep (hooked in progress_emails, which
     rides every gmail-sync cycle): once per UTC day per shipment, mornings,
     it calls daylight.trace(probill) and:
       - "No results found"  -> pre-pickup, note only, keep polling
       - first real status   -> picked_up_at + order_event (quiet)
       - status text change  -> order_event 'daylight_status_update' (quiet)
       - out-for-delivery or estimated delivery TODAY -> customer
         "delivered TODAY" GMAIL DRAFT (progress_emails body) + notify, once
       - delivered           -> delivered_at + event, recorded quietly
  3. Nothing here sends customer email directly - drafts only.

The externalTrace response schema is undocumented, so parsing is defensive:
status-ish keys are collected by name, delivered/out-for-delivery are
substring checks on the status text, and the raw response rides along in
daylight_shipments.last_response for the human.
"""

import json
import re
from datetime import date, datetime, timezone

from psycopg2.extras import RealDictCursor

import daylight
from db_helpers import get_db

PROBILL_RE = re.compile(r"^\d{8,10}$")
POLL_START_UTC = 10          # mornings, matches the R+L delivery poll
POLL_MAX_AGE_DAYS = 45       # stop polling ancient shipments
LAST_RESPONSE_MAX = 4000     # chars of raw trace JSON kept on the row


def ensure_daylight_shipments(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daylight_shipments (
                id SERIAL PRIMARY KEY,
                order_id VARCHAR(20) NOT NULL,
                probill VARCHAR(12) NOT NULL UNIQUE,
                warehouse TEXT,
                status TEXT,
                last_response TEXT,
                registered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_poll_date DATE,
                picked_up_at TIMESTAMP WITH TIME ZONE,
                delivered_at TIMESTAMP WITH TIME ZONE,
                delivery_notice_at TIMESTAMP WITH TIME ZONE,
                active BOOLEAN DEFAULT TRUE
            )
        """)
        conn.commit()


def _event(conn, order_id, event_type, data):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO order_events (order_id, event_type, event_data, source)
            VALUES (%s, %s, %s, 'daylight_tracking')
        """, (order_id, event_type, json.dumps(data)))


def _status_text(resp):
    """Join every status-ish string in the (undocumented) trace response."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif isinstance(v, str) and "status" in str(k).lower():
                    found.append(v)
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(resp)
    return " | ".join(found)


def _est_delivery_today(resp, today):
    """True when any delivery/appointment date field parses to today."""
    hits = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                    continue
                kl = str(k).lower()
                if ("delivery" in kl or "appt" in kl) and isinstance(v, str):
                    hits.append(v.strip())
        elif isinstance(o, list):
            for it in o:
                walk(it)

    walk(resp)
    for h in hits:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                if datetime.strptime(h[:10], fmt).date() == today:
                    return True
            except ValueError:
                continue
    return False


def register_probill(order_id, probill, warehouse=None, stamp_tracking=True,
                     force_stamp=False):
    """Register a Daylight PRO for an order. Verifies by tracing (a not-found
    PRO is still accepted - it may not be scanned yet), stamps orders.tracking
    only-if-empty (arms the existing tracking-email draft), records an event."""
    probill = str(probill or "").strip()
    if not PROBILL_RE.match(probill):
        return {"status": "error",
                "message": f"probill '{probill}' invalid - Daylight PROs are "
                           "8-10 digits (the BOL number is NOT the PRO)"}

    trace_note = ""
    try:
        resp = daylight.trace(probill)
        inner = resp.get("externalTraceResp", resp) if isinstance(resp, dict) else {}
        trace_note = inner.get("message") or _status_text(resp) or "trace ok"
    except Exception as e:
        trace_note = f"trace failed: {str(e)[:120]}"

    with get_db() as conn:
        ensure_daylight_shipments(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT order_id, tracking FROM orders WHERE order_id = %s",
                        (order_id,))
            order = cur.fetchone()
        if not order:
            return {"status": "error", "message": f"order {order_id} not found"}

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daylight_shipments (order_id, probill, warehouse)
                VALUES (%s, %s, %s)
                ON CONFLICT (probill) DO NOTHING
                RETURNING id
            """, (order_id, probill, warehouse))
            row = cur.fetchone()
            if not row:
                conn.commit()
                return {"status": "already_registered", "order_id": order_id,
                        "probill": probill}

            stamped = False
            existing = (order.get("tracking") or "").strip()
            if stamp_tracking and (force_stamp or not existing):
                cur.execute("""UPDATE orders SET tracking = %s, updated_at = NOW()
                               WHERE order_id = %s""",
                            (f"Daylight Transport PRO {probill}", order_id))
                stamped = True

            _event(conn, order_id, "daylight_probill_registered",
                   {"probill": probill, "warehouse": warehouse,
                    "trace_note": trace_note[:200], "tracking_stamped": stamped})
            conn.commit()

    return {"status": "ok", "order_id": order_id, "probill": probill,
            "trace_note": trace_note[:200], "tracking_stamped": stamped,
            "note": ("tracking stamped - the progress sweep will draft the "
                     "customer tracking email (draft-first)" if stamped else
                     "tracking NOT stamped (already set or stamp_tracking=false)")}


def list_shipments():
    with get_db() as conn:
        ensure_daylight_shipments(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT id, order_id, probill, warehouse, status,
                                  registered_at, last_poll_date, picked_up_at,
                                  delivered_at, delivery_notice_at, active
                           FROM daylight_shipments
                           ORDER BY registered_at DESC LIMIT 100""")
            rows = cur.fetchall()
    return {"status": "ok", "shipments": [dict(r) for r in rows]}


def remove_shipment(probill):
    """Delete a registry row (drill cleanup / mis-entered PRO). Does NOT touch
    orders.tracking - clear that via /progress/{id}/reset-tracking if needed."""
    with get_db() as conn:
        ensure_daylight_shipments(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM daylight_shipments WHERE probill = %s",
                        (str(probill).strip(),))
            deleted = cur.rowcount
            conn.commit()
    return {"status": "ok", "probill": probill, "deleted": bool(deleted)}


def poll_daylight_shipments(out=None, force=False):
    """Once per UTC day per active shipment, mornings (matches the R+L poll).
    force=True ignores the morning/once-a-day gates (manual drills)."""
    if out is None:
        out = {}
    for k in ("daylight_polled", "delivery_today", "delivered", "errors"):
        out.setdefault(k, [])

    now = datetime.now(timezone.utc)
    if not force and now.hour < POLL_START_UTC:
        return out
    today = date.today()

    with get_db() as conn:
        ensure_daylight_shipments(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM daylight_shipments
                WHERE active = TRUE
                  AND delivered_at IS NULL
                  AND (%s OR last_poll_date IS NULL OR last_poll_date < %s)
                  AND registered_at > NOW() - (%s || ' days')::interval
                LIMIT 25
            """, (force, today, POLL_MAX_AGE_DAYS))
            rows = cur.fetchall()

        for s in rows:
            oid, pro = s["order_id"], s["probill"]
            try:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE daylight_shipments SET last_poll_date = %s
                                   WHERE id = %s""", (today, s["id"]))
                    conn.commit()

                resp = daylight.trace(pro)
                inner = resp.get("externalTraceResp", resp) if isinstance(resp, dict) else {}
                msg = (inner.get("message") or "") if isinstance(inner, dict) else ""
                status = _status_text(resp) or msg
                raw = json.dumps(resp)[:LAST_RESPONSE_MAX]
                out["daylight_polled"].append({"order_id": oid, "probill": pro,
                                               "status": status[:160]})

                if "no results found" in (msg or "").lower():
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE daylight_shipments
                                       SET status = %s, last_response = %s
                                       WHERE id = %s""",
                                    ("pre-pickup: not in Daylight system yet",
                                     raw, s["id"]))
                        conn.commit()
                    continue

                status_changed = (status or "") != (s.get("status") or "")
                with conn.cursor() as cur:
                    cur.execute("""UPDATE daylight_shipments
                                   SET status = %s, last_response = %s
                                   WHERE id = %s""", (status, raw, s["id"]))
                    if not s.get("picked_up_at"):
                        cur.execute("""UPDATE daylight_shipments
                                       SET picked_up_at = NOW() WHERE id = %s""",
                                    (s["id"],))
                        _event(conn, oid, "daylight_picked_up",
                               {"probill": pro, "status": status[:200]})
                    elif status_changed:
                        _event(conn, oid, "daylight_status_update",
                               {"probill": pro, "status": status[:200]})
                    conn.commit()

                low = (status or "").lower()
                if "delivered" in low:
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE daylight_shipments
                                       SET delivered_at = NOW() WHERE id = %s""",
                                    (s["id"],))
                        _event(conn, oid, "customer_delivery_confirmed",
                               {"probill": pro, "carrier": "Daylight",
                                "status": status[:200]})
                        conn.commit()
                    out["delivered"].append(oid)
                elif (not s.get("delivery_notice_at")
                      and ("out for delivery" in low
                           or _est_delivery_today(resp, today))):
                    from progress_emails import (_delivery_today_body,
                                                 _make_draft, _notify)
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute("SELECT * FROM orders WHERE order_id = %s",
                                    (oid,))
                        order = cur.fetchone()
                    if order and order.get("email"):
                        body = _delivery_today_body(order)
                        draft_id = _make_draft(
                            order["email"],
                            f"Order #{oid} - out for delivery today", body)
                        if draft_id:
                            with conn.cursor() as cur:
                                cur.execute("""UPDATE daylight_shipments
                                               SET delivery_notice_at = NOW()
                                               WHERE id = %s""", (s["id"],))
                                conn.commit()
                            _notify(oid, "delivery-today (Daylight)", body)
                            out["delivery_today"].append(oid)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"daylight poll {oid}/{pro}: {e}")

    return out
