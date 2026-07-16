"""
substitutions.py
Customer-approved SKU substitution flow (William rulings 2026-07-16/17).

Flow:
  1. An item is out of stock at the warehouse; a substitute is identified
     (and warehouse stock for the substitute has been confirmed).
  2. POST /substitutions/propose -> EMAILS the customer a proposal styled like
     the B2BWave order-confirmation email: plain-language message box on top,
     [Approve] and [No — tell us what you'd prefer] buttons.
  3. NOTHING changes on the order until the customer responds. The email
     buttons land on a confirmation page (one extra click) so email-scanner
     link prefetching can never phantom-approve.
  4. Approve -> the line swap is applied to the website order via the B2BWave
     API with the substitute priced at the ORIGINAL line's price (customer
     total unchanged — CFC eats the difference), then William is alerted.
  5. Decline with a note -> the note is parsed for a SKU request; the customer
     is shown up to 3 fuzzy-matched IN-LINE options ("Did you mean...?") and
     their pick auto-applies (they confirmed the exact SKU themselves = the
     99.9% + sanity-check ruling) with a confirmation email + William FYI.
     No match / "none of these" -> plain decline: note recorded, William
     alerted (with recognition detail), order untouched.

B2BWave API quirks (proven live 2026-07-16 on test orders 4860/5706):
  - Accept: application/json is REQUIRED — without it mutations APPLY but the
    response is 500/406 (silent-success trap). Always verify by readback.
  - notify must be the string "true" or int 1 (JSON true -> HTTP 500), and on
    this store the notify flag sends no customer email anyway — which is why
    this module sends its own emails via the guarded Gmail path.
  - There is no order-level dollar-discount endpoint; per-line custom price
    (has_custom_price=1) achieves the same customer total.
"""

import difflib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from config import B2BWAVE_URL
from db_helpers import get_db

B2BWAVE_USERNAME = os.environ.get("B2BWAVE_USERNAME", "").strip()
B2BWAVE_API_KEY = os.environ.get("B2BWAVE_API_KEY", "").strip()
PUBLIC_BASE_URL = os.environ.get("CHECKOUT_BASE_URL",
                                 "https://cfcorderbackend-sandbox.onrender.com").strip().rstrip("/")
INTERNAL_ALERT_EMAIL = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL",
                                      "cabinetsforcontractors@gmail.com").strip()

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CFC-Orders-Backend"


# =============================================================================
# TABLE
# =============================================================================

def ensure_substitutions_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS order_substitutions (
                id SERIAL PRIMARY KEY,
                token VARCHAR(64) UNIQUE NOT NULL,
                order_id VARCHAR(20) NOT NULL,
                original_sku VARCHAR(100) NOT NULL,
                substitute_sku VARCHAR(100) NOT NULL,
                quantity INTEGER,
                keep_price DECIMAL(10,2),
                reason TEXT,
                status VARCHAR(30) DEFAULT 'pending',
                customer_email VARCHAR(200),
                customer_name VARCHAR(200),
                customer_note TEXT,
                emailed_at TIMESTAMP WITH TIME ZONE,
                responded_at TIMESTAMP WITH TIME ZONE,
                applied_at TIMESTAMP WITH TIME ZONE,
                apply_result TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE order_substitutions ADD COLUMN IF NOT EXISTS requested_sku VARCHAR(100)")
        cur.execute("ALTER TABLE order_substitutions ADD COLUMN IF NOT EXISTS requested_name TEXT")
        cur.execute("ALTER TABLE order_substitutions ADD COLUMN IF NOT EXISTS requested_price DECIMAL(10,2)")
        cur.execute("ALTER TABLE order_substitutions ADD COLUMN IF NOT EXISTS requested_detail TEXT")
        conn.commit()


# =============================================================================
# B2BWAVE API HELPERS
# =============================================================================

def _b2b(method: str, path: str, body: dict = None, timeout: int = 30):
    """B2BWave API call with the mandatory Accept header. Returns (status, data)."""
    if not (B2BWAVE_URL and B2BWAVE_USERNAME and B2BWAVE_API_KEY):
        return None, {"error": "B2BWave API not configured"}
    url = f"{B2BWAVE_URL}/api/{path}"
    try:
        resp = requests.request(
            method, url, json=body, timeout=timeout,
            auth=(B2BWAVE_USERNAME, B2BWAVE_API_KEY),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": _UA})
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"raw": resp.text[:300]}
    except Exception as e:
        return None, {"error": str(e)}


def fetch_b2b_order(order_id: str) -> Optional[Dict]:
    st, data = _b2b("GET", f"orders.json?id_eq={order_id}")
    if st == 200 and isinstance(data, list) and data:
        return data[0].get("order", data[0])
    return None


def fetch_b2b_product(code: str) -> Optional[Dict]:
    st, data = _b2b("GET", f"products.json?code_eq={code}")
    if st == 200 and isinstance(data, list) and data:
        return data[0].get("product", data[0])
    return None


def search_b2b_products(fragment: str) -> list:
    st, data = _b2b("GET", f"products.json?code_cont={fragment}")
    if st == 200 and isinstance(data, list):
        return [p.get("product", p) for p in data]
    return []


def _order_products(order: Dict) -> list:
    return [p.get("order_product", p) for p in (order.get("order_products") or [])]


# =============================================================================
# DECLINE-NOTE SKU RECOGNITION + FUZZY IN-LINE SUGGESTIONS
# =============================================================================

_SKU_TOKEN = re.compile(r"\b([A-Za-z]{1,6}(?:-[A-Za-z0-9./]+)*-?[A-Za-z]*\d[A-Za-z0-9./-]*)\b")


def resolve_note_sku(note: str, original_sku: str) -> Dict:
    """Parse a customer's decline note for a SKU request.

    Handles: full SKUs ("WSP-WF342"), bare body tokens ("TF396" -> completed
    with the original line's prefix), and single-hit catalog fragments.
    Returns {"resolved": bool, "sku","name","price","product_id","detail"}.
    "TF396 exists in other lines but not this one" is reported, never guessed.
    """
    out = {"resolved": False, "sku": None, "name": None, "price": None,
           "product_id": None, "detail": ""}
    if not note:
        out["detail"] = "no note"
        return out
    prefix = (original_sku or "").split("-")[0].upper()
    tokens = sorted({t.upper().strip("-.") for t in _SKU_TOKEN.findall(note)},
                    key=len, reverse=True)
    if not tokens:
        out["detail"] = "no SKU-like token in note"
        return out

    for tok in tokens[:5]:
        # 1) note contains a full SKU
        p = fetch_b2b_product(tok)
        # 2) bare body token -> complete with the original line's prefix
        if not p and prefix and not tok.startswith(prefix + "-"):
            p = fetch_b2b_product(f"{prefix}-{tok}")
        if p:
            out.update({"resolved": True, "sku": p.get("code"), "name": p.get("name"),
                        "price": p.get("price"), "product_id": p.get("id"),
                        "detail": f"matched token '{tok}'"})
            return out
        # 3) catalog fragment search — accept only an unambiguous hit
        hits = search_b2b_products(tok)
        if hits:
            in_line = [h for h in hits if (h.get("code") or "").upper().startswith(prefix + "-")]
            if len(in_line) == 1:
                p = in_line[0]
                out.update({"resolved": True, "sku": p.get("code"), "name": p.get("name"),
                            "price": p.get("price"), "product_id": p.get("id"),
                            "detail": f"matched token '{tok}' within line {prefix}"})
                return out
            codes = ", ".join((h.get("code") or "") for h in hits[:8])
            out["detail"] = (f"'{tok}' exists in the catalog ({codes}"
                             f"{'...' if len(hits) > 8 else ''}) but NOT in line "
                             f"{prefix} — needs a human decision")
            return out
    out["detail"] = f"no catalog match for token(s): {', '.join(tokens[:5])}"
    return out


def suggest_in_line_alternatives(note: str, original_sku: str,
                                 limit: int = 3) -> List[Dict]:
    """Fuzzy 'did you mean' candidates WITHIN the customer's line, for the
    decline page. Bodies come from rta_products (the SOT-derived catalog —
    the practical canonical stand-in: TF396 vs WF396 score 0.8); each
    candidate is verified live on B2BWave before being offered.
    Exact matches score 1.0 and sort first."""
    prefix = (original_sku or "").split("-")[0].upper()
    if not prefix or not note:
        return []
    tokens = sorted({t.upper().strip("-.") for t in _SKU_TOKEN.findall(note)},
                    key=len, reverse=True)
    if not tokens:
        return []

    bodies = {}
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT product_sku FROM rta_products WHERE product_sku LIKE %s",
                            (prefix + "-%",))
                for (sku,) in cur.fetchall():
                    body = sku.split("-", 1)[1].upper() if "-" in sku else sku.upper()
                    bodies[body] = sku
    except Exception as e:
        print(f"[SUBS] suggestion body lookup failed: {e}")
        return []
    if not bodies:
        return []

    scored = {}
    original_body = original_sku.split("-", 1)[1].upper() if "-" in original_sku else ""
    for tok in tokens[:3]:
        body_tok = tok.split("-", 1)[1] if tok.startswith(prefix + "-") else tok
        for m in difflib.get_close_matches(body_tok, list(bodies), n=limit * 3, cutoff=0.55):
            if m == original_body:
                continue  # don't suggest the item that's out of stock
            score = difflib.SequenceMatcher(None, body_tok, m).ratio()
            sku = bodies[m]
            if sku not in scored or scored[sku] < score:
                scored[sku] = score

    out = []
    for sku, score in sorted(scored.items(), key=lambda x: -x[1]):
        p = fetch_b2b_product(sku)  # must be live on the site to be offered
        if p:
            out.append({"sku": p.get("code"), "name": p.get("name") or "",
                        "price": p.get("price"), "product_id": p.get("id"),
                        "score": round(score, 3)})
        if len(out) >= limit:
            break
    return out


# =============================================================================
# PROPOSAL EMAIL (clone of the B2BWave order-confirmation look)
# =============================================================================

_BTN = ("display:inline-block;padding:12px 28px;border-radius:6px;color:#ffffff;"
        "text-decoration:none;font-weight:bold;font-size:15px;margin:4px 8px 4px 0;")


def build_proposal_email(order: Dict, sub: Dict) -> str:
    """Proposal HTML: message box + buttons on top, order-style change table below.
    Mirrors the B2BWave notification styling (Open Sans 13px, same table look)."""
    first = (sub.get("customer_name") or order.get("customer_name") or "there").split()[0]
    order_id = sub["order_id"]
    landing = f"{PUBLIC_BASE_URL}/substitution/{sub['token']}"
    td = ("border-bottom:1px solid #dddddd;padding:6px;")
    qty = sub.get("quantity") or 1
    price = float(sub.get("keep_price") or 0)
    line_total = price * qty

    return f"""
<div style='color:#393939;font-family:"Open Sans","Helvetica Neue",Helvetica,Arial,sans-serif;font-size:13px;line-height:1.5;max-width:50em;'>
  <h1 style="margin-bottom:6px;">A change needs your OK — Order #{order_id}</h1>

  <div style="border:1px solid #f0ad4e;background:#fdf7ec;border-radius:6px;padding:14px 16px;margin:12px 0;">
    <p style="margin:0 0 8px 0;">Hi {first},</p>
    <p style="margin:0 0 8px 0;">
      One item on your order <strong>#{order_id}</strong> is out of stock:
      <strong>{sub['original_sku']}</strong> — {sub.get('original_name', '')}.
    </p>
    <p style="margin:0 0 8px 0;">
      We can replace it with <strong>{sub['substitute_sku']}</strong> — {sub.get('substitute_name', '')}.
      <strong>Your price stays exactly the same</strong> — we cover any difference in cost.
    </p>
    <p style="margin:0 0 12px 0;">
      If that works for you, click <strong>Approve</strong>. If not, click <strong>No</strong> and
      tell us what you'd like us to do instead. Nothing changes on your order until we hear from you.
    </p>
    <p style="margin:0;">
      <a href="{landing}?intent=approve" style="{_BTN}background:#1dc9b7;">&#10003; Approve substitution</a>
      <a href="{landing}?intent=no" style="{_BTN}background:#fd397a;">&#10005; No — tell us what you'd prefer</a>
    </p>
  </div>

  <table style="width:100%;max-width:50em;border-collapse:collapse;margin-bottom:20px;">
    <thead>
      <tr style="color:#707070;background:#f2f2f2;">
        <th style="{td}" align="left"></th>
        <th style="{td}" align="left">Code</th>
        <th style="{td}" align="left">Name</th>
        <th style="{td}" align="right">Price</th>
        <th style="{td}" align="right">Quantity</th>
        <th style="{td}" align="right">Total</th>
      </tr>
    </thead>
    <tbody>
      <tr style="color:#b0b0b0;">
        <td style="{td}">Out of stock</td>
        <td style="{td}"><s>{sub['original_sku']}</s></td>
        <td style="{td}"><s>{sub.get('original_name', '')}</s></td>
        <td style="{td}" align="right"><s>${price:,.2f}</s></td>
        <td style="{td}" align="right"><s>{qty}</s></td>
        <td style="{td}" align="right"><s>${line_total:,.2f}</s></td>
      </tr>
      <tr style="background:#f0fbf7;">
        <td style="{td}"><strong style="color:#1dc9b7;">Replacement</strong></td>
        <td style="{td}"><strong>{sub['substitute_sku']}</strong></td>
        <td style="{td}">{sub.get('substitute_name', '')}</td>
        <td style="{td}" align="right"><strong>${price:,.2f}</strong></td>
        <td style="{td}" align="right"><strong>{qty}</strong></td>
        <td style="{td}" align="right"><strong>${line_total:,.2f}</strong></td>
      </tr>
    </tbody>
  </table>

  <p style="margin:0 0 4px 0;">Every other item on your order is unchanged, and your order total stays the same.</p>
  <p>Thank you,<br>The CFC Team<br>(770) 990-4885</p>
</div>
"""


def _send_guarded_email(order_id: str, to_email: str, subject: str, html: str,
                        triggered_by: str) -> Dict:
    """Send raw HTML through the same Gmail path + EMAIL_ALLOWLIST guard as
    email_sender.send_order_email (which is template-registry-bound)."""
    from config import GMAIL_SEND_ENABLED
    from email_sender import _gmail_send, _log_email_event

    if not to_email or "@" not in to_email:
        return {"success": False, "error": f"invalid email: {to_email}"}
    if not GMAIL_SEND_ENABLED:
        return {"success": False, "error": "GMAIL_SEND_ENABLED=false", "dry_run": True}
    allowlist = os.environ.get("EMAIL_ALLOWLIST", "").strip()
    if allowlist:
        allowed = {e.strip().lower() for e in allowlist.split(",") if e.strip()}
        if to_email.lower() not in allowed:
            redirect = os.environ.get("INTERNAL_SAFETY_EMAIL", "").strip()
            if redirect:
                print(f"[SUBS-GUARD] redirected {to_email} -> {redirect} order={order_id}")
                to_email = redirect
            else:
                print(f"[SUBS-GUARD] blocked {to_email} order={order_id}")
                return {"success": False, "error": "recipient not in EMAIL_ALLOWLIST",
                        "dry_run": True, "original_to": to_email}
    try:
        message_id = _gmail_send(to_email, subject, html, order_id=order_id)
        _log_email_event(order_id=order_id, template_id="substitution_flow",
                         to_email=to_email, subject=subject, message_id=message_id,
                         triggered_by=triggered_by, source="email_send")
        return {"success": bool(message_id), "message_id": message_id, "to": to_email}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# CREATE PROPOSAL
# =============================================================================

def create_substitution_proposal(order_id: str, original_sku: str,
                                 substitute_sku: str, reason: str = "out_of_stock") -> Dict:
    """Create + email a substitution proposal. Does NOT touch the order."""
    order = fetch_b2b_order(order_id)
    if not order:
        return {"status": "error", "message": f"order {order_id} not found on B2BWave"}
    line = next((p for p in _order_products(order)
                 if (p.get("product_code") or "").upper() == original_sku.upper()), None)
    if not line:
        return {"status": "error",
                "message": f"order {order_id} has no line with SKU {original_sku}"}
    sub_product = fetch_b2b_product(substitute_sku)
    if not sub_product:
        return {"status": "error",
                "message": f"substitute SKU {substitute_sku} not found on B2BWave"}

    sub = {
        "token": secrets.token_urlsafe(24),
        "order_id": str(order_id),
        "original_sku": line.get("product_code"),
        "original_name": line.get("product_name") or "",
        "substitute_sku": sub_product.get("code"),
        "substitute_name": sub_product.get("name") or "",
        "substitute_product_id": sub_product.get("id"),
        "quantity": int(float(line.get("quantity") or 1)),
        "keep_price": float(line.get("final_price") or 0),
        "customer_email": order.get("customer_email") or "",
        "customer_name": order.get("customer_name") or "",
    }
    html = build_proposal_email(order, sub)
    subject = f"Order #{order_id} — one item needs your OK"

    with get_db() as conn:
        ensure_substitutions_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_substitutions
                    (token, order_id, original_sku, substitute_sku, quantity,
                     keep_price, reason, status, customer_email, customer_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                RETURNING id
            """, (sub["token"], sub["order_id"], sub["original_sku"],
                  sub["substitute_sku"], sub["quantity"], sub["keep_price"],
                  reason, sub["customer_email"], sub["customer_name"]))
            sub_id = cur.fetchone()[0]
            conn.commit()

    email_result = _send_guarded_email(order_id, sub["customer_email"], subject,
                                       html, triggered_by="substitution_propose")
    with get_db() as conn:
        with conn.cursor() as cur:
            if email_result.get("success"):
                cur.execute("UPDATE order_substitutions SET emailed_at = NOW() WHERE id = %s",
                            (sub_id,))
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'substitution_proposed', %s, 'substitutions')
            """, (str(order_id), json.dumps({
                "substitution_id": sub_id, "original_sku": sub["original_sku"],
                "substitute_sku": sub["substitute_sku"], "quantity": sub["quantity"],
                "keep_price": sub["keep_price"], "email": email_result,
            })))
            conn.commit()
    return {"status": "ok", "substitution_id": sub_id, "token": sub["token"],
            "landing_url": f"{PUBLIC_BASE_URL}/substitution/{sub['token']}",
            "email": email_result,
            "proposal": {k: sub[k] for k in ("original_sku", "substitute_sku",
                                             "quantity", "keep_price", "customer_email")}}


# =============================================================================
# CUSTOMER RESPONSE + APPLY
# =============================================================================

def get_substitution(token: str) -> Optional[Dict]:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_substitutions_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM order_substitutions WHERE token = %s", (token,))
            return cur.fetchone()


def record_response(token: str, approved: bool, note: str = "") -> Dict:
    """Record the customer's click. Approve -> attempt the B2BWave apply.
    Decline -> parse the note for a SKU request (recognized item goes into
    William's alert with a one-click counter-apply). Idempotent per token."""
    sub = get_substitution(token)
    if not sub:
        return {"status": "error", "message": "unknown token"}
    if sub["status"] != "pending":
        return {"status": "already_responded", "substitution": _public_view(sub)}

    new_status = "approved" if approved else "declined"
    requested = None
    if not approved and note:
        try:
            requested = resolve_note_sku(note, sub["original_sku"])
        except Exception as e:
            requested = {"resolved": False, "detail": f"recognition error: {e}"}

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE order_substitutions
                SET status = %s, customer_note = %s, responded_at = NOW(),
                    requested_sku = %s, requested_name = %s,
                    requested_price = %s, requested_detail = %s
                WHERE token = %s AND status = 'pending'
            """, (new_status, (note or "")[:2000],
                  (requested or {}).get("sku"), (requested or {}).get("name"),
                  (requested or {}).get("price"), (requested or {}).get("detail"),
                  token))
            changed = cur.rowcount
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'substitution_response', %s, 'substitutions')
            """, (sub["order_id"], json.dumps({
                "substitution_id": sub["id"], "approved": approved,
                "note": (note or "")[:500], "recognized": requested,
            })))
            conn.commit()
    if not changed:
        return {"status": "already_responded", "substitution": _public_view(sub)}

    apply_result = None
    if approved:
        apply_result = apply_substitution(sub)

    _alert_william(sub, approved, note, apply_result, requested)
    return {"status": "ok", "approved": approved, "apply_result": apply_result,
            "recognized": requested}


def record_customer_choice(token: str, chosen_sku: str, note: str = "") -> Dict:
    """Customer picked a specific in-line SKU from the 'did you mean' options.
    They confirmed the exact item themselves -> auto-apply (guarded), send
    them the confirmation email, and FYI William. Idempotent per token."""
    sub = get_substitution(token)
    if not sub:
        return {"status": "error", "message": "unknown token"}
    if sub["status"] != "pending":
        return {"status": "already_responded", "substitution": _public_view(sub)}
    product = fetch_b2b_product(chosen_sku)
    if not product:
        return {"status": "error", "message": f"{chosen_sku} not found on the site"}

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE order_substitutions
                SET status = 'declined', customer_note = %s, responded_at = NOW(),
                    requested_sku = %s, requested_name = %s,
                    requested_price = %s,
                    requested_detail = 'customer chose from suggestions'
                WHERE token = %s AND status = 'pending'
            """, ((note or "")[:2000], product.get("code"), product.get("name"),
                  product.get("price"), token))
            changed = cur.rowcount
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'substitution_response', %s, 'substitutions')
            """, (sub["order_id"], json.dumps({
                "substitution_id": sub["id"], "approved": False,
                "customer_choice": product.get("code"), "note": (note or "")[:500],
            })))
            conn.commit()
    if not changed:
        return {"status": "already_responded", "substitution": _public_view(sub)}

    fresh = get_substitution(token)
    result = counter_apply(fresh)

    verdict_lines = [
        f"<p><strong>Substitution — customer CHOSE THEIR OWN replacement</strong> "
        f"— Order #{sub['order_id']}</p>",
        f"<p>Proposed {sub['original_sku']} &rarr; {sub['substitute_sku']}; customer "
        f"picked <strong>{product.get('code')}</strong> from the suggestions instead "
        f"(held at ${float(sub['keep_price'] or 0):,.2f}).</p>",
        f"<p>Customer: {sub.get('customer_name')} &lt;{sub.get('customer_email')}&gt;</p>",
    ]
    if note:
        verdict_lines.append(f"<p><strong>Their note:</strong> {note[:1000]}</p>")
    if result.get("applied"):
        verdict_lines.append("<p>Website order UPDATED automatically "
                             "(verified by readback); customer confirmation sent.</p>")
    else:
        verdict_lines.append(f"<p style='color:#c00;'><strong>ACTION NEEDED:</strong> "
                             f"not applied yet — {result.get('error', 'unknown')}. "
                             f"Re-run POST /substitutions/{sub['id']}/counter-apply "
                             f"once mutations are enabled.</p>")
    _send_guarded_email(sub["order_id"], INTERNAL_ALERT_EMAIL,
                        f"Substitution: customer chose {product.get('code')} "
                        f"on order #{sub['order_id']}",
                        "\n".join(verdict_lines),
                        triggered_by="substitution_customer_choice")
    return {"status": "ok", "chosen": product.get("code"), "apply_result": result}


def apply_substitution(sub: Dict, substitute_sku: str = None,
                       applied_status: str = "applied",
                       pending_status: str = "approved_pending_apply") -> Dict:
    """Swap the line on the B2BWave order: ADD the substitute at the original
    price first (customer total unchanged), verify by readback, then REMOVE
    the original line, verify again. Guarded by B2BWAVE_MUTATIONS_ENABLED.
    substitute_sku overrides the proposed one (counter-apply)."""
    target_sku = substitute_sku or sub["substitute_sku"]
    result = {"applied": False, "substitute_sku": target_sku, "steps": []}
    if os.environ.get("B2BWAVE_MUTATIONS_ENABLED", "true").lower() == "false":
        result["error"] = ("approved by customer but NOT applied: B2BWave "
                           "mutations disabled (B2BWAVE_MUTATIONS_ENABLED=false)")
        _store_apply_result(sub, pending_status, result)
        return result

    order_id = sub["order_id"]
    order = fetch_b2b_order(order_id)
    if not order:
        result["error"] = "order not found on B2BWave"
        _store_apply_result(sub, "apply_failed", result)
        return result
    line = next((p for p in _order_products(order)
                 if (p.get("product_code") or "").upper() == sub["original_sku"].upper()), None)
    if not line:
        result["error"] = f"original line {sub['original_sku']} no longer on order"
        _store_apply_result(sub, "apply_failed", result)
        return result
    sub_product = fetch_b2b_product(target_sku)
    if not sub_product:
        result["error"] = f"substitute {target_sku} not found"
        _store_apply_result(sub, "apply_failed", result)
        return result

    before_ids = {p["id"] for p in _order_products(order)}
    qty = int(float(line.get("quantity") or 1))
    price = float(sub["keep_price"] or line.get("final_price") or 0)

    st, d = _b2b("PATCH", f"orders/{order_id}/add_product", {
        "product_id": sub_product["id"], "quantity": qty,
        "has_custom_price": 1, "price": price,
        "note": f"Substituted for {sub['original_sku']} with customer approval "
                f"(substitution #{sub['id']}); priced at original line price.",
    })
    result["steps"].append({"add_product": st})
    check = fetch_b2b_order(order_id)
    new_lines = [p for p in _order_products(check or {}) if p["id"] not in before_ids]
    added = next((p for p in new_lines
                  if (p.get("product_code") or "").upper() == target_sku.upper()), None)
    if not added:
        result["error"] = f"add_product did not verify (HTTP {st})"
        _store_apply_result(sub, "apply_failed", result)
        return result
    result["steps"].append({"added_line_id": added["id"],
                            "added_price": added.get("final_price")})

    st, d = _b2b("PATCH", f"orders/{order_id}/remove_product",
                 {"order_product_id": line["id"]})
    result["steps"].append({"remove_product": st})
    check = fetch_b2b_order(order_id)
    still_there = any(p["id"] == line["id"] for p in _order_products(check or {}))
    if still_there:
        result["error"] = (f"substitute added but ORIGINAL LINE NOT REMOVED "
                           f"(HTTP {st}) — fix manually on order {order_id}")
        _store_apply_result(sub, "apply_failed", result)
        return result

    result["applied"] = True
    _store_apply_result(sub, applied_status, result)
    return result


def counter_apply(sub: Dict, sku_override: str = None) -> Dict:
    """Swap to the CUSTOMER-REQUESTED item (from their decline note/choice, or
    an explicit admin override). On success the customer gets a confirmation
    email. Held at the original line price, same as any substitution.
    If the mutations guard blocks it the row STAYS 'declined' so this call
    can simply be repeated after the guard is lifted."""
    target = (sku_override or sub.get("requested_sku") or "").strip()
    if not target:
        return {"applied": False,
                "error": ("no recognized SKU on this substitution "
                          f"({sub.get('requested_detail') or 'no note parsed'}); "
                          "pass {\"sku\": \"...\"} to override")}
    result = apply_substitution(sub, substitute_sku=target,
                                applied_status="counter_applied",
                                pending_status="declined")
    if result.get("applied"):
        first = (sub.get("customer_name") or "there").split()[0]
        html = f"""
<div style='color:#393939;font-family:"Open Sans","Helvetica Neue",Helvetica,Arial,sans-serif;font-size:13px;line-height:1.5;max-width:50em;'>
  <h1>Done — order #{sub['order_id']} updated</h1>
  <p>Hi {first},</p>
  <p>As you asked, we replaced <s>{sub['original_sku']}</s> with
     <strong>{target}</strong> on your order #{sub['order_id']}.
     <strong>Your price stays the same</strong> — we covered the difference.</p>
  <p>Every other item is unchanged. Questions? Just reply or call (770) 990-4885.</p>
  <p>Thank you,<br>The CFC Team</p>
</div>"""
        result["customer_email"] = _send_guarded_email(
            sub["order_id"], sub.get("customer_email") or "",
            f"Order #{sub['order_id']} — updated as you asked ({target})",
            html, triggered_by="substitution_counter_apply")
    return result


def _store_apply_result(sub: Dict, status: str, result: Dict):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE order_substitutions
                SET status = %s, apply_result = %s,
                    applied_at = CASE WHEN %s IN ('applied', 'counter_applied')
                                      THEN NOW() ELSE applied_at END
                WHERE id = %s
            """, (status, json.dumps(result)[:4000], status, sub["id"]))
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'substitution_apply', %s, 'substitutions')
            """, (sub["order_id"], json.dumps({"substitution_id": sub["id"],
                                               "status": status, "result": result})))
            conn.commit()


def _alert_william(sub: Dict, approved: bool, note: str,
                   apply_result: Optional[Dict], requested: Optional[Dict] = None):
    verdict = "APPROVED" if approved else "DECLINED"
    lines = [
        f"<p><strong>Substitution {verdict}</strong> — Order #{sub['order_id']}</p>",
        f"<p>{sub['original_sku']} &rarr; {sub['substitute_sku']} "
        f"(qty {sub['quantity']}, held at ${float(sub['keep_price'] or 0):,.2f})</p>",
        f"<p>Customer: {sub.get('customer_name')} &lt;{sub.get('customer_email')}&gt;</p>",
    ]
    if note:
        lines.append(f"<p><strong>Customer note:</strong> {note[:1000]}</p>")
    if approved:
        if apply_result and apply_result.get("applied"):
            lines.append("<p>Website order UPDATED automatically (verified by readback).</p>")
        else:
            err = (apply_result or {}).get("error", "unknown")
            lines.append(f"<p style='color:#c00;'><strong>ACTION NEEDED:</strong> "
                         f"order NOT updated — {err}</p>")
    else:
        if requested and requested.get("resolved"):
            cat = requested.get("price")
            cat_txt = f"${float(cat):,.2f}" if cat is not None else "n/a"
            lines.append(
                f"<p style='background:#eef7ff;padding:8px;border-radius:6px;'>"
                f"<strong>Recognized request:</strong> {requested['sku']} — "
                f"{requested.get('name', '')}<br>"
                f"Catalog price {cat_txt} vs held ${float(sub['keep_price'] or 0):,.2f}.<br>"
                f"One click to accept: POST /substitutions/{sub['id']}/counter-apply "
                f"(swaps at the held price + emails the customer).</p>")
        elif requested:
            lines.append(f"<p style='background:#fff4e5;padding:8px;border-radius:6px;'>"
                         f"<strong>Could not auto-recognize the request:</strong> "
                         f"{requested.get('detail', '')}</p>")
        lines.append("<p>Order untouched. Follow up with the customer.</p>")
    _send_guarded_email(sub["order_id"], INTERNAL_ALERT_EMAIL,
                        f"Substitution {verdict}: order #{sub['order_id']} "
                        f"({sub['original_sku']} -> {sub['substitute_sku']})",
                        "\n".join(lines), triggered_by="substitution_response")


def _public_view(sub: Dict) -> Dict:
    return {k: sub.get(k) for k in
            ("order_id", "original_sku", "substitute_sku", "quantity",
             "status", "responded_at")}
