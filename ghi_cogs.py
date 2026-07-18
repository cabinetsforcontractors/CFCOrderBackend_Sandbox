"""
ghi_cogs.py
GHI COGS PRICE FINGERPRINT (William ruling 2026-07-18, the 5693 / SO 17118 case).

GHI sales orders bill our COGS to the penny (proven: SO 17024 wall line billed
149.36 x 4 = 597.44 = W361824 walnut cost EXACTLY; the standard-depth W3618
costs 117.32 — nowhere close). So the line PRICE independently identifies which
cabinet GHI actually keyed, even when the truncated description is ambiguous.

Belt & suspenders (William): the description says WHICH cabinet, the price
CONFIRMS it. A price that matches a DIFFERENT sku than the description resolved
to = flag -> discrepancy -> human. This is a SANITY CHECK ONLY — it never
auto-resolves a line by price alone, and a missing COGS row never blocks.

COGS source: William's cogs.csv (Desktop\\VERIFIED SOT 6_30_26\\COGS FILES\\GHI),
uploaded via POST /supplier-orders/cogs/GHI (multipart, his exact CSV format:
SKU column + one COGS_* column per door color). RELOAD AFTER GHI REPRICES —
Todd Gertz's 6% price break is coming; stale COGS will flag every line as
"price list may have changed" until the new file is uploaded.
"""

import csv
import io

from fastapi import APIRouter, Depends, File, UploadFile

from auth import require_admin
from db_helpers import get_db

ghi_cogs_router = APIRouter(tags=["ghi-cogs"])

# cogs.csv column header -> our website line prefix
GHI_COGS_COLUMNS = {
    "COGS_NORFOLK_LINEN": "NOR",
    "COGS_SHAKER_APPALACHIAN_WALNUT": "APW",
    "COGS_SHAKER_APPALACHIAN_KNOTTY": "AKS",
    "COGS_SHAKER_GREIGE": "GRSH",
    "COGS_SONONA_SAND": "SNS",
    "COGS_SANIBEL_SEA_OATS": "SNW",
}

# GHI bills cost to the penny (SO 17024 proof) — tolerance is cents-level
PRICE_TOL = 0.02

_cache = {"map": None}


def ensure_cogs_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supplier_cogs (
                supplier VARCHAR(20) NOT NULL,
                line_prefix VARCHAR(10) NOT NULL,
                token VARCHAR(60) NOT NULL,
                cost NUMERIC(12,2) NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (supplier, line_prefix, token)
            )
        """)
        conn.commit()


def load_cogs_map():
    """{(line_prefix, TOKEN): cost} for GHI. Cached per process; upload clears."""
    if _cache["map"] is not None:
        return _cache["map"]
    m = {}
    try:
        with get_db() as conn:
            ensure_cogs_table(conn)
            with conn.cursor() as cur:
                cur.execute("""SELECT line_prefix, token, cost FROM supplier_cogs
                               WHERE supplier = 'GHI'""")
                for pre, tok, cost in cur.fetchall():
                    m[(pre, str(tok).upper())] = float(cost)
    except Exception:
        return {}
    _cache["map"] = m
    return m


def annotate_price_fingerprint(lines):
    """Attach price-fingerprint verdicts to parsed+resolved GHI SO lines.

    Sets on each line (when COGS data exists for its prefix):
      price_id_ok = True             — billed price == cost of the resolved sku
      memo = "PRICE-ID: ..."         — price matches a DIFFERENT sku than the
                                       description resolved to (two_sided_diff
                                       turns memo into a flag -> discrepancy)
      memo = "PRICE-CHECK: ..."      — price matches nothing incl. the resolved
                                       sku (likely a reprice -> reload cogs.csv)
      note (unresolved lines only)   — which sku(s) the price points at, as a
                                       hint for the human; never auto-resolves
    """
    cogs = load_cogs_map()
    if not cogs:
        return lines
    for ln in lines:
        price = ln.get("price")
        pre = ln.get("line_prefix")
        if not price or not pre:
            continue
        sku = ln.get("website_sku") or ""
        body = sku.split("-", 1)[1].upper() if "-" in sku else None
        tokens = ([body] if body else []) + [str(t).upper()
                                             for t in (ln.get("ghi_tokens") or [])]
        expected = None
        for t in tokens:
            if (pre, t) in cogs:
                expected = cogs[(pre, t)]
                break
        price_matches = sorted({t for (p, t), c in cogs.items()
                                if p == pre and abs(c - float(price)) <= PRICE_TOL})
        if expected is not None:
            if abs(expected - float(price)) <= PRICE_TOL:
                ln["price_id_ok"] = True
            elif price_matches:
                ln["memo"] = (f"PRICE-ID: billed {float(price):.2f} = "
                              f"{'/'.join(price_matches)} cost, but line resolved to "
                              f"{body or '?'} (cost {expected:.2f})")
            else:
                ln["memo"] = (f"PRICE-CHECK: billed {float(price):.2f} vs COGS "
                              f"{expected:.2f} for {body or '?'} - price list may "
                              f"have changed, reload cogs.csv")
        elif price_matches and not sku:
            ln["note"] = ((ln.get("note") + "; ") if ln.get("note") else "") + \
                f"price {float(price):.2f} matches {'/'.join(price_matches)}"
    return lines


@ghi_cogs_router.post("/supplier-orders/cogs/{supplier}")
async def upload_cogs(supplier: str, file: UploadFile = File(...),
                      _: bool = Depends(require_admin)):
    """Load William's cogs.csv (SKU + COGS_<color> columns) into supplier_cogs."""
    if supplier.upper() != "GHI":
        return {"status": "error", "message": "only GHI cogs supported today"}
    raw = (await file.read()).decode("utf-8-sig", errors="ignore")
    rdr = csv.DictReader(io.StringIO(raw))
    cols = {c: GHI_COGS_COLUMNS[c] for c in (rdr.fieldnames or [])
            if c in GHI_COGS_COLUMNS}
    if not cols:
        return {"status": "error",
                "message": f"no known COGS columns in {rdr.fieldnames}"}
    rows = 0
    with get_db() as conn:
        ensure_cogs_table(conn)
        with conn.cursor() as cur:
            for row in rdr:
                tok = (row.get("SKU") or "").strip().upper()
                if not tok:
                    continue
                for col, pre in cols.items():
                    val = (row.get(col) or "").strip()
                    if not val:
                        continue
                    try:
                        cost = float(val.replace(",", "").replace("$", ""))
                    except ValueError:
                        continue
                    cur.execute("""
                        INSERT INTO supplier_cogs
                            (supplier, line_prefix, token, cost, updated_at)
                        VALUES ('GHI', %s, %s, %s, NOW())
                        ON CONFLICT (supplier, line_prefix, token)
                        DO UPDATE SET cost = EXCLUDED.cost, updated_at = NOW()
                    """, (pre, tok, cost))
                    rows += 1
            conn.commit()
    _cache["map"] = None
    return {"status": "ok", "rows_upserted": rows,
            "columns_loaded": cols}


@ghi_cogs_router.get("/supplier-orders/cogs/{supplier}/summary")
def cogs_summary(supplier: str, _: bool = Depends(require_admin)):
    with get_db() as conn:
        ensure_cogs_table(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT line_prefix, COUNT(*), MAX(updated_at)
                           FROM supplier_cogs WHERE supplier = %s
                           GROUP BY line_prefix ORDER BY line_prefix""",
                        (supplier.upper(),))
            rows = cur.fetchall()
    return {"status": "ok", "supplier": supplier.upper(),
            "lines": [{"line_prefix": r[0], "tokens": r[1],
                       "updated_at": str(r[2])} for r in rows]}
