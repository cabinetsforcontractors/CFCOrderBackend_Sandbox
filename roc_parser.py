"""
roc_parser.py
ROC Cabinetry order-confirmation / invoice EMAIL grammar (automated HTML from
weborders@roccabinetry.com). Belongs to the supplier_doc_parser grammar
family; kept as its own module.

Format (decoded from real confirmation 2026-07-06, order #000040179):
  - carries OUR PO: "PO Number# 5684"
  - their order number: "#000040179" (9 digits)
  - items table: per row the store SKU appears twice ("SNW-B12" then
    "SKU: SNW-B12"), followed by qty and the LINE TOTAL ("$615.08"), then an
    Assembly Charges cell ("-" or an amount).
  NOTE: ROC's store SKUs are their own codes (this order used SNW-*, which
  coincidentally collides with our SNW prefix) — verification therefore runs
  in BODY space against the sent lines' supplier tokens.
"""

import re
from typing import Dict, List

_MONEY = re.compile(r"\$([\d,]+\.\d{2})")


def _tokens(html: str) -> List[str]:
    text = re.sub(r"<[^>]+>", "|", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return [t.strip() for t in re.sub(r"\|+", "|", text).split("|") if t.strip()]


def parse_roc_confirmation_html(html: str) -> Dict:
    """ROC confirmation/invoice HTML -> {'supplier','po','roc_order_number',
    'total','lines':[{sku, qty, line_total}]}"""
    po = None
    m = re.search(r"PO\s*Number\s*#?\s*(\d{3,6}[A-Z]?)", html, re.IGNORECASE)
    if m:
        po = m.group(1)
    onum = None
    m = re.search(r"#\s*(\d{9})", html)
    if m:
        onum = m.group(1)

    toks = _tokens(html)
    lines = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.upper().startswith("SKU:"):
            sku = t.split(":", 1)[1].strip().upper()
            qty = None
            line_total = None
            # qty = next pure-integer token; total = next $ token (within a few cells)
            for j in range(i + 1, min(i + 5, len(toks))):
                tj = toks[j]
                if qty is None and re.fullmatch(r"\d{1,4}", tj):
                    qty = int(tj)
                    continue
                mm = _MONEY.search(tj)
                if qty is not None and mm:
                    line_total = float(mm.group(1).replace(",", ""))
                    break
            if sku and qty:
                lines.append({"sku": sku, "qty": qty, "line_total": line_total})
        i += 1

    total = None
    for k, tok in enumerate(toks):
        if tok.strip().lower() == "grand total" or tok.strip().lower() == "total":
            for j in range(k + 1, min(k + 3, len(toks))):
                mm = _MONEY.search(toks[j])
                if mm:
                    total = float(mm.group(1).replace(",", ""))
                    break
    return {"supplier": "ROC", "po": po, "roc_order_number": onum,
            "total": total, "lines": lines}


def fold_roc_lines(lines: List[Dict]) -> List[Dict]:
    """Fold to body space: each ROC store SKU contributes itself and its
    after-prefix body as match candidates (SNW-B12 -> {SNWB12, B12})."""
    out = []
    for ln in lines:
        sku = ln["sku"]
        bodies = [sku]
        if "-" in sku:
            bodies.append(sku.split("-", 1)[1])
        out.append({"bodies": bodies, "qty": ln["qty"], "raw": sku, "flags": []})
    return out


def looks_like_roc_confirmation(html: str) -> bool:
    h = (html or "").lower()
    return ("roc cabinetry" in h and "sku:" in h
            and ("order confirmation" in h or "po number" in h or "invoice" in h))
