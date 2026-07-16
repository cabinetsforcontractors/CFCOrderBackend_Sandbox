"""
estimate_verifier.py
Auto-verify on reply — the loop-closer for the auto-ordering lane.

Watches Gmail for supplier reply documents (estimates / sales orders /
quotes / confirmations), runs the validated per-supplier parsers, diffs
against what we sent, and flips the supplier_orders state row:

    clean diff        -> status 'confirmed' (quiet — shows in the digest)
    anything off      -> status 'discrepancy' + alert email to William
                         (human-readable tables, never raw JSON)

Document routing (content-marker based, NOT sender based — in the beta all
test emails come from the safety inbox, and markers are what the documents
actually are):
    PDF attachment markers (freight_routes._detect_pdf_supplier):
        GHI Sales Order, Milestone QUOFL, C&S Quotation, LI Estimate/Invoice
    HTML body "#SO#####" + NetSuite markers -> DuraStone (also feeds the
        revision tripwire history via supplier_doc_parser).
    HTML body ROC markers ("Roc Cabinetry" + "SKU:") -> ROC confirmation /
        invoice (roc_parser; carries our PO in "PO Number#"). A-* companion
        lines (free trays/assembly) are excluded from the diff.

Diff space matches the /freight/verify-order endpoint: GHI + DS in
website-SKU space, LI/LM/C&S/ROC in body space.

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

# Gmail searches for candidate reply documents (content markers do the real
# routing; these just narrow the scan volume)
CANDIDATE_QUERY_PDF = ('has:attachment filename:pdf '
                       '(ghicabinets OR QUOFL OR "Quotation_S" OR Estimate_ OR '
                       'Invoice_ OR milestonecabinetry OR cabinetstone OR '
                       '"Cabinetry Distribution" OR "Sales Order")')
CANDIDATE_QUERY_HTML = ('(from:roccabinetry.com OR from:sent-via.netsuite.com) '
                        '("order confirmation" OR "Invoice" OR "Sales Order")')


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


def verify_roc_html(order_id: str, html: str) -> Dict:
    """ROC confirmation/invoice HTML -> body-space diff vs the sent ROC lines.
    A-* companion lines (free trays/assembly) are excluded from the diff and
    reported under 'companions'."""
    import supplier_doc_parser as sdp
    from freight_routes import _order_lines_for_supplier
    from roc_parser import fold_roc_lines, parse_roc_confirmation_html
    from psycopg2.extras import RealDictCursor

    parsed = parse_roc_confirmation_html(html)
    folded = fold_roc_lines(parsed["lines"])
    companions = [f for f in folded if f.get("fee")]
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sent = _order_lines_for_supplier(cur, order_id, (),
                                             ("ROC", "ROC Cabinetry"))
        report = sdp.body_space_diff(sent, [f for f in folded if not f.get("fee")])
    if companions:
        report["companions"] = [{"sku": c.get("raw") or c.get("body"),
                                 "qty": c.get("qty")} for c in companions]
    return {"supplier": "ROC", "doc_ref": parsed.get("roc_order_number"),
            "po": parsed.get("po"), "report": report,
            "line_count": len(parsed.get("lines") or []), "sent_count": len(sent)}


# =============================================================================
# DISCREPANCY ALERT (human-readable — never raw JSON)
# =============================================================================

_TD = "padding:4px 12px;border-bottom:1px solid #eee;"
_TH = "padding:4px 12px;background:#f2f2f2;text-align:left;"


def _tbl(rows, headers) -> str:
    th = "".join(f"<th style='{_TH}'>{h}</th>" for h in headers)
    trs = "".join(
        "<tr>" + "".join(f"<td style='{_TD}'>{c}</td>" for c in row) + "</tr>"
        for row in rows)
    return (f"<table style='border-collapse:collapse;font-size:14px;"
            f"margin:4px 0 14px 0;'><tr>{th}</tr>{trs}</table>")


def _clean_sku(entry) -> str:
    """Display label: unexpected body labels like 'SNWVSD42/VSD42' -> 'VSD42'."""
    s = str(entry.get("sku") or entry.get("body") or "?")
    if "/" in s:
        s = min(s.split("/"), key=len)
    return s


def _more(vals, cap=10) -> str:
    return (f"<p style='color:#888;margin:-8px 0 12px 0;'>"
            f"...and {len(vals) - cap} more</p>" if len(vals) > cap else "")


def _discrepancy_email_html(order_id: str, supplier: str, doc_ref: str,
                            r: Dict) -> str:
    parts = [
        f"<div style='font-family:Arial,sans-serif;font-size:14px;max-width:640px;'>",
        f"<h2 style='margin:0 0 2px 0;'>Discrepancy &mdash; Order #{order_id}</h2>",
        f"<p style='margin:0 0 14px 0;color:#666;'>{supplier} &middot; "
        f"document {doc_ref or '?'}</p>",
    ]
    qm = r.get("qty_mismatch") or []
    if qm:
        parts.append("<p style='margin:0;'><strong>Quantity mismatches</strong></p>")
        parts.append(_tbl([(e.get("sku"), e.get("sent_qty"), e.get("supplier_qty"))
                           for e in qm[:10]], ("SKU", "We sent", "They confirm")))
        parts.append(_more(qm))
    ms = r.get("missing_at_supplier") or []
    if ms:
        parts.append("<p style='margin:0;'><strong>We sent &mdash; they didn't confirm</strong></p>")
        parts.append(_tbl([(e.get("sku"), e.get("sent_qty")) for e in ms[:10]],
                          ("SKU", "Qty")))
        parts.append(_more(ms))
    ux = r.get("unexpected_from_supplier") or []
    if ux:
        parts.append("<p style='margin:0;'><strong>They list &mdash; we didn't send</strong></p>")
        parts.append(_tbl([(_clean_sku(e), e.get("supplier_qty")) for e in ux[:10]],
                          ("Their SKU", "Qty")))
        parts.append(_more(ux))
    subs = r.get("possible_substitutions") or []
    if subs:
        parts.append("<p style='margin:0;'><strong>Probable dialect pairs "
                     "(matching quantities &mdash; likely the same item)</strong></p>")
        parts.append(_tbl([(e.get("sent_sku"), e.get("supplier_sku"), e.get("sent_qty"))
                           for e in subs[:10]], ("We sent", "They used", "Qty")))
        parts.append(_more(subs))
    unres = r.get("unresolved_supplier_lines") or []
    if unres:
        parts.append("<p style='margin:0;'><strong>Document lines we couldn't resolve</strong></p>")
        parts.append(_tbl([((e.get("item") or e.get("desc") or "?"), e.get("qty"),
                            (e.get("note") or "")[:70]) for e in unres[:10]],
                          ("Line", "Qty", "Note")))
        parts.append(_more(unres))
    comp = r.get("companions") or []
    if comp:
        parts.append("<p style='margin:0;'><strong>Companion lines "
                     "(free trays/assembly &mdash; informational)</strong></p>")
        parts.append(_tbl([(e.get("sku"), e.get("qty")) for e in comp[:10]],
                          ("SKU", "Qty")))
    flags = r.get("flags") or []
    if flags:
        parts.append("<p style='margin:0;'><strong>Flags</strong></p><ul style='margin-top:4px;'>"
                     + "".join(f"<li>{f}</li>" for f in flags[:10]) + "</ul>")
    matched_n = r.get("matched_qty")
    if matched_n is None:
        matched_n = len(r.get("matched") or [])
    parts.append(f"<p style='color:#666;'>Matched: {matched_n} &middot; "
                 f"units sent {r.get('sent_line_total', '?')} vs confirmed "
                 f"{r.get('supplier_line_total', '?')}</p>")
    parts.append(f"<p style='color:#888;font-size:13px;'>Full detail: "
                 f"GET /supplier-orders?order_id={order_id}</p></div>")
    return "".join(parts)


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
        _send_email(order_id, INTERNAL_ALERT_EMAIL,
                    f"DISCREPANCY: {supplier} doc for order #{order_id}",
                    _discrepancy_email_html(order_id, supplier, doc_ref, report),
                    triggered_by="estimate_verifier")
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

    html = msg.get("html") or ""

    # DuraStone NetSuite HTML body
    if html and "#SO" in html and "netsuite" in html.lower():
        try:
            import supplier_doc_parser as sdp
            sdp_parsed = sdp.parse_durastone_email(html)
            if sdp_parsed.get("so_number") and sdp_parsed.get("po"):
                # feed the revision tripwire history too
                with get_db() as conn:
                    try:
                        sdp.record_and_check_ds_email(conn, message_id, html)
                    except Exception:
                        pass
                v = verify_ds_html(sdp_parsed["po"], html)
                results.append(v)
        except Exception as e:
            results.append({"supplier": "DuraStone", "error": str(e)})

    # ROC confirmation / invoice HTML body
    if html:
        try:
            from roc_parser import looks_like_roc_confirmation, parse_roc_confirmation_html
            if looks_like_roc_confirmation(html):
                pre = parse_roc_confirmation_html(html)
                if pre.get("po") and pre.get("lines"):
                    order_key = "".join(c for c in str(pre["po"]) if c.isdigit())
                    v = verify_roc_html(order_key, html)
                    results.append(v)
        except Exception as e:
            results.append({"supplier": "ROC", "error": str(e)})

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
    seen = set()
    for query in (CANDIDATE_QUERY_PDF, CANDIDATE_QUERY_HTML):
        try:
            messages = search_emails(f"newer_than:{int(hours_back)}h {query}")
        except Exception as e:
            out["errors"].append(f"search: {e}")
            continue
        for m in messages:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
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
