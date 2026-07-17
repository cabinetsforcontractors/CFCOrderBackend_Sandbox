"""
ghi_checks.py
GHI CHECK TRACKER + NAGGER (William rulings 2026-07-16: GHI is paid by CHECK
from Square or MidFlorida CU; GHI is bad about cashing them — "we have to be
very pushy with ghi").

Flow per check record:
  1. pending_send — a check needs to be written/mailed. EVERY DAY William
     gets "Send check for $X to GHI for PO Y" with buttons
     [I mailed it] / [Remind me later] until he marks it sent.
  2. sent — check is in the mail. After CLEAR_DAYS (14) with no clearing,
     GHI gets a nag ("check was mailed on DATE — did you receive it?",
     CC us) and the nag REPEATS every NAG_EVERY_DAYS (2) until cleared.
  3. cleared / canceled — done.

Clearing detection: MANUAL for now ([Check cleared] button on the landing
page). Gmail-watch for the bank's check-cleared alert gets wired once
William forwards a sample alert email (grammar unknown until then).

Daily cadence with no new cron: run_daily_check_nags() is called from the
gmail sync loop (every ~15 min) but self-limits to one reminder per check
per ET calendar day.

All emails go through the guarded dispatch mailer (EMAIL_ALLOWLIST applies —
in the beta everything redirects to the test inbox).
"""

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from auth import require_admin
from db_helpers import get_db

ghi_check_router = APIRouter(tags=["ghi-checks"])

CLEAR_DAYS = 14          # uncashed this long -> start nagging GHI
NAG_EVERY_DAYS = 2       # repeat GHI nag cadence (pushy, per William)
_ET_OFFSET = timedelta(hours=-5)  # ET approximation for "one reminder per day"


def _et_date(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + _ET_OFFSET).strftime("%Y-%m-%d")


def ensure_ghi_checks_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ghi_checks (
                id SERIAL PRIMARY KEY,
                token VARCHAR(64) UNIQUE NOT NULL,
                order_id VARCHAR(20) NOT NULL,
                amount DECIMAL(12,2) NOT NULL,
                status VARCHAR(20) DEFAULT 'pending_send',
                note TEXT,
                check_sent_at TIMESTAMP WITH TIME ZONE,
                cleared_at TIMESTAMP WITH TIME ZONE,
                last_reminder_at TIMESTAMP WITH TIME ZONE,
                last_ghi_nag_at TIMESTAMP WITH TIME ZONE,
                ghi_nag_count INTEGER DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        conn.commit()


def create_check(order_id: str, amount: float, note: str = None) -> Dict:
    """One open check record per order (refuse duplicates while one is
    pending/sent — same double-open philosophy as substitutions)."""
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_ghi_checks_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT id, status FROM ghi_checks
                           WHERE order_id = %s AND status IN ('pending_send', 'sent')""",
                        (str(order_id),))
            row = cur.fetchone()
            if row:
                return {"status": "error",
                        "message": f"check #{row['id']} for order {order_id} is "
                                   f"already open ({row['status']})"}
            token = secrets.token_urlsafe(24)
            cur.execute("""
                INSERT INTO ghi_checks (token, order_id, amount, note)
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (token, str(order_id), float(amount), note))
            check_id = cur.fetchone()["id"]
            cur.execute("""
                INSERT INTO order_events (order_id, event_type, event_data, source)
                VALUES (%s, 'ghi_check_created', %s, 'ghi_checks')
            """, (str(order_id), json.dumps({"check_id": check_id,
                                             "amount": float(amount)})))
            conn.commit()
    # fire the first reminder immediately
    result = {"status": "ok", "check_id": check_id, "token": token}
    try:
        result["first_reminder"] = _send_send_reminder(_get_by_id(check_id))
    except Exception as e:
        result["first_reminder"] = {"success": False, "error": str(e)}
    return result


def _get_by_id(check_id: int) -> Optional[Dict]:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_ghi_checks_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM ghi_checks WHERE id = %s", (check_id,))
            return cur.fetchone()


def _get_by_token(token: str) -> Optional[Dict]:
    from psycopg2.extras import RealDictCursor
    with get_db() as conn:
        ensure_ghi_checks_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM ghi_checks WHERE token = %s", (token,))
            return cur.fetchone()


def _landing_url(token: str) -> str:
    import os
    base = os.environ.get("CHECKOUT_BASE_URL",
                          "https://cfcorderbackend-sandbox.onrender.com").strip().rstrip("/")
    return f"{base}/ghi-check/{token}"


def _send_send_reminder(check: Dict) -> Dict:
    """Daily 'send the check' email to US with the two buttons."""
    from supplier_orders import _send_email, INTERNAL_ALERT_EMAIL
    url = _landing_url(check["token"])
    days_open = (datetime.now(timezone.utc) -
                 check["created_at"]).days if check.get("created_at") else 0
    urgency = f" (day {days_open + 1})" if days_open else ""
    note_html = f"<p>{check['note']}</p>" if check.get("note") else ""
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.6;'>"
            f"<p><strong>Send check for ${float(check['amount']):,.2f} to GHI "
            f"for PO {check['order_id']}</strong>{urgency}</p>"
            f"{note_html}"
            f"<p>"
            f"<a href='{url}?intent=sent' style='display:inline-block;padding:12px 24px;"
            f"border-radius:6px;color:#fff;background:#1dc9b7;text-decoration:none;"
            f"font-weight:bold;margin-right:8px;'>&#10003; I mailed it</a>"
            f"<a href='{url}' style='display:inline-block;padding:12px 24px;"
            f"border-radius:6px;color:#fff;background:#888;text-decoration:none;"
            f"font-weight:bold;'>Remind me later</a></p>"
            f"<p style='color:#888;font-size:12px;'>This reminder repeats daily "
            f"until the check is marked mailed.</p></div>")
    send = _send_email(check["order_id"], INTERNAL_ALERT_EMAIL,
                       f"SEND CHECK: ${float(check['amount']):,.2f} to GHI - PO {check['order_id']}",
                       html, "ghi_check_reminder")
    if send.get("success"):
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ghi_checks SET last_reminder_at = NOW() WHERE id = %s",
                            (check["id"],))
                conn.commit()
    return send


def _send_ghi_nag(check: Dict) -> Dict:
    """Pushy 'did you get/cash our check' email TO GHI, CC us."""
    from config import SUPPLIER_INFO
    from supplier_orders import (_send_email, supplier_greeting,
                                 SIGNATURE_HTML, INTERNAL_ALERT_EMAIL)
    sent_date = check["check_sent_at"].strftime("%m/%d/%Y") if check.get("check_sent_at") else "recently"
    nag_no = int(check.get("ghi_nag_count") or 0) + 1
    followup = ("" if nag_no == 1 else
                f"<p>Following up again (#{nag_no}) — we still show it uncashed.</p>")
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.6;'>"
            f"<p>{supplier_greeting('GHI')}</p>"
            f"<p>We mailed check for <strong>${float(check['amount']):,.2f}</strong> "
            f"on <strong>{sent_date}</strong> for <strong>PO {check['order_id']}</strong> "
            f"and it has not been cashed yet.</p>"
            f"<p>Did you receive it? Please deposit it, or let us know if it "
            f"needs to be reissued.</p>"
            f"{followup}"
            f"{SIGNATURE_HTML}</div>")
    to_addr = (SUPPLIER_INFO.get("GHI") or {}).get("email", "")
    send = _send_email(check["order_id"], to_addr,
                       f"Check for PO {check['order_id']} - please confirm receipt",
                       html, "ghi_check_nag", cc=INTERNAL_ALERT_EMAIL)
    if send.get("success"):
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE ghi_checks
                               SET last_ghi_nag_at = NOW(),
                                   ghi_nag_count = COALESCE(ghi_nag_count, 0) + 1
                               WHERE id = %s""", (check["id"],))
                conn.commit()
    return send


def run_daily_check_nags() -> Dict:
    """Called from the gmail sync loop every cycle; self-limits to one action
    per check per ET calendar day. Returns counts."""
    from psycopg2.extras import RealDictCursor
    now = datetime.now(timezone.utc)
    today_et = _et_date(now)
    out = {"send_reminders": 0, "ghi_nags": 0, "errors": []}
    with get_db() as conn:
        ensure_ghi_checks_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT * FROM ghi_checks
                           WHERE status IN ('pending_send', 'sent')""")
            checks = cur.fetchall()
    for c in checks:
        try:
            if c["status"] == "pending_send":
                if _et_date(c.get("last_reminder_at")) == today_et:
                    continue  # already reminded today
                if _send_send_reminder(c).get("success"):
                    out["send_reminders"] += 1
            elif c["status"] == "sent":
                sent_at = c.get("check_sent_at") or c.get("created_at")
                if not sent_at or (now - sent_at).days < CLEAR_DAYS:
                    continue
                last_nag = c.get("last_ghi_nag_at")
                if last_nag and (now - last_nag).days < NAG_EVERY_DAYS:
                    continue
                if _et_date(last_nag) == today_et:
                    continue
                if _send_ghi_nag(c).get("success"):
                    out["ghi_nags"] += 1
        except Exception as e:
            out["errors"].append(f"check {c.get('id')}: {e}")
    return out


def _log_event(order_id: str, event_type: str, data: Dict):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO order_events (order_id, event_type, event_data, source)
                    VALUES (%s, %s, %s, 'ghi_checks')
                """, (str(order_id), event_type, json.dumps(data, default=str)))
                conn.commit()
    except Exception as e:
        print(f"[GHI-CHECKS] event log failed: {e}")


# =============================================================================
# ROUTES
# =============================================================================

class CheckCreate(BaseModel):
    order_id: str
    amount: float
    note: Optional[str] = None


_PAGE_STYLE = """
  body { color:#393939; font-family:'Open Sans','Helvetica Neue',Helvetica,Arial,sans-serif;
         font-size:15px; line-height:1.6; max-width:640px; margin:40px auto; padding:0 16px; }
  .card { border:1px solid #e3e3e3; border-radius:8px; padding:24px; }
  .btn { display:inline-block; padding:12px 28px; border-radius:6px; color:#fff; border:0;
         text-decoration:none; font-weight:bold; font-size:16px; cursor:pointer; margin:6px 8px 6px 0; }
  .go { background:#1dc9b7; } .done { background:#5578eb; } .neutral { background:#888; }
"""


def _page(title: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card">{body_html}</div></body></html>""")


@ghi_check_router.post("/ghi-checks")
def create_ghi_check(req: CheckCreate, _: bool = Depends(require_admin)):
    """Open a check record [admin]: fires the first send-reminder immediately,
    then daily until marked mailed."""
    try:
        return create_check(req.order_id, req.amount, req.note)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@ghi_check_router.get("/ghi-checks")
def list_ghi_checks(status: str = None, limit: int = 50,
                    _: bool = Depends(require_admin)):
    from psycopg2.extras import RealDictCursor
    q = "SELECT * FROM ghi_checks WHERE TRUE"
    args = []
    if status:
        q += " AND status = %s"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT %s"
    args.append(min(int(limit), 200))
    with get_db() as conn:
        ensure_ghi_checks_table(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, args)
            rows = cur.fetchall()
    return {"status": "ok", "count": len(rows), "checks": rows}


@ghi_check_router.post("/ghi-checks/run")
def run_nags_now(_: bool = Depends(require_admin)):
    """Run the daily reminder/nag pass on demand [admin] (it also runs
    automatically inside the gmail sync, one action per check per day)."""
    try:
        return {"status": "ok", **run_daily_check_nags()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@ghi_check_router.get("/ghi-check/{token}", response_class=HTMLResponse)
def ghi_check_landing(token: str, intent: str = ""):
    """Landing from the reminder email buttons — real actions live here
    (POST) so mail-scanner prefetch can never mark a check mailed."""
    c = _get_by_token(token)
    if not c:
        return _page("Not found", "<h2>Link not found</h2>")
    if c["status"] in ("cleared", "canceled"):
        return _page("All done", f"<h2>Check for PO {c['order_id']} is "
                                 f"{c['status']}.</h2>")
    sent_line = ""
    if c["status"] == "sent":
        d = c["check_sent_at"].strftime("%m/%d/%Y") if c.get("check_sent_at") else "?"
        nags = int(c.get("ghi_nag_count") or 0)
        nag_txt = f" GHI nagged {nags}x." if nags else ""
        sent_line = f"<p>Marked mailed on <strong>{d}</strong>.{nag_txt}</p>"
    hint = ("" if intent != "sent" else
            "<p style='color:#1dc9b7;'><strong>Confirm below that the check "
            "is in the mail.</strong></p>")
    sent_btn = ("<button class='btn go' type='submit' name='action' value='sent'>"
                "&#10003; I mailed the check</button>"
                if c["status"] == "pending_send" else "")
    return _page(f"GHI check — PO {c['order_id']}", f"""
      <h2>Check for PO {c['order_id']}: ${float(c['amount']):,.2f} to GHI</h2>
      <p>Status: <strong>{c['status'].replace('_', ' ')}</strong></p>
      {sent_line}{hint}
      <form method="post" action="/ghi-check/{token}/decide">
        {sent_btn}
        <button class="btn done" type="submit" name="action" value="cleared">
          Check has CLEARED</button>
        <button class="btn neutral" type="submit" name="action" value="later">
          Remind me later</button>
        <button class="btn neutral" type="submit" name="action" value="canceled">
          Cancel this record</button>
      </form>
    """)


@ghi_check_router.post("/ghi-check/{token}/decide", response_class=HTMLResponse)
def ghi_check_decide(token: str, action: str = Form(...)):
    c = _get_by_token(token)
    if not c:
        return _page("Not found", "<h2>Link not found</h2>")
    if c["status"] in ("cleared", "canceled"):
        return _page("All done", f"<h2>Already {c['status']}.</h2>")

    if action == "later":
        return _page("OK", "<h2>OK — you'll get the reminder again "
                           "tomorrow.</h2>")
    if action == "sent" and c["status"] == "pending_send":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE ghi_checks
                               SET status = 'sent', check_sent_at = NOW()
                               WHERE token = %s AND status = 'pending_send'""",
                            (token,))
                conn.commit()
        _log_event(c["order_id"], "ghi_check_sent", {"check_id": c["id"]})
        return _page("Marked mailed", f"""
          <h2>&#10003; Check for PO {c['order_id']} marked MAILED</h2>
          <p>Daily reminders stop. If it hasn't cleared in {CLEAR_DAYS} days,
             GHI gets an automatic "did you receive our check?" email
             (CC you) every {NAG_EVERY_DAYS} days until it clears.</p>""")
    if action == "cleared":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE ghi_checks
                               SET status = 'cleared', cleared_at = NOW()
                               WHERE token = %s AND status IN ('pending_send', 'sent')""",
                            (token,))
                conn.commit()
        _log_event(c["order_id"], "ghi_check_cleared", {"check_id": c["id"]})
        return _page("Cleared", f"<h2>&#10003; Check for PO {c['order_id']} "
                                f"CLEARED — record closed.</h2>")
    if action == "canceled":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE ghi_checks SET status = 'canceled'
                               WHERE token = %s AND status IN ('pending_send', 'sent')""",
                            (token,))
                conn.commit()
        _log_event(c["order_id"], "ghi_check_canceled", {"check_id": c["id"]})
        return _page("Canceled", f"<h2>Check record for PO {c['order_id']} "
                                 f"canceled.</h2>")
    return _page("No change", "<h2>Nothing changed.</h2>")
