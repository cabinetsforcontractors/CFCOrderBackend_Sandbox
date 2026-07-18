"""
progress_emails.py
CUSTOMER PROGRESS EMAILS (William spec 2026-07-18; windows BLESSED 2026-07-18).

Three customer touches, ALL created as GMAIL DRAFTS for William to review and
send (draft-first law — nothing customer-facing sends itself):

  1. POST-PAYMENT — right after payment lands: "sent to the warehouse, we'll
     update you as it progresses" + honest arrival window in real dates.
  2. DELAY — expected ship date blown with no tracking captured: apologize,
     re-promise from today.
  3. TRACKING — tracking/PRO captured: numbers + the mandatory note that
     tracking shows nothing until the carrier scans the pickup.

Ship windows are SUPPLIER-BASED, from the 2026-07-18 email root-cause audit
(SUPPLIER_DELAY_ROOT_CAUSE_AUDIT_20260718.md — blessed as-is): the historical
3-10 day swings were mostly OUR lag (late approvals, unanswered supplier
questions, blind pickup windows), which the platform now automates/watches, so
promises are keyed to each supplier's demonstrated speed. Language law: NEVER
"in production" — post-payment voice is "sent to the warehouse".

Runs as a sweep on every gmail-sync cycle (hooked in estimate_verifier.
scan_replies) + manual POST /progress/run [admin]. One draft per stage per
order (progress_promises table). Pickup orders may still get a draft — the
draft-first review is the filter (William edits/deletes; detection heuristics
would guess).
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends

from auth import require_admin
from db_helpers import get_db

progress_router = APIRouter(tags=["progress-emails"])

# BLESSED ship windows in BUSINESS DAYS (paid -> carrier pickup), per supplier
SHIP_WINDOWS = {
    "ROC": (1, 3),
    "GHI": (2, 5),
    "Love-Milestone": (2, 5),
}
DEFAULT_SHIP_WINDOW = (3, 8)   # unmeasured suppliers — provisional, conservative
TRANSIT_UPS = (1, 3)
TRANSIT_LTL = (2, 5)
LTL_FLOOR = (2, 5)             # freight orders never promise faster than this
LTL_WEIGHT_LB = 150.0
LTL_TOTAL_USD = 1500.0

SIGNATURE = ("--\nWilliam Prince\nCabinets For Contractors\n"
             "www.CabinetsForContractors.net\n(770) 990-4885")


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
    return d.strftime("%A, %B %-d") if hasattr(d, "strftime") and False else \
        d.strftime("%A, %B ") + str(d.day)


# =============================================================================
# WINDOW COMPUTATION
# =============================================================================

def order_suppliers(conn, order_id: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT wm.warehouse_name
            FROM order_line_items oli
            JOIN warehouse_mapping wm
              ON UPPER(SPLIT_PART(oli.product_sku, '-', 1)) = UPPER(wm.sku_prefix)
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
    lines = []
    if order.get("tracking"):
        lines.append(f"Tracking: {order['tracking']}")
    if order.get("pro_number"):
        lines.append(f"R+L Carriers PRO #: {order['pro_number']}")
    nums = "\n".join(lines) or "Tracking: (see below)"
    return (
        f"Hey {_first_name(order)},\n\n"
        f"Your order #{order['order_id']} is on the way!\n\n"
        f"{nums}\n\n"
        f"One note: tracking will not show any movement until the carrier "
        f"scans the shipment at pickup - if it looks empty for a day, that is "
        f"normal.\n\n{SIGNATURE}")


# =============================================================================
# THE SWEEP (rides every gmail-sync cycle; idempotent per stage per order)
# =============================================================================

def run_progress_sweep(dry_run: bool = False, days_back: int = 7) -> Dict:
    from psycopg2.extras import RealDictCursor

    out = {"status": "ok", "post_payment": [], "delay": [], "tracking": [],
           "dry_run": dry_run, "errors": []}
    with get_db() as conn:
        ensure_progress_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # A) POST-PAYMENT: recently paid, no promise row yet
            cur.execute("""
                SELECT o.* FROM orders o
                LEFT JOIN progress_promises p ON p.order_id = o.order_id
                WHERE o.payment_received = TRUE
                  AND o.payment_received_at > NOW() - (%s || ' days')::interval
                  AND o.email IS NOT NULL AND o.email <> ''
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
                out["errors"].append(f"tracking {o.get('order_id')}: {e}")

    return out


# =============================================================================
# ENDPOINTS
# =============================================================================

@progress_router.post("/progress/run")
def progress_run(dry_run: bool = False, days_back: int = 7,
                 _: bool = Depends(require_admin)):
    return run_progress_sweep(dry_run=dry_run, days_back=days_back)


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
