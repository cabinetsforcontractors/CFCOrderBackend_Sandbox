"""
freight_monthly.py — Wave 5 smalls (William 2026-08-02): the MONTHLY
FREIGHT ROLL-UP + snapshot, riding the ledger cycle.

On the first cycle of each new month (idempotent per month via an
order_events stamp) it emails wpjob1 the charged-vs-quoted picture from
the freight_billed event ledger (the 7/29 bill auditor's records) plus
the month's shipment counts. The R+L ShipmentHistory API isn't wired in
the repo, so the snapshot is built from OUR OWN records — stated plainly
in the email, never implied otherwise.
"""

import json
import os
from datetime import date

from db_helpers import get_db

INTERNAL_ALERT_EMAIL = os.environ.get(
    "WAREHOUSE_NOTIFICATION_EMAIL", "orders@cabinetsforcontractors.com").strip()


def _month_key() -> str:
    t = date.today()
    return f"{t.year:04d}-{t.month:02d}"


def _already_ran(conn, key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM order_events
                       WHERE event_type = 'freight_monthly_rollup'
                         AND event_data::text ILIKE %s LIMIT 1""",
                    (f"%{key}%",))
        return cur.fetchone() is not None


def run_monthly_rollup() -> dict:
    key = _month_key()
    out = {"month": key, "ran": False}
    with get_db() as conn:
        if _already_ran(conn, key):
            return out
        # last ~35 days of freight_billed events (the bill auditor's ledger)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_id, event_data FROM order_events
                WHERE event_type = 'freight_billed'
                  AND created_at > NOW() - INTERVAL '35 days'
                ORDER BY created_at
            """)
            billed_rows = cur.fetchall()
            cur.execute("""
                SELECT COUNT(*) FROM order_events
                WHERE event_type IN ('rl_delivered',
                                     'customer_delivery_confirmed')
                  AND created_at > NOW() - INTERVAL '35 days'
            """)
            delivered_count = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*) FROM order_events
                WHERE event_type = 'bol_created'
                  AND created_at > NOW() - INTERVAL '35 days'
            """)
            bol_count = cur.fetchone()[0]

        rows_html, tot_billed, tot_quoted, n = "", 0.0, 0.0, 0
        for oid, data in billed_rows:
            try:
                d = json.loads(data) if isinstance(data, str) else (data or {})
            except Exception:
                d = {}
            billed = float(d.get("billed") or d.get("gross") or 0)
            quoted = float(d.get("quoted") or d.get("quote") or 0)
            tot_billed += billed
            tot_quoted += quoted
            n += 1
            diff = billed - quoted if quoted else None
            rows_html += (
                f"<tr><td style='padding:3px 10px;'>#{oid}</td>"
                f"<td style='padding:3px 10px;'>{d.get('pro', '')}</td>"
                f"<td style='padding:3px 10px;' align='right'>${quoted:,.2f}</td>"
                f"<td style='padding:3px 10px;' align='right'>${billed:,.2f}</td>"
                f"<td style='padding:3px 10px;' align='right'>"
                f"{('$%.2f' % diff) if diff is not None else '—'}</td></tr>")

        html = (
            f"<div style='font-family:Arial,sans-serif;font-size:14px;'>"
            f"<p><strong>Monthly freight roll-up — {key}</strong> "
            f"(built from our own records; the R+L ShipmentHistory API is "
            f"not wired, so nothing here comes from R+L's side).</p>"
            f"<p>BOLs created (35 days): <strong>{bol_count}</strong> &middot; "
            f"deliveries recorded: <strong>{delivered_count}</strong> &middot; "
            f"freight bills audited: <strong>{n}</strong></p>"
            + (f"<table style='border-collapse:collapse;font-size:13px;'>"
               f"<tr style='background:#f2f2f2;'>"
               f"<th style='padding:3px 10px;'>Order</th>"
               f"<th style='padding:3px 10px;'>PRO</th>"
               f"<th style='padding:3px 10px;'>Quoted</th>"
               f"<th style='padding:3px 10px;'>Billed</th>"
               f"<th style='padding:3px 10px;'>Diff</th></tr>{rows_html}"
               f"<tr><td colspan='2' style='padding:3px 10px;'>"
               f"<strong>Totals</strong></td>"
               f"<td style='padding:3px 10px;' align='right'>"
               f"<strong>${tot_quoted:,.2f}</strong></td>"
               f"<td style='padding:3px 10px;' align='right'>"
               f"<strong>${tot_billed:,.2f}</strong></td>"
               f"<td style='padding:3px 10px;' align='right'>"
               f"<strong>${tot_billed - tot_quoted:,.2f}</strong></td></tr>"
               f"</table>" if n else
               "<p>No audited freight bills in the window.</p>")
            + "</div>")

        from supplier_orders import _send_email
        res = _send_email("", INTERNAL_ALERT_EMAIL,
                          f"Monthly freight roll-up - {key} "
                          f"({n} bills, {delivered_count} deliveries)",
                          html, triggered_by="freight_monthly")
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES ('', 'freight_monthly_rollup', %s, 'freight_monthly')
            """, (json.dumps({"month": key, "bills": n,
                              "delivered": delivered_count,
                              "sent": res.get("success")}),))
            conn.commit()
        out.update(ran=True, bills=n, delivered=delivered_count,
                   sent=res.get("success"))
    return out
