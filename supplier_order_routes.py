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
         scheduled, picked_up, delivered, invoice_verified...).
  GET  /supplier-orders/digest — the "what needs me today" view.
  POST /supplier-orders/scan-replies?hours_back=24 — scan Gmail for supplier
       reply documents (estimates/SOs/quotes), verify, flip rows.
  POST /supplier-orders/verify-email/{message_id}?force=false — process one
       specific Gmail message (any sender — content markers route it).
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
    return set_status(row_id, req.status, req.note, req.supplier_doc_ref)


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
