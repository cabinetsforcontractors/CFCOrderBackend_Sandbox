"""
verify_consensus.py — LANE 4: THE FALLBACK LAW (William-blessed 2026-07-31:
"two checkers must agree before 'this is ok'... Either one flags anything —
or they disagree with each other → DISCREPANCY, human eyes, nothing
auto-approves").

WHY (his words): mistakes cost 30-40% of net. GHI / LM / C&S "screw up all
the time — they miss skus and qtys, sometimes the wrong color." One checker
is not enough.

THE TWO CHECKERS:
  A — the grammar parser (estimate_verifier + supplier_doc_parser):
      deterministic, line-by-line diff against the sent order. Exists.
  B — the AI reader (this module): reads the RAW document text + what we
      ordered, independently matches skus / qtys / colors, primed with the
      supplier's known-mistake vigilance list from their playbook.

THE VERDICT LAW:
  both clean                 -> OK
  either flags anything      -> DISCREPANCY (human eyes)
  they disagree              -> DISCREPANCY (human eyes)
  B unavailable / unreadable -> DISCREPANCY (can't agree = can't approve;
                                the machine may only be wrong in the
                                too-careful direction)

Safety valve: env VERIFY_CONSENSUS_ENABLED=false reverts to parser-only
verdicts without a deploy.

Doors [admin]:
  POST /consensus/drill/{message_id} — replay both checkers on a real
       Gmail doc message, side-by-side, NO writes.
"""

import io
import json
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends

from auth import require_admin
from db_helpers import get_db

consensus_router = APIRouter(tags=["consensus"])

CONSENSUS_MODEL = os.environ.get("CONSENSUS_MODEL", "claude-sonnet-5").strip()


def consensus_enabled() -> bool:
    return os.environ.get("VERIFY_CONSENSUS_ENABLED", "true").strip().lower() \
        not in ("false", "0", "no", "off")


# =============================================================================
# RAW DOCUMENT TEXT
# =============================================================================

def pdf_text(data: bytes) -> str:
    """Raw text out of a PDF — B reads the document itself, not A's parse."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        print(f"[CONSENSUS] pdf text failed: {e}")
        return ""


def html_text(html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "",
               flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</tr>|</p>|</div>", "\n", t, flags=re.I)
    t = re.sub(r"</td>|</th>", " | ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


# =============================================================================
# WHAT WE ORDERED (independent of Checker A's extraction)
# =============================================================================

def _sent_lines(order_id: str, supplier: str) -> List[Dict]:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT oli.sku, oli.quantity, rp.supplier_sku
                FROM order_line_items oli
                LEFT JOIN rta_products rp ON rp.product_sku = oli.sku
                WHERE oli.order_id = %s AND oli.warehouse = %s
            """, (order_id, supplier))
            rows = cur.fetchall()
            if not rows:
                # warehouse naming drift — fall back to the whole order and
                # let the reader know other suppliers' slices may appear
                cur.execute("""
                    SELECT oli.sku, oli.quantity, rp.supplier_sku,
                           oli.warehouse
                    FROM order_line_items oli
                    LEFT JOIN rta_products rp ON rp.product_sku = oli.sku
                    WHERE oli.order_id = %s
                """, (order_id,))
                rows = cur.fetchall()
    return [dict(r) for r in rows]


def _vigilance(supplier: str) -> str:
    try:
        from dossier import get_playbook
        return get_playbook(supplier) or ""
    except Exception:
        return ""


# =============================================================================
# CHECKER B — THE AI READER
# =============================================================================

_READER_PROMPT = """You are the SECOND, independent checker for a wholesale \
cabinet company. A supplier sent a document (estimate / sales order / \
confirmation). Your ONLY job: verify the document matches what we ordered — \
skus, QUANTITIES, and COLORS/door-style prefixes. Suppliers here miss skus, \
miss quantities, and swap colors, so read hard.

Notes:
- Supplier documents use THEIR own sku dialect. Our sku "AKS-B12" may appear \
as "B12", "SW-B12", etc. Match on the body/form token and judge the \
color/line prefix separately.
- Fee/assembly/companion lines (pallet fees, A-* assembly charges) are not \
order lines — ignore them.
- If the order list includes a `warehouse` column, this document may cover \
only ONE supplier's slice of a multi-supplier order — lines from other \
warehouses are NOT missing.
- If the document text is garbled or you cannot confidently read its line \
items, say verdict "unreadable" — NEVER guess a clean.

KNOWN MISTAKES THIS SUPPLIER MAKES (check these hardest):
{vigilance}

WHAT WE ORDERED (order {order_id}, supplier slice {supplier}):
{sent}

THE SUPPLIER'S DOCUMENT (raw text):
{doc}

Answer with ONLY this JSON, nothing else:
{{"verdict": "clean" or "flag" or "unreadable",
  "findings": [{{"issue": "missing|qty|color|extra|unreadable-line|other",
                "sku": "...", "detail": "one plain sentence"}}],
  "note": "one sentence overall"}}"""


def second_reader(order_id: str, supplier: str, doc_text: str,
                  parser_report: Dict = None) -> Dict:
    """Checker B. Independent read of the raw document vs the sent order.
    Returns {"verdict": clean|flag|unreadable|unavailable, "findings": [...]}"""
    from config import ANTHROPIC_API_KEY
    if not consensus_enabled():
        return {"verdict": "disabled", "findings": []}
    if not ANTHROPIC_API_KEY:
        return {"verdict": "unavailable", "findings": [],
                "note": "ANTHROPIC_API_KEY not set"}
    if not (doc_text or "").strip():
        return {"verdict": "unreadable", "findings": [],
                "note": "no document text could be extracted"}

    sent = _sent_lines(order_id, supplier)
    if not sent:
        return {"verdict": "flag", "findings": [
            {"issue": "other", "sku": "",
             "detail": f"no order lines found in our DB for order {order_id} "
                       f"— cannot verify anything"}],
            "note": "order lines missing on our side"}

    sent_txt = "\n".join(
        f"- {r.get('sku')} (supplier token {r.get('supplier_sku') or '?'}) "
        f"x{r.get('quantity')}"
        + (f" [warehouse {r.get('warehouse')}]" if r.get("warehouse") else "")
        for r in sent)

    prompt = _READER_PROMPT.format(
        vigilance=_vigilance(supplier) or "(none on file yet)",
        order_id=order_id, supplier=supplier,
        sent=sent_txt[:8000], doc=(doc_text or "")[:24000])

    payload = {"model": CONSENSUS_MODEL, "max_tokens": 1500,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", ANTHROPIC_API_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            result = json.loads(r.read().decode())
        text = (result.get("content") or [{}])[0].get("text", "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        parsed = json.loads(m.group(0)) if m else None
        if not parsed or parsed.get("verdict") not in ("clean", "flag",
                                                       "unreadable"):
            return {"verdict": "unavailable", "findings": [],
                    "note": f"reader returned unparseable output: {text[:150]}"}
        parsed["findings"] = (parsed.get("findings") or [])[:20]
        return parsed
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200] if e.fp else ""
        return {"verdict": "unavailable", "findings": [],
                "note": f"api {e.code} {body}"}
    except Exception as e:
        return {"verdict": "unavailable", "findings": [],
                "note": f"reader failed: {e}"}


# =============================================================================
# THE VERDICT LAW
# =============================================================================

def consensus_verdict(parser_ok: bool, reader: Dict) -> Dict:
    """Both must agree clean, or it's a discrepancy. Disabled = parser-only."""
    rv = (reader or {}).get("verdict", "unavailable")
    if rv == "disabled":
        return {"final_ok": bool(parser_ok), "agree": None,
                "reason": "consensus disabled — parser-only verdict"}
    if rv == "clean" and parser_ok:
        return {"final_ok": True, "agree": True,
                "reason": "both checkers clean"}
    if rv == "clean" and not parser_ok:
        return {"final_ok": False, "agree": False,
                "reason": "parser flagged, AI reader clean — checkers "
                          "disagree, human eyes"}
    if rv == "flag" and parser_ok:
        return {"final_ok": False, "agree": False,
                "reason": "parser clean, AI reader flagged — checkers "
                          "disagree, human eyes"}
    if rv == "flag" and not parser_ok:
        return {"final_ok": False, "agree": True,
                "reason": "both checkers flagged"}
    # unreadable / unavailable
    return {"final_ok": False, "agree": False,
            "reason": f"second reader {rv} — cannot agree, cannot approve"}


# =============================================================================
# DRILL DOOR — replay both checkers on a real message, NO writes
# =============================================================================

@consensus_router.post("/consensus/drill/{message_id}")
def consensus_drill(message_id: str, _: bool = Depends(require_admin)):
    """Side-by-side replay of Checker A and Checker B on a real Gmail
    document message [admin]. Writes NOTHING — the proving ground."""
    try:
        from estimate_verifier import (fetch_message_full, process_message,
                                       verify_pdf_from_doc)
        from freight_routes import _detect_pdf_supplier
    except Exception as e:
        return {"status": "error", "message": f"verifier import failed: {e}"}

    msg = fetch_message_full(message_id)
    if not msg:
        return {"status": "error", "message": "could not fetch message"}

    # Checker A: the existing dry-run (parse + diff, no writes)
    a = process_message(message_id, force=True, dry_run=True)

    # Checker B: raw text per document
    b_runs = []
    for att in msg.get("attachments") or []:
        sup = _detect_pdf_supplier(att["data"])
        if not sup:
            continue
        text = pdf_text(att["data"])
        try:
            va = verify_pdf_from_doc(att["data"], sup)
            oid = va.get("po") and "".join(
                c for c in str(va["po"]) if c.isdigit())
        except Exception:
            oid = None
        reader = second_reader(oid or "", sup, text) if oid else \
            {"verdict": "unavailable", "note": "no PO resolved"}
        b_runs.append({"document": att["filename"], "supplier": sup,
                       "order_id": oid, "reader": reader})
    if msg.get("html") and not b_runs:
        text = html_text(msg["html"])
        for r in (a.get("results") or []):
            if r.get("po") and r.get("supplier"):
                oid = "".join(c for c in str(r["po"]) if c.isdigit())
                b_runs.append({"document": "html-body",
                               "supplier": r["supplier"], "order_id": oid,
                               "reader": second_reader(oid, r["supplier"],
                                                       text)})

    # consensus per matching result
    verdicts = []
    for r in (a.get("results") or []):
        parser_ok = bool(r.get("report_ok"))
        b = next((x for x in b_runs if x.get("supplier") == r.get("supplier")),
                 None)
        cv = consensus_verdict(parser_ok,
                               (b or {}).get("reader") or
                               {"verdict": "unavailable",
                                "note": "no reader run for this document"})
        verdicts.append({"supplier": r.get("supplier"),
                         "doc_ref": r.get("doc_ref"),
                         "parser_ok": parser_ok,
                         "reader_verdict": ((b or {}).get("reader") or {}).get("verdict"),
                         "reader_findings": ((b or {}).get("reader") or {}).get("findings"),
                         "consensus": cv})

    return {"status": "ok", "message_id": message_id,
            "subject": msg.get("subject"),
            "checker_a": a.get("results"),
            "checker_b": b_runs,
            "verdicts": verdicts,
            "law": "both clean -> OK; any flag or disagreement -> "
                   "DISCREPANCY, human eyes, nothing auto-approves"}
