"""
claims_routes.py — Replacement Request (claims) form + records
(William 2026-07-29: "I want to use the existing Replacement Request form as
a ref to build a better boot strapped form and we will record the results and
open with a link in the orders app").

Design (the 5696 lesson baked in — no more "BFH 30" mystery SKUs):
  - The form opens from a TOKENIZED per-order link (checkout monthly token),
    so the SKU picker only offers what the customer actually ordered and
    quantities are capped at what they bought.
  - Issue types: freight damage (BOL-noted / NOT noted), manufacturing
    defect, missing item, wrong item. Photos required unless every line is
    a missing item.
  - Submitting records the claim (replacement_requests +
    replacement_request_photos), fires a REPLACEMENT REQUEST alert to
    orders@, and logs a replacement_request_created order event.
  - Admin doors power the orders-app Claims tab: list / detail / photo /
    status.
"""

import json
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response

from auth import require_admin
from checkout import generate_checkout_token, verify_checkout_token
from db_helpers import get_db, get_order_by_id

claims_router = APIRouter(tags=["claims"])

BASE_URL = (os.environ.get("CHECKOUT_BASE_URL", "").strip()
            or "https://cfcorderbackend-sandbox.onrender.com")
INTERNAL_ALERT_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "orders@cabinetsforcontractors.com").strip()

ISSUE_TYPES = {
    "freight_damage_bol": "Freight damage — noted on the BOL",
    "freight_damage_nobol": "Freight damage — NOT noted on the BOL",
    "defect": "Manufacturing defect",
    "missing": "Missing item",
    "wrong_item": "Wrong item received",
}

MAX_PHOTO_BYTES = 8 * 1024 * 1024      # 8 MB per photo
MAX_PHOTOS = 10

_tables_ready = False


def _ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS replacement_requests (
                    id SERIAL PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    contact_name TEXT,
                    contact_email TEXT,
                    contact_phone TEXT,
                    bol_noted TEXT,
                    description TEXT,
                    lines JSONB,
                    status TEXT DEFAULT 'new',
                    status_note TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS replacement_request_photos (
                    id SERIAL PRIMARY KEY,
                    request_id INTEGER NOT NULL,
                    filename TEXT,
                    mime TEXT,
                    content BYTEA,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )""")
            conn.commit()
    _tables_ready = True


def claims_form_url(order_id) -> str:
    """The tokenized per-order claim-form link (monthly token — same
    machinery as confirm-commercial)."""
    tok = generate_checkout_token(str(order_id), long_lived=True)
    return f"{BASE_URL}/claims/{order_id}?token={tok}"


def _order_lines(order_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT sku, product_name, quantity
                           FROM order_line_items WHERE order_id = %s
                           ORDER BY sku""", (str(order_id),))
            return [{"sku": r[0], "name": r[1] or "",
                     "qty": int(float(r[2] or 0))} for r in cur.fetchall()]


# =============================================================================
# PUBLIC FORM
# =============================================================================

_PAGE_CSS = """
body{font-family:'Open Sans',Helvetica,Arial,sans-serif;background:#f4f6f8;
margin:0;color:#333}
.wrap{max-width:680px;margin:24px auto;padding:0 12px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;
padding:26px 28px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
h1{font-size:22px;color:#1a365d;margin:0 0 4px}
.sub{color:#718096;font-size:13px;margin-bottom:18px}
label{display:block;font-weight:600;font-size:13px;margin:14px 0 4px}
input,select,textarea{width:100%;box-sizing:border-box;padding:9px 10px;
border:1px solid #cbd5e0;border-radius:6px;font-size:14px;font-family:inherit}
textarea{min-height:80px}
.row{display:flex;gap:10px}.row>div{flex:1}
.line{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
padding:12px;margin:10px 0}
.btn{display:inline-block;background:#1D4ED8;color:#fff;border:none;
border-radius:8px;padding:12px 28px;font-size:15px;font-weight:700;
cursor:pointer}
.btn2{background:#fff;color:#1D4ED8;border:1px solid #1D4ED8;border-radius:6px;
padding:7px 14px;font-size:13px;font-weight:600;cursor:pointer}
.note{background:#FFFBEA;border:1px solid #F0E0A0;border-radius:8px;
padding:12px 16px;font-size:13px;line-height:1.6;margin:16px 0}
.err{color:#c0392b;font-size:13px;margin-top:8px;display:none}
.del{float:right;background:none;border:none;color:#c0392b;cursor:pointer;
font-size:13px}
"""


def _form_html(order_id: str, token: str, order: dict, lines: list) -> str:
    from email_templates import proper_name
    opts = "".join(
        f'<option value="{l["sku"]}" data-max="{l["qty"]}">'
        f'{l["sku"]} (ordered {l["qty"]})</option>' for l in lines)
    issue_opts = "".join(f'<option value="{k}">{v}</option>'
                         for k, v in ISSUE_TYPES.items())
    name = proper_name(order.get("customer_name") or "")
    email = order.get("email") or ""
    phone = order.get("phone") or ""
    return f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Replacement Request — Order #{order_id}</title>
<style>{_PAGE_CSS}</style></head><body><div class="wrap"><div class="card">
<h1>Replacement Request</h1>
<div class="sub">Order #{order_id} &bull; Cabinets For Contractors</div>
<div class="note">
<strong>Before you file:</strong> freight damage must have been noted on the
delivery receipt (BOL) at the time of delivery &mdash; no exceptions. Damage
that occurs during or after assembly or installation is not claimable: once a
cabinet has been assembled, modified, or installed, it is considered
accepted. Claims must be filed within <strong>48 hours</strong> of delivery
with photos.
</div>
<form id="f" method="post" enctype="multipart/form-data"
      action="{BASE_URL}/claims/{order_id}/submit">
<input type="hidden" name="token" value="{token}">
<input type="hidden" name="lines" id="linesJson">
<div class="row">
<div><label>Your name</label><input name="name" value="{name}" required></div>
<div><label>Phone</label><input name="phone" value="{phone}"></div>
</div>
<label>Email</label><input name="email" type="email" value="{email}" required>
<label>Was the damage noted on the delivery receipt (BOL)?</label>
<select name="bol_noted" required>
<option value="">Choose one…</option>
<option value="yes">Yes — noted on the BOL at delivery</option>
<option value="no">No — it was not noted</option>
<option value="na">Not applicable (missing/wrong item or warehouse pickup)</option>
</select>
<label style="margin-top:20px">Items</label>
<div id="lines"></div>
<button type="button" class="btn2" onclick="addLine()">+ Add an item</button>
<label>Tell us what happened</label>
<textarea name="description" placeholder="What happened, and when was the order delivered?"></textarea>
<label>Photos (required for damage/defect/wrong-item claims — show the item AND its packaging)</label>
<input type="file" name="photos" id="photos" accept="image/*" multiple>
<div class="err" id="err"></div>
<p style="margin-top:22px"><button class="btn" type="submit">Submit request</button></p>
</form></div></div>
<script>
var SKUS={json.dumps({l["sku"]: l["qty"] for l in lines})};
function addLine(){{
var d=document.createElement('div');d.className='line';
d.innerHTML='<button type="button" class="del" onclick="this.parentNode.remove()">remove</button>'+
'<div class="row"><div><label>Item (SKU)</label><select class="lsku">{opts.replace("'", "&#39;")}</select></div>'+
'<div style="max-width:110px"><label>Qty affected</label><input class="lqty" type="number" min="1" value="1"></div></div>'+
'<label>Issue</label><select class="ltype">{issue_opts}</select>'+
'<label>Notes for this item (optional)</label><input class="lnote">';
document.getElementById('lines').appendChild(d);}}
addLine();
document.getElementById('f').addEventListener('submit',function(ev){{
var out=[],bad='';
document.querySelectorAll('#lines .line').forEach(function(d){{
var sku=d.querySelector('.lsku').value,q=parseInt(d.querySelector('.lqty').value||'0');
var max=SKUS[sku]||0;
if(q<1)bad='Quantity must be at least 1.';
if(q>max)bad='Qty for '+sku+' cannot exceed the '+max+' you ordered.';
out.push({{sku:sku,qty:q,issue:d.querySelector('.ltype').value,
note:d.querySelector('.lnote').value}});}});
if(!out.length)bad='Add at least one item.';
var needPhoto=out.some(function(l){{return l.issue!=='missing';}});
if(needPhoto&&!document.getElementById('photos').files.length)
bad='Photos are required for damage, defect, or wrong-item claims.';
if(bad){{ev.preventDefault();var e=document.getElementById('err');
e.textContent=bad;e.style.display='block';return false;}}
document.getElementById('linesJson').value=JSON.stringify(out);}});
</script></body></html>"""


@claims_router.get("/claims/{order_id}", response_class=HTMLResponse)
def claim_form(order_id: str, token: str = ""):
    """The public tokenized claim form for one order."""
    _ensure_tables()
    if not verify_checkout_token(str(order_id), token or ""):
        return HTMLResponse(
            "<h3 style='font-family:Arial;margin:40px'>This claim link has "
            "expired. Please reply to any order email and we will send a "
            "fresh one.</h3>", status_code=403)
    order = get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    lines = _order_lines(order_id)
    if not lines:
        return HTMLResponse(
            "<h3 style='font-family:Arial;margin:40px'>We could not load the "
            "items for this order — please reply to your order email "
            "instead.</h3>", status_code=200)
    return HTMLResponse(_form_html(order_id, token, order, lines))


@claims_router.post("/claims/{order_id}/submit", response_class=HTMLResponse)
async def claim_submit(order_id: str,
                       token: str = Form(...),
                       name: str = Form(""),
                       email: str = Form(""),
                       phone: str = Form(""),
                       bol_noted: str = Form(""),
                       description: str = Form(""),
                       lines: str = Form("[]"),
                       photos: list[UploadFile] = File(None)):
    _ensure_tables()
    if not verify_checkout_token(str(order_id), token or ""):
        raise HTTPException(status_code=403, detail="bad token")
    order = get_order_by_id(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        line_list = json.loads(lines or "[]")
        assert isinstance(line_list, list)
    except Exception:
        raise HTTPException(status_code=400, detail="bad lines payload")

    # server-side re-check of the 5696 rule: only ordered SKUs, capped qty
    ordered = {l["sku"]: l["qty"] for l in _order_lines(order_id)}
    clean = []
    for l in line_list[:40]:
        sku = str(l.get("sku") or "")
        qty = int(l.get("qty") or 0)
        issue = str(l.get("issue") or "")
        if sku not in ordered or issue not in ISSUE_TYPES:
            continue
        clean.append({"sku": sku, "qty": max(1, min(qty, ordered[sku])),
                      "issue": issue, "note": str(l.get("note") or "")[:300]})
    if not clean:
        raise HTTPException(status_code=400,
                            detail="no valid claim lines (SKUs must be on the order)")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO replacement_requests
                (order_id, contact_name, contact_email, contact_phone,
                 bol_noted, description, lines)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (str(order_id), name[:200], email[:200], phone[:50],
                 bol_noted[:20], description[:4000], json.dumps(clean)))
            req_id = cur.fetchone()[0]
            saved_photos = 0
            for f in (photos or [])[:MAX_PHOTOS]:
                try:
                    data = await f.read()
                except Exception:
                    continue
                if not data or len(data) > MAX_PHOTO_BYTES:
                    continue
                cur.execute("""INSERT INTO replacement_request_photos
                    (request_id, filename, mime, content)
                    VALUES (%s,%s,%s,%s)""",
                    (req_id, (f.filename or "photo")[:200],
                     (f.content_type or "image/jpeg")[:100], data))
                saved_photos += 1
            cur.execute("""INSERT INTO order_events
                (order_id, event_type, event_data, source)
                VALUES (%s,'replacement_request_created',%s,'claims')""",
                (str(order_id), json.dumps(
                    {"request_id": req_id, "lines": clean,
                     "bol_noted": bol_noted, "photos": saved_photos})))
            conn.commit()

    # alert (numbers in a table — William's law)
    rows = "".join(
        f"<tr><td style='padding:4px 10px;border-bottom:1px solid #eee'>{l['sku']}</td>"
        f"<td style='padding:4px 10px;border-bottom:1px solid #eee;text-align:right'>{l['qty']}</td>"
        f"<td style='padding:4px 10px;border-bottom:1px solid #eee'>{ISSUE_TYPES[l['issue']]}</td>"
        f"<td style='padding:4px 10px;border-bottom:1px solid #eee'>{l['note']}</td></tr>"
        for l in clean)
    try:
        from supplier_orders import _send_email
        _send_email(str(order_id), INTERNAL_ALERT_EMAIL,
                    f"REPLACEMENT REQUEST - order #{order_id} "
                    f"({len(clean)} item{'s' if len(clean) != 1 else ''})",
                    f"<p>A replacement request was filed for order "
                    f"<strong>#{order_id}</strong> by {name or 'the customer'} "
                    f"({email or 'no email'}, {phone or 'no phone'}).</p>"
                    f"<p><strong>BOL noted:</strong> {bol_noted or '?'} &bull; "
                    f"<strong>Photos:</strong> {saved_photos}</p>"
                    f"<table style='border-collapse:collapse;font-size:13px'>"
                    f"<tr><th style='padding:4px 10px;text-align:left'>SKU</th>"
                    f"<th style='padding:4px 10px;text-align:right'>Qty</th>"
                    f"<th style='padding:4px 10px;text-align:left'>Issue</th>"
                    f"<th style='padding:4px 10px;text-align:left'>Notes</th></tr>{rows}</table>"
                    f"<p><strong>Description:</strong> {description[:1000] or '—'}</p>"
                    f"<p>Review it in the orders app (Claims tab, request "
                    f"#{req_id}).</p>",
                    triggered_by="replacement_request")
    except Exception as e:
        print(f"[CLAIMS] alert failed for {order_id}: {e}")

    return HTMLResponse(
        f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_PAGE_CSS}</style></head><body><div class="wrap"><div class="card">
<h1>Request received &#9989;</h1>
<p style="font-size:14px;line-height:1.7">Thank you — your replacement
request for order #{order_id} is in (reference #{req_id}). A real person
reviews every request; we will get back to you within one business day.</p>
</div></div></body></html>""")


# =============================================================================
# ADMIN DOORS (power the orders-app Claims tab)
# =============================================================================

@claims_router.get("/claims")
def list_claims(status: str = "", order_id: str = "",
                _: bool = Depends(require_admin)):
    _ensure_tables()
    from psycopg2.extras import RealDictCursor
    q = """SELECT r.id, r.order_id, r.contact_name, r.contact_email,
                  r.bol_noted, r.lines, r.status, r.status_note, r.created_at,
                  (SELECT COUNT(*) FROM replacement_request_photos p
                   WHERE p.request_id = r.id) AS photo_count
           FROM replacement_requests r WHERE TRUE"""
    params = []
    if status.strip():
        q += " AND r.status = %s"
        params.append(status.strip())
    if order_id.strip():
        q += " AND r.order_id = %s"
        params.append(order_id.strip())
    q += " ORDER BY r.created_at DESC LIMIT 200"
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, params)
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if isinstance(r.get("lines"), str):
            try:
                r["lines"] = json.loads(r["lines"])
            except Exception:
                pass
        r["created_at"] = str(r["created_at"])
    return {"status": "ok", "count": len(rows), "claims": rows}


@claims_router.get("/claims/{req_id}/detail")
def claim_detail(req_id: int, _: bool = Depends(require_admin)):
    _ensure_tables()
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM replacement_requests WHERE id = %s",
                        (req_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="claim not found")
            row = dict(row)
            cur.execute("""SELECT id, filename, mime,
                                  octet_length(content) AS bytes
                           FROM replacement_request_photos
                           WHERE request_id = %s ORDER BY id""", (req_id,))
            photos = [dict(p) for p in cur.fetchall()]
    if isinstance(row.get("lines"), str):
        try:
            row["lines"] = json.loads(row["lines"])
        except Exception:
            pass
    row["created_at"] = str(row["created_at"])
    row["updated_at"] = str(row["updated_at"])
    row["photos"] = photos
    return {"status": "ok", "claim": row}


@claims_router.get("/claims/photo/{photo_id}")
def claim_photo(photo_id: int, _: bool = Depends(require_admin)):
    _ensure_tables()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT mime, content FROM replacement_request_photos
                           WHERE id = %s""", (photo_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="photo not found")
    return Response(content=bytes(row[1]), media_type=row[0] or "image/jpeg")


@claims_router.patch("/claims/{req_id}/status")
def claim_status(req_id: int, status: str, note: str = "",
                 _: bool = Depends(require_admin)):
    _ensure_tables()
    valid = ["new", "reviewing", "approved", "denied", "resolved"]
    if status not in valid:
        raise HTTPException(status_code=400,
                            detail=f"status must be one of {valid}")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE replacement_requests
                           SET status = %s, status_note = %s,
                               updated_at = NOW()
                           WHERE id = %s RETURNING order_id""",
                        (status, note[:500], req_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="claim not found")
            cur.execute("""INSERT INTO order_events
                (order_id, event_type, event_data, source)
                VALUES (%s,'replacement_request_status',%s,'claims')""",
                (row[0], json.dumps({"request_id": req_id, "status": status,
                                     "note": note[:500]})))
            conn.commit()
    return {"status": "ok", "request_id": req_id, "new_status": status}


@claims_router.get("/orders/{order_id}/claims-link")
def order_claims_link(order_id: str, _: bool = Depends(require_admin)):
    """The tokenized claim-form URL for one order [admin] — the orders-app
    'open claim form' button."""
    if not get_order_by_id(order_id):
        raise HTTPException(status_code=404, detail="Order not found")
    return {"status": "ok", "order_id": order_id,
            "url": claims_form_url(order_id)}
