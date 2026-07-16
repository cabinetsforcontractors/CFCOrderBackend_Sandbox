"""
estimate_verifier.py
Auto-verify on reply — the loop-closer for the auto-ordering lane.

Watches Gmail for supplier reply documents (estimates / sales orders /
quotes), runs the validated per-supplier parsers, diffs against what we sent,
and flips the supplier_orders state row:

    clean diff        -> status 'confirmed' (quiet — shows in the digest)
    anything off      -> status 'discrepancy' + alert email to William

Document routing (content-marker based, NOT sender based — in the beta all
test emails come from the safety inbox, and markers are what the documents
actually are):
    PDF attachment markers (freight_routes._detect_pdf_supplier):
        GHI Sales Order, Milestone QUOFL, C&S Quotation, LI Estimate/Invoice
    HTML body "#SO#####" + NetSuite markers -> DuraStone (also feeds the
        revision tripwire history via supplier_doc_parser).

Diff space matches the /freight/verify-order endpoint: GHI + DS in
website-SKU space, LI/LM/C&S in body space (their line-code maps are still
pending William).

Every processed Gmail message is recorded in supplier_reply_scans so the
periodic scan is idempotent; the manual endpoint can force-reprocess.
"""

import base64
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db_helpers import get_db

INTERNAL_ALERT_EMAIL = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL",
                                      "cabinetsforcontractors@gmail.com").strip()

# Gmail search for candidate reply documents (content markers do the real
# routing; this just narrows the scan volume)
CANDIDATE_QUERY = ('has:attachment filename:pdf '
                   '(ghicabinets OR QUOFL OR "Quotation_S" OR Estimate_ OR '
                   'Invoice_ OR milestonecabinetry OR cabinetstone OR '
                   '"Cabinetry Distribution" OR "Sales Order")')


# =============================================================================
# TABLE
# =============================================================================

def ensure_scan_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supplier_reply_scans (
                id SERIAL PRIMARY KEY,
                message_id VARCHAR(120) UNIQUE NOT NULL,
                order_id VARCHAR(20),
                supplier VARCHAR(50),
                doc_ref VARCHAR(100),
                verdict VARCHAR(30),
                report_json TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        conn.commit()


def _already_scanned(message_id: str) -> bool:
    with get_db() as conn:
        ensure_scan_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM supplier_reply_scans WHERE message_id = %s",
                        (message_id,))
            return cur.fetchone() is not None


def _record_scan(message_id: str, order_id, supplier, doc_ref, verdict, report):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO supplier_reply_scans
                    (message_id, order_id, supplier, doc_ref, verdict, report_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (message_id) DO UPDATE SET
                    order_id = EXCLUDED.order_id, supplier = EXCLUDED.supplier,
                    doc_ref = EXCLUDED.doc_ref, verdict = EXCLUDED.verdict,
                    report_json = EXCLUDED.report_json
            """, (message_id, order_id, supplier, doc_ref, verdict,
                  json.dumps(report, default=str)[:6000] if report else None))
            conn.commit()


# =============================================================================
# GMAIL HELPERS
# =============================================================================

def fetch_message_full(message_id: str) -> Optional[Dict]:
    """Full message: headers, html body, and PDF attachments (bytes)."""
    from gmail_sync import gmail_api_request

    data = gmail_api_request(f"messages/{message_id}", {"format": "full"})
    if not data:
        return None
    payload = data.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    html = None
    attachments = []

    def walk(part):
        nonlocal html
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        fname = part.get("filename") or ""
        if fname.lower().endswith(".pdf") and body.get("attachmentId"):
            att = gmail_api_request(
                f"messages/{message_id}/attachments/{body['attachmentId']}")
            if att and att.get("data"):
                attachments.append({
                    "filename": fname,
                    "data": base64.urlsafe_b64decode(att["data"])})
        elif mime == "text/html" and body.get("data") and html is None:
            html = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="ignore")
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    return {"id": message_id, "threadId": data.get("threadId"),
            "subject": headers.get("subject", ""), "from": headers.get("from", ""),
            "date": headers.get("date", ""), "html": html,
            "attachments": attachments}


# =============================================================================
# VERIFICATION CORE (mirrors /freight/verify-order)
# =============================================================================

def verify_pdf(order_id: str, data: bytes, supplier: str) -> Dict:
    """Diff a parsed supplier PDF against the sent order. Returns
    {'supplier','doc_ref','report','line_count'}."""
    import supplier_doc_parser as sdp
    from freight_routes import _VERIFY_SUPPLIERS, _order_lines_for_supplier
    from psycopg2.extras import RealDictCursor

    prefixes, aliases = _VERIFY_SUPPLIERS[supplier]
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sent = _order_lines_for_supplier(cur, order_id, prefixes, aliases)
        if supplier == "GHI":
            rev = sdp.build_reverse_map(conn)
            parsed = sdp.resolve_ghi_lines(sdp.parse_ghi_pdf(data), rev)
            report = sdp.two_sided_diff(
                sdp.expand_composites(sent, sdp.GHI_COMPOSITES), parsed["lines"])
            doc_ref = parsed.get("so_number")
        elif supplier == "Love-Milestone":
            parsed = sdp.parse_lm_quote_pdf(data)
            folded = [f for f in sdp.fold_lm_lines(parsed["lines"]) if not f.get("fee")]
            report = sdp.body_space_diff(sent, folded)
            doc_ref = parsed.get("quote_number")
        elif supplier == "Cabinet & Stone":
            parsed = sdp.parse_cs_quotation_pdf(data)
            report = sdp.body_space_diff(sent, sdp.fold_cs_lines(parsed["lines"]))
            doc_ref = parsed.get("quote_number")
        else:  # LI
            parsed = sdp.parse_li_estimate_pdf(data)
            report = sdp.body_space_diff(sent, sdp.fold_li_lines(parsed["lines"]))
            doc_ref = parsed.get("estimate_number")
    return {"supplier": supplier, "doc_ref": doc_ref, "po": parsed.get("po"),
            "report": report, "line_count": len(parsed.get("lines") or []),
            "sent_count": len(sent)}


def verify_ds_html(order_id: str, html: str) -> Dict:
    import supplier_doc_parser as sdp
    from freight_routes import _VERIFY_SUPPLIERS, _order_lines_for_supplier
    from psycopg2.extras import RealDictCursor

    prefixes, aliases = _VERIFY_SUPPLIERS["DuraStone"]
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sent = _order_lines_for_supplier(cur, order_id, prefixes, aliases)
        rev = sdp.build_reverse_map(conn)
        parsed = sdp.resolve_durastone_lines(sdp.parse_durastone_email(html), rev)
        report = sdp.two_sided_diff(sent, parsed["lines"])
    return {"supplier": "DuraStone", "doc_ref": parsed.get("so_number"),
            "po": parsed.get("po"), "report": report,
            "line_count": len(parsed.get("lines") or []), "sent_count": len(sent)}


def _apply_verdict(order_id: str, supplier: str, verdict_ok: bool,
                   doc_ref: str, report: Dict, message_id: str) -> str:
    """Flip (or create) the supplier_orders row and alert on discrepancy."""
    from supplier_orders import ensure_supplier_orders_table, _send_email

    status = "confirmed" if verdict_ok else "discrepancy"
    with get_db() as conn:
        ensure_supplier_orders_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO supplier_orders
                    (order_id, warehouse, status, supplier_doc_ref,
                     discrepancy_json, confirmed_at, note, updated_at)
                VALUES (%s, %s, %s, %s, %s,
                        CASE WHEN %s = 'confirmed' THEN NOW() END,
                        %s, NOW())
                ON CONFLICT (order_id, warehouse) DO UPDATE SET
                    status = EXCLUDED.status,
                    supplier_doc_ref = COALESCE(EXCLUDED.supplier_doc_ref,
                                                supplier_orders.supplier_doc_ref),
                    discrepancy_json = EXCLUDED.discrepancy_json,
                    confirmed_at = CASE WHEN EXCLUDED.status = 'confirmed'
                                        THEN NOW() ELSE supplier_orders.confirmed_at END,
                    note = EXCLUDED.note,
                    updated_at = NOW()
            """, (order_id, supplier, status, doc_ref,
                  None if verdict_ok else json.dumps(report, default=str)[:6000],
                  status, f"auto-verified from Gmail msg {message_id}"))
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'supplier_reply_verified', %s, 'estimate_verifier')
            """, (order_id, json.dumps({
                "supplier": supplier, "doc_ref": doc_ref, "verdict": status,
                "message_id": message_id,
                "summary": {k: len(report.get(k) or [])
                            for k in ("matched", "qty_mismatch", "missing_at_supplier",
                                      "unexpected_from_supplier",
                                      "unresolved_supplier_lines", "flags")},
            }, default=str)))
            conn.commit()

    if not verdict_ok:
        r = report
        parts = [f"<p><strong>DISCREPANCY</strong> — order #{order_id}, "
                 f"{supplier}, doc {doc_ref or '?'}</p>"]
        for key, label in (("qty_mismatch", "Qty mismatches"),
                           ("missing_at_supplier", "We sent, they didn't confirm"),
                           ("unexpected_from_supplier", "They list, we didn't send"),
                           ("unresolved_supplier_lines", "Unresolved lines"),
                           ("flags", "Flags")):
            vals = r.get(key) or []
            if vals:
                shown = json.dumps(vals[:6], default=str)
                parts.append(f"<p><strong>{label} ({len(vals)}):</strong> {shown}</p>")
        if r.get("possible_substitutions"):
            parts.append(f"<p><strong>Probable dialect pairs:</strong> "
                         f"{json.dumps(r['possible_substitutions'][:6], default=str)}</p>")
        parts.append(f"<p>Full detail: GET /supplier-orders?order_id={order_id} "
                     f"(discrepancy_json).</p>")
        _send_email(order_id, INTERNAL_ALERT_EMAIL,
                    f"DISCREPANCY: {supplier} doc for order #{order_id}",
                    "\n".join(parts), triggered_by="estimate_verifier")
    return status


# =============================================================================
# MESSAGE PROCESSING
# =============================================================================

def process_message(message_id: str, force: bool = False) -> Dict:
    """Detect + parse + verify one Gmail message. Idempotent unless force."""
    if not force and _already_scanned(message_id):
        return {"status": "already_scanned", "message_id": message_id}

    msg = fetch_message_full(message_id)
    if not msg:
        return {"status": "error", "message": "could not fetch message"}

    results = []
    # PDF attachments -> marker-based supplier detection
    from freight_routes import _detect_pdf_supplier
    for att in msg["attachments"]:
        sup = _detect_pdf_supplier(att["data"])
        if not sup:
            continue
        try:
            v = verify_pdf_from_doc(att["data"], sup)
        except Exception as e:
            results.append({"attachment": att["filename"], "supplier": sup,
                            "error": str(e)})
            continue
        results.append(v)

    # DuraStone NetSuite HTML body
    if msg.get("html") and "#SO" in msg["html"] and "netsuite" in msg["html"].lower():
        try:
            import supplier_doc_parser as sdp
            sdp_parsed = sdp.parse_durastone_email(msg["html"])
            if sdp_parsed.get("so_number") and sdp_parsed.get("po"):
                # feed the revision tripwire history too
                with get_db() as conn:
                    try:
                        sdp.record_and_check_ds_email(conn, message_id, msg["html"])
                    except Exception:
                        pass
                v = verify_ds_html(sdp_parsed["po"], msg["html"])
                results.append(v)
        except Exception as e:
            results.append({"supplier": "DuraStone", "error": str(e)})

    processed = []
    for v in results:
        if v.get("error") or not v.get("po"):
            processed.append(v)
            continue
        order_id = str(v["po"]).lstrip("0")
        # PO like '5694A' -> strip trailing letters for the order key
        order_key = "".join(c for c in order_id if c.isdigit()) or order_id
        status = _apply_verdict(order_key, v["supplier"],
                                bool(v["report"].get("ok")), v.get("doc_ref"),
                                v["report"], message_id)
        v["verdict"] = status
        v["order_id"] = order_key
        processed.append({k: v[k] for k in
                          ("supplier", "doc_ref", "order_id", "verdict",
                           "line_count", "sent_count")})
        _record_scan(message_id, order_key, v["supplier"], v.get("doc_ref"),
                     status, v["report"])

    if not processed:
        _record_scan(message_id, None, None, None, "no_document", None)
        return {"status": "no_document", "message_id": message_id,
                "subject": msg["subject"], "attachments":
                [a["filename"] for a in msg["attachments"]]}
    return {"status": "ok", "message_id": message_id, "subject": msg["subject"],
            "results": processed}


def verify_pdf_from_doc(data: bytes, supplier: str) -> Dict:
    """Parse the PDF first to learn its PO, then verify against that order."""
    import supplier_doc_parser as sdp
    if supplier == "GHI":
        parsed = sdp.parse_ghi_pdf(data)
    elif supplier == "Love-Milestone":
        parsed = sdp.parse_lm_quote_pdf(data)
    elif supplier == "Cabinet & Stone":
        parsed = sdp.parse_cs_quotation_pdf(data)
    else:
        parsed = sdp.parse_li_estimate_pdf(data)
    po = parsed.get("po")
    if not po:
        return {"supplier": supplier, "error": "no PO found in document",
                "doc_ref": parsed.get("so_number") or parsed.get("quote_number")
                or parsed.get("estimate_number")}
    order_key = "".join(c for c in str(po) if c.isdigit())
    return verify_pdf(order_key, data, supplier)


def scan_replies(hours_back: int = 24) -> Dict:
    """Periodic Gmail scan (wired into gmail_sync + manual endpoint)."""
    from gmail_sync import gmail_configured, search_emails

    if not gmail_configured():
        return {"status": "skipped", "reason": "gmail_not_configured",
                "processed": 0, "discrepancies": 0, "errors": []}
    out = {"status": "ok", "checked": 0, "processed": 0, "confirmed": 0,
           "discrepancies": 0, "errors": [], "details": []}
    try:
        messages = search_emails(f"newer_than:{int(hours_back)}h {CANDIDATE_QUERY}")
    except Exception as e:
        return {"status": "error", "processed": 0, "discrepancies": 0,
                "errors": [f"search: {e}"]}
    for m in messages:
        out["checked"] += 1
        try:
            res = process_message(m["id"])
            if res.get("status") == "ok":
                out["processed"] += 1
                for r in res.get("results", []):
                    if r.get("verdict") == "confirmed":
                        out["confirmed"] += 1
                    elif r.get("verdict") == "discrepancy":
                        out["discrepancies"] += 1
                out["details"].append(res)
        except Exception as e:
            out["errors"].append(f"{m.get('id')}: {e}")
    return out
