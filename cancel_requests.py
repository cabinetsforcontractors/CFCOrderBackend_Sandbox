"""
cancel_requests.py
Confirm-first customer cancel flow (William ruling 2026-07-16).

WHY: the original path auto-canceled an order (DB + B2BWave) whenever a
customer email merely CONTAINED the word "cancel" — lifecycle_engine's
CANCEL_PATTERNS starts with r'\\bcancel\\b', so "I do NOT want to cancel"
matched. The Gmail scan runs every sync cycle in-process (sync_service
auto-sync thread), making that a live hair-trigger on the store whenever
B2BWAVE_MUTATIONS_ENABLED=true.

NOW:
  1. Detection uses STRICT_CANCEL_PATTERNS below — real cancel PHRASES only
     (bare "cancel"/"cancellation policy"/quoted-thread mentions don't match).
  2. Detection no longer cancels ANYTHING. It records a token-gated cancel
     request and emails William a [Confirm cancel] / [Dismiss] landing link —
     same scanner-safe pattern as the substitution flow (GET shows real
     buttons, POST decides, so mail-scanner prefetch can never confirm).
  3. Only a human Confirm runs lifecycle_engine.cancel_order (which still
     respects B2BWAVE_MUTATIONS_ENABLED for the website side).

Dedupe (the Gmail sync rescans the same 2h window every cycle):
  - same Gmail message id already recorded        -> skip, no email
  - a PENDING request already open for the order  -> skip, no email
  - order already canceled in the tracker         -> skip, no email

Routes:
  GET  /cancel-request/{token}          public landing (Confirm / Dismiss)
  POST /cancel-request/{token}/decide   the human's decision
  GET  /cancel-requests                 recent requests + statuses   [admin]
  POST /cancel-requests/simulate        drill: run detection+creation [admin]
"""

import html as _html
import json
import os
import re
import secrets
from typing import Dict, Optional

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from auth import require_admin
from db_helpers import get_db

PUBLIC_BASE_URL = os.environ.get("CHECKOUT_BASE_URL",
                                 "https://cfcorderbackend-sandbox.onrender.com").strip().rstrip("/")
INTERNAL_ALERT_EMAIL = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL",
                                      "cabinetsforcontractors@gmail.com").strip()

cancel_request_router = APIRouter(tags=["cancel-requests"])


# =============================================================================
# STRICT PHRASE DETECTION (replaces lifecycle_engine's bare-word matcher
# for the Gmail path; lifecycle_engine.detect_cancel_keyword is now unused)
# =============================================================================

STRICT_CANCEL_PATTERNS = [
    r'\bcancel\s+(?:my|this|the|that|our)\s+(?:entire\s+|whole\s+)?order\b',
    r'\bcancel\s+order\s*#?\s*\d+\b',
    r'\bplease\s+cancel\b',
    r'\b(?:want|need|like)\s+to\s+cancel\b',
    r'\bwould\s+like\s+to\s+cancel\b',
    r'\bgo\s+ahead\s+and\s+cancel\b',
    r'\bcancel\s+it\b',
    r'\bcancel\s+everything\b',
    r'\bcancel\s+the\s+whole\s+thing\b',
]
_STRICT_RES = [re.compile(p, re.IGNORECASE) for p in STRICT_CANCEL_PATTERNS]


def detect_cancel_phrase(text: str) -> Optional[str]:
    """Return the matched cancel PHRASE, or None. Phrases only — the bare
    word 'cancel' (policies, negations, quoted threads) does not match."""
    if not text:
        return None
    for rx in _STRICT_RES:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


# =============================================================================
# TABLE + CREATE
# =============================================================================

def ensure_cancel_requests_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_cancel_requests (
                id SERIAL PRIMARY KEY,
                token VARCHAR(64) UNIQUE NOT NULL,
                order_id VARCHAR(20) NOT NULL,
                email_subject TEXT,
                email_snippet TEXT,
                matched_phrase TEXT,
                gmail_message_id VARCHAR(120),
                status VARCHAR(20) DEFAULT 'pending',
                decided_at TIMESTAMP WITH TIME ZONE,
                result TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        conn.commit()


def create_cancel_request(order_id: str, email_subject: str, email_body: str,
                          matched_phrase: str = None,
                          message_id: str = None) -> Dict:
    """Record a detected cancel request + email William the Confirm/Dismiss
    link. NEVER cancels anything itself. Returns {'status': 'created'|
    'deduped'|'skipped', ...}."""
    customer_name = None
    order_total = None
    with get_db() as conn:
        ensure_cancel_requests_table(conn)
        with conn.cursor() as cur:
            if message_id:
                cur.execute("""SELECT id FROM order_cancel_requests
                               WHERE gmail_message_id = %s""", (message_id,))
                row = cur.fetchone()
                if row:
                    return {"status": "deduped",
                            "reason": f"message already recorded (request #{row[0]})"}
            cur.execute("""SELECT id FROM order_cancel_requests
                           WHERE order_id = %s AND status = 'pending'""",
                        (str(order_id),))
            row = cur.fetchone()
            if row:
                return {"status": "deduped",
                        "reason": f"pending request #{row[0]} already open"}
            try:
                cur.execute("""SELECT lifecycle_status, customer_name, order_total
                               FROM orders WHERE order_id = %s""", (str(order_id),))
                o = cur.fetchone()
                if o:
                    if (o[0] or "") == "canceled":
                        return {"status": "skipped", "reason": "order already canceled"}
                    customer_name, order_total = o[1], o[2]
            except Exception:
                conn.rollback()

            token = secrets.token_urlsafe(24)
            cur.execute("""
                INSERT INTO order_cancel_requests
                    (token, order_id, email_subject, email_snippet,
                     matched_phrase, gmail_message_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (token, str(order_id), (email_subject or "")[:300],
                  (email_body or "")[:1000], matched_phrase, message_id))
            req_id = cur.fetchone()[0]
            conn.commit()

    landing = f"{PUBLIC_BASE_URL}/cancel-request/{token}"
    total_txt = f"${float(order_total):,.2f}" if order_total is not None else "n/a"
    html = f"""
<div style='color:#393939;font-family:"Open Sans","Helvetica Neue",Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6;max-width:640px;'>
  <h2 style="margin:0 0 8px 0;">CONFIRM CANCEL? &mdash; Order #{_html.escape(str(order_id))}</h2>
  <p>A customer email looks like a <strong>cancel request</strong>. Nothing has been
     canceled &mdash; the order is untouched until you decide.</p>
  <table style="border-collapse:collapse;margin:10px 0;">
    <tr><td style="padding:3px 12px 3px 0;color:#707070;">Customer</td>
        <td style="padding:3px 0;">{_html.escape(customer_name or 'unknown')}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#707070;">Order total</td>
        <td style="padding:3px 0;">{total_txt}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#707070;">Matched phrase</td>
        <td style="padding:3px 0;"><strong>&quot;{_html.escape(matched_phrase or '')}&quot;</strong></td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#707070;">Email subject</td>
        <td style="padding:3px 0;">{_html.escape(email_subject or '')}</td></tr>
  </table>
  <p style="background:#f7f7f7;border-radius:6px;padding:10px 12px;color:#555;">
    {_html.escape((email_body or '')[:400])}</p>
  <p><a href="{landing}"
        style="display:inline-block;padding:12px 28px;border-radius:6px;color:#ffffff;
               text-decoration:none;font-weight:bold;background:#fd397a;">
     Review &mdash; Confirm or Dismiss</a></p>
  <p style="color:#888;font-size:12px;">Confirm-first flow: the link opens a page with the
     real buttons; the order is only canceled after you click Confirm there.</p>
</div>
"""
    try:
        from substitutions import _send_guarded_email
        email_result = _send_guarded_email(
            str(order_id), INTERNAL_ALERT_EMAIL,
            f"CONFIRM CANCEL? order #{order_id} - customer email mentions canceling",
            html, triggered_by="cancel_request_detected")
    except Exception as e:
        email_result = {"success": False, "error": str(e)}
    return {"status": "created", "request_id": req_id, "token": token,
            "landing_url": landing, "email": email_result}


def _get_request(token: str) -> Optional[Dict]:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_cancel_requests_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM order_cancel_requests WHERE token = %s", (token,))
            return cur.fetchone()


# =============================================================================
# PAGES
# =============================================================================

_PAGE_STYLE = """
  body { color:#393939; font-family:'Open Sans','Helvetica Neue',Helvetica,Arial,sans-serif;
         font-size:15px; line-height:1.6; max-width:640px; margin:40px auto; padding:0 16px; }
  .card { border:1px solid #e3e3e3; border-radius:8px; padding:24px; }
  .btn { display:inline-block; padding:12px 28px; border-radius:6px; color:#fff; border:0;
         text-decoration:none; font-weight:bold; font-size:16px; cursor:pointer; margin:6px 8px 6px 0; }
  .danger { background:#fd397a; } .neutral { background:#888888; }
  .snippet { background:#f7f7f7; border-radius:6px; padding:10px 12px; color:#555; }
"""


def _page(title: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card">{body_html}</div></body></html>""")


@cancel_request_router.get("/cancel-request/{token}", response_class=HTMLResponse)
def cancel_request_landing(token: str):
    """Landing page from the alert email. Real buttons live HERE (POST form)
    so a mail-scanner link prefetch can never confirm a cancel."""
    req = _get_request(token)
    if not req:
        return _page("Not found", "<h2>Link not found</h2>"
                                  "<p>This cancel-request link is invalid.</p>")
    if req["status"] != "pending":
        return _page("Already handled",
                     f"<h2>Already handled</h2><p>This cancel request for order "
                     f"<strong>#{req['order_id']}</strong> was already decided "
                     f"(status: {req['status']}).</p>")
    return _page(f"Cancel request — order #{req['order_id']}", f"""
      <h2>Cancel order #{req['order_id']}?</h2>
      <p>Detected phrase: <strong>&quot;{_html.escape(req.get('matched_phrase') or '')}&quot;</strong></p>
      <p>Email subject: {_html.escape(req.get('email_subject') or '')}</p>
      <p class="snippet">{_html.escape((req.get('email_snippet') or '')[:400])}</p>
      <form method="post" action="/cancel-request/{token}/decide">
        <button class="btn danger" type="submit" name="decision" value="cancel">
          Confirm &mdash; cancel this order</button>
        <button class="btn neutral" type="submit" name="decision" value="dismiss">
          Dismiss &mdash; NOT a cancel request</button>
      </form>
      <p style="color:#888;font-size:13px;">Nothing happens to the order until you choose.</p>
    """)


@cancel_request_router.post("/cancel-request/{token}/decide", response_class=HTMLResponse)
def cancel_request_decide(token: str, decision: str = Form(...)):
    """Confirm -> lifecycle cancel (DB + B2BWave, the latter still gated by
    B2BWAVE_MUTATIONS_ENABLED). Dismiss -> mark and leave the order alone.
    Idempotent per token."""
    req = _get_request(token)
    if not req:
        return _page("Not found", "<h2>Link not found</h2>")
    if req["status"] != "pending":
        return _page("Already handled",
                     f"<h2>Already handled</h2><p>Status: {req['status']}.</p>")

    new_status = "confirmed" if decision == "cancel" else "dismissed"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE order_cancel_requests
                           SET status = %s, decided_at = NOW()
                           WHERE token = %s AND status = 'pending'""",
                        (new_status, token))
            changed = cur.rowcount
            conn.commit()
    if not changed:
        return _page("Already handled", "<h2>Already handled</h2>")

    if decision != "cancel":
        _log_event(req["order_id"], "cancel_request_dismissed",
                   {"request_id": req["id"]})
        return _page("Dismissed", f"""
          <h2>Dismissed &mdash; order untouched</h2>
          <p>Order <strong>#{req['order_id']}</strong> stays exactly as it was.
             This link is now dead.</p>""")

    from lifecycle_engine import cancel_order
    result = cancel_order(req["order_id"], reason="customer_request_confirmed")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE order_cancel_requests SET result = %s
                           WHERE token = %s""",
                        (json.dumps(result, default=str)[:2000], token))
            conn.commit()
    _log_event(req["order_id"], "cancel_request_confirmed",
               {"request_id": req["id"], "result": result})

    if result.get("b2bwave_canceled"):
        site_line = "<p>Website order canceled too (verified against B2BWave).</p>"
    else:
        err = (result.get("b2bwave_result") or {}).get("error", "unknown")
        site_line = (f"<p style='color:#c00;'><strong>Website NOT canceled:</strong> {_html.escape(str(err))}."
                     f" Cancel it on the site manually, or re-confirm after enabling mutations.</p>")
    return _page("Canceled", f"""
      <h2>Order #{req['order_id']} canceled in the tracker</h2>
      {site_line}
      <p>Reason recorded: customer request (human-confirmed).</p>""")


def _log_event(order_id: str, event_type: str, data: Dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, %s, %s, 'cancel_requests')
                """, (str(order_id), event_type, json.dumps(data, default=str)))
                conn.commit()
    except Exception as e:
        print(f"[CANCEL-REQ] event log failed: {e}")


# =============================================================================
# ADMIN
# =============================================================================

class SimulateRequest(BaseModel):
    order_id: str
    subject: str = ""
    body: str = ""


@cancel_request_router.get("/cancel-requests")
def list_cancel_requests(limit: int = 50, _: bool = Depends(require_admin)):
    """Recent cancel requests + statuses [admin]."""
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_cancel_requests_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, order_id, matched_phrase, email_subject, status,
                       gmail_message_id, decided_at, created_at
                FROM order_cancel_requests
                ORDER BY created_at DESC LIMIT %s
            """, (min(int(limit), 200),))
            rows = cur.fetchall()
    return {"status": "ok", "count": len(rows), "requests": rows}


@cancel_request_router.post("/cancel-requests/simulate")
def simulate_cancel_detection(req: SimulateRequest, _: bool = Depends(require_admin)):
    """Drill endpoint [admin]: run the strict detection + confirm-first
    creation on a made-up email. Never cancels anything (that still takes a
    human click on the landing page)."""
    text = f"{req.subject} {req.body}"
    phrase = detect_cancel_phrase(text)
    if not phrase:
        return {"status": "ok", "detected": False,
                "note": "no strict cancel phrase in that text — nothing created"}
    result = create_cancel_request(req.order_id, req.subject, req.body,
                                   matched_phrase=phrase,
                                   message_id=f"simulated-{secrets.token_hex(6)}")
    return {"status": "ok", "detected": True, "matched_phrase": phrase,
            "request": result}
