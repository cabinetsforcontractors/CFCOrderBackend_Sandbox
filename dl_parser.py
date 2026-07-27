"""
dl_parser.py
DL Cabinetry order-confirmation EMAIL grammar (Magento dealer-portal email,
ecomm@dlcabinetry.com / dealerportal.dlcabinetry.com). Sister module of
roc_parser in the supplier_doc_parser grammar family.

Format (decoded from the real confirmation of 2026-04-06, forwarded by
William 2026-07-27 — JOB #5517 / their #DL010423):
  - OUR order id rides the SUBJECT: "DL Cabinetry order confirmation
    JOB Name #5517". The body carries no PO — the subject is the carrier,
    so the parser takes the subject alongside the html.
  - their order number in the body: "Order Number: #DL010423"
  - items table: per item the SKU appears twice (a name line, then
    "SKU: SSE B12"), followed by qty, then a $ amount which is the LINE
    TOTAL (proven: 70+45+110+126+126 = the $477.00 subtotal on the real
    email, not unit prices).
  - DL SKU dialect = {line code} SPACE {our form token}: "SSE B12" ->
    body B12 (space-separated where ROC hyphenates). Their line codes are
    their own -> verification runs in BODY space.
  - totals: Subtotal / Shipping & Handling / Tax / Grand Total.
"""

import re
from typing import Dict, List

_MONEY = re.compile(r"\$([\d,]+\.\d{2})")
_JOB_PO = re.compile(r"JOB\s*Name\s*#?\s*(\d{3,6})", re.IGNORECASE)
_DL_NUM = re.compile(r"#\s*(DL\d{5,})", re.IGNORECASE)


def _tokens(html: str) -> List[str]:
    """Tag-and-newline token split (same family as roc_parser._tokens —
    newlines added so the grammar also parses plain-text renderings)."""
    text = re.sub(r"<[^>]+>", "|", html or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("\r", "").replace("\n", "|")
    return [t.strip() for t in re.sub(r"\|+", "|", text).split("|") if t.strip()]


def looks_like_dl_confirmation(html: str, subject: str = "") -> bool:
    h = (html or "").lower()
    s = (subject or "").lower()
    return ("dlcabinetry" in h and "sku:" in h
            and ("dl cabinetry order confirmation" in s
                 or "thank you for placing an order with dl cabinetry" in h))


def parse_dl_confirmation_html(html: str, subject: str = "") -> Dict:
    """DL confirmation email -> {'supplier','po','dl_order_number','total',
    'lines':[{sku, qty, line_total}]}"""
    po = None
    m = _JOB_PO.search(subject or "") or _JOB_PO.search(html or "")
    if m:
        po = m.group(1)

    onum = None
    m = _DL_NUM.search(html or "")
    if m:
        onum = m.group(1).upper()

    toks = _tokens(html)
    lines = []
    for i, t in enumerate(toks):
        if not t.upper().startswith("SKU:"):
            continue
        sku = t.split(":", 1)[1].strip().upper()
        qty = None
        line_total = None
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

    total = None
    for k, tok in enumerate(toks):
        if tok.strip().lower() == "grand total":
            for j in range(k + 1, min(k + 3, len(toks))):
                mm = _MONEY.search(toks[j])
                if mm:
                    total = float(mm.group(1).replace(",", ""))
                    break
    return {"supplier": "DL", "po": po, "dl_order_number": onum,
            "total": total, "lines": lines}


def fold_dl_lines(lines: List[Dict]) -> List[Dict]:
    """Fold to body space: 'SSE B12' contributes itself, its no-space form,
    and the after-line-code body as match candidates -> {SSE B12, SSEB12,
    B12}."""
    out = []
    for ln in lines:
        sku = ln["sku"]
        bodies = [sku, sku.replace(" ", "")]
        parts = sku.split(None, 1)
        if len(parts) == 2:
            bodies.append(parts[1].replace(" ", ""))
        out.append({"bodies": bodies, "qty": ln["qty"], "raw": sku,
                    "flags": []})
    return out
