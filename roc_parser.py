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

CART-PAGE STOCK GRAMMAR (learned live 2026-07-17, order 5700): ROC's stock
truth is PAGE-LEVEL — "This product is out of stock." appears under the SKU
on the cart/product pages. The cart CSV export does NOT carry stock flags,
and the quick-order upload silently DROPS not-carried SKUs entirely. So the
workflow is: upload CSV -> copy the whole cart page -> parse_roc_cart_page.
"""

import re
from typing import Dict, List

_MONEY = re.compile(r"\$([\d,]+\.\d{2})")

# store SKU as it appears on cart/product pages: SNW-B18, A-BER-B,
# "SNW-BP2496 3/4" (space + slash tokens exist)
_CART_SKU = re.compile(r"\b([A-Z]{1,4}-[A-Z0-9][A-Z0-9\-\./]*(?:\s+\d/\d)?)\b")
_OOS_MARKER = re.compile(r"out\s+of\s+stock", re.IGNORECASE)


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


def parse_roc_cart_page(page_text: str) -> Dict:
    """Parse a PASTED ROC cart page (raw copy of the whole page — text, HTML,
    or markdown-ish paste all work) into stock status per store SKU.

    Grammar: SKUs appear as their own text runs (often twice: link text +
    plain); the line "This product is out of stock." appears BELOW the SKU it
    belongs to, before the next SKU. So: walk the text, remember the last SKU
    seen, and an out-of-stock marker flags that SKU.

    Returns {'skus': [in order seen], 'out_of_stock': [...], 'in_stock': [...],
    'oos_count', 'sku_count'}.
    """
    text = page_text or ""
    if "<" in text and ">" in text:  # tolerate raw HTML paste too
        text = re.sub(r"<[^>]+>", "\n", text)
    lines = [l.strip() for l in text.splitlines()]

    seen: List[str] = []
    oos: List[str] = []
    last_sku = None
    for line in lines:
        if not line:
            continue
        if _OOS_MARKER.search(line):
            if last_sku and last_sku not in oos:
                oos.append(last_sku)
            continue
        m = _CART_SKU.search(line.upper())
        if m:
            sku = m.group(1).strip()
            # ignore obvious non-SKU hits (URLs already stripped of scheme
            # won't match; money won't match)
            last_sku = sku
            if sku not in seen:
                seen.append(sku)

    in_stock = [s for s in seen if s not in oos]
    return {"skus": seen, "out_of_stock": oos, "in_stock": in_stock,
            "oos_count": len(oos), "sku_count": len(seen)}
