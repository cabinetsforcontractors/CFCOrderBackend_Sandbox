"""
substitution_routes.py
Customer-approved SKU substitution flow — routes.

Admin:
  POST /substitutions/propose {order_id, original_sku, substitute_sku, reason?}
       -> creates the proposal + emails the customer. Order untouched.
  GET  /substitutions?limit=50 -> recent proposals + statuses.
  POST /substitutions/{sub_id}/apply -> retry the B2BWave apply for an
       approval that landed while mutations were disabled (or failed).

Public (token-gated, linked from the proposal email):
  GET  /substitution/{token}         -> landing page with the real Approve /
                                        No buttons (email links land here so a
                                        mail scanner prefetch can never approve)
  POST /substitution/{token}/respond -> records the choice; on approve applies
                                        the swap on B2BWave (see substitutions.py)
"""

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from auth import require_admin

substitution_router = APIRouter(tags=["substitutions"])

_PAGE_STYLE = """
  body { color:#393939; font-family:'Open Sans','Helvetica Neue',Helvetica,Arial,sans-serif;
         font-size:15px; line-height:1.6; max-width:640px; margin:40px auto; padding:0 16px; }
  .card { border:1px solid #e3e3e3; border-radius:8px; padding:24px; }
  .btn { display:inline-block; padding:12px 28px; border-radius:6px; color:#fff; border:0;
         text-decoration:none; font-weight:bold; font-size:16px; cursor:pointer; margin:6px 8px 6px 0; }
  .approve { background:#1dc9b7; } .decline { background:#fd397a; }
  textarea { width:100%; min-height:90px; padding:8px; border:1px solid #ccc; border-radius:6px;
             font-family:inherit; font-size:14px; }
  s { color:#b0b0b0; }
"""


def _page(title: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card">{body_html}</div></body></html>""")


# =============================================================================
# ADMIN
# =============================================================================

class ProposalRequest(BaseModel):
    order_id: str
    original_sku: str
    substitute_sku: str
    reason: str = "out_of_stock"


@substitution_router.post("/substitutions/propose")
def propose_substitution(req: ProposalRequest, _: bool = Depends(require_admin)):
    """Create + email a substitution proposal [admin]. The order is NOT
    changed until the customer approves via the emailed link."""
    from substitutions import create_substitution_proposal
    try:
        return create_substitution_proposal(req.order_id, req.original_sku,
                                            req.substitute_sku, req.reason)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@substitution_router.get("/substitutions")
def list_substitutions(limit: int = 50, _: bool = Depends(require_admin)):
    """Recent substitution proposals + statuses [admin]."""
    from psycopg2.extras import RealDictCursor
    from db_helpers import get_db
    from substitutions import ensure_substitutions_table
    with get_db() as conn:
        ensure_substitutions_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, order_id, original_sku, substitute_sku, quantity,
                       keep_price, status, customer_email, customer_note,
                       emailed_at, responded_at, applied_at, created_at
                FROM order_substitutions
                ORDER BY created_at DESC LIMIT %s
            """, (min(int(limit), 200),))
            rows = cur.fetchall()
    return {"status": "ok", "count": len(rows), "substitutions": rows}


@substitution_router.post("/substitutions/{sub_id}/apply")
def apply_substitution_now(sub_id: int, _: bool = Depends(require_admin)):
    """Retry the B2BWave apply for an approved substitution [admin].
    For approvals that landed while B2BWAVE_MUTATIONS_ENABLED=false
    (status approved_pending_apply) or whose apply failed."""
    from psycopg2.extras import RealDictCursor
    from db_helpers import get_db
    from substitutions import apply_substitution, ensure_substitutions_table
    with get_db() as conn:
        ensure_substitutions_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM order_substitutions WHERE id = %s", (sub_id,))
            sub = cur.fetchone()
    if not sub:
        return {"status": "error", "message": f"substitution {sub_id} not found"}
    if sub["status"] not in ("approved", "approved_pending_apply", "apply_failed"):
        return {"status": "error",
                "message": f"substitution {sub_id} is '{sub['status']}' — only "
                           f"customer-approved, not-yet-applied ones can be applied"}
    return {"status": "ok", "substitution_id": sub_id,
            "apply_result": apply_substitution(sub)}


# =============================================================================
# PUBLIC (token-gated)
# =============================================================================

@substitution_router.get("/substitution/{token}", response_class=HTMLResponse)
def substitution_landing(token: str, intent: str = ""):
    """Landing page from the email buttons. Real buttons live HERE so email
    scanners that prefetch links can never phantom-approve."""
    from substitutions import get_substitution
    sub = get_substitution(token)
    if not sub:
        return _page("Not found", "<h2>Link not found</h2>"
                     "<p>This substitution link is invalid. Call us at (770) 990-4885.</p>")
    if sub["status"] != "pending":
        return _page("Already answered",
                     f"<h2>You're all set</h2><p>This substitution for order "
                     f"<strong>#{sub['order_id']}</strong> was already answered "
                     f"(status: {sub['status'].replace('_', ' ')}). "
                     f"Questions? Call (770) 990-4885.</p>")

    price = float(sub.get("keep_price") or 0)
    note_open = "open" if intent == "no" else ""
    return _page(f"Order #{sub['order_id']} substitution", f"""
      <h2>Order #{sub['order_id']} — substitution approval</h2>
      <p>Out of stock: <s>{sub['original_sku']}</s></p>
      <p>Replacement: <strong>{sub['substitute_sku']}</strong>
         (qty {sub['quantity']}, your price stays ${price:,.2f} — unchanged)</p>
      <form method="post" action="/substitution/{token}/respond">
        <button class="btn approve" type="submit" name="choice" value="approve">
          &#10003; Approve substitution</button>
        <details {note_open} style="margin-top:14px;">
          <summary style="cursor:pointer;color:#fd397a;font-weight:bold;">
            &#10005; No — tell us what you'd prefer</summary>
          <p style="margin:10px 0 6px 0;">Tell us what you'd like instead, or ask a question:</p>
          <textarea name="note" placeholder="What would you like us to do?"></textarea>
          <p><button class="btn decline" type="submit" name="choice" value="decline">
            Send my answer</button></p>
        </details>
      </form>
      <p style="color:#888;font-size:13px;">Nothing changes on your order until you choose.
         Questions? Call (770) 990-4885.</p>
    """)


@substitution_router.post("/substitution/{token}/respond", response_class=HTMLResponse)
def substitution_respond(token: str, choice: str = Form(...), note: str = Form("")):
    """Record the customer's decision; on approve, apply the swap on B2BWave."""
    from substitutions import record_response
    approved = (choice == "approve")
    result = record_response(token, approved, note)
    if result.get("status") == "error":
        return _page("Not found", "<h2>Link not found</h2>"
                     "<p>This substitution link is invalid. Call us at (770) 990-4885.</p>")
    if result.get("status") == "already_responded":
        return _page("Already answered", "<h2>You're all set</h2>"
                     "<p>This substitution was already answered. "
                     "Questions? Call (770) 990-4885.</p>")
    if approved:
        return _page("Approved", """
          <h2>&#10003; Substitution approved — thank you!</h2>
          <p>We've updated your order and your total stays the same.
             You'll receive an updated order confirmation shortly.</p>
          <p>Questions? Call (770) 990-4885.</p>""")
    return _page("Got it", """
      <h2>Got it — nothing has been changed</h2>
      <p>Your order is untouched and a real person will get back to you shortly.</p>
      <p>Need us sooner? Call (770) 990-4885.</p>""")
