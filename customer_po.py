"""
customer_po.py — Wave 3 build K (William 2026-08-02): INBOUND CUSTOMER-PO
READER.

Nationwide/UFP email us THEIR purchase orders as PDFs (Jenny Buysse,
"NEW PO - CABINETS FOR CONTRACTORS - MARTINSVILLE PO #..."). Those rode
pure hand-work. Now the ledger cycle spots them, parses the PDF with the
UFP grammar (decoded from the real PO_0398138917.pdf sample), maps their
SKUs toward ours, and rings ONE bell with the parsed table.

NEVER AUTO-CREATES AN ORDER. The prepare door returns the would-be
B2BWave payload for review (dry-run only in v1); creating the real
order stays William's hand until he arms it with a customer id and the
word.

SKU mapping: UFP writes our catalog SKUs under their color prefix
(BLK-B18 = black B18). CUSTOMER_SKU_PREFIX_MAP holds the translations —
BLK -> NBLK is the standing guess and is VALIDATED against rta_products
before it's shown; unmatched lines are listed plainly, never guessed
silently.
"""

import json
import os
import re
from typing import Dict, List

from db_helpers import get_db

INTERNAL_ALERT_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "orders@cabinetsforcontractors.com").strip()

# sender domain -> customer key
CUSTOMER_PO_SENDERS = {
    "ufpdllc.com": "UFP",
    "nationwidecustomhomes.com": "Nationwide",
}

# (customer, their prefix) -> our prefix. BLK->NBLK CONFIRMED by William
# 2026-08-02: NBLK = LI's line, Li calls it "black shaker"; BLK is UFP's
# own notation, not a CFC prefix. Every mapped SKU is still validated
# against rta_products.
CUSTOMER_SKU_PREFIX_MAP = {
    ("UFP", "BLK"): "NBLK",
}

# B2BWave customer account per PO sender. 1397 = Nationwide Custom Homes
# (Dominic, price list .3675-UFP) — VERIFIED in William's customer export
# 2026-08-02. Env override wins.
CUSTOMER_B2BWAVE_IDS = {
    "UFP": int(os.environ.get("UFP_B2BWAVE_CUSTOMER_ID", "1397") or 1397),
    "Nationwide": int(os.environ.get("UFP_B2BWAVE_CUSTOMER_ID", "1397") or 1397),
}

_ORDER_NO_RE = re.compile(r"Order No\.?:\s*\n?(\d+)")
_CUSTPO_RE = re.compile(r"DropShip CustPO:\s*\n?([\w-]+)")
_PROMISED_RE = re.compile(r"PROMISED ON[\s\S]{0,200}?(\d{1,2}/\d{1,2}/\d{4})")
_SHIPTO_RE = re.compile(r"SHIP TO:\s*\n([\s\S]{0,400}?)\nFOB POINT")
# "1\nBLK-W362424: WALL REFRIGERATOR ... - \n2D - 1S - BLACK\nEA\n1\n160.24800\n160.25"
_LINE_RE = re.compile(
    r"\n(\d{1,3})\n([A-Z0-9]+-[A-Z0-9-]+):\s*([\s\S]*?)\nEA\n(\d+)\n"
    r"([\d.]+)\n([\d.]+)")


def parse_ufp_po(text: str) -> Dict:
    """The UFP PO grammar (3-page PDFs repeat their header per page —
    lines are deduped by line number)."""
    out = {"po_number": None, "dropship_custpo": None, "promised_on": None,
           "ship_to_raw": None, "lines": []}
    m = _ORDER_NO_RE.search(text)
    if m:
        out["po_number"] = m.group(1)
    m = _CUSTPO_RE.search(text)
    if m:
        out["dropship_custpo"] = m.group(1)
    m = _PROMISED_RE.search(text)
    if m:
        out["promised_on"] = m.group(1)
    m = _SHIPTO_RE.search(text)
    if m:
        out["ship_to_raw"] = m.group(1).strip()
    seen = set()
    for ln, sku, desc, qty, unit, ext in _LINE_RE.findall(text):
        if ln in seen:
            continue
        seen.add(ln)
        out["lines"].append({
            "line_no": int(ln), "their_sku": sku.strip(),
            "description": " ".join(desc.split())[:200],
            "qty": int(qty), "unit_price": float(unit),
            "ext_price": float(ext)})
    out["lines"].sort(key=lambda x: x["line_no"])
    return out


def map_lines(customer: str, lines: List[Dict]) -> Dict:
    """Translate their SKUs toward ours + validate against rta_products."""
    mapped, unmatched = [], []
    candidates = {}
    for ln in lines:
        their = ln["their_sku"]
        pre, _, rest = their.partition("-")
        ours_pre = CUSTOMER_SKU_PREFIX_MAP.get((customer, pre.upper()))
        candidates[their] = f"{ours_pre}-{rest}" if ours_pre and rest else None
    known = set()
    reals = [c for c in candidates.values() if c]
    if reals:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT product_sku FROM rta_products
                                   WHERE UPPER(product_sku) = ANY(%s)""",
                                ([c.upper() for c in reals],))
                    known = {r[0].upper() for r in cur.fetchall()}
        except Exception:
            pass
    for ln in lines:
        cand = candidates.get(ln["their_sku"])
        if cand and cand.upper() in known:
            mapped.append({**ln, "our_sku": cand})
        else:
            unmatched.append({**ln, "tried": cand})
    return {"mapped": mapped, "unmatched": unmatched}


def _ensure_seen_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_po_seen (
                message_id VARCHAR(120) PRIMARY KEY,
                customer VARCHAR(40),
                po_number VARCHAR(40),
                parsed TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        conn.commit()


def _pdf_texts(message_id: str) -> List[Dict]:
    from estimate_verifier import fetch_message_full
    msg = fetch_message_full(message_id) or {}
    out = []
    for att in msg.get("attachments") or []:
        name = (att.get("filename") or "").lower()
        if not name.endswith(".pdf"):
            continue
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(att["data"]))
            out.append({"filename": att.get("filename"),
                        "text": "\n".join((p.extract_text() or "")
                                          for p in reader.pages)})
        except Exception as e:
            out.append({"filename": att.get("filename"), "error": str(e)})
    return out


def prepare_from_message(message_id: str) -> Dict:
    """Parse + map one customer-PO message; returns the review payload.
    DRY-RUN BY DESIGN: v1 never creates the order."""
    texts = _pdf_texts(message_id)
    if not texts:
        return {"status": "error", "message": "no PDF attachments found"}
    results = []
    for t in texts:
        if t.get("error"):
            results.append(t)
            continue
        parsed = parse_ufp_po(t["text"])
        mapping = map_lines("UFP", parsed["lines"])
        results.append({
            "filename": t["filename"], "parsed": parsed,
            "mapped": mapping["mapped"], "unmatched": mapping["unmatched"],
            "would_be_order": {
                "customer_id": os.environ.get("UFP_B2BWAVE_CUSTOMER_ID") or
                               "(env UFP_B2BWAVE_CUSTOMER_ID unset)",
                "comments": (f"UFP PO {parsed['po_number']} / DropShip "
                             f"CustPO {parsed['dropship_custpo']} / promised "
                             f"{parsed['promised_on']}"),
                "products": [{"sku": m["our_sku"], "quantity": m["qty"]}
                             for m in mapping["mapped"]],
            }})
    return {"status": "ok", "message_id": message_id, "documents": results,
            "armed": False,
            "note": "v1 is review-only; creating the B2BWave order stays a "
                    "human step"}


def create_order_from_message(message_id: str, dry_run: bool = True) -> Dict:
    """ARMED 2026-08-02 (William's word + customer id 1397 verified in his
    export): create the real B2BWave order from a parsed customer PO.
    Refuses while ANY line is unmatched (resolve those first — the
    mismatch draft to the sender exists for exactly that). dry_run=true
    (default) returns the exact payload; dry_run=false creates the order
    and VERIFIES BY READBACK. Respects B2BWAVE_MUTATIONS_ENABLED."""
    prepared = prepare_from_message(message_id)
    if prepared.get("status") != "ok":
        return prepared
    doc = (prepared.get("documents") or [{}])[0]
    parsed = doc.get("parsed") or {}
    if doc.get("unmatched"):
        return {"status": "error",
                "message": (f"{len(doc['unmatched'])} line(s) unmatched — "
                            f"resolve with the sender first (a draft is "
                            f"waiting), then re-fire"),
                "unmatched": doc["unmatched"]}
    mapped = doc.get("mapped") or []
    if not mapped:
        return {"status": "error", "message": "no mapped lines"}

    customer = "UFP"
    cid = CUSTOMER_B2BWAVE_IDS.get(customer, 1397)
    from substitutions import _b2b, fetch_b2b_order, fetch_b2b_product
    products = []
    missing = []
    for m in mapped:
        prod = fetch_b2b_product(m["our_sku"])
        if not prod:
            missing.append(m["our_sku"])
            continue
        products.append({"product_id": prod["id"], "quantity": m["qty"],
                         "sku": m["our_sku"]})
    if missing:
        return {"status": "error",
                "message": f"not on B2BWave: {', '.join(missing[:6])}"}

    comments = (f"UFP PO {parsed.get('po_number')} / DropShip CustPO "
                f"{parsed.get('dropship_custpo')} / promised "
                f"{parsed.get('promised_on')} — entered by the robot from "
                f"the emailed PO PDF")
    payload = {"order": {
        "customer_id": cid,
        "comments": comments,
        "prevent_emails": 1,
        "order_products": [{"product_id": p["product_id"],
                            "quantity": p["quantity"]} for p in products],
    }}
    if dry_run:
        return {"status": "dry_run", "customer_id": cid,
                "lines": len(products), "payload": payload}
    if os.environ.get("B2BWAVE_MUTATIONS_ENABLED", "true").lower() == "false":
        return {"status": "error",
                "message": "B2BWAVE_MUTATIONS_ENABLED=false"}

    st, resp = _b2b("POST", "orders", payload)
    new_id = None
    if isinstance(resp, dict):
        new_id = (resp.get("id") or (resp.get("order") or {}).get("id"))
    # VERIFY BY READBACK (the standing law): the created order must exist
    # and carry the right line count
    verified = False
    if new_id:
        check = fetch_b2b_order(str(new_id))
        if check:
            got = len((check.get("order_products") or
                       check.get("products") or []))
            verified = got == len(products)
    result = {"status": "ok" if verified else "needs_human",
              "http": st, "new_order_id": new_id, "verified": verified,
              "lines_sent": len(products)}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'customer_po_order_created', %s, 'customer_po')
            """, (str(new_id or ""), json.dumps(
                {"message_id": message_id, "ufp_po": parsed.get("po_number"),
                 "customer_id": cid, "lines": len(products),
                 "verified": verified, "http": st}, default=str)))
            conn.commit()
    from supplier_orders import _send_email
    _send_email(str(new_id or ""), INTERNAL_ALERT_EMAIL,
                f"CUSTOMER-PO ORDER {'CREATED' if verified else 'NEEDS A LOOK'}"
                f" - {customer} PO {parsed.get('po_number')} -> "
                f"order #{new_id}",
                f"<p>The robot entered {customer} PO "
                f"{parsed.get('po_number')} on B2BWave as order "
                f"#{new_id} ({len(products)} lines, customer {cid}). "
                f"Readback verified: {verified}.</p>",
                triggered_by="customer_po_create")
    return result


def process_customer_po_scan(hours_back: int = 48,
                             dry_run: bool = False) -> Dict:
    """Rides the ledger cycle: fresh inbound mail from customer-PO domains
    with 'PO' in the subject -> parse + one alert. Idempotent per message."""
    out = {"scanned": 0, "detected": [], "alerted": 0, "already": 0,
           "dry_run": dry_run, "errors": []}
    dom_re = "|".join(re.escape(d) for d in CUSTOMER_PO_SENDERS)
    with get_db() as conn:
        _ensure_seen_table(conn)
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT message_id, subject, from_addr FROM email_ledger
                WHERE folder = 'inbox'
                  AND from_addr ~* %s
                  AND subject ~* '\\mPO\\M'
                  AND email_date > NOW() - (%s || ' hours')::interval
                LIMIT 50
            """, (dom_re, int(hours_back)))
            rows = cur.fetchall()
        for mid, subject, from_addr in rows:
            out["scanned"] += 1
            try:
                with conn.cursor() as cur:
                    cur.execute("""SELECT 1 FROM customer_po_seen
                                   WHERE message_id = %s""", (mid,))
                    if cur.fetchone():
                        out["already"] += 1
                        continue
                customer = next((c for d, c in CUSTOMER_PO_SENDERS.items()
                                 if d in (from_addr or "").lower()), "?")
                prepared = prepare_from_message(mid)
                doc = (prepared.get("documents") or [{}])[0]
                parsed = doc.get("parsed") or {}
                hit = {"message_id": mid, "customer": customer,
                       "subject": subject,
                       "po_number": parsed.get("po_number"),
                       "lines": len(parsed.get("lines") or []),
                       "mapped": len(doc.get("mapped") or []),
                       "unmatched": len(doc.get("unmatched") or [])}
                out["detected"].append(hit)
                if dry_run:
                    continue
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO customer_po_seen
                            (message_id, customer, po_number, parsed)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (message_id) DO NOTHING
                    """, (mid, customer, parsed.get("po_number"),
                          json.dumps(doc, default=str)[:20000]))
                    conn.commit()
                _alert(customer, hit, doc)
                out["alerted"] += 1
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["errors"].append(f"{mid}: {e}")
    return out


def _alert(customer: str, hit: Dict, doc: Dict):
    from supplier_orders import _send_email
    parsed = doc.get("parsed") or {}
    rows = "".join(
        f"<tr><td style='padding:3px 8px;'>{m['qty']}</td>"
        f"<td style='padding:3px 8px;'>{m['their_sku']}</td>"
        f"<td style='padding:3px 8px;'><strong>{m.get('our_sku', '?')}</strong></td>"
        f"<td style='padding:3px 8px;'>${m['ext_price']:,.2f}</td></tr>"
        for m in (doc.get("mapped") or []))
    un = "".join(
        f"<li>{u['qty']}x {u['their_sku']} (tried {u.get('tried')})</li>"
        for u in (doc.get("unmatched") or []))
    html = (
        f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
        f"<p><strong>{customer} sent a purchase order: "
        f"PO {parsed.get('po_number')}</strong> (DropShip CustPO "
        f"{parsed.get('dropship_custpo')}, promised "
        f"{parsed.get('promised_on')}).</p>"
        f"<table style='border-collapse:collapse;font-size:13px;'>"
        f"<tr style='background:#f2f2f2;'><th style='padding:3px 8px;'>Qty</th>"
        f"<th style='padding:3px 8px;'>Their SKU</th>"
        f"<th style='padding:3px 8px;'>Ours</th>"
        f"<th style='padding:3px 8px;'>Ext</th></tr>{rows}</table>"
        f"{f'<p>UNMATCHED lines (need a human):</p><ul>{un}</ul>' if un else ''}"
        f"<p>Review payload: <code>POST /customer-po/prepare/"
        f"{hit['message_id']}</code> — creating the order stays your hand.</p>"
        f"</div>")
    _send_email("", INTERNAL_ALERT_EMAIL,
                f"CUSTOMER PO IN - {customer} PO {parsed.get('po_number')} "
                f"({hit['mapped']} mapped, {hit['unmatched']} unmatched)",
                html, triggered_by="customer_po")

    # MISMATCH DRAFT (William 2026-08-02): unmatched lines get a ready
    # DRAFT back to the sender — William clears it internally first, then
    # fires the draft himself.
    if doc.get("unmatched"):
        try:
            import re as _re
            from email_sender import create_gmail_draft
            m = _re.search(r"[\w.+-]+@[\w.-]+", hit.get("from") or "")
            sender = m.group(0) if m else None
            if sender:
                un_rows = "".join(
                    f"<li>{u['qty']}x <strong>{u['their_sku']}</strong> — "
                    f"{u.get('description', '')}</li>"
                    for u in doc["unmatched"])
                subj = hit.get("subject") or f"PO {parsed.get('po_number')}"
                if not subj.upper().startswith("RE"):
                    subj = f"RE: {subj}"
                create_gmail_draft(
                    sender, subj,
                    f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                    f"<p>Hey There,</p>"
                    f"<p>Quick question on PO {parsed.get('po_number')} — "
                    f"we want to make sure we get these exactly right. Could "
                    f"you confirm the following item(s)?</p>"
                    f"<ul>{un_rows}</ul>"
                    f"<p>Everything else on the PO is clear and in process.</p>"
                    f"<p>Thank you,<br>--<br>William Prince<br>"
                    f"Cabinets For Contractors<br>(770) 990-4885</p></div>")
        except Exception as e:
            print(f"[CUSTOMER-PO] mismatch draft failed: {e}")
