"""
rl_bill_audit.py — the FREIGHT-BILL AUDITOR (Phase 2, William 2026-07-29
"proceed to Phase 2"; underlying ruling 2026-07-16: quote vs R+L billed).

AUDITOR v2 (William-ruled 2026-07-30, the 5693 lesson):
  Win some, lose some — small swings are EXPECTED and stay quiet; the
  running average over time should sit near zero. Only a WAY-OFF shipment
  screams.
  - WAY OFF = |charged - billed| beyond min(15% of charged, $75) —
    whichever bound is SMALLER trips first, either direction (a big WIN
    means the quote model is off too).
  - ROLLING 90-DAY LEDGER on every audit email: shipments, total charged,
    total billed, net margin, average per shipment. A shipment 91+ days
    old drops off. The ledger reads each order's CURRENT shipping_cost
    (not the value frozen at stamp time), so backfilling a missing charge
    heals the average retroactively.

Feed: R+L's BILL2 report emails (reports@rlcarriers.com, subject
"R&L CARRIERS - BILL2 - C0211X", usually forwarded from Connie's box). The
report is a CSV attachment. Columns (decoded from the real CS0043061797.CSV):

  PRO, BOL#, SHIPPER..., CONSIGNEE..., ETA, APP DATE, DEL DATE, SHIP DATE,
  PIECES, WEIGHT, APPT MSG, GROSS REV, NET REV, STATUS, PO

  NET REV  = what R+L actually bills us (net of discount) -> "billed"
  GROSS REV = tariff gross (recorded, not compared)

Order matching, in order of confidence:
  1. the PO column (R+L's reference: "5697")
  2. digits in BOL# ("PO 5693")
  3. PRO vs orders.pro_number / tracking / order_shipments.pro_number

For each matched row (real mode):
  - order_event `freight_billed` {pro, billed, gross, ship/del dates, status,
    report} — once per (pro, report), re-runs are idempotent;
  - comparison vs orders.shipping_cost (what the CUSTOMER was charged);
  - one summary email to orders@ with every row in a table (numbers in
    tables) — flagged rows first, 90-day ledger footer always.

Rides the ledger cycle (unprocessed BILL2 ledger rows) + manual door:
  POST /freight/rl-bill-audit/{message_id}?dry_run=true  [admin]
"""

import csv
import io
import json
import re
from typing import Dict, List, Optional

from db_helpers import get_db

INTERNAL_ALERT = "orders@cabinetsforcontractors.com"

WAY_OFF_PCT = 0.15    # 15% of charged...
WAY_OFF_ABS = 75.0    # ...or $75 — whichever is SMALLER trips the flag
LEDGER_DAYS = 90      # rolling window; 91+ days old drops off

_BILL2_SUBJ = re.compile(r"R&L CARRIERS - BILL2", re.I)
_OID_RE = re.compile(r"\b(5\d{3})\b")


def parse_bill2_csv(text: str) -> List[Dict]:
    rows = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return rows
    idx = {h.strip().upper(): i for i, h in enumerate(header)}

    def g(row, key):
        i = idx.get(key)
        return (row[i].strip() if i is not None and i < len(row) else "")

    for row in reader:
        if not row or not g(row, "PRO"):
            continue
        def money(key):
            try:
                return float(g(row, key).replace(",", "") or 0)
            except ValueError:
                return 0.0
        rows.append({
            "pro": g(row, "PRO"),
            "bol": g(row, "BOL#"),
            "po": g(row, "PO"),
            "consignee": g(row, "CONSIGNEE"),
            "ship_date": g(row, "SHIP DATE"),
            "del_date": g(row, "DEL DATE"),
            "eta": g(row, "ETA"),
            "status": g(row, "STATUS"),
            "pieces": g(row, "PIECES"),
            "weight": g(row, "WEIGHT"),
            "gross": money("GROSS REV"),
            "billed": money("NET REV"),
        })
    return rows


def _match_order(conn, r: Dict) -> Optional[str]:
    for source in (r.get("po", ""), r.get("bol", "")):
        m = _OID_RE.search(source or "")
        if m:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM orders WHERE order_id = %s",
                            (m.group(1),))
                if cur.fetchone():
                    return m.group(1)
    pro = r.get("pro") or ""
    if pro:
        with conn.cursor() as cur:
            cur.execute("""SELECT order_id FROM orders
                           WHERE pro_number = %s OR tracking ILIKE %s
                           LIMIT 1""", (pro, f"%{pro}%"))
            row = cur.fetchone()
            if row:
                return str(row[0])
            cur.execute("""SELECT order_id FROM order_shipments
                           WHERE pro_number = %s LIMIT 1""", (pro,))
            row = cur.fetchone()
            if row:
                return str(row[0])
    return None


def _already_stamped(conn, pro: str, report: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM order_events
                       WHERE event_type = 'freight_billed'
                         AND event_data::text LIKE %s
                         AND event_data::text LIKE %s
                       LIMIT 1""", (f'%{pro}%', f'%{report}%'))
        return bool(cur.fetchone())


def way_off_flag(charged: float, billed: float) -> str:
    """William 2026-07-30: threshold = min(15% of charged, $75) —
    whichever is smaller trips first; both directions flag."""
    if charged <= 0:
        return "no charged-shipping recorded on the order"
    margin = charged - billed
    threshold = min(charged * WAY_OFF_PCT, WAY_OFF_ABS)
    if abs(margin) > threshold:
        side = "LOSS" if margin < 0 else "WIN"
        return (f"WAY OFF ({side}) — margin ${margin:,.2f} beyond the "
                f"±${threshold:,.2f} band (smaller of 15% / $75)")
    return ""


def rolling_ledger(days: int = LEDGER_DAYS) -> Dict:
    """The win-some-lose-some gauge (William 2026-07-30): every
    freight_billed shipment from the last `days` days, margin computed
    against the order's CURRENT shipping_cost (self-healing on backfill),
    one entry per (pro, report). 91+ days old has dropped off."""
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT e.event_data, e.order_id,
                       COALESCE(o.shipping_cost, 0) AS charged_now
                FROM order_events e
                LEFT JOIN orders o ON o.order_id = e.order_id
                WHERE e.event_type = 'freight_billed'
                  AND e.created_at > NOW() - make_interval(days => %s)
                ORDER BY e.created_at DESC
            """, (int(days),))
            rows = cur.fetchall()
    seen = set()
    n = 0
    tot_billed = tot_charged = tot_margin = 0.0
    unrecorded = 0
    for row in rows:
        d = row["event_data"]
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:
                continue
        d = d or {}
        key = (d.get("pro"), d.get("report"))
        if key in seen:
            continue
        seen.add(key)
        billed = float(d.get("billed") or 0)
        charged = float(row.get("charged_now") or 0)
        if charged <= 0:
            unrecorded += 1
            continue
        n += 1
        tot_billed += billed
        tot_charged += charged
        tot_margin += (charged - billed)
    return {"window_days": int(days), "shipments": n,
            "total_charged": round(tot_charged, 2),
            "total_billed": round(tot_billed, 2),
            "net_margin": round(tot_margin, 2),
            "avg_margin": round(tot_margin / n, 2) if n else 0.0,
            "unrecorded_charge": unrecorded}


def audit_bill2_message(message_id: str, dry_run: bool = True) -> Dict:
    """Parse one BILL2 email's CSV attachment(s), match rows to orders,
    stamp freight_billed events + send the audit summary (real mode)."""
    from estimate_verifier import fetch_message_full
    msg = fetch_message_full(message_id)
    if not msg:
        return {"status": "error", "message": "could not fetch message"}
    out = {"status": "ok", "message_id": message_id,
           "subject": msg.get("subject"), "dry_run": dry_run,
           "rows": [], "unmatched": [], "flagged": 0}

    csv_atts = [a for a in (msg.get("attachments") or [])
                if (a.get("filename") or "").lower().endswith(".csv")]
    if not csv_atts:
        return {"status": "error", "message": "no CSV attachment on message",
                "subject": msg.get("subject")}

    with get_db() as conn:
        for att in csv_atts:
            report = att.get("filename") or "report"
            try:
                text = att["data"].decode("utf-8", errors="replace")
            except Exception as e:
                out["rows"].append({"report": report, "error": str(e)})
                continue
            for r in parse_bill2_csv(text):
                r["report"] = report
                oid = _match_order(conn, r)
                if not oid:
                    out["unmatched"].append(
                        {"pro": r["pro"], "po": r["po"], "bol": r["bol"],
                         "consignee": r["consignee"]})
                    continue
                r["order_id"] = oid
                with conn.cursor() as cur:
                    cur.execute("""SELECT COALESCE(shipping_cost, 0)
                                   FROM orders WHERE order_id = %s""", (oid,))
                    row = cur.fetchone()
                charged = float(row[0] or 0) if row else 0.0
                r["charged"] = charged
                r["margin"] = round(charged - r["billed"], 2)
                r["flag"] = way_off_flag(charged, r["billed"])
                if r["flag"]:
                    out["flagged"] += 1
                out["rows"].append(r)

                if not dry_run and not _already_stamped(conn, r["pro"], report):
                    with conn.cursor() as cur:
                        cur.execute("""INSERT INTO order_events
                            (order_id, event_type, event_data, source)
                            VALUES (%s, 'freight_billed', %s,
                                    'rl_bill_audit')""",
                            (oid, json.dumps({
                                "pro": r["pro"], "report": report,
                                "billed": r["billed"], "gross": r["gross"],
                                "charged": charged, "margin": r["margin"],
                                "ship_date": r["ship_date"],
                                "del_date": r["del_date"],
                                "status": r["status"], "flag": r["flag"]})))
                        conn.commit()

    try:
        out["ledger_90d"] = rolling_ledger()
    except Exception as e:
        out["ledger_90d"] = {"error": str(e)}

    if not dry_run and out["rows"]:
        _send_summary(out)
    return out


def _send_summary(out: Dict):
    flagged = [r for r in out["rows"] if r.get("flag")]
    clean = [r for r in out["rows"] if not r.get("flag") and "error" not in r]

    def tr(r):
        return (f"<tr><td style='padding:4px 10px;border-bottom:1px solid #eee'>#{r.get('order_id','?')}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee'>{r['pro']}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee;text-align:right'>${r['billed']:,.2f}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee;text-align:right'>${r.get('charged',0):,.2f}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee;text-align:right'>${r.get('margin',0):,.2f}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee'>{r.get('flag') or 'ok'}</td>"
                f"<td style='padding:4px 10px;border-bottom:1px solid #eee'>{r.get('del_date') or r.get('status') or ''}</td></tr>")

    head = ("<tr><th style='padding:4px 10px;text-align:left'>Order</th>"
            "<th style='padding:4px 10px;text-align:left'>PRO</th>"
            "<th style='padding:4px 10px;text-align:right'>R+L billed</th>"
            "<th style='padding:4px 10px;text-align:right'>We charged</th>"
            "<th style='padding:4px 10px;text-align:right'>Margin</th>"
            "<th style='padding:4px 10px;text-align:left'>Flag</th>"
            "<th style='padding:4px 10px;text-align:left'>Delivered/status</th></tr>")
    rows_html = "".join(tr(r) for r in flagged + clean)
    unmatched = "".join(
        f"<li>PRO {u['pro']} (PO '{u['po']}', BOL '{u['bol']}', "
        f"{u['consignee'].strip()})</li>" for u in out["unmatched"])
    subj = (f"FREIGHT BILL AUDIT - {len(out['rows'])} shipment"
            f"{'s' if len(out['rows']) != 1 else ''}"
            + (f", {out['flagged']} WAY OFF" if out['flagged'] else " - all in band"))

    led = out.get("ledger_90d") or {}
    ledger_html = ""
    if led and not led.get("error"):
        ledger_html = (
            f"<p style='margin-top:14px'><strong>90-day ledger</strong> "
            f"(win some / lose some — average should sit near zero):<br>"
            f"{led['shipments']} shipments &middot; "
            f"charged ${led['total_charged']:,.2f} &middot; "
            f"billed ${led['total_billed']:,.2f} &middot; "
            f"net margin <strong>${led['net_margin']:,.2f}</strong> &middot; "
            f"average <strong>${led['avg_margin']:,.2f}/shipment</strong>"
            + (f" &middot; {led['unrecorded_charge']} with no charged-shipping "
               f"recorded (excluded)" if led.get("unrecorded_charge") else "")
            + "</p>")

    html = (f"<p>R+L BILL2 report processed "
            f"({out.get('subject', '')}).</p>"
            f"<table style='border-collapse:collapse;font-size:13px'>{head}"
            f"{rows_html}</table>"
            + (f"<p><strong>Unmatched rows:</strong><ul>{unmatched}</ul></p>"
               if unmatched else "")
            + ledger_html)
    try:
        from supplier_orders import _send_email
        first_oid = next((r.get("order_id") for r in out["rows"]
                          if r.get("order_id")), "")
        _send_email(first_oid, INTERNAL_ALERT, subj, html,
                    triggered_by="rl_bill_audit")
    except Exception as e:
        print(f"[BILL-AUDIT] summary send failed: {e}")


def process_bill2_reports(hours_back: int = 48, dry_run: bool = False) -> Dict:
    """Ledger-cycle rider: find unprocessed BILL2 emails, audit each once
    (idempotent via a bill2_audited order-less marker in order_events on the
    first matched order + per-(pro,report) stamp dedupe)."""
    out = {"status": "ok", "checked": 0, "audited": [], "already": 0,
           "errors": []}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT message_id, subject FROM email_ledger
                WHERE subject ILIKE '%%BILL2%%'
                  AND email_date > NOW() - make_interval(hours => %s)
            """, (int(hours_back),))
            rows = cur.fetchall()
        for mid, subject in rows:
            out["checked"] += 1
            with conn.cursor() as cur:
                cur.execute("""SELECT 1 FROM order_events
                               WHERE event_type = 'bill2_processed'
                                 AND event_data::text LIKE %s LIMIT 1""",
                            (f'%{mid}%',))
                if cur.fetchone():
                    out["already"] += 1
                    continue
            try:
                res = audit_bill2_message(mid, dry_run=dry_run)
                out["audited"].append(
                    {"message_id": mid, "rows": len(res.get("rows", [])),
                     "flagged": res.get("flagged"),
                     "status": res.get("status")})
                if not dry_run and res.get("status") == "ok":
                    anchor = next((r.get("order_id")
                                   for r in res.get("rows", [])
                                   if r.get("order_id")), None)
                    if anchor:
                        with conn.cursor() as cur:
                            cur.execute("""INSERT INTO order_events
                                (order_id, event_type, event_data, source)
                                VALUES (%s, 'bill2_processed', %s,
                                        'rl_bill_audit')""",
                                (anchor, json.dumps({"message_id": mid,
                                                     "subject": subject})))
                            conn.commit()
            except Exception as e:
                out["errors"].append(f"{mid}: {e}")
    return out
