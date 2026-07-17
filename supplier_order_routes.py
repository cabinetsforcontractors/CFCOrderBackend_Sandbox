"""
supplier_order_routes.py
Routes for the supplier-order state machine + dispatch engine (see
supplier_orders.py) and the reply auto-verifier (estimate_verifier.py).
All admin-gated.

  POST /supplier-orders/dispatch/{order_id}?auto_send=true&dry_run=false
       — generate + send every warehouse's order artifact. dry_run returns
         previews without sending or changing state. auto_send=false routes
         even the email suppliers' artifacts to us for confirm-and-forward.
  GET  /supplier-orders?order_id=&status=&limit=
  POST /supplier-orders/{row_id}/status {status, note?, supplier_doc_ref?}
       — manual transitions (mark-sent after a portal upload, confirmed,
         scheduled, picked_up, delivered, invoice_verified...). picked_up /
         delivered transitions also run the B2BWave all-legs checkpoint
         (status-driven lifecycle, William 2026-07-17): ALL legs picked up
         -> store shows Sent; ALL delivered -> Complete.
  GET  /supplier-orders/digest — the "what needs me today" view.
  POST /supplier-orders/scan-replies?hours_back=24 — scan Gmail for supplier
       reply documents (estimates/SOs/quotes), verify, flip rows.
  POST /supplier-orders/verify-email/{message_id}?force=false — process one
       specific Gmail message (any sender — content markers route it).
  POST /supplier-orders/b2bwave-status-backfill?dry_run=true — compute (and
       with dry_run=false apply) the ladder status every open order should
       be at from local checkpoints. Never cancels, never downgrades.
  POST /supplier-orders/b2bwave-status/{order_id} {target} — manual single
       ladder write (awaiting_payment | being_prepared | sent | complete).
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import require_admin

supplier_order_router = APIRouter(tags=["supplier-orders"])


class StatusUpdate(BaseModel):
    status: str
    note: Optional[str] = None
    supplier_doc_ref: Optional[str] = None


class B2BStatusSet(BaseModel):
    target: str  # awaiting_payment | being_prepared | sent | complete


@supplier_order_router.post("/supplier-orders/dispatch/{order_id}")
def dispatch(order_id: str, auto_send: bool = True, dry_run: bool = False,
             _: bool = Depends(require_admin)):
    """Dispatch an order's supplier artifacts [admin]."""
    from supplier_orders import dispatch_order
    try:
        return dispatch_order(order_id, auto_send=auto_send, dry_run=dry_run,
                              triggered_by="manual_dispatch")
    except Exception as e:
        return {"status": "error", "message": str(e)}


@supplier_order_router.get("/supplier-orders")
def list_rows(order_id: str = None, status: str = None, limit: int = 100,
              _: bool = Depends(require_admin)):
    from supplier_orders import list_supplier_orders
    return {"status": "ok",
            "supplier_orders": list_supplier_orders(order_id, status, limit)}


@supplier_order_router.post("/supplier-orders/{row_id}/status")
def update_status(row_id: int, req: StatusUpdate,
                  _: bool = Depends(require_admin)):
    from supplier_orders import set_status
    result = set_status(row_id, req.status, req.note, req.supplier_doc_ref)
    # Status-driven lifecycle checkpoint (William 2026-07-17): when a leg
    # reaches picked_up/delivered/invoice_verified, apply the ALL-legs rule
    # on B2BWave (Sent only when every leg is out; Complete only when every
    # leg is delivered — partial marking would let a stuck warehouse fail
    # silently). Guarded — never breaks the transition itself.
    if (result.get("status") == "ok"
            and req.status in ("picked_up", "delivered", "invoice_verified")):
        try:
            from b2bwave_status import progress_from_supplier_legs
            result["b2bwave_status"] = progress_from_supplier_legs(
                result["row"]["order_id"])
        except Exception as e:
            result["b2bwave_status"] = {"applied": False, "error": str(e)}
    return result


@supplier_order_router.get("/supplier-orders/digest")
def get_digest(_: bool = Depends(require_admin)):
    from supplier_orders import digest
    return digest()


@supplier_order_router.post("/supplier-orders/scan-replies")
def scan_replies_now(hours_back: int = 24, _: bool = Depends(require_admin)):
    """Scan Gmail for supplier reply documents and auto-verify [admin].
    Also runs automatically inside gmail_sync."""
    from estimate_verifier import scan_replies
    try:
        return scan_replies(hours_back=hours_back)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@supplier_order_router.post("/supplier-orders/verify-email/{message_id}")
def verify_email_now(message_id: str, force: bool = False,
                     _: bool = Depends(require_admin)):
    """Process ONE Gmail message through the reply verifier [admin].
    Content markers route it regardless of sender — built for beta testing
    where documents arrive from the safety inbox."""
    from estimate_verifier import process_message
    try:
        return process_message(message_id, force=force)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@supplier_order_router.post("/supplier-orders/b2bwave-status-backfill")
def b2bwave_status_backfill(dry_run: bool = True, limit: int = 200,
                            _: bool = Depends(require_admin)):
    """Status-driven lifecycle backfill [admin]: compute the ladder status
    every open order SHOULD be at (from payment_link_sent / payment_received /
    supplier legs) vs what B2BWave shows. dry_run=true (default) only reports;
    dry_run=false writes — each write ladder-guarded, readback-verified, and
    still gated by B2BWAVE_MUTATIONS_ENABLED. Never cancels, never downgrades,
    never touches Temporary/Canceled/Invoiced orders."""
    from b2bwave_status import backfill_statuses
    try:
        return backfill_statuses(dry_run=dry_run, limit=limit)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@supplier_order_router.post("/supplier-orders/b2bwave-status/{order_id}")
def b2bwave_status_set(order_id: str, req: B2BStatusSet,
                       _: bool = Depends(require_admin)):
    """Manual single B2BWave ladder write [admin]. Same guards as the
    automatic checkpoints (mutations gate, no downgrade, readback)."""
    from b2bwave_status import (set_order_status, STATUS_AWAITING_PAYMENT,
                                STATUS_BEING_PREPARED, STATUS_SENT,
                                STATUS_COMPLETE)
    targets = {"awaiting_payment": STATUS_AWAITING_PAYMENT,
               "being_prepared": STATUS_BEING_PREPARED,
               "sent": STATUS_SENT, "complete": STATUS_COMPLETE}
    target = targets.get((req.target or "").lower().strip())
    if not target:
        return {"status": "error",
                "message": f"invalid target '{req.target}' (valid: {list(targets)})"}
    try:
        return set_order_status(order_id, target, "manual admin set",
                                "manual_status_set")
    except Exception as e:
        return {"status": "error", "message": str(e)}
