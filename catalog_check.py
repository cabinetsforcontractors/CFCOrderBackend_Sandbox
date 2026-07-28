"""
catalog_check.py — SUBSTITUTION INTAKE TRIGGER (William 2026-07-28, "yes"):
catch SKUs that are on the website but do NOT exist in the supplier's real
catalog (the 5731 B09 case — GHI makes only B09-FH) BEFORE an invoice exists.

Catalog truth today = supplier_cogs (GHI only — William's cogs.csv mirrors
GHI's MASTER LIST token-for-token). Lines whose supplier token is missing
from the catalog are flagged; when exactly ONE catalog token extends or
shortens the missing token (B09 -> B09-FH), that becomes the substitution
candidate (website sku by the line convention: PREFIX-TOKEN). Flags NEVER
block silently — auto_invoice turns them into a proposal + NEEDS-A-HUMAN.

Suppliers without loaded catalog truth are not judged.
"""

from db_helpers import get_db


def phantom_sku_check(order_id: str) -> dict:
    """Returns {"flagged": [{sku, token, candidate_sku|None}], "checked": N}."""
    import supplier_doc_parser as sdp
    from ghi_cogs import load_cogs_map
    from supplier_doc_parser import GHI_PREFIXES, norm

    cogs = load_cogs_map()
    if not cogs:
        return {"flagged": [], "checked": 0, "note": "no catalog truth loaded"}

    by_prefix = {}
    for (pre, tok) in cogs:
        by_prefix.setdefault(pre, set()).add(norm(tok).rstrip("*"))

    with get_db() as conn:
        fwd = sdp.build_forward_map(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT sku FROM order_line_items WHERE order_id = %s",
                        (order_id,))
            skus = [r[0] for r in cur.fetchall() if r[0]]

    flagged, checked = [], 0
    for sku in skus:
        pre = (sku or "").split("-")[0].upper()
        toks = by_prefix.get(pre)
        if pre not in GHI_PREFIXES or not toks:
            continue
        checked += 1
        body = sku.split("-", 1)[1] if "-" in sku else sku
        tok = norm(fwd.get(sku) or body).rstrip("*")
        if tok in toks:
            continue
        # composite pantries and known dialect fixes are legitimate non-catalog
        # tokens — never phantom
        if tok in {norm(k) for k in sdp.GHI_COMPOSITES}:
            continue
        dialect = norm(sdp.GHI_DIALECT.get(body, ""))
        if dialect and dialect in toks:
            continue
        cands = sorted(t for t in toks
                       if (t.startswith(tok) or tok.startswith(t)) and t != tok)
        candidate = f"{pre}-{cands[0]}" if len(cands) == 1 else None
        flagged.append({"sku": sku, "token": tok, "candidate_sku": candidate,
                        "near": cands[:4]})
    return {"flagged": flagged, "checked": checked}
