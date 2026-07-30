"""
fire_log.py — THE FIRING-ORDER LAW (William's ruling 2026-07-30).

The systemic issue this fixes: the machine fires (quote, BOL, stamp,
invoice...), overwrites row state, and the previous truth disappears.
936 events on order 5731 and the day's real BOL wasn't one of them.

THE LAW:
  1. NO FIRE WITHOUT A FULL RECORD — every action writes an append-only
     order_events row whose event_data carries the COMPLETE result
     (amounts, numbers, addresses, who, why). Never a bare marker.
  2. DIFF-ON-WRITE — the writer looks up the previous fire of the same
     kind on the same order and records what changed INSIDE the new
     event (_fire.changes: was -> now per field). A second quote/stamp/
     BOL can never silently replace the first.
  3. Row columns (orders.tracking, payment_amount...) stay as
     latest-value convenience copies only. History lives here and is
     never edited or deleted.

Every event carries _fire = {seq, prev_at, changes}:
  seq      = how many times this kind has fired on this order
  prev_at  = timestamp of the previous fire of this kind (None if first)
  changes  = {"changed": {field: {"was":..,"now":..}}, "dropped": [...]}
             or "no-change" or None (first fire)

QUOTE-NUMBER LAW (2026-07-30): last_quote_number(order_id) surfaces the
most recent R+L quote number known for an order — the BOL must carry it
so the quoted price HOLDS ("if we send a bol out of nowhere the price
may change").

Doors:
  POST /fire-log/record/{order_id}?kind=&source=   [admin]
       body = the full payload dict. Manual recording / history backfill.
  GET  /orders/{order_id}/fires?kind=&limit=       [admin]
       Clean fire history — noise types (b2bwave_sync) excluded,
       newest first, diffs visible.
"""

import json
import re
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Body
from psycopg2.extras import RealDictCursor

from auth import require_admin
from db_helpers import get_db

# Event types that are machine heartbeat, not fires — excluded from the
# /fires view so the real story is readable (5731 lesson: 900+ sync rows).
NOISE_EVENT_TYPES = ("b2bwave_sync",)

fire_log_router = APIRouter(tags=["fire-log"])


# =============================================================================
# CORE WRITER
# =============================================================================

def _clean(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Comparable copy of a payload: no _fire/_meta keys."""
    return {k: v for k, v in (payload or {}).items() if not k.startswith("_")}


def _diff(prev: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    changed = {}
    for k, v in new.items():
        if k not in prev:
            changed[k] = {"was": None, "now": v}
        elif prev[k] != v:
            changed[k] = {"was": prev[k], "now": v}
    dropped = [k for k in prev if k not in new]
    return {"changed": changed, "dropped": dropped}


def record_fire(order_id: str, kind: str, payload: Dict[str, Any],
                source: str = "api") -> Dict[str, Any]:
    """THE writer. Append the full payload as an order_events row with
    diff-vs-previous embedded. Returns {"seq", "changes"}."""
    payload = _clean(payload)
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT event_data, created_at FROM order_events
                   WHERE order_id = %s AND event_type = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (order_id, kind))
            prev_row = cur.fetchone()

            fire = {"seq": 1, "prev_at": None, "changes": None}
            if prev_row:
                prev_data = prev_row.get("event_data") or {}
                if isinstance(prev_data, str):
                    try:
                        prev_data = json.loads(prev_data)
                    except Exception:
                        prev_data = {}
                prev_fire = prev_data.get("_fire") or {}
                try:
                    fire["seq"] = int(prev_fire.get("seq") or 1) + 1
                except Exception:
                    fire["seq"] = 2
                created = prev_row.get("created_at")
                fire["prev_at"] = created.isoformat() if created else None
                d = _diff(_clean(prev_data), payload)
                fire["changes"] = d if (d["changed"] or d["dropped"]) else "no-change"

            data = dict(payload)
            data["_fire"] = fire
            cur.execute(
                """INSERT INTO order_events
                   (order_id, event_type, event_data, source)
                   VALUES (%s, %s, %s, %s)""",
                (order_id, kind, json.dumps(data, default=str), source))
        conn.commit()
    return {"seq": fire["seq"], "changes": fire["changes"]}


# =============================================================================
# QUOTE-NUMBER LAW
# =============================================================================

def last_quote_number(order_id: str) -> Optional[str]:
    """The most recent R+L quote number known for an order. Sources,
    newest first: freight_quoted fires (quote_numbers per leg), then
    orders.rl_quote_no. The BOL builder attaches this so the quoted
    price HOLDS (William 2026-07-30)."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT event_data FROM order_events
                   WHERE order_id = %s AND event_type = 'freight_quoted'
                   ORDER BY created_at DESC LIMIT 5""", (order_id,))
            for row in cur.fetchall():
                d = row.get("event_data")
                if isinstance(d, str):
                    try:
                        d = json.loads(d)
                    except Exception:
                        continue
                qns = (d or {}).get("quote_numbers") or {}
                for v in qns.values():
                    if v:
                        return str(v)
            cur.execute("SELECT rl_quote_no FROM orders WHERE order_id = %s",
                        (order_id,))
            row = cur.fetchone()
            if row and row.get("rl_quote_no"):
                return str(row["rl_quote_no"])
    return None


# =============================================================================
# BOL FIRE (wired from shipping_routes /rl/bol)
# =============================================================================

def record_bol_fire(request_dict: Dict[str, Any], result: Dict[str, Any],
                    source: str = "rl_bol_door") -> Optional[Dict[str, Any]]:
    """Record an rl_bol_created fire keyed off the BOL's po_number and
    keep orders.tracking as the latest-value convenience copy (previous
    tracking is preserved inside the event payload)."""
    po = str(request_dict.get("po_number") or "")
    m = re.search(r"\d{4,6}", po)
    if not m:
        return None
    order_id = m.group(0)

    prev_tracking = None
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tracking FROM orders WHERE order_id = %s",
                        (order_id,))
            row = cur.fetchone()
            if not row:
                return None  # BOL for something we don't track — no fire
            prev_tracking = row[0]

    pro = (result or {}).get("pro_number")
    payload = {
        "pro_number": pro,
        "pickup_request_id": (result or {}).get("pickup_request_id"),
        "po_number": po,
        "quote_number": request_dict.get("quote_number") or None,
        "shipper": f"{request_dict.get('shipper_name')} | "
                   f"{request_dict.get('shipper_address')}, "
                   f"{request_dict.get('shipper_city')} "
                   f"{request_dict.get('shipper_state')} "
                   f"{request_dict.get('shipper_zip')}",
        "consignee": f"{request_dict.get('consignee_name')} | "
                     f"{request_dict.get('consignee_address')}, "
                     f"{request_dict.get('consignee_city')} "
                     f"{request_dict.get('consignee_state')} "
                     f"{request_dict.get('consignee_zip')}",
        "weight_lbs": request_dict.get("weight_lbs"),
        "pieces": request_dict.get("pieces"),
        "freight_class": request_dict.get("freight_class"),
        "description": request_dict.get("description"),
        "include_pickup": request_dict.get("include_pickup"),
        "pickup_date": request_dict.get("pickup_date"),
        "pickup_window": f"{request_dict.get('pickup_ready_time')}-"
                         f"{request_dict.get('pickup_close_time')}",
        "prev_tracking": prev_tracking,
    }
    out = record_fire(order_id, "rl_bol_created", payload, source)

    if pro:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE orders SET tracking = %s, updated_at = NOW()
                       WHERE order_id = %s""",
                    (f"R+L Carriers PRO {pro}", order_id))
            conn.commit()
    out["order_id"] = order_id
    return out


# =============================================================================
# DOORS
# =============================================================================

@fire_log_router.post("/fire-log/record/{order_id}")
def fire_log_record(order_id: str, kind: str, source: str = "manual",
                    payload: Dict[str, Any] = Body(...),
                    _: bool = Depends(require_admin)):
    """Manually record a fire (or backfill history that pre-dates the
    law). [admin] Body = the full payload dict."""
    if not kind or not kind.strip():
        raise HTTPException(status_code=400, detail="kind is required")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM orders WHERE order_id = %s", (order_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Order not found")
    out = record_fire(order_id, kind.strip(), payload, source)
    return {"status": "ok", "order_id": order_id, "kind": kind.strip(), **out}


@fire_log_router.get("/orders/{order_id}/fires")
def order_fires(order_id: str, kind: Optional[str] = None, limit: int = 50,
                _: bool = Depends(require_admin)):
    """Clean fire history for an order — noise excluded, newest first,
    diffs visible. [admin]"""
    limit = max(1, min(int(limit or 50), 500))
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if kind:
                cur.execute(
                    """SELECT event_id, event_type, event_data, source,
                              created_at
                       FROM order_events
                       WHERE order_id = %s AND event_type = %s
                       ORDER BY created_at DESC LIMIT %s""",
                    (order_id, kind, limit))
            else:
                cur.execute(
                    """SELECT event_id, event_type, event_data, source,
                              created_at
                       FROM order_events
                       WHERE order_id = %s
                         AND NOT (event_type = ANY(%s))
                       ORDER BY created_at DESC LIMIT %s""",
                    (order_id, list(NOISE_EVENT_TYPES), limit))
            rows = [dict(r) for r in cur.fetchall()]
    return {"status": "ok", "order_id": order_id, "count": len(rows),
            "noise_excluded": (None if kind else list(NOISE_EVENT_TYPES)),
            "fires": rows}
