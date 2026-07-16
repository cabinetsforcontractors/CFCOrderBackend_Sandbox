"""
freight_routes.py
Freight plan + supplier-sheet + order-verification endpoints — expose the
freight_logic engine and the website-SKU <-> supplier-SKU translation for
automatic warehouse ordering.

GET  /freight/plan/{order_id} [admin] — per-warehouse pallet plans (R+L
RateQuote-ready handling units + fees + flags). See freight_logic.py.

GET  /freight/supplier-sheet/{order_id} [admin] — the warehouse order sheet with
each line translated to the SUPPLIER'S OWN SKU (rta_products.supplier_sku,
loaded from the SOT map SUPPLIER_SKU_MAP_20260716.csv). This is the payload for
the auto-email-to-warehouse workflow (William 2026-07-16): order placed in
website SKUs -> lookup -> send the warehouse their SKUs.

POST /freight/verify-order/{order_id} [admin] — upload the supplier's returned
document (GHI Sales Order PDF or DuraStone NetSuite SO email HTML) -> parse to
our SKUs (supplier_doc_parser) -> two-sided diff vs the sent order ->
discrepancy report (missing/unexpected/qty/backorder/price-math). 50% of the
lane's value is verifying what the supplier confirmed. NEVER auto-accept.

POST /freight/scan-durastone [admin] — manual trigger for the DuraStone
NetSuite revision tripwire (also runs inside gmail_sync). Same SO# re-sent =
their mistake signal; alerts on dropped items/quantities.

GET  /freight/roc-csv/{order_id} [admin] — ROC quick-order CSV (sku,qty) for
roccabinetry.com/quick-order upload. Refuses if any ROC line is untranslated.

POST /freight/ghi-sheet/{order_id} [admin] — fill the GHI xlsx order sheet
(5707.xlsx format; template uploaded or GHI_TEMPLATE_PATH env) from the order's
GHI lines. Proven flow: order 5155 -> FTS tab, 13/14 lines, William approved.
"""

import os

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import PlainTextResponse, Response
from psycopg2.extras import RealDictCursor

from auth import require_admin
from config import SUPPLIER_INFO
from db_helpers import get_db
from freight_logic import plan_order

freight_router = APIRouter(tags=["freight"])


@freight_router.get("/freight/plan/{order_id}")
def get_freight_plan(order_id: str, residential: bool = False, liftgate: bool = False,
                     _: bool = Depends(require_admin)):
    """Per-warehouse freight plan for an order [admin]."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT sku, quantity, warehouse FROM order_line_items
                   WHERE order_id = %s""",
                (order_id,),
            )
            rows = cur.fetchall()
    if not rows:
        return {"status": "error", "message": f"no line items found for order {order_id}"}

    warehouses = {}
    for r in rows:
        wh = r.get("warehouse") or "UNMAPPED"
        warehouses.setdefault(wh, []).append({"sku": r["sku"], "quantity": r["quantity"]})

    plan = plan_order(warehouses, residential=residential, liftgate=liftgate)
    plan["status"] = "ok"
    plan["order_id"] = order_id
    return plan


@freight_router.get("/freight/supplier-sheet/{order_id}")
def get_supplier_sheet(order_id: str, _: bool = Depends(require_admin)):
    """
    Warehouse order sheet in SUPPLIER SKUs [admin].
    Groups the order's line items per warehouse and translates every website SKU
    to the supplier's own SKU via rta_products. Lines with no translation are
    returned under 'untranslated' — DO NOT auto-send those; they need a human.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
            if not order:
                return {"status": "error", "message": f"order {order_id} not found"}
            cur.execute(
                "SELECT sku, quantity, product_name, warehouse FROM order_line_items WHERE order_id = %s",
                (order_id,),
            )
            items = cur.fetchall()
            skus = [i["sku"] for i in items if i.get("sku")]
            lookup = {}
            if skus:
                cur.execute(
                    """SELECT product_sku, supplier, supplier_sku FROM rta_products
                       WHERE product_sku = ANY(%s)""",
                    (skus,),
                )
                lookup = {r["product_sku"]: r for r in cur.fetchall()}

    warehouses = {}
    for it in items:
        wh = it.get("warehouse") or "UNMAPPED"
        rec = lookup.get(it.get("sku") or "")
        if wh == "UNMAPPED" and rec and rec.get("supplier"):
            wh = rec["supplier"]
        if wh not in warehouses:
            sinfo = SUPPLIER_INFO.get(wh, {"name": wh, "address": "", "contact": "", "email": ""})
            warehouses[wh] = {
                "supplier_name": sinfo.get("name", wh),
                "supplier_email": sinfo.get("email", ""),
                "supplier_contact": sinfo.get("contact", ""),
                "items": [],
                "untranslated": [],
            }
        line = {
            "quantity": it.get("quantity") or 1,
            "website_sku": it.get("sku") or "",
            "supplier_sku": (rec or {}).get("supplier_sku") or "",
            "product_name": it.get("product_name") or "",
        }
        if line["supplier_sku"]:
            warehouses[wh]["items"].append(line)
        else:
            warehouses[wh]["untranslated"].append(line)

    ready = all(not w["untranslated"] for w in warehouses.values())
    return {
        "status": "ok",
        "order_id": order_id,
        "customer_name": order.get("company_name") or order.get("customer_name") or "",
        "comments": order.get("comments") or "",
        "warehouses": warehouses,
        "ready_to_send": ready,
    }


# =============================================================================
# ORDER VERIFICATION (two-sided diff) — auto-ordering lane 2026-07-16
# =============================================================================

# supplier key -> (website prefixes, warehouse-name aliases)
_VERIFY_SUPPLIERS = {
    "GHI": (("AKS", "APW", "GRSH", "NOR", "SNS", "SNW"), ("GHI", "GHI Cabinets")),
    "DuraStone": (("NSN", "CMEN", "NBDS", "SIV"), ("DuraStone",)),
}


def _order_lines_for_supplier(cur, order_id, prefixes, warehouse_names):
    """The order's line items belonging to one supplier (by website-SKU prefix,
    with the warehouse column as a fallback signal)."""
    cur.execute(
        "SELECT sku, quantity, warehouse FROM order_line_items WHERE order_id = %s",
        (order_id,),
    )
    out = []
    for r in cur.fetchall():
        sku = (r.get("sku") or "").upper()
        pre = sku.split("-")[0]
        wh = r.get("warehouse") or ""
        if pre in prefixes or wh in warehouse_names:
            out.append({"website_sku": r.get("sku"), "quantity": r.get("quantity") or 1})
    return out


@freight_router.post("/freight/verify-order/{order_id}")
async def verify_order(order_id: str, file: UploadFile = File(...),
                       supplier: str = "", _: bool = Depends(require_admin)):
    """
    Two-sided verification [admin]: upload the supplier's returned document,
    parse it back to website SKUs, diff against what we sent on this order.
    GHI = Sales Order PDF; DuraStone = NetSuite SO email HTML (.html/.eml body).
    The report is for a human — discrepancies are never auto-accepted.
    """
    import supplier_doc_parser as sdp

    data = await file.read()
    if not data:
        return {"status": "error", "message": "empty upload"}
    is_pdf = data[:5] in (b"%PDF-",) or data[:4] == b"%PDF"
    sup = (supplier or "").strip() or ("GHI" if is_pdf else "DuraStone")
    if sup not in _VERIFY_SUPPLIERS:
        return {"status": "error",
                "message": f"unsupported supplier '{sup}' (have: {sorted(_VERIFY_SUPPLIERS)})"}
    prefixes, wh_names = _VERIFY_SUPPLIERS[sup]

    try:
        with get_db() as conn:
            rev = sdp.build_reverse_map(conn)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                sent = _order_lines_for_supplier(cur, order_id, prefixes, wh_names)
            if sup == "GHI":
                parsed = sdp.resolve_ghi_lines(sdp.parse_ghi_pdf(data), rev)
                sent = sdp.expand_composites(sent, sdp.GHI_COMPOSITES)
            else:
                html = data.decode("utf-8", errors="ignore")
                parsed = sdp.resolve_durastone_lines(sdp.parse_durastone_email(html), rev)
                # keep every DS SO we see — feeds the revision tripwire history
                try:
                    sdp.record_and_check_ds_email(
                        conn, f"upload:{order_id}:{parsed.get('so_number')}", html)
                except Exception:
                    pass
    except Exception as e:
        return {"status": "error", "message": f"parse failed: {e}"}

    if not sent:
        return {"status": "error",
                "message": f"order {order_id} has no {sup} line items to verify"}
    report = sdp.two_sided_diff(sent, parsed["lines"])
    doc_po = parsed.get("po")
    po_matches = bool(doc_po) and str(doc_po).lstrip("0").startswith(str(order_id))
    if doc_po and not po_matches:
        report["flags"].append(f"DOCUMENT PO '{doc_po}' does not match order {order_id}")
        report["ok"] = False
    return {
        "status": "ok", "order_id": order_id, "supplier": sup,
        "document": {"po": doc_po, "so_number": parsed.get("so_number"),
                     "total": parsed.get("total"), "line_count": len(parsed["lines"])},
        "report": report,
    }


@freight_router.post("/freight/scan-durastone")
def scan_durastone(hours_back: int = 48, _: bool = Depends(require_admin)):
    """Manual trigger for the DuraStone NetSuite SO revision tripwire [admin].
    Also runs automatically inside gmail_sync."""
    from supplier_doc_parser import scan_durastone_emails

    try:
        with get_db() as conn:
            return scan_durastone_emails(conn, hours_back=hours_back)
    except Exception as e:
        return {"status": "error", "message": str(e)}


# =============================================================================
# OUTBOUND ADAPTERS — ROC quick-order CSV + GHI xlsx order sheet
# =============================================================================

@freight_router.get("/freight/roc-csv/{order_id}")
def roc_quick_order_csv(order_id: str, _: bool = Depends(require_admin)):
    """ROC quick-order CSV (sku,qty) for the portal upload [admin].
    Refuses when any ROC line is untranslated — those need a human first."""
    sheet = get_supplier_sheet(order_id, True)
    if sheet.get("status") != "ok":
        return sheet
    roc = None
    for wh, w in (sheet.get("warehouses") or {}).items():
        if "ROC" in wh.upper():
            roc = w
            break
    if not roc:
        return {"status": "error", "message": f"order {order_id} has no ROC warehouse lines"}
    if roc["untranslated"]:
        return {"status": "error",
                "message": "ROC lines without supplier SKU — resolve before upload",
                "untranslated": roc["untranslated"]}
    csv_text = "sku,qty\n" + "".join(
        f"{i['supplier_sku']},{i['quantity']}\n" for i in roc["items"])
    return PlainTextResponse(
        csv_text, media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ROC_order_{order_id}.csv"})


@freight_router.post("/freight/ghi-sheet/{order_id}")
async def ghi_order_sheet(order_id: str, template: UploadFile = File(None),
                          report_only: bool = False,
                          _: bool = Depends(require_admin)):
    """
    Fill the GHI xlsx order sheet from this order's GHI lines [admin].
    Template = upload, or GHI_TEMPLATE_PATH env (the 5707.xlsx format).
    report_only=true returns the placement report JSON instead of the file —
    check unplaced/unmapped BEFORE sending anything.
    """
    import supplier_doc_parser as sdp

    tpl_bytes = None
    if template is not None:
        tpl_bytes = await template.read()
    if not tpl_bytes:
        tpl_path = os.environ.get("GHI_TEMPLATE_PATH", "").strip()
        if tpl_path and os.path.exists(tpl_path):
            with open(tpl_path, "rb") as f:
                tpl_bytes = f.read()
    if not tpl_bytes:
        return {"status": "error",
                "message": "no GHI template: upload 'template' or set GHI_TEMPLATE_PATH"}

    prefixes, wh_names = _VERIFY_SUPPLIERS["GHI"]
    with get_db() as conn:
        fwd = sdp.build_forward_map(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            items = _order_lines_for_supplier(cur, order_id, prefixes, wh_names)
            cur.execute("SELECT company_name, customer_name FROM orders WHERE order_id = %s",
                        (order_id,))
            order = cur.fetchone() or {}
    if not items:
        return {"status": "error", "message": f"order {order_id} has no GHI line items"}
    ship_to = (order.get("company_name") or order.get("customer_name") or "")
    try:
        xlsx, report = sdp.make_ghi_sheets(items, tpl_bytes, order_id, fwd,
                                           ship_to=f"{ship_to} / PO {order_id}".strip(" /"))
    except Exception as e:
        return {"status": "error", "message": f"sheet generation failed: {e}"}
    if report_only or report["unplaced"] or report["unmapped_prefix"]:
        # anything unplaced needs a human (Misc sheet / email note) — return the
        # report instead of a silently-incomplete sheet
        return {"status": "ok" if not (report["unplaced"] or report["unmapped_prefix"])
                else "needs_review",
                "order_id": order_id, "report": report,
                "note": "re-run with report_only=false once clean, or handle unplaced lines manually"}
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=GHI_ORDER_{order_id}.xlsx",
                 "X-GHI-Placed": str(len(report["placed"])),
                 "X-GHI-Tabs": ",".join(report["tabs"])})
