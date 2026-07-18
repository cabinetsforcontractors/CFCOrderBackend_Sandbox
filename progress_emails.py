"""
progress_emails.py
CUSTOMER PROGRESS EMAILS (William spec 2026-07-18; windows BLESSED + tightened
2026-07-18 after the root-cause audit).

Customer touches, ALL created as GMAIL DRAFTS for William to review and send
(draft-first law — nothing customer-facing sends itself):

  1. POST-PAYMENT — arrival window in real dates, supplier-based.
  2. DELAY — promised ship date blown with no tracking: re-promise from today.
  3. TRACKING — tracking/PRO captured: numbers + CLICK-TO-FOLLOW carrier links.
  4. DELIVERY DAY — R+L ShipmentTracing poll (morning, once per day per
     shipment): when the PRO shows estimated delivery TODAY or out-for-
     delivery, draft "your order is scheduled to be delivered today"
     (+ inspect-before-signing note). Delivered -> recorded quietly.
     UPS delivery-day needs a UPS API account (not available yet).

TRACKING TRUTH REPAIR (William 2026-07-18): the DB missed tracking that went
out in hand-written emails. Two mechanisms:
  - POST /progress/backfill-tracking — one-time sweep of SENT "TRACKING INFO"
    emails (default 90 days): stamps orders.tracking/pro_number ONLY when the
    fields are empty, and marks the promise row complete (customer already got
    tracking by hand — the robot must NOT draft a second tracking email).
  - The same guard runs continuously inside the sweep for the last 48h of
    sent mail, so future hand-sent tracking emails keep the DB honest.
    Orders already stamped are skipped entirely (idempotent, no event spam).

Ship windows are SUPPLIER-BASED (SUPPLIER_DELAY_ROOT_CAUSE_AUDIT_20260718.md).
Language law: NEVER "in production" — post-payment voice is "sent to the
warehouse". Runs as a sweep on every gmail-sync cycle (hooked in
estimate_verifier.scan_replies) + manual POST /progress/run [admin].

INCIDENT LESSON (2026-07-18): the email-detection scanner reads MAILBOX
CONTENT for PRO patterns — never put real order ids with fake tracking
numbers into any email/draft; examples belong in chat, not in the mailbox.
"""

import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends

from auth import require_admin
from db_helpers import get_db

progress_router = APIRouter(tags=["progress-emails"])

# BLESSED ship windows in BUSINESS DAYS (paid -> carrier pickup), per supplier
# (William tightened 2026-07-18 PM: LM 2-3 per their stated 48h, ROC 1-2,
# GHI 2-4)
SHIP_WINDOWS = {
    "ROC": (1, 2),
    "GHI": (2, 4),
    "Love-Milestone": (2, 3),
}
DEFAULT_SHIP_WINDOW = (3, 8)   # unmeasured suppliers — provisional, conservative
TRANSIT_UPS = (1, 3)
TRANSIT_LTL = (2, 5)
LTL_FLOOR = (2, 5)             # freight orders never promise faster than this
LTL_WEIGHT_LB = 150.0
LTL_TOTAL_USD = 1500.0

SIGNATURE = ("--\nWilliam Prince\nCabinets For Contractors\n"
             "www.CabinetsForContractors.net\n(770) 990-4885")

RL_TRACE_URL = ("https://www2.rlcarriers.com/freight/shipping/shipment-tracing"
                "?pro={pro}&docType=PRO&source=web")
UPS_TRACK_URL = "https://www.ups.com/track?tracknum={num}"

# delivery-day poll: once per ET day per shipment, mornings (>= 10:00 UTC)
DELIVERY_POLL_START_UTC = 10
DELIVERY_POLL_MAX_AGE_DAYS = 45   # stop polling ancient shipments


# =============================================================================
# BUSINESS-DAY MATH (weekends + federal holidays via alerts_engine when loaded)
# =============================================================================

def _is_bd(d: date) -> bool:
    try:
        from alerts_engine import _is_business_day
        return _is_business_day(d)
    except Exception:
        return d.weekday() < 5


def biz_add(start: date, days: int) -> date:
    d = start
    left = days
    guard = 0
    while left > 0 and guard < 90:
        d += timedelta(days=1)
        guard += 1
        if _is_bd(d):
            left -= 1
    return d


def _nice(d: date) -> str:
    return d.strftime("%A, %B ") + str(d.day)


# =============================================================================
# WINDOW COMPUTATION
# =============================================================================

def order_suppliers(conn, order_id: str) -> List[str]:
    """Suppliers on the order: line-item warehouse column first, prefix map
    fallback (order_line_items columns: sku, sku_prefix, warehouse)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT COALESCE(NULLIF(oli.warehouse, ''), wm.warehouse_name)
            FROM order_line_items oli
            LEFT JOIN warehouse_mapping wm
              ON UPPER(oli.sku_prefix) = UPPER(wm.sku_prefix)
            WHERE oli.order_id = %s
        """, (order_id,))
        return sorted({r[0] for r in cur.fetchall() if r[0]})


def compute_window(conn, order: Dict) -> Dict:
    suppliers = order_suppliers(conn, order["order_id"])
    lo = hi = 0
    for s in (suppliers or ["?"]):
        w = SHIP_WINDOWS.get(s, DEFAULT_SHIP_WINDOW)
        lo, hi = max(lo, w[0]), max(hi, w[1])
    if not suppliers:
        lo, hi = DEFAULT_SHIP_WINDOW
    weight = float(order.get("total_weight") or 0)
    total = float(order.get("order_total") or 0)
    is_ltl = weight >= LTL_WEIGHT_LB or total >= LTL_TOTAL_USD
    if is_ltl:
        lo, hi = max(lo, LTL_FLOOR[0]), max(hi, LTL_FLOOR[1])
        t_lo, t_hi = TRANSIT_LTL
    else:
        t_lo, t_hi = TRANSIT_UPS
    paid = order.get("payment_received_at")
    start = paid.date() if hasattr(paid, "date") else (
        datetime.fromisoformat(str(paid)).date() if paid else date.today())
    return {
        "suppliers": suppliers, "method": "LTL" if is_ltl else "UPS",
        "ship_lo": lo, "ship_hi": hi, "transit_lo": t_lo, "transit_hi": t_hi,
        "ship_by": biz_add(start, hi),
        "arrive_min": biz_add(start, lo + t_lo),
        "arrive_max": biz_add(start, hi + t_hi),
    }


# =============================================================================
# TABLE
# =============================================================================

def ensure_progress_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS progress_promises (
                order_id VARCHAR(20) PRIMARY KEY,
                suppliers TEXT,
                method VARCHAR(10),
                ship_by DATE,
                arrive_min DATE,
                arrive_max DATE,
                post_payment_at TIMESTAMP WITH TIME ZONE,
                delay_at TIMESTAMP WITH TIME ZONE,
                tracking_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        for col, typ in (("delivery_notice_at", "TIMESTAMP WITH TIME ZONE"),
                         ("delivered_at", "TIMESTAMP WITH TIME ZONE"),
                         ("last_poll_date", "DATE")):
            try:
                cur.execute(f"ALTER TABLE progress_promises "
                            f"ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                conn.rollback()
        conn.commit()


# =============================================================================
# DRAFT CREATION (customer drafts NEVER auto-send — William reviews)
# =============================================================================

def _first_name(order: Dict) -> str:
    name = (order.get("customer_name") or "").strip()
    first = name.split(" ")[0] if name else ""
    return first if first else "there"


def _make_draft(to_email: str, subject: str, body: str) -> Optional[str]:
    import base64
    from email.mime.text import MIMEText
    from ghi_inbox import _gmail_post

    mime = MIMEText(body)
    mime["To"] = to_email
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    res = _gmail_post("drafts", {"message": {"raw": raw}})
    return res.get("id") if res else None


def _notify(order_id: str, kind: str, body: str):
    try:
        from supplier_orders import _send_email, INTERNAL_ALERT_EMAIL
        _send_email(order_id, INTERNAL_ALERT_EMAIL,
                    f"PROGRESS DRAFT READY - {kind} - order #{order_id}",
                    f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                    f"<p>A customer progress draft is waiting in Gmail drafts - "
                    f"review and send.</p>"
                    f"<pre style='background:#f5f5f5;padding:12px;"
                    f"white-space:pre-wrap;'>{body}</pre></div>",
                    triggered_by="progress_email")
    except Exception as e:
        print(f"[PROGRESS] notify failed: {e}")


def _post_payment_body(order: Dict, w: Dict) -> str:
    return (
        f"Hey {_first_name(order)},\n\n"
        f"Thank you for your payment on order #{order['order_id']}! Your order "
        f"has been sent to the warehouse and we will update you as it "
        f"progresses.\n\n"
        f"Generally it takes {w['ship_lo']}-{w['ship_hi']} business days for "
        f"the warehouse to pull and pack your order, and "
        f"{w['transit_lo']}-{w['transit_hi']} business days in transit. You "
        f"can expect your order to arrive between {_nice(w['arrive_min'])} and "
        f"{_nice(w['arrive_max'])}.\n\n"
        f"We will send your tracking information as soon as the carrier picks "
        f"it up.\n\nAny questions, just reply.\n\n{SIGNATURE}")


def _delay_body(order: Dict, w: Dict) -> str:
    return (
        f"Hey {_first_name(order)},\n\n"
        f"A quick update on order #{order['order_id']} - there has been a "
        f"delay pulling your order at the warehouse. Your new expected "
        f"arrival is between {_nice(w['arrive_min'])} and "
        f"{_nice(w['arrive_max'])}.\n\n"
        f"We are on it and will send your tracking the moment it ships. "
        f"Sorry for the wait.\n\n{SIGNATURE}")


def _tracking_body(order: Dict) -> str:
    """One clean block per shipment number, each with its carrier's live
    tracking link (William 2026-07-18: no duplicate lines, include the link,
    'click here to follow')."""
    pro = (order.get("pro_number") or "").strip()
    trk = (order.get("tracking") or "").strip()
    lines = []
    if pro:
        lines.append(f"R+L Carriers PRO #: {pro}")
        lines.append(f"Click here to follow your delivery any time:")
        lines.append(RL_TRACE_URL.format(pro=pro))
    ups_nums = re.findall(r"\b(1Z[0-9A-Z]{10,16})\b", trk.upper())
    for u in ups_nums:
        if lines:
            lines.append("")
        lines.append(f"UPS Tracking #: {u}")
        lines.append(f"Click here to follow your delivery any time:")
        lines.append(UPS_TRACK_URL.format(num=u))
    if not pro and not ups_nums and trk:
        lines.append(f"Tracking: {trk}")
    nums = "\n".join(lines)
    return (
        f"Hey {_first_name(order)},\n\n"
        f"Your order #{order['order_id']} is on the way!\n\n"
        f"{nums}\n\n"
        f"One note: tracking will not show any movement until the carrier "
        f"scans the shipment at pickup - if it looks empty for a day, that is "
        f"normal.\n\n{SIGNATURE}")


def _delivery_today_body(order: Dict) -> str:
    return (
        f"Hey {_first_name(order)},\n\n"
        f"Good news - your order #{order['order_id']} is scheduled to be "
        f"delivered TODAY.\n\n"
        f"When it arrives, please look over the pallet BEFORE signing the "
        f"delivery receipt and note any visible damage on the receipt - that "
        f"protects you if anything needs a claim.\n\n"
        f"Any questions, just reply.\n\n{SIGNATURE}")


# =============================================================================
# R+L SHIPMENT TRACING (delivery-day layer)
# =============================================================================

def _rl_trace(pro: str) -> Optional[Dict]:
    """First shipment record from R+L ShipmentTracing, or None."""
    import os
    key = os.environ.get("RL_CARRIERS_API_KEY", "")
    if not key or not pro:
        return None
    url = ("https://api.rlc.com/ShipmentTracing?"
           + urllib.parse.urlencode({"TraceNumbers": pro, "TraceType": "PRO"}))
    try:
        req = urllib.request.Request(url)
        req.add_header("apiKey", key)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode(errors="replace"))
        ships = data.get("Shipments") or []
        return ships[0] if ships else None
    except Exception as e:
        print(f"[PROGRESS] RL trace failed for {pro}: {e}")
        return None


def run_delivery_poll(out: Dict):
    """Morning poll (once per ET day per shipment): R+L PRO -> delivered
    (recorded quietly) or estimated-delivery-today / out-for-delivery ->
    'scheduled to be delivered TODAY' customer draft."""
    from psycopg2.extras import RealDictCursor

    now = datetime.now(timezone.utc)
    if now.hour < DELIVERY_POLL_START_UTC:
        return
    today = date.today()
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT o.*, p.last_poll_date FROM progress_promises p
                JOIN orders o ON o.order_id = p.order_id
                WHERE p.tracking_at IS NOT NULL
                  AND p.delivered_at IS NULL
                  AND p.delivery_notice_at IS NULL
                  AND (p.last_poll_date IS NULL OR p.last_poll_date < %s)
                  AND p.created_at > NOW() - (%s || ' days')::interval
                  AND o.pro_number IS NOT NULL AND o.pro_number <> ''
                LIMIT 25
            """, (today, DELIVERY_POLL_MAX_AGE_DAYS))
            candidates = cur.fetchall()

        for o in candidates:
            try:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE progress_promises SET last_poll_date = %s
                                   WHERE order_id = %s""", (today, o["order_id"]))
                    conn.commit()
                ship = _rl_trace(o["pro_number"])
                if not ship:
                    continue
                short = (ship.get("ShortStatus") or "").lower()
                est = (ship.get("EstimatedDelivery") or "").strip()
                est_today = False
                try:
                    est_today = (datetime.strptime(est, "%m/%d/%Y").date()
                                 == today) if est else False
                except ValueError:
                    pass
                if "delivered" in short:
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE progress_promises
                                       SET delivered_at = NOW()
                                       WHERE order_id = %s""", (o["order_id"],))
                        cur.execute("""
                            INSERT INTO order_events
                                (order_id, event_type, event_data, source)
                            VALUES (%s, 'customer_delivery_confirmed', %s,
                                    'progress_emails')
                        """, (o["order_id"], json.dumps({
                            "pro": o["pro_number"],
                            "delivery": ship.get("DeliveryDate"),
                            "status": ship.get("LongStatus")})))
                        conn.commit()
                    out["delivered"].append(o["order_id"])
                elif est_today or "out for delivery" in short:
                    body = _delivery_today_body(o)
                    draft_id = _make_draft(
                        o["email"],
                        f"Order #{o['order_id']} - out for delivery today",
                        body)
                    if draft_id:
                        with conn.cursor() as cur:
                            cur.execute("""UPDATE progress_promises
                                           SET delivery_notice_at = NOW()
                                           WHERE order_id = %s""",
                                        (o["order_id"],))
                            conn.commit()
                        _notify(o["order_id"], "delivery-today", body)
                        out["delivery_today"].append(o["order_id"])
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"delivery poll {o.get('order_id')}: {e}")


# =============================================================================
# TRACKING TRUTH: hand-sent "TRACKING INFO" emails -> stamp the DB
# =============================================================================

_PRO_RE = re.compile(r"PRO\s*(?:#|Number)?[:\s]*([A-Z]{0,2}\d{8,10}(?:-\d)?)",
                     re.IGNORECASE)
_UPS_RE = re.compile(r"\b(1Z[0-9A-Z]{10,16})\b")
_ORDER_RE = re.compile(r"#\s?(\d{4})\b")


def stamp_manual_tracking(hours_back: int = 48, dry_run: bool = False) -> Dict:
    """Scan SENT 'TRACKING INFO' emails; stamp orders.tracking/pro_number ONLY
    when empty, and mark the promise row's tracking stage complete (the
    customer already got tracking by hand — no robot tracking draft).
    Orders that already carry tracking are skipped (idempotent)."""
    from gmail_sync import gmail_configured, search_emails
    from ghi_inbox import _fetch_text

    out = {"status": "ok", "checked": 0, "stamped": [], "skipped": [],
           "errors": [], "dry_run": dry_run}
    if not gmail_configured():
        out["status"] = "skipped"
        return out
    try:
        msgs = search_emails(
            f'newer_than:{int(hours_back)}h in:sent subject:"TRACKING INFO"', 50)
    except Exception as e:
        return {"status": "error", "errors": [f"search: {e}"]}
    with get_db() as conn:
        ensure_progress_table(conn)
        for m in msgs:
            try:
                text, subject, _sender = _fetch_text(m["id"])
                out["checked"] += 1
                om = _ORDER_RE.search(subject)
                if not om:
                    continue
                oid = om.group(1)
                pros = _PRO_RE.findall(text)
                ups = _UPS_RE.findall(text.upper())
                if not pros and not ups:
                    out["skipped"].append(f"{oid}: no numbers parsed")
                    continue
                pro = pros[0] if pros else None
                trk = " ".join(ups) if ups else (f"R+L PRO {pro}" if pro else "")
                with conn.cursor() as cur:
                    cur.execute("""SELECT tracking, pro_number FROM orders
                                   WHERE order_id = %s""", (oid,))
                    row = cur.fetchone()
                    if not row:
                        out["skipped"].append(f"{oid}: no order row")
                        continue
                    cur_trk, cur_pro = row
                    if (cur_trk or "").strip() or (cur_pro or "").strip():
                        out["skipped"].append(f"{oid}: already stamped")
                        continue
                    item = {"order_id": oid, "pro": pro, "ups": ups}
                    if dry_run:
                        out["stamped"].append(item)
                        continue
                    cur.execute("""UPDATE orders SET tracking = %s,
                                   updated_at = NOW() WHERE order_id = %s""",
                                (trk, oid))
                    if pro:
                        cur.execute("""UPDATE orders SET pro_number = %s,
                                       updated_at = NOW() WHERE order_id = %s""",
                                    (pro, oid))
                    # hand-sent tracking = stage complete; keep delivery poll
                    # alive by NOT touching delivered/delivery_notice fields
                    cur.execute("""
                        INSERT INTO progress_promises
                            (order_id, suppliers, post_payment_at, tracking_at)
                        VALUES (%s, 'manual-tracking', NOW(), NOW())
                        ON CONFLICT (order_id) DO UPDATE
                        SET tracking_at = COALESCE(progress_promises.tracking_at,
                                                   NOW())
                    """, (oid,))
                    cur.execute("""
                        INSERT INTO order_events
                            (order_id, event_type, event_data, source)
                        VALUES (%s, 'manual_tracking_stamped', %s,
                                'progress_emails')
                    """, (oid, json.dumps({"message_id": m["id"],
                                           "pro": pro, "ups": ups})))
                    conn.commit()
                    out["stamped"].append(item)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"{m.get('id')}: {e}")
    return out


# =============================================================================
# THE SWEEP (rides every gmail-sync cycle; idempotent per stage per order)
# =============================================================================

def run_progress_sweep(dry_run: bool = False, days_back: int = 7) -> Dict:
    from psycopg2.extras import RealDictCursor

    out = {"status": "ok", "post_payment": [], "delay": [], "tracking": [],
           "delivery_today": [], "delivered": [], "dry_run": dry_run,
           "errors": []}

    # 0) keep the DB honest about hand-sent tracking BEFORE drafting anything
    try:
        guard = stamp_manual_tracking(hours_back=48, dry_run=dry_run)
        if guard.get("stamped"):
            out["manual_tracking_stamped"] = guard["stamped"]
    except Exception as e:
        out["errors"].append(f"manual tracking guard: {e}")

    with get_db() as conn:
        ensure_progress_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # A) POST-PAYMENT: recently paid, no promise row, NOT already
            #    shipped (pre-system orders with tracking were handled by hand)
            cur.execute("""
                SELECT o.* FROM orders o
                LEFT JOIN progress_promises p ON p.order_id = o.order_id
                WHERE o.payment_received = TRUE
                  AND o.payment_received_at > NOW() - (%s || ' days')::interval
                  AND o.email IS NOT NULL AND o.email <> ''
                  AND (o.tracking IS NULL OR o.tracking = '')
                  AND (o.pro_number IS NULL OR o.pro_number = '')
                  AND p.order_id IS NULL
                ORDER BY o.payment_received_at
            """, (int(days_back),))
            new_paid = cur.fetchall()
            # B) DELAY: promised ship date blown, nothing captured, no delay yet
            cur.execute("""
                SELECT o.*, p.ship_by FROM progress_promises p
                JOIN orders o ON o.order_id = p.order_id
                WHERE p.post_payment_at IS NOT NULL
                  AND p.delay_at IS NULL
                  AND p.tracking_at IS NULL
                  AND p.ship_by < CURRENT_DATE
                  AND (o.tracking IS NULL OR o.tracking = '')
                  AND (o.pro_number IS NULL OR o.pro_number = '')
            """)
            late = cur.fetchall()
            # C) TRACKING: tracking/PRO captured, tracking draft not yet made
            cur.execute("""
                SELECT o.* FROM progress_promises p
                JOIN orders o ON o.order_id = p.order_id
                WHERE p.tracking_at IS NULL
                  AND ((o.tracking IS NOT NULL AND o.tracking <> '')
                       OR (o.pro_number IS NOT NULL AND o.pro_number <> ''))
            """)
            shipped = cur.fetchall()

        for o in new_paid:
            try:
                w = compute_window(conn, o)
                item = {"order_id": o["order_id"], "email": o["email"],
                        "suppliers": w["suppliers"], "method": w["method"],
                        "arrive": f"{w['arrive_min']}..{w['arrive_max']}"}
                if not dry_run:
                    body = _post_payment_body(o, w)
                    draft_id = _make_draft(
                        o["email"],
                        f"Order #{o['order_id']} - payment received, "
                        f"here's what happens next",
                        body)
                    if not draft_id:
                        raise RuntimeError("draft create failed")
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO progress_promises
                                (order_id, suppliers, method, ship_by,
                                 arrive_min, arrive_max, post_payment_at)
                            VALUES (%s, %s, %s, %s, %s, %s, NOW())
                            ON CONFLICT (order_id) DO NOTHING
                        """, (o["order_id"], ",".join(w["suppliers"]),
                              w["method"], w["ship_by"], w["arrive_min"],
                              w["arrive_max"]))
                        conn.commit()
                    _notify(o["order_id"], "post-payment", body)
                    item["draft_id"] = draft_id
                out["post_payment"].append(item)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"post-payment {o.get('order_id')}: {e}")

        for o in late:
            try:
                w = compute_window(conn, o)
                # re-promise from TODAY, not the original payment date
                today = date.today()
                w["arrive_min"] = biz_add(today, w["ship_lo"] + w["transit_lo"])
                w["arrive_max"] = biz_add(today, w["ship_hi"] + w["transit_hi"])
                item = {"order_id": o["order_id"],
                        "new_arrive": f"{w['arrive_min']}..{w['arrive_max']}"}
                if not dry_run:
                    body = _delay_body(o, w)
                    draft_id = _make_draft(
                        o["email"],
                        f"Order #{o['order_id']} - a quick update", body)
                    if not draft_id:
                        raise RuntimeError("draft create failed")
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE progress_promises
                            SET delay_at = NOW(), arrive_min = %s,
                                arrive_max = %s
                            WHERE order_id = %s
                        """, (w["arrive_min"], w["arrive_max"], o["order_id"]))
                        conn.commit()
                    _notify(o["order_id"], "delay", body)
                    item["draft_id"] = draft_id
                out["delay"].append(item)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"delay {o.get('order_id')}: {e}")

        for o in shipped:
            try:
                item = {"order_id": o["order_id"],
                        "tracking": o.get("tracking") or o.get("pro_number")}
                if not dry_run:
                    body = _tracking_body(o)
                    draft_id = _make_draft(
                        o["email"],
                        f"Order #{o['order_id']} has shipped - tracking inside",
                        body)
                    if not draft_id:
                        raise RuntimeError("draft create failed")
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE progress_promises SET tracking_at = NOW()
                                       WHERE order_id = %s""", (o["order_id"],))
                        conn.commit()
                    _notify(o["order_id"], "tracking", body)
                    item["draft_id"] = draft_id
                out["tracking"].append(item)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"tracking {o.get('order_id')}: {e}")

    # 4) delivery-day layer (R+L only; UPS needs a UPS API account)
    if not dry_run:
        try:
            run_delivery_poll(out)
        except Exception as e:
            out["errors"].append(f"delivery poll: {e}")

    return out


# =============================================================================
# ENDPOINTS
# =============================================================================

@progress_router.post("/progress/run")
def progress_run(dry_run: bool = False, days_back: int = 7,
                 _: bool = Depends(require_admin)):
    return run_progress_sweep(dry_run=dry_run, days_back=days_back)


@progress_router.post("/progress/backfill-tracking")
def progress_backfill(days_back: int = 90, dry_run: bool = True,
                      _: bool = Depends(require_admin)):
    """One-time Gmail truth repair: stamp tracking from historical hand-sent
    TRACKING INFO emails (only-if-empty; marks stage complete, no drafts)."""
    return stamp_manual_tracking(hours_back=days_back * 24, dry_run=dry_run)


@progress_router.get("/progress")
def progress_list(_: bool = Depends(require_admin)):
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_progress_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT * FROM progress_promises
                           ORDER BY created_at DESC LIMIT 100""")
            rows = cur.fetchall()
    return {"status": "ok", "promises": [dict(r) for r in rows]}


@progress_router.post("/progress/{order_id}/mark")
def progress_mark(order_id: str, _: bool = Depends(require_admin)):
    """Silence an order's progress emails (manual-era orders): upserts the
    promise row with tracking_at set, so no further drafts are ever made."""
    with get_db() as conn:
        ensure_progress_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO progress_promises
                    (order_id, suppliers, post_payment_at, tracking_at)
                VALUES (%s, 'manual-skip', NOW(), NOW())
                ON CONFLICT (order_id) DO UPDATE
                SET tracking_at = NOW(),
                    post_payment_at = COALESCE(progress_promises.post_payment_at,
                                               NOW())
            """, (order_id,))
            conn.commit()
    return {"status": "ok", "order_id": order_id, "silenced": True}


@progress_router.post("/progress/{order_id}/reset-tracking")
def progress_reset_tracking(order_id: str, rearm: bool = True,
                            clear_fields: bool = False,
                            _: bool = Depends(require_admin)):
    """Undo a bogus tracking capture. clear_fields wipes orders.tracking +
    pro_number; rearm nulls the promise row's tracking_at so the REAL tracking
    email can draft later. Leave rearm=false while the poisoned content is
    still in the mailbox — the detection scanner could re-stamp it."""
    with get_db() as conn:
        ensure_progress_table(conn)
        with conn.cursor() as cur:
            if clear_fields:
                cur.execute("""UPDATE orders SET tracking = NULL,
                               pro_number = NULL, updated_at = NOW()
                               WHERE order_id = %s""", (order_id,))
            if rearm:
                cur.execute("""UPDATE progress_promises SET tracking_at = NULL
                               WHERE order_id = %s""", (order_id,))
            conn.commit()
    return {"status": "ok", "order_id": order_id, "cleared_fields": clear_fields,
            "rearmed": rearm}


@progress_router.post("/progress/{order_id}/redo-post-payment")
def progress_redo_post_payment(order_id: str, _: bool = Depends(require_admin)):
    """Delete the promise row so the next sweep re-drafts post-payment with
    the CURRENT windows (used after William re-blesses window numbers).
    Delete the outdated Gmail draft after the new one appears."""
    with get_db() as conn:
        ensure_progress_table(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM progress_promises WHERE order_id = %s",
                        (order_id,))
            deleted = cur.rowcount
            conn.commit()
    return {"status": "ok", "order_id": order_id, "row_deleted": bool(deleted)}
