"""
do_box.py — THE DO-BOX (William-ruled 2026-08-01).

LAW CHANGE, his words: "my thought process has evolved... when I state
what to do it fires the bol or invoice from the tasks tab... if I want to
override that rule I can do so from the tasks tab."

This supersedes the 7/31 'typed words never fire anything' law FOR THIS
BOX ONLY. The say-box (reply composer) stays words-only forever; the
DO-box is a separate container with separate logic — his ruling: "totally
seperate logic and function to keep from causing cross over issues."

The preview law still holds: every command previews first — what will
fire, on which order, which gates stand in the way — and nothing runs
until the FIRE click. Overrides must be stated ("force" / "override") and
every forced fire is recorded.

Actions v1:
  invoice   -> run_auto_invoice (quote freight, Square link, send)
  bol       -> create_bol_for_shipment on the order's newest shipment
               (override bypasses payment/warehouse/already-sent gates)
  dispatch  -> dispatch_order (build + send supplier orders)

Doors [admin]:
  POST /do/preview {text, order_id?}  -> the plan + gates, fires nothing
  POST /do/execute {text, order_id?, override?} -> fires, answers honestly
"""

import json
import re
from typing import Dict, Optional

from fastapi import APIRouter, Body, Depends

from auth import require_admin
from db_helpers import get_db

do_router = APIRouter(tags=["do-box"])

_OID_RE = re.compile(r"\b(5\d{3})\b")
_OVERRIDE_RE = re.compile(r"\b(force|override|anyway|regardless)\b", re.I)

ACTION_WORDS = [
    ("bol", re.compile(r"\bbol\b|bill of lading", re.I)),
    ("invoice", re.compile(r"\binvoice\b", re.I)),
    ("dispatch", re.compile(r"\bdispatch\b|send to warehouse|supplier order",
                            re.I)),
]

ACTION_LABELS = {
    "invoice": "INVOICE — quote freight, make the Square link, send the "
               "invoice email",
    "bol": "BOL — create the R+L Bill of Lading for the order's shipment "
           "(PRO number lands on the order)",
    "dispatch": "DISPATCH — build every warehouse's supplier order and "
                "send/prepare it",
}


def _parse(text: str, order_id: Optional[str]) -> Dict:
    t = text or ""
    action = None
    for name, rx in ACTION_WORDS:
        if rx.search(t):
            action = name
            break
    m = _OID_RE.search(t)
    oid = m.group(1) if m else (str(order_id).strip() if order_id else None)
    return {"action": action, "order_id": oid,
            "override": bool(_OVERRIDE_RE.search(t))}


def _newest_shipment(order_id: str) -> Optional[Dict]:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.shipment_id, s.warehouse, s.status, s.bol_sent,
                       s.pro_number, o.payment_received, o.warehouse_confirmed
                FROM order_shipments s
                JOIN orders o ON o.order_id = s.order_id
                WHERE s.order_id = %s
                ORDER BY s.updated_at DESC NULLS LAST
                LIMIT 1
            """, (str(order_id),))
            row = cur.fetchone()
            return dict(row) if row else None


def _bol_gates(ship: Dict) -> list:
    return [
        {"gate": "payment received", "ok": bool(ship.get("payment_received"))},
        {"gate": "warehouse confirmed",
         "ok": bool(ship.get("warehouse_confirmed"))},
        {"gate": "no BOL yet on this shipment",
         "ok": not ship.get("bol_sent"),
         "detail": (f"already has PRO {ship.get('pro_number')} — a forced "
                    f"re-fire creates a SECOND PRO with R+L"
                    if ship.get("bol_sent") else "")},
    ]


def _record_fire(order_id: str, text: str, action: str, override: bool,
                 result: Dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO order_events
                        (order_id, event_type, event_data, source)
                    VALUES (%s, 'do_box_fired', %s, 'do_box')
                """, (str(order_id), json.dumps({
                    "text": (text or "")[:300], "action": action,
                    "override": override,
                    "result": json.dumps(result, default=str)[:800]})))
            conn.commit()
    except Exception as e:
        print(f"[DO-BOX] fire record failed {order_id}: {e}")


def _plan(text: str, order_id: Optional[str]) -> Dict:
    """Shared brain of preview + execute: what would fire, and what stands
    in the way. Never fires anything itself."""
    p = _parse(text, order_id)
    if not p["action"]:
        return {"status": "error",
                "message": "no action recognized — say invoice, BOL, or "
                           "dispatch (plus the order number if the card "
                           "has none)"}
    if not p["order_id"]:
        return {"status": "error",
                "message": "no order — type the order number in the command "
                           "(e.g. 'force bol 5737')"}
    plan = {"status": "ok", "action": p["action"],
            "order_id": p["order_id"], "override": p["override"],
            "label": ACTION_LABELS[p["action"]],
            "gates": [], "blocked": False, "lines": []}

    if p["action"] == "bol":
        ship = _newest_shipment(p["order_id"])
        if not ship:
            return {"status": "error",
                    "message": f"order {p['order_id']} has no shipment "
                               "record — a BOL needs a shipment row first"}
        plan["shipment_id"] = ship["shipment_id"]
        plan["gates"] = _bol_gates(ship)
        plan["lines"].append(
            f"Shipment {ship['shipment_id']} from {ship['warehouse']}")
    elif p["action"] == "invoice":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT payment_link_sent, payment_received
                               FROM orders WHERE order_id = %s""",
                            (p["order_id"],))
                row = cur.fetchone()
        if not row:
            return {"status": "error",
                    "message": f"order {p['order_id']} not found"}
        plan["gates"] = [
            {"gate": "not already invoiced", "ok": not row[0],
             "detail": "invoice/link already sent — the machine will report "
                       "'skipped: already invoiced' (no override for this "
                       "one yet)" if row[0] else ""},
        ]
        plan["lines"].append("Quotes freight (can take up to ~2 minutes if "
                             "R+L is slow), makes the Square link, sends "
                             "the invoice email (allowlist rules apply)")
    elif p["action"] == "dispatch":
        plan["lines"].append("Builds every warehouse's artifact; email-auto "
                             "suppliers get their PO, portal suppliers land "
                             "as UPLOAD NEEDED to us; untranslated lines "
                             "block their warehouse")

    plan["blocked"] = any(not g["ok"] for g in plan["gates"])
    plan["needs_override"] = plan["blocked"] and p["action"] == "bol"
    return plan


@do_router.post("/do/preview")
def do_preview(payload: Dict = Body(...), _: bool = Depends(require_admin)):
    """The plan, nothing fired [admin]: action, order, gates in the way."""
    return _plan((payload or {}).get("text", ""),
                 (payload or {}).get("order_id"))


@do_router.post("/do/execute")
def do_execute(payload: Dict = Body(...), _: bool = Depends(require_admin)):
    """FIRE [admin]. override=true (or 'force'/'override' typed in the
    command) bypasses the BOL gates — William's 8/1 ruling. Every fire and
    every override lands in order_events as do_box_fired."""
    text = (payload or {}).get("text", "")
    plan = _plan(text, (payload or {}).get("order_id"))
    if plan.get("status") != "ok":
        return plan
    override = plan["override"] or bool((payload or {}).get("override"))
    action, oid = plan["action"], plan["order_id"]

    if plan["blocked"] and action == "bol" and not override:
        bad = ", ".join(g["gate"] for g in plan["gates"] if not g["ok"])
        return {"status": "blocked", "action": action, "order_id": oid,
                "message": f"gates in the way: {bad}. Say 'force' or tick "
                           "override to fire anyway."}

    try:
        if action == "invoice":
            from auto_invoice import run_auto_invoice
            result = run_auto_invoice(oid, triggered_by="do_box",
                                      dry_run=False)
        elif action == "dispatch":
            from supplier_orders import dispatch_order
            result = dispatch_order(oid, auto_send=True, dry_run=False,
                                    triggered_by="do_box")
        else:  # bol
            from fastapi import HTTPException
            from bol_routes import create_bol_for_shipment
            try:
                result = create_bol_for_shipment(
                    plan["shipment_id"], pickup_date=None, force=override)
            except HTTPException as he:
                return {"status": "error", "action": action, "order_id": oid,
                        "message": str(he.detail)}
    except Exception as e:
        return {"status": "error", "action": action, "order_id": oid,
                "message": f"{action} crashed: {e}"}

    _record_fire(oid, text, action, override, result)
    return {"status": "ok", "action": action, "order_id": oid,
            "override": override, "result": result}
