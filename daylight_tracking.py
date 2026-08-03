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
        # STALL LAYER columns (William 2026-08-02): in-transit polls every
        # 4 business hours; a status unchanged 8 business hours = stalled
        cur.execute("""ALTER TABLE daylight_shipments
                       ADD COLUMN IF NOT EXISTS last_poll_at TIMESTAMPTZ""")
        cur.execute("""ALTER TABLE daylight_shipments
                       ADD COLUMN IF NOT EXISTS last_status_change_at
                       TIMESTAMPTZ""")
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
    today = date.today()

    with get_db() as conn:
        ensure_daylight_shipments(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM daylight_shipments
                WHERE active = TRUE
                  AND delivered_at IS NULL
                  AND registered_at > NOW() - (%s || ' days')::interval
                LIMIT 25
            """, (POLL_MAX_AGE_DAYS,))
            rows = cur.fetchall()

        for s in rows:
            oid, pro = s["order_id"], s["probill"]
            try:
                # CADENCE (William 2026-08-02): pre-pickup = once per UTC
                # day, mornings (unchanged). IN TRANSIT (picked up, not
                # delivered) = every 4 BUSINESS hours, so the 8-hour stall
                # clock has teeth.
                if not force:
                    if s.get("picked_up_at"):
                        from oos_detect import business_hours_between
                        if s.get("last_poll_at") and business_hours_between(
                                s["last_poll_at"]) < 4:
                            continue
                    else:
                        if now.hour < POLL_START_UTC:
                            continue
                        if s.get("last_poll_date") == today:
                            continue
                with conn.cursor() as cur:
                    cur.execute("""UPDATE daylight_shipments
                                   SET last_poll_date = %s, last_poll_at = NOW()
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
                                       SET picked_up_at = NOW(),
                                           last_status_change_at = NOW()
                                       WHERE id = %s""",
                                    (s["id"],))
                        _event(conn, oid, "daylight_picked_up",
                               {"probill": pro, "status": status[:200]})
                    elif status_changed:
                        cur.execute("""UPDATE daylight_shipments
                                       SET last_status_change_at = NOW()
                                       WHERE id = %s""", (s["id"],))
                        _event(conn, oid, "daylight_status_update",
                               {"probill": pro, "status": status[:200]})
                    conn.commit()

                # STALL ALARM (William 2026-08-02, same law as R+L): status
                # unchanged 8 BUSINESS hours while in transit -> one alarm
                # per stuck-state + the ask (Daylight CS box, else William)
                try:
                    if (s.get("picked_up_at") and not status_changed
                            and s.get("last_status_change_at")):
                        from oos_detect import business_hours_between
                        stall_h = business_hours_between(
                            s["last_status_change_at"])
                        if stall_h >= 8:
                            import hashlib as _hl
                            fp = _hl.sha1((status or "").encode()) \
                                .hexdigest()[:12]
                            with conn.cursor() as cur:
                                cur.execute(
                                    """SELECT 1 FROM order_events
                                       WHERE event_type = 'daylight_stall_alerted'
                                         AND event_data::text ILIKE %s
                                       LIMIT 1""", (f"%{pro}:{fp}%",))
                                seen = cur.fetchone()
                            if not seen:
                                _event(conn, oid, "daylight_stall_alerted",
                                       {"key": f"{pro}:{fp}",
                                        "stall_hours": stall_h,
                                        "status": (status or "")[:160]})
                                conn.commit()
                                import os as _os
                                from supplier_orders import _send_email
                                alert_to = _os.environ.get(
                                    "WAREHOUSE_NOTIFICATION_EMAIL",
                                    "orders@cabinetsforcontractors.com").strip()
                                _send_email(
                                    oid, alert_to,
                                    f"DAYLIGHT SHIPMENT STALLED - order "
                                    f"#{oid} - no movement "
                                    f"{int(stall_h)} business hours",
                                    f"<p>Daylight PRO {pro} (order #{oid}) "
                                    f"has shown the same status for "
                                    f"{int(stall_h)} business hours: "
                                    f"'{(status or '')[:160]}'. The robot "
                                    f"is asking Daylight.</p>",
                                    triggered_by="daylight_stall")
                                cs = _os.environ.get(
                                    "DAYLIGHT_CS_EMAIL", "").strip()
                                if cs:
                                    _send_email(
                                        oid, cs,
                                        f"PRO {pro} - shipment status "
                                        f"inquiry",
                                        f"<p>Hey There,</p><p>Our shipment "
                                        f"PRO <strong>{pro}</strong> has "
                                        f"shown no movement for "
                                        f"{int(stall_h)} business hours. "
                                        f"Can you tell us the holdup and "
                                        f"when it will move?</p>"
                                        f"<p>Thank you,<br>--<br>William "
                                        f"Prince<br>Cabinets For "
                                        f"Contractors<br>(770) 990-4885</p>",
                                        triggered_by="daylight_stall_ask")
                except Exception as _e:
                    print(f"[DAYLIGHT] stall check failed {oid}: {_e}")

                low = (status or "").lower()
                if "delivered" in low:
                    with conn.cursor() as cur:
                        # WATCH-HEAL (8/3, the 5695 lesson): a delivered
                        # shipment must also DEACTIVATE — active=true kept
                        # regenerating its shipment-watch card forever.
                        cur.execute("""UPDATE daylight_shipments
                                       SET delivered_at = NOW(),
                                           active = FALSE
                                       WHERE id = %s""",
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
