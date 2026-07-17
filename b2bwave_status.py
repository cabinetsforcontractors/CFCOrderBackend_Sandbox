"""
b2bwave_status.py
Status-driven lifecycle — checkpoint writes to B2BWave (William ruling 2026-07-17).

WHY: nothing used to update B2BWave when an order progressed, so every order
sat "Submitted" forever — stale orders polluted reports, and the lifecycle
engine had to guess progress from Gmail silence (which nearly auto-canceled
5 PAID orders in the 7/17 dry run). Now the backend writes progress to the
store at each checkpoint, the store is the customer-truth SOT, and the
day-21 track only ever applies to orders still waiting on payment.

Checkpoint map (live-store status ids, GET /api/status_orders proven 7/16):
    payment link sent            -> 3 Awaiting Payment
    payment received             -> 4 Being Prepared
    ALL warehouse legs picked up -> 5 Sent
    ALL warehouse legs delivered -> 6 Complete
    day-21 UNPAID only           -> 7 Canceled (lifecycle_engine, unchanged)

Rules:
  - LADDER 2->3->4->5->6: never downgrade; a repeat checkpoint is a no-op.
  - Never touch orders at 1 Temporary (a cart), 7 Canceled, or 8 Invoiced.
  - Sent/Complete use the ALL-legs rule (William: "mark Sent when all have —
    any other way may fail silently"); canceled legs are excluded from ALL.
  - Every write is guarded by B2BWAVE_MUTATIONS_ENABLED and verified by
    readback (Accept-header silent-success trap).
  - notify flag is not sent; change_status is silent on this store (proven).
"""

import json
import os
from typing import Dict, List, Optional

from db_helpers import get_db

# Live-store status ids (overridable per-store via env)
STATUS_TEMPORARY = int(os.environ.get("B2BWAVE_TEMPORARY_STATUS_ID", "1"))
STATUS_SUBMITTED = int(os.environ.get("B2BWAVE_SUBMITTED_STATUS_ID", "2"))
STATUS_AWAITING_PAYMENT = int(os.environ.get("B2BWAVE_AWAITING_PAYMENT_STATUS_ID", "3"))
STATUS_BEING_PREPARED = int(os.environ.get("B2BWAVE_BEING_PREPARED_STATUS_ID", "4"))
STATUS_SENT = int(os.environ.get("B2BWAVE_SENT_STATUS_ID", "5"))
STATUS_COMPLETE = int(os.environ.get("B2BWAVE_COMPLETE_STATUS_ID", "6"))
STATUS_CANCELED = int(os.environ.get("B2BWAVE_CANCELED_STATUS_ID", "7"))
STATUS_INVOICED = int(os.environ.get("B2BWAVE_INVOICED_STATUS_ID", "8"))

# progression order — a write only ever moves RIGHT along this ladder
_LADDER = [STATUS_SUBMITTED, STATUS_AWAITING_PAYMENT, STATUS_BEING_PREPARED,
           STATUS_SENT, STATUS_COMPLETE]
_TERMINAL = {STATUS_CANCELED, STATUS_INVOICED}

STATUS_NAMES = {
    STATUS_TEMPORARY: "Temporary", STATUS_SUBMITTED: "Submitted",
    STATUS_AWAITING_PAYMENT: "Awaiting Payment",
    STATUS_BEING_PREPARED: "Being Prepared", STATUS_SENT: "Sent",
    STATUS_COMPLETE: "Complete", STATUS_CANCELED: "Canceled",
    STATUS_INVOICED: "Invoiced",
}

# supplier_orders leg statuses that satisfy each all-legs milestone
_LEG_PICKED_UP_PLUS = {"picked_up", "delivered", "invoice_verified"}
_LEG_DELIVERED_PLUS = {"delivered", "invoice_verified"}
_LEG_EXCLUDED = {"canceled"}  # canceled legs never block the ALL rule


def _rank(status_id: int) -> Optional[int]:
    try:
        return _LADDER.index(status_id)
    except ValueError:
        return None


def set_order_status(order_id: str, target_id: int, reason: str,
                     triggered_by: str = "b2bwave_status") -> Dict:
    """Move an order's B2BWave status UP the ladder to target_id.
    No-op (with explanation) when: mutations disabled, order not found,
    order at Temporary/Canceled/Invoiced, or already at/past target.
    Applied writes are readback-verified and logged to order_events."""
    from substitutions import _b2b, fetch_b2b_order

    out = {"applied": False, "order_id": str(order_id),
           "target": target_id, "target_name": STATUS_NAMES.get(target_id),
           "reason": reason}

    target_rank = _rank(target_id)
    if target_rank is None:
        out["skipped"] = f"target {target_id} is not on the progression ladder"
        return out

    order = fetch_b2b_order(order_id)
    if not order:
        out["skipped"] = "order not found on B2BWave"
        return out
    current = int(order.get("status_order_id") or 0)
    out["current"] = current
    out["current_name"] = STATUS_NAMES.get(current, str(current))

    if current in _TERMINAL:
        out["skipped"] = f"order is {out['current_name']} — terminal, never touched"
        return out
    if current == STATUS_TEMPORARY:
        out["skipped"] = "order is Temporary (cart) — not ours to progress"
        return out
    current_rank = _rank(current)
    if current_rank is not None and current_rank >= target_rank:
        out["skipped"] = f"already at/past target ({out['current_name']})"
        return out

    if os.environ.get("B2BWAVE_MUTATIONS_ENABLED", "true").lower() == "false":
        out["blocked"] = "B2BWAVE_MUTATIONS_ENABLED=false — would have applied"
        _log_event(order_id, "b2bwave_status_blocked", out, triggered_by)
        return out

    st, data = _b2b("PATCH", f"orders/{order_id}/change_status",
                    {"status_order_id": target_id})
    out["http"] = st
    check = fetch_b2b_order(order_id)
    new_status = int((check or {}).get("status_order_id") or 0)
    out["readback"] = new_status
    if new_status == target_id:
        out["applied"] = True
        print(f"[B2B-STATUS] order {order_id}: {out['current_name']} -> "
              f"{out['target_name']} ({reason})")
    else:
        out["error"] = (f"change_status did not verify (HTTP {st}, "
                        f"readback {new_status})")
    _log_event(order_id, "b2bwave_status_set", out, triggered_by)
    return out


def _log_event(order_id: str, event_type: str, data: Dict, source: str):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, %s, %s, %s)
                """, (str(order_id), event_type, json.dumps(data, default=str),
                      source))
                conn.commit()
    except Exception as e:
        print(f"[B2B-STATUS] event log failed: {e}")


# =============================================================================
# CHECKPOINT HOOKS (each guarded — a status write failing must never break
# the flow that triggered it)
# =============================================================================

def on_payment_link_sent(order_id: str) -> Dict:
    """Checkpoint: payment link went to the customer -> 3 Awaiting Payment."""
    try:
        return set_order_status(order_id, STATUS_AWAITING_PAYMENT,
                                "payment link sent", "checkpoint_link_sent")
    except Exception as e:
        return {"applied": False, "error": str(e)}


def on_payment_received(order_id: str) -> Dict:
    """Checkpoint: payment landed -> 4 Being Prepared."""
    try:
        return set_order_status(order_id, STATUS_BEING_PREPARED,
                                "payment received", "checkpoint_paid")
    except Exception as e:
        return {"applied": False, "error": str(e)}


def _order_leg_statuses(order_id: str) -> List[str]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT status FROM supplier_orders
                           WHERE order_id = %s""", (str(order_id),))
            return [r[0] for r in cur.fetchall()]


def progress_from_supplier_legs(order_id: str) -> Dict:
    """Checkpoint: a warehouse leg advanced. Apply the ALL-legs rule
    (William 2026-07-17: Sent/Complete only when EVERY leg is there —
    partial marking lets a stuck warehouse fail silently).
    Canceled legs are excluded; zero countable legs = no-op."""
    try:
        legs = [s for s in _order_leg_statuses(order_id)
                if s not in _LEG_EXCLUDED]
        if not legs:
            return {"applied": False, "skipped": "no active supplier legs"}
        if all(s in _LEG_DELIVERED_PLUS for s in legs):
            return set_order_status(order_id, STATUS_COMPLETE,
                                    f"all {len(legs)} legs delivered",
                                    "checkpoint_all_delivered")
        if all(s in _LEG_PICKED_UP_PLUS for s in legs):
            return set_order_status(order_id, STATUS_SENT,
                                    f"all {len(legs)} legs picked up",
                                    "checkpoint_all_picked_up")
        lagging = [s for s in legs if s not in _LEG_PICKED_UP_PLUS]
        return {"applied": False,
                "skipped": f"{len(lagging)}/{len(legs)} legs not picked up yet "
                           f"— order stays Being Prepared (all-legs rule)"}
    except Exception as e:
        return {"applied": False, "error": str(e)}


# =============================================================================
# BACKFILL — one-time cleanup of orders stuck at Submitted (dry_run default!)
# =============================================================================

def backfill_statuses(dry_run: bool = True, limit: int = 200) -> Dict:
    """Compute the ladder status every open order SHOULD be at from local
    checkpoints (payment_link_sent / payment_received / supplier legs) and —
    only when dry_run=false AND mutations enabled — write it to B2BWave.
    Never cancels, never downgrades, never touches Temporary/Canceled/Invoiced."""
    from psycopg2.extras import RealDictCursor
    from substitutions import fetch_b2b_order

    plan = []
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT order_id, customer_name, order_total,
                       payment_link_sent, payment_received
                FROM orders
                WHERE (is_complete = FALSE OR is_complete IS NULL)
                ORDER BY order_id DESC LIMIT %s
            """, (min(int(limit), 500),))
            orders = cur.fetchall()

    for o in orders:
        oid = o["order_id"]
        target = None
        why = None
        if o.get("payment_received"):
            target, why = STATUS_BEING_PREPARED, "paid"
            legs = [s for s in _order_leg_statuses(oid) if s not in _LEG_EXCLUDED]
            if legs and all(s in _LEG_DELIVERED_PLUS for s in legs):
                target, why = STATUS_COMPLETE, f"paid + all {len(legs)} legs delivered"
            elif legs and all(s in _LEG_PICKED_UP_PLUS for s in legs):
                target, why = STATUS_SENT, f"paid + all {len(legs)} legs picked up"
        elif o.get("payment_link_sent"):
            target, why = STATUS_AWAITING_PAYMENT, "payment link sent, unpaid"
        if target is None:
            continue

        b2b = fetch_b2b_order(oid)
        current = int((b2b or {}).get("status_order_id") or 0) if b2b else None
        entry = {"order_id": oid, "customer": o.get("customer_name"),
                 "total": float(o.get("order_total") or 0),
                 "current": STATUS_NAMES.get(current, current),
                 "target": STATUS_NAMES.get(target), "why": why}
        if b2b is None:
            entry["action"] = "skip — not found on B2BWave"
        elif current in _TERMINAL or current == STATUS_TEMPORARY:
            entry["action"] = f"skip — {STATUS_NAMES.get(current, current)}"
        elif (_rank(current) or 0) >= _rank(target) and _rank(current) is not None:
            entry["action"] = "skip — already at/past target"
        elif dry_run:
            entry["action"] = "WOULD SET"
        else:
            result = set_order_status(oid, target, f"backfill: {why}",
                                      "backfill")
            entry["action"] = ("SET" if result.get("applied") else
                               result.get("blocked") or result.get("error")
                               or result.get("skipped", "no-op"))
        plan.append(entry)

    changes = [p for p in plan if p["action"] in ("WOULD SET", "SET")]
    return {"status": "ok", "dry_run": dry_run,
            "open_orders_scanned": len(orders),
            "changes": len(changes), "plan": plan}
