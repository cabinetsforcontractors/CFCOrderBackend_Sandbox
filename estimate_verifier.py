"""
estimate_verifier.py
Auto-verify on reply — the loop-closer for the auto-ordering lane.

Watches Gmail for supplier reply documents (estimates / sales orders /
quotes / confirmations), runs the validated per-supplier parsers, diffs
against what we sent, and flips the supplier_orders state row:

    clean diff   -> status 'confirmed' (quiet — shows in the digest)
    discrepancy  -> REVISION-REQUEST EMAIL TO THE SUPPLIER (their SKUs, their
                    door name — never ours), CC us (William 2026-07-17).
                    Escalation clock: no update after 4 business hours ->
                    "just making sure you saw this" to the supplier; 4 more ->
                    "ALERT!! NO RESPONSE FOR DISCREPANCIES ORDER #x" to us.
                    Internal-table alert only as fallback when no actionable
                    revision can be composed.

THE FALLBACK LAW (William-blessed 2026-07-31): every verdict now runs TWO
checkers — this module's grammar parser (Checker A) AND verify_consensus's
independent AI reader (Checker B). Both must agree clean or the row goes
DISCREPANCY: either flags anything, they disagree, or B is unreadable/
unavailable -> human eyes, nothing auto-approves. Consensus detail rides
the supplier_reply_verified fire. Env VERIFY_CONSENSUS_ENABLED=false is
the no-deploy rollback to parser-only.

GHI EXCEPTION (William 2026-07-18): GHI's channel is a HUMAN reply — their
"review and approve for processing" step. Instead of an auto revision request,
every GHI verdict (clean or flagged) produces an approval REPLY DRAFT in the
GHI thread via ghi_inbox.create_approval_draft; William reviews and sends
(draft-first law). ghi_inbox.ghi_thread_capture also rides every scan: GHI
answers about one order arrive in other orders' threads, so every GHI email
is scanned for PO mentions and filed against the orders it names.

PROGRESS SWEEP (William 2026-07-18): progress_emails.run_progress_sweep also
rides every scan — post-payment arrival-window / delay / tracking customer
DRAFTS (draft-first), promised windows keyed to blessed per-supplier speeds.

ALERT THROTTLE: an identical discrepancy report (same order/supplier/hash)
triggers the supplier email AT MOST once per 6 hours, no matter how many
times the verifier runs. Rows still update every run. A NEW document with a
different diff restarts the cycle (and the clock).

Document routing is content-marker based (PDF markers for GHI/LM/C&S/LI;
HTML markers for DuraStone NetSuite and ROC confirmations). Diff space:
GHI + DS in website-SKU space, LI/LM/C&S/ROC in body space.
"""

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from db_helpers import get_db

INTERNAL_ALERT_EMAIL = os.environ.get("WAREHOUSE_NOTIFICATION_EMAIL",
                                      "orders@cabinetsforcontractors.com").strip()

CANDIDATE_QUERY_PDF = ('has:attachment filename:pdf '
                       '(ghicabinets OR QUOFL OR "Quotation_S" OR Estimate_ OR '
                       'Invoice_ OR milestonecabinetry OR cabinetstone OR '
                       '"Cabinetry Distribution" OR "Sales Order" OR '
                       'lnccabinetryvab)')
CANDIDATE_QUERY_HTML = ('(from:roccabinetry.com OR from:sent-via.netsuite.com '
                        'OR from:dlcabinetry.com) '
                        '("order confirmation" OR "Invoice" OR "Sales Order")')

# business window approx 9a-5p ET expressed in UTC
_BIZ_START_UTC, _BIZ_END_UTC = 13, 21
FOLLOWUP_AFTER_BH = 4        # "just making sure you saw this" to supplier
NO_RESPONSE_ALERT_BH = 8     # ALERT!! to us


# =============================================================================
# TABLES / SCAN DEDUPE
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
        if fname.lower().endswith((".pdf", ".csv", ".txt")) and body.get("attachmentId"):
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
        elif supplier == "L&C Cabinetry":
            parsed = sdp.parse_lc_estimate_pdf(data)
            report = sdp.body_space_diff(
                sent, [f for f in sdp.fold_lc_lines(parsed["lines"])
                       if not f.get("fee")])
            doc_ref = parsed.get("estimate_number")
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


def _reclass_cross_supplier(order_id: str, report: Dict) -> Dict:
    """MULTI-SUPPLIER ORDERS (William ruling 2026-07-27, the PO 5635 lesson:
    one order is supplied by several suppliers — C&S AND L&C both answered
    PO 5635 — and each sends a doc covering only THEIR slice). A doc line
    that isn't in this supplier's slice but IS on the order belongs to the
    attribution conversation, not the discrepancy lane: reclassified to
    'attributed_elsewhere' (informational) so revision requests never fire
    over another supplier's lines. Lines on nobody's slice stay unexpected."""
    unexpected = report.get("unexpected_from_supplier") or []
    if not unexpected:
        return report
    from psycopg2.extras import RealDictCursor
    bodies = set()
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT oli.sku, rp.supplier_sku
                           FROM order_line_items oli
                           LEFT JOIN rta_products rp ON rp.product_sku = oli.sku
                           WHERE oli.order_id = %s""", (order_id,))
            for r in cur.fetchall():
                for raw in (r.get("sku"), r.get("supplier_sku")):
                    s = (raw or "").upper().replace(" ", "")
                    if not s:
                        continue
                    bodies.add(s)
                    if "-" in s:
                        bodies.add(s.split("-", 1)[1])
    keep, moved = [], []
    for e in unexpected:
        b = str(e.get("body") or e.get("sku") or "").upper().replace(" ", "")
        cands = {b} | ({b.split("-", 1)[1]} if "-" in b else set())
        (moved if cands & bodies else keep).append(e)
    if moved:
        report["attributed_elsewhere"] = moved
        report["unexpected_from_supplier"] = keep
        if (not keep and not report.get("qty_mismatch")
                and not report.get("missing_at_supplier")):
            report["ok"] = True
    return report


def verify_dl_html(order_id: str, html: str, subject: str = "") -> Dict:
    """DL Cabinetry confirmation email (grammar decoded 2026-07-27 from the
    real JOB #5517 email). DL SKUs = their line code + space + our form
    token -> body-space diff, same family as ROC."""
    import supplier_doc_parser as sdp
    from freight_routes import _order_lines_for_supplier
    from dl_parser import fold_dl_lines, parse_dl_confirmation_html
    from psycopg2.extras import RealDictCursor

    parsed = parse_dl_confirmation_html(html, subject)
    folded = fold_dl_lines(parsed["lines"])
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sent = _order_lines_for_supplier(cur, order_id, (),
                                             ("DL", "DL Cabinetry"))
        report = sdp.body_space_diff(sent, folded)
    return {"supplier": "DL", "doc_ref": parsed.get("dl_order_number"),
            "po": parsed.get("po"), "report": report,
            "line_count": len(parsed.get("lines") or []),
            "sent_count": len(sent)}


def verify_roc_html(order_id: str, html: str) -> Dict:
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
# SUPPLIER REVISION REQUEST (their SKUs / their door name — never ours)
# =============================================================================

def _their_sku(our_sku: str, supplier: str) -> str:
    """The supplier's token for one of OUR skus (rta forward map; ROC gets
    the store prefix). Falls back to the bare body."""
    tok = None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT supplier_sku FROM rta_products WHERE product_sku = %s",
                            (our_sku,))
                row = cur.fetchone()
                if row and row[0]:
                    tok = row[0]
    except Exception:
        pass
    if not tok:
        tok = our_sku.split("-", 1)[1] if "-" in our_sku else our_sku
    if supplier == "ROC":
        from supplier_orders import ROC_STORE_PREFIX
        pre = (our_sku or "").split("-")[0].upper()
        sp = ROC_STORE_PREFIX.get(pre)
        if sp and not tok.upper().startswith(sp + "-"):
            tok = f"{sp}-{tok}"
    return tok


def _clean_their_label(entry) -> str:
    s = str(entry.get("sku") or entry.get("body") or "?")
    if "/" in s:
        s = min(s.split("/"), key=len)
    return s


def build_revision_request(order_id: str, supplier: str, doc_ref: str,
                           report: Dict) -> Optional[Dict]:
    """Compose the revise-your-quote email in the SUPPLIER's dialect.
    Returns {'html','subject'} or None when there is nothing actionable."""
    from supplier_orders import SUPPLIER_DOOR_INFO

    asks = []
    for e in report.get("qty_mismatch") or []:
        their = _their_sku(e.get("sku") or "", supplier)
        asks.append(f"Change <strong>{their}</strong> to Qty "
                    f"<strong>{e.get('sent_qty')}</strong> "
                    f"(your document shows {e.get('supplier_qty')})")
    for e in report.get("missing_at_supplier") or []:
        their = _their_sku(e.get("sku") or "", supplier)
        need = e.get("unconfirmed_qty") or e.get("sent_qty")
        total = e.get("sent_qty")
        if need and total and need != total:
            asks.append(f"Add <strong>{need}</strong> each "
                        f"<strong>{their}</strong> for a total Qty of "
                        f"<strong>{total}</strong>")
        else:
            asks.append(f"Add <strong>{total}</strong> each <strong>{their}</strong>")
    for e in report.get("unexpected_from_supplier") or []:
        their = _clean_their_label(e)
        asks.append(f"Please confirm <strong>{their}</strong> x"
                    f"{e.get('supplier_qty')} — this is not on our PO; "
                    f"remove if added in error")
    if not asks:
        return None

    # door header from the sent side's line prefix
    door_txt = ""
    first_our = next((e.get("sku") for e in
                      (report.get("missing_at_supplier") or []) +
                      (report.get("qty_mismatch") or []) if e.get("sku")), "")
    pre = (first_our or "").split("-")[0].upper()
    door = SUPPLIER_DOOR_INFO.get((supplier, pre))
    if door:
        door_txt = f" ({door['door_name']}, {door['presku']})"

    items = "".join(f"<li style='margin:4px 0;'>{a}</li>" for a in asks)
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
            f"<p>Hello,</p>"
            f"<p>Please revise your document <strong>{doc_ref or ''}</strong> "
            f"for our order <strong>PO {order_id}</strong>:{door_txt}</p>"
            f"<ol>{items}</ol>"
            f"<p>Please send the corrected confirmation for our records.</p>"
            f"<p>Thank you,<br>Cabinets For Contractors<br>(770) 990-4885</p></div>")
    from supplier_orders import po_tag
    return {"html": html,
            "subject": f"{po_tag(order_id, supplier)} - please revise "
                       f"{doc_ref or 'your confirmation'}"}


# =============================================================================
# INTERNAL FALLBACK ALERT (human tables — used when nothing actionable)
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


def _internal_discrepancy_html(order_id, supplier, doc_ref, r,
                               consensus: Dict = None) -> str:
    parts = [f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
             f"<h2 style='margin:0 0 2px 0;'>Discrepancy &mdash; Order #{order_id}</h2>"
             f"<p style='margin:0 0 14px 0;color:#666;'>{supplier} &middot; "
             f"document {doc_ref or '?'} &middot; no actionable revision — "
             f"needs a human</p>"]
    if consensus:
        cv = consensus.get("consensus") or {}
        parts.append(f"<p style='margin:0 0 8px 0;'><strong>Two-checker verdict:"
                     f"</strong> {cv.get('reason') or ''} "
                     f"(AI reader: {consensus.get('verdict')})</p>")
        finds = consensus.get("findings") or []
        if finds:
            parts.append("<p style='margin:0;'><strong>AI reader findings</strong></p>")
            parts.append(_tbl([(f.get("issue"), f.get("sku"),
                                (f.get("detail") or "")[:90])
                               for f in finds[:10]],
                              ("Issue", "SKU", "Detail")))
    unres = r.get("unresolved_supplier_lines") or []
    if unres:
        parts.append("<p style='margin:0;'><strong>Document lines we couldn't resolve</strong></p>")
        parts.append(_tbl([((e.get("item") or e.get("desc") or "?"), e.get("qty"),
                            (e.get("note") or "")[:70]) for e in unres[:10]],
                          ("Line", "Qty", "Note")))
    flags = r.get("flags") or []
    if flags:
        parts.append("<p style='margin:0;'><strong>Flags</strong></p><ul>"
                     + "".join(f"<li>{f}</li>" for f in flags[:10]) + "</ul>")
    parts.append(f"<p style='color:#888;font-size:13px;'>Full detail: "
                 f"GET /supplier-orders?order_id={order_id}</p></div>")
    return "".join(parts)


# =============================================================================
# VERDICT + REVISION SEND (throttled)
# =============================================================================

def _alert_already_sent(order_id: str, report_hash: str) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM order_events
                    WHERE order_id = %s
                      AND event_type = 'discrepancy_alerted'
                      AND event_data LIKE %s
                      AND created_at > NOW() - interval '6 hours'
                    LIMIT 1
                """, (order_id, f'%{report_hash}%'))
                return cur.fetchone() is not None
    except Exception:
        return False


def _mark_alert_sent(order_id: str, supplier: str, report_hash: str):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, 'discrepancy_alerted', %s, 'estimate_verifier')
                """, (order_id, json.dumps({"supplier": supplier,
                                            "hash": report_hash})))
                conn.commit()
    except Exception as e:
        print(f"[VERIFY] failed to mark alert sent: {e}")


def _norm_doc_ref(r: str) -> str:
    """Leading-zeros-blind, case-blind doc-ref compare key."""
    return (r or "").strip().upper().lstrip("0")


def _subject_is_invoice(message_id: str) -> bool:
    """One metadata call: does the triggering message call itself an
    invoice? Errors read as 'no' — the stamp is a bonus, never a blocker."""
    try:
        from gmail_sync import gmail_api_request
        meta = gmail_api_request(
            f"messages/{message_id}",
            {"format": "metadata", "metadataHeaders": "Subject"}) or {}
        for h in (meta.get("payload", {}) or {}).get("headers", []):
            if h["name"].lower() == "subject":
                return "invoice" in (h["value"] or "").lower()
    except Exception:
        pass
    return False


def _apply_verdict(order_id: str, supplier: str, verdict_ok: bool,
                   doc_ref: str, report: Dict, message_id: str,
                   consensus: Dict = None) -> str:
    """Flip (or create) the supplier_orders row; on discrepancy, email the
    SUPPLIER a revision request (CC us) and start the escalation clock.
    GHI instead gets an approval REPLY DRAFT (draft-first, William 2026-07-18).
    verdict_ok is the CONSENSUS verdict (fallback law 2026-07-31); consensus
    detail rides the fire.
    WAVE 2 (2026-08-02): build F — a SECOND different doc ref on the same
    PO+warehouse rings the DUPLICATE-ENTRY alarm (the S118998 both-stores
    trap). Build G — a clean verify from an invoice-subject message on a row
    already confirmed-or-later advances it to invoice_verified."""
    from config import SUPPLIER_INFO
    from supplier_orders import ensure_supplier_orders_table, _send_email

    status = "confirmed" if verdict_ok else "discrepancy"
    prior_status = prior_ref = None
    with get_db() as conn:
        ensure_supplier_orders_table(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT status, supplier_doc_ref FROM supplier_orders
                           WHERE order_id = %s AND warehouse = %s""",
                        (order_id, supplier))
            row = cur.fetchone()
            if row:
                prior_status, prior_ref = row[0], row[1]

    dup_entry = bool(doc_ref and prior_ref
                     and _norm_doc_ref(doc_ref) != _norm_doc_ref(prior_ref))
    if verdict_ok and not dup_entry \
            and prior_status in ("confirmed", "scheduled", "picked_up",
                                 "delivered") \
            and _subject_is_invoice(message_id):
        status = "invoice_verified"
    cons_summary = None
    if consensus:
        cons_summary = {
            "reader_verdict": consensus.get("verdict"),
            "reader_findings": len(consensus.get("findings") or []),
            "reason": (consensus.get("consensus") or {}).get("reason"),
            "agree": (consensus.get("consensus") or {}).get("agree"),
        }
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
                  None if verdict_ok else json.dumps(
                      {"report": report, "consensus": cons_summary}
                      if cons_summary else report, default=str)[:6000],
                  status, f"auto-verified from Gmail msg {message_id}"))
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'supplier_reply_verified', %s, 'estimate_verifier')
            """, (order_id, json.dumps({
                "supplier": supplier, "doc_ref": doc_ref, "verdict": status,
                "message_id": message_id,
                "consensus": cons_summary,
                "summary": {k: len(report.get(k) or [])
                            for k in ("matched", "qty_mismatch", "missing_at_supplier",
                                      "unexpected_from_supplier",
                                      "attributed_elsewhere",
                                      "unresolved_supplier_lines", "flags")},
            }, default=str)))
            conn.commit()

    # DUPLICATE-ENTRY ALARM (build F): two different supplier documents for
    # one PO+warehouse — the order may be entered TWICE at the supplier
    # (S118998 was entered at both C&S stores and nearly double-shipped).
    # Alarm once per ref pair; the verify itself proceeds normally.
    if dup_entry:
        pair_hash = hashlib.sha1(
            f"dup:{order_id}:{supplier}:"
            f"{':'.join(sorted([_norm_doc_ref(doc_ref), _norm_doc_ref(prior_ref)]))}"
            .encode()).hexdigest()[:16]
        if not _alert_already_sent(order_id, pair_hash):
            res = _send_email(
                order_id, INTERNAL_ALERT_EMAIL,
                f"DUPLICATE ENTRY? two {supplier} documents for PO {order_id}",
                f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                f"<p><strong>Two DIFFERENT {supplier} document numbers have now "
                f"verified against PO {order_id}:</strong></p>"
                f"<p>Already on file: <strong>{prior_ref}</strong><br>"
                f"Just arrived: <strong>{doc_ref}</strong> "
                f"(Gmail msg {message_id})</p>"
                f"<p>The S118998 lesson: the same order can be entered at TWO "
                f"supplier locations and both will ship. Confirm with "
                f"{supplier} which document is real and kill the other.</p>"
                f"</div>",
                triggered_by="duplicate_entry_alarm")
            if res.get("success"):
                _mark_alert_sent(order_id, supplier, pair_hash)

    # ⚖️ 2026-08-02 (William: "the 7/18 draft ruling is stale"): GHI's
    # approval-draft special path RETIRED — GHI rides the same auto
    # revision-request flow as every other supplier. On a CLEAN verify the
    # robot still sends GHI the approval reply (their process asks for one),
    # but automatically, from orders@, no draft step.
    if supplier == "GHI" and verdict_ok:
        try:
            supplier_email = (SUPPLIER_INFO.get("GHI") or {}).get("email", "")
            if supplier_email:
                _send_email(
                    order_id, supplier_email,
                    f"Re: PO {order_id}",
                    f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                    f"<p>Hey There,</p>"
                    f"<p>{doc_ref or 'Your sales order'} for our PO "
                    f"{order_id} checks out line for line — approved for "
                    f"processing. Please let us know when it's ready to ship "
                    f"and we will send a BOL.</p>"
                    f"<p>Thank you,<br>--<br>William Prince<br>"
                    f"Cabinets For Contractors<br>(770) 990-4885</p></div>",
                    triggered_by="ghi_auto_approval")
        except Exception as e:
            print(f"[VERIFY] GHI auto-approval error: {e}")
        return status

    if verdict_ok:
        return status

    report_hash = hashlib.sha1(
        json.dumps({"o": order_id, "s": supplier, "d": doc_ref, "r": report,
                    "c": cons_summary},
                   sort_keys=True, default=str).encode()).hexdigest()[:16]
    if _alert_already_sent(order_id, report_hash):
        print(f"[VERIFY] revision request throttled order={order_id} hash={report_hash}")
        return status

    # The revision request rides Checker A's concrete line asks. When A was
    # clean and the discrepancy came from Checker B (or B unavailable), there
    # is nothing line-actionable to send the supplier -> internal alert path.
    revision = build_revision_request(order_id, supplier, doc_ref, report)
    supplier_email = (SUPPLIER_INFO.get(supplier) or {}).get("email", "")
    sent_ok = False
    if revision and supplier_email:
        result = _send_email(order_id, supplier_email, revision["subject"],
                             revision["html"], triggered_by="revision_request",
                             cc=INTERNAL_ALERT_EMAIL)
        sent_ok = result.get("success", False)
        if sent_ok:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE supplier_orders
                        SET revision_requested_at = NOW(),
                            followup_sent_at = NULL,
                            no_response_alerted_at = NULL,
                            updated_at = NOW()
                        WHERE order_id = %s AND warehouse = %s
                    """, (order_id, supplier))
                    conn.commit()
    if not sent_ok:
        # fallback: internal alert (nothing actionable, or send failed)
        result = _send_email(order_id, INTERNAL_ALERT_EMAIL,
                             f"DISCREPANCY (needs human): {supplier} doc for "
                             f"order #{order_id}",
                             _internal_discrepancy_html(order_id, supplier,
                                                        doc_ref, report,
                                                        consensus=consensus),
                             triggered_by="estimate_verifier")
        sent_ok = result.get("success", False)
    if sent_ok:
        _mark_alert_sent(order_id, supplier, report_hash)
    return status


# =============================================================================
# ESCALATION CLOCK (4 business hours -> follow-up; 8 -> ALERT to us)
# =============================================================================

def business_hours_between(start: datetime, end: datetime) -> int:
    """Whole hours inside Mon-Fri 9a-5p ET (approximated as 13-21 UTC)."""
    if not start or start >= end:
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    hours = 0
    t = start.replace(minute=0, second=0, microsecond=0)
    steps = 0
    while t < end and steps < 24 * 30:
        if t.weekday() < 5 and _BIZ_START_UTC <= t.hour < _BIZ_END_UTC:
            hours += 1
        t += timedelta(hours=1)
        steps += 1
    return hours


def check_discrepancy_followups() -> Dict:
    """Runs with every reply scan: pushes the supplier at +4 business hours,
    alerts William at +8. Cleared automatically when a new document flips the
    row (confirmed) or a new revision restarts the clock."""
    from config import SUPPLIER_INFO
    from supplier_orders import _send_email
    from psycopg2.extras import RealDictCursor

    out = {"followups": 0, "alerts": 0, "checked": 0}
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM supplier_orders
                WHERE status = 'discrepancy'
                  AND revision_requested_at IS NOT NULL
            """)
            rows = cur.fetchall()
    for row in rows:
        out["checked"] += 1
        bh = business_hours_between(row["revision_requested_at"], now)
        supplier_email = (SUPPLIER_INFO.get(row["warehouse"]) or {}).get("email", "")
        if bh >= FOLLOWUP_AFTER_BH and not row["followup_sent_at"] and supplier_email:
            res = _send_email(
                row["order_id"], supplier_email,
                f"PO {row['order_id']} - checking in on the revision "
                f"({row['supplier_doc_ref'] or ''})",
                f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                f"<p>Hello,</p><p>Just making sure you saw our revision request "
                f"for <strong>PO {row['order_id']}</strong> "
                f"(your document {row['supplier_doc_ref'] or ''}). "
                f"Could you confirm it's being updated?</p>"
                f"<p>Thank you,<br>Cabinets For Contractors<br>(770) 990-4885</p></div>",
                triggered_by="revision_followup", cc=INTERNAL_ALERT_EMAIL)
            if res.get("success"):
                out["followups"] += 1
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE supplier_orders SET followup_sent_at = NOW(),
                                       updated_at = NOW() WHERE id = %s""", (row["id"],))
                        conn.commit()
        if bh >= NO_RESPONSE_ALERT_BH and not row["no_response_alerted_at"]:
            res = _send_email(
                row["order_id"], INTERNAL_ALERT_EMAIL,
                f"ALERT!! NO RESPONSE FOR DISCREPANCIES ORDER #{row['order_id']}",
                f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
                f"<p><strong>{row['warehouse']}</strong> has not responded to the "
                f"revision request for <strong>PO {row['order_id']}</strong> "
                f"(doc {row['supplier_doc_ref'] or '?'}).</p>"
                f"<p>Revision requested: {row['revision_requested_at']}<br>"
                f"Follow-up sent: {row['followup_sent_at'] or 'no'}</p>"
                f"<p>Time to pick up the phone: "
                f"{(SUPPLIER_INFO.get(row['warehouse']) or {}).get('contact', '')}</p></div>",
                triggered_by="revision_no_response")
            if res.get("success"):
                out["alerts"] += 1
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE supplier_orders SET no_response_alerted_at = NOW(),
                                       updated_at = NOW() WHERE id = %s""", (row["id"],))
                        conn.commit()
    return out


# =============================================================================
# MESSAGE PROCESSING
# =============================================================================

def process_message(message_id: str, force: bool = False,
                    dry_run: bool = False) -> Dict:
    """Detect + parse + verify one Gmail message. Idempotent unless force.
    dry_run=True (2026-07-27): full detect + parse + diff but NO writes —
    no supplier_orders row, no revision request, no scan record. The safe
    drill mode for proving grammars on real messages."""
    if not force and _already_scanned(message_id):
        return {"status": "already_scanned", "message_id": message_id}

    msg = fetch_message_full(message_id)
    if not msg:
        return {"status": "error", "message": "could not fetch message"}

    results = []
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
        # raw text for Checker B (fallback law 2026-07-31)
        try:
            from verify_consensus import pdf_text
            v["_doc_text"] = pdf_text(att["data"])
        except Exception:
            v["_doc_text"] = ""
        results.append(v)

    html = msg.get("html") or ""
    html_doc_text = None
    if html:
        try:
            from verify_consensus import html_text
            html_doc_text = html_text(html)
        except Exception:
            html_doc_text = ""

    if html and "#SO" in html and "netsuite" in html.lower():
        try:
            import supplier_doc_parser as sdp
            sdp_parsed = sdp.parse_durastone_email(html)
            if sdp_parsed.get("so_number") and sdp_parsed.get("po"):
                with get_db() as conn:
                    try:
                        sdp.record_and_check_ds_email(conn, message_id, html)
                    except Exception:
                        pass
                v = verify_ds_html(sdp_parsed["po"], html)
                v["_doc_text"] = html_doc_text
                results.append(v)
        except Exception as e:
            results.append({"supplier": "DuraStone", "error": str(e)})

    if html:
        try:
            from roc_parser import looks_like_roc_confirmation, parse_roc_confirmation_html
            if looks_like_roc_confirmation(html):
                pre = parse_roc_confirmation_html(html)
                if pre.get("po") and pre.get("lines"):
                    order_key = "".join(c for c in str(pre["po"]) if c.isdigit())
                    v = verify_roc_html(order_key, html)
                    v["_doc_text"] = html_doc_text
                    results.append(v)
        except Exception as e:
            results.append({"supplier": "ROC", "error": str(e)})

    if html:
        try:
            from dl_parser import looks_like_dl_confirmation, parse_dl_confirmation_html
            subject = msg.get("subject") or ""
            if looks_like_dl_confirmation(html, subject):
                pre = parse_dl_confirmation_html(html, subject)
                if pre.get("po") and pre.get("lines"):
                    order_key = "".join(c for c in str(pre["po"]) if c.isdigit())
                    v = verify_dl_html(order_key, html, subject)
                    v["_doc_text"] = html_doc_text
                    results.append(v)
        except Exception as e:
            results.append({"supplier": "DL", "error": str(e)})

    # multi-supplier reclass (William 2026-07-27): runs for real AND dry —
    # doc lines that belong to another supplier's slice of the same order
    # become informational, not discrepancies
    for v in results:
        if v.get("error") or not v.get("po") or not v.get("report"):
            continue
        okey = "".join(c for c in str(v["po"]) if c.isdigit())
        try:
            v["report"] = _reclass_cross_supplier(okey, v["report"])
        except Exception:
            pass

    if dry_run:
        return {"status": "dry_run", "message_id": message_id,
                "subject": msg["subject"],
                "attachments": [a["filename"] for a in msg["attachments"]],
                "results": [
                    {k: v.get(k) for k in
                     ("supplier", "doc_ref", "po", "line_count", "sent_count",
                      "error")} | {"report_summary": {
                          k: len((v.get("report") or {}).get(k) or [])
                          for k in ("matched", "qty_mismatch",
                                    "missing_at_supplier",
                                    "unexpected_from_supplier",
                                    "attributed_elsewhere")}
                          if v.get("report") else None,
                          "report_ok": (v.get("report") or {}).get("ok")}
                    for v in results]}

    processed = []
    for v in results:
        if v.get("error") or not v.get("po"):
            processed.append(v)
            continue
        order_id = str(v["po"]).lstrip("0")
        order_key = "".join(c for c in order_id if c.isdigit()) or order_id
        # THE FALLBACK LAW (2026-07-31): parser verdict + AI reader verdict
        # must AGREE clean, or the row goes discrepancy.
        parser_ok = bool(v["report"].get("ok"))
        final_ok = parser_ok
        consensus = None
        try:
            from verify_consensus import second_reader, consensus_verdict
            consensus = second_reader(order_key, v["supplier"],
                                      v.get("_doc_text") or "", v["report"])
            cv = consensus_verdict(parser_ok, consensus)
            consensus["consensus"] = cv
            final_ok = cv["final_ok"]
        except Exception as e:
            consensus = {"verdict": "unavailable", "findings": [],
                         "consensus": {"final_ok": False, "agree": False,
                                       "reason": f"second reader crashed: "
                                                 f"{str(e)[:120]}"}}
            final_ok = False
        status = _apply_verdict(order_key, v["supplier"], final_ok,
                                v.get("doc_ref"), v["report"], message_id,
                                consensus=consensus)
        v["verdict"] = status
        v["order_id"] = order_key
        processed.append({k: v[k] for k in
                          ("supplier", "doc_ref", "order_id", "verdict",
                           "line_count", "sent_count")}
                         | {"parser_ok": parser_ok,
                            "reader_verdict": (consensus or {}).get("verdict"),
                            "consensus_reason":
                                ((consensus or {}).get("consensus") or {}).get("reason")})
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
    elif supplier == "L&C Cabinetry":
        parsed = sdp.parse_lc_estimate_pdf(data)
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
    """Periodic Gmail scan (wired into gmail_sync + manual endpoint).
    Also runs the discrepancy escalation clock."""
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
    try:
        out["escalations"] = check_discrepancy_followups()
    except Exception as e:
        out["errors"].append(f"escalation clock: {e}")
    try:
        from ghi_inbox import ghi_thread_capture
        out["ghi_inbox"] = ghi_thread_capture(hours_back)
    except Exception as e:
        out["errors"].append(f"ghi inbox capture: {e}")
    try:
        from progress_emails import run_progress_sweep
        out["progress"] = run_progress_sweep()
    except Exception as e:
        out["errors"].append(f"progress sweep: {e}")
    return out
