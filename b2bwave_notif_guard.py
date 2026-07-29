"""
b2bwave_notif_guard.py — the NEW-CUSTOMER NOTIFICATION GUARD
(William 2026-07-29: B2BWave customer notifications are OFF fleet-wide and
the robot sends the order emails; new customers — campaign signups — default
the checkbox back ON, so a guard sweeps strays off).

Rides the ledger cycle at most once per NOTIF_GUARD_HOURS (default 24; one
customers.json GET per run — API calls cost money, William's law). Manual
door: POST /b2bwave/notif-guard?dry_run=true [admin].

TRAP (proven 2026-07-29): rapid-fire PATCHes get connection-refused —
space calls and retry.
"""

import json
import os
import time
from typing import Dict

from db_helpers import get_db

GUARD_MARKER = "NOTIF-GUARD"


def _last_run(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT MAX(created_at) FROM order_events
                       WHERE order_id = %s
                         AND event_type = 'notif_guard_run'""",
                    (GUARD_MARKER,))
        row = cur.fetchone()
        return row[0] if row else None


def run_notif_guard(dry_run: bool = False, force: bool = False) -> Dict:
    out = {"status": "ok", "dry_run": dry_run, "patched": [], "failed": []}
    hours = float(os.environ.get("NOTIF_GUARD_HOURS", "24") or 24)
    with get_db() as conn:
        if not force:
            last = _last_run(conn)
            if last is not None:
                from datetime import datetime, timezone, timedelta
                if datetime.now(timezone.utc) - last < timedelta(hours=hours):
                    out["skipped"] = f"ran within the last {hours:g}h"
                    return out

    from substitutions import _b2b
    st, data = _b2b("GET", "customers.json")
    if st != 200 or not isinstance(data, list):
        out["status"] = "error"
        out["message"] = f"customers list failed (HTTP {st})"
        return out
    strays = []
    for row in data:
        c = row.get("customer", row) if isinstance(row, dict) else {}
        if c.get("receive_email_notifications"):
            strays.append({"id": c.get("id"),
                           "company": c.get("company_name") or c.get("name")})
    out["strays_found"] = len(strays)

    if not dry_run:
        for s in strays:
            done = False
            for attempt in range(3):
                try:
                    st2, _d = _b2b(
                        "PATCH", f"customers/{s['id']}",
                        {"customer": {"receive_email_notifications": False}})
                    if st2 == 200:
                        done = True
                        break
                except Exception:
                    pass
                time.sleep(3 + attempt * 3)
            (out["patched"] if done else out["failed"]).append(s)
            time.sleep(1.2)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO order_events
                    (order_id, event_type, event_data, source)
                    VALUES (%s, 'notif_guard_run', %s, 'notif_guard')""",
                    (GUARD_MARKER, json.dumps(
                        {"strays": len(strays),
                         "patched": len(out["patched"]),
                         "failed": [f["id"] for f in out["failed"]]})))
            conn.commit()
        if strays:
            try:
                from supplier_orders import _send_email
                rows = "".join(
                    f"<tr><td style='padding:4px 10px'>{s['id']}</td>"
                    f"<td style='padding:4px 10px'>{s['company']}</td></tr>"
                    for s in strays)
                _send_email(
                    "", "orders@cabinetsforcontractors.com",
                    f"NOTIF GUARD - {len(strays)} customer"
                    f"{'s' if len(strays) != 1 else ''} had B2BWave "
                    f"notifications back ON - turned off",
                    f"<p>The daily guard found and fixed these (new "
                    f"signups default the checkbox on):</p>"
                    f"<table style='border-collapse:collapse;font-size:13px'>"
                    f"<tr><th style='padding:4px 10px;text-align:left'>id</th>"
                    f"<th style='padding:4px 10px;text-align:left'>company</th>"
                    f"</tr>{rows}</table>",
                    triggered_by="notif_guard")
            except Exception as e:
                print(f"[NOTIF-GUARD] alert failed: {e}")
    return out
