"""
dossier.py — THE ORDER DOSSIER + SUPPLIER PLAYBOOKS (Lane 3, William's
ruling 2026-07-30: "maybe make a mini md for each order to refer back to,
maybe we have a supplier specific md so we know with this supplier x needs
to happen... the order md pulls in the supplier specific logic as the
header, records the steps and each time you fire on that order leave as is
or update to show the latest results while recording the previous result").

THE DOSSIER IS GENERATED, NEVER HAND-KEPT — assembled on demand from the
append-only record (fires + ledgered emails), so it cannot drift and cannot
lie:
    header   = the supplier playbook(s) for the order's warehouses
    facts    = the order row (latest-value convenience copies)
    story    = every fire + every ledgered email, chronological,
               b2bwave_sync noise excluded, diffs visible

SUPPLIER PLAYBOOKS live in the supplier_playbooks table — ONE text per
supplier, readable by humans in the dossier AND available to machinery via
get_playbook(). Seeded from the William-ruled laws; edit through the door,
no deploys.

Doors:
  GET /orders/{order_id}/dossier?format=md|json   [admin]
  GET /playbooks                                  [admin]
  GET /playbooks/{supplier}                       [admin]
  PUT /playbooks/{supplier}   body={"text": ...}  [admin]
"""

import json
from typing import Dict, Optional

from fastapi import APIRouter, Depends, Body, HTTPException
from fastapi.responses import PlainTextResponse
from psycopg2.extras import RealDictCursor

from auth import require_admin
from db_helpers import get_db

dossier_router = APIRouter(tags=["dossier"])

NOISE_EVENT_TYPES = ("b2bwave_sync",)

# Seed playbooks — the William-ruled supplier laws as of 2026-07-30.
# The TABLE is the living truth; these only load when a supplier has no row.
SEED_PLAYBOOKS = {
    "GHI": (
        "GHI Cabinets — Palmetto FL 34221. Contact Kathryn Belfiore, orders@"
        "ghicabinets.com ONLY (personal boxes unmonitored).\n"
        "- Order = xlsx sheet on the eff-7/2 form (MASTER LIST tab only); "
        "dispatch builds it automatically from the stored template.\n"
        "- They reply 'review and approve for processing' with an SO — the "
        "verifier diffs it; approval goes as a REPLY DRAFT in their thread.\n"
        "- GHI is paid by CHECK (track cashing — they forget; nag after 14 "
        "days). Pallet fee doubles at the 1->2 pallet break (~1,100 lb).\n"
        "- Ship window 2-4 business days."
    ),
    "ROC": (
        "ROC Cabinetry — Norcross GA 30071. PORTAL ONLY: upload the "
        "quick-order CSV at roccabinetry.com/quick-order (store-prefixed "
        "skus, e.g. LNS->SNW), ENTER THE PO in their reference field so the "
        "confirmation auto-verifies.\n"
        "- After upload paste the whole cart page to roc-stock-paste "
        "(catches out-of-stock + silently-dropped skus).\n"
        "- Their confirmation email flips the row to confirmed hands-free.\n"
        "- Nudge the day BEFORE the promised ready date. Ship window 1-2 "
        "business days. Easy-reach bases auto-add the free A-BER-B tray."
    ),
    "Cabinet & Stone": (
        "Cabinet & Stone — Houston TX (1760 Stebbins) AND Pico Rivera CA "
        "(7105 Paramount, zip 90660). Amy Cao is the account contact; "
        "Jennifer/Gloria cover CA and vacations.\n"
        "- ESPRESSO (ESCS) ships from CA — quote the CA lane.\n"
        "- TRAP (the S118998 lesson): they can enter the same order at BOTH "
        "locations — an expired-reservation email from one store may be a "
        "harmless duplicate of an order the other store already shipped. "
        "Always check for double-billing.\n"
        "- Their line substitutions are real: no MB30 (DB30 converts), "
        "diagonal corner is 24x40 only. Put every substitution in writing "
        "to the customer BEFORE the truck arrives."
    ),
    "Love-Milestone": (
        "Love-Milestone — Orlando FL 32824 (10963 Florida Crown Dr). "
        "Contact is BELLA (2026-07-30; no longer Ireen).\n"
        "- Channel: PO email (sanitized table, no customer info). Portal "
        "CSV possible at shop.milestonecabinetry.com/quick-order once the "
        "store line codes are ruled.\n"
        "- Ship window 2-3 business days (their stated 48h)."
    ),
    "LI": (
        "Cabinetry Distribution (LI) — 561 Keuka Rd, Interlachen FL 32148. "
        "Li Yang.\n"
        "- 561 Keuka is a COMMERCIAL warehouse dock — never quote "
        "residential to it (hard-coded override).\n"
        "- Customer info IS allowed on LI correspondence (forward-style)."
    ),
    "DL": (
        "DL Cabinetry — Jacksonville FL 32256. Lily Chen. Portal-prepared "
        "channel: PO file emailed to us, we upload (~2 min)."
    ),
}


def ensure_playbooks(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supplier_playbooks (
                supplier VARCHAR(60) PRIMARY KEY,
                playbook TEXT,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        for sup, text in SEED_PLAYBOOKS.items():
            cur.execute("""INSERT INTO supplier_playbooks (supplier, playbook)
                           VALUES (%s, %s) ON CONFLICT (supplier) DO NOTHING""",
                        (sup, text))
        conn.commit()


def get_playbook(supplier: str) -> Optional[str]:
    """The machinery-readable rule text for a supplier (None if unknown)."""
    if not supplier:
        return None
    with get_db() as conn:
        ensure_playbooks(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT playbook FROM supplier_playbooks
                           WHERE UPPER(supplier) = UPPER(%s)""", (supplier,))
            row = cur.fetchone()
            return row[0] if row else None


def _order_warehouses(conn, order_id: str):
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT warehouse FROM order_line_items
                       WHERE order_id = %s AND warehouse IS NOT NULL
                         AND warehouse <> ''""", (order_id,))
        return sorted(r[0] for r in cur.fetchall())


def build_dossier(order_id: str) -> Dict:
    with get_db() as conn:
        ensure_playbooks(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            order = cur.fetchone()
            if not order:
                return {"status": "error", "message": "Order not found"}
            order = dict(order)

            warehouses = _order_warehouses(conn, order_id)
            playbooks = {}
            for wh in warehouses:
                cur.execute("""SELECT playbook FROM supplier_playbooks
                               WHERE UPPER(supplier) = UPPER(%s)""", (wh,))
                row = cur.fetchone()
                if row:
                    playbooks[wh] = row["playbook"]

            cur.execute("""SELECT event_type, event_data, source, created_at
                           FROM order_events
                           WHERE order_id = %s
                             AND NOT (event_type = ANY(%s))
                           ORDER BY created_at""",
                        (order_id, list(NOISE_EVENT_TYPES)))
            fires = []
            for r in cur.fetchall():
                data = r["event_data"]
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:
                        data = {"raw": str(data)[:200]}
                fires.append({"at": r["created_at"].isoformat()
                              if r["created_at"] else "",
                              "kind": r["event_type"],
                              "source": r["source"] or "system",
                              "data": data or {}})

            emails = []
            try:
                cur.execute("""SELECT subject, folder, from_addr, email_date,
                                      kind FROM email_ledger
                               WHERE order_ids ~ ('(^|,)' || %s || '($|,)')
                               ORDER BY email_date""", (order_id,))
                for r in cur.fetchall():
                    emails.append({"at": r["email_date"].isoformat()
                                   if r["email_date"] else "",
                                   "folder": r["folder"],
                                   "from": r["from_addr"],
                                   "kind": r["kind"],
                                   "subject": r["subject"]})
            except Exception:
                conn.rollback()

    return {"status": "ok", "order_id": order_id, "order": order,
            "warehouses": warehouses, "playbooks": playbooks,
            "fires": fires, "emails": emails}


def _money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def render_dossier_md(d: Dict) -> str:
    o = d["order"]
    lines = [f"# ORDER {d['order_id']} — DOSSIER",
             f"_generated from the append-only record; nothing here is "
             f"hand-kept or editable_", ""]

    # ---- supplier playbook header (William: the supplier logic AS the header)
    for wh, text in (d.get("playbooks") or {}).items():
        lines += [f"## SUPPLIER PLAYBOOK — {wh}", "", text, ""]
    for wh in d.get("warehouses") or []:
        if wh not in (d.get("playbooks") or {}):
            lines += [f"## SUPPLIER PLAYBOOK — {wh}",
                      "", "(no playbook on file — add one via "
                      f"PUT /playbooks/{wh})", ""]

    # ---- facts
    lines += ["## ORDER FACTS (latest values)", "",
              "| field | value |", "|---|---|",
              f"| Customer | {o.get('company_name') or ''} "
              f"({o.get('customer_name') or ''}) |",
              f"| Ship-to | {o.get('street') or ''}, {o.get('city') or ''} "
              f"{o.get('state') or ''} {o.get('zip_code') or ''} |",
              f"| Items total | {_money(o.get('order_total'))} |",
              f"| Charged shipping | {_money(o.get('shipping_cost'))} |",
              f"| Paid | {'YES ' + _money(o.get('payment_amount'))
                          if o.get('payment_received') else 'no'} |",
              f"| Tracking | {o.get('tracking') or '-'} |",
              f"| Pickup order | {'yes' if o.get('is_pickup') else 'no'} |",
              f"| Comments | {(o.get('comments') or '-')[:120]} |", ""]

    # ---- the story
    lines += ["## THE STORY (fires + emails, oldest first — every fire "
              "carries its own diff vs the previous one of its kind)", ""]
    merged = ([{"at": f["at"], "line": _fire_line(f)} for f in d["fires"]]
              + [{"at": e["at"], "line": _email_line(e)} for e in d["emails"]])
    merged.sort(key=lambda x: x["at"] or "")
    for m in merged:
        lines.append(m["line"])
    if not merged:
        lines.append("(no recorded story yet)")
    lines.append("")
    return "\n".join(lines)


def _fire_line(f: Dict) -> str:
    data = dict(f.get("data") or {})
    fire = data.pop("_fire", None) or {}
    bits = json.dumps(data, default=str)
    if len(bits) > 220:
        bits = bits[:220] + "…"
    seq = fire.get("seq")
    tag = f" (fire #{seq}" if seq else ""
    ch = fire.get("changes")
    if ch and ch != "no-change" and isinstance(ch, dict) and ch.get("changed"):
        deltas = "; ".join(f"{k}: {v.get('was')} → {v.get('now')}"
                           for k, v in list(ch["changed"].items())[:4])
        tag += f", changed: {deltas}"
    if tag:
        tag += ")"
    return (f"- `{(f.get('at') or '')[:16]}` **{f.get('kind')}** "
            f"[{f.get('source')}]{tag} — {bits}")


def _email_line(e: Dict) -> str:
    return (f"- `{(e.get('at') or '')[:16]}` ✉ **{e.get('subject') or '(no subject)'}** "
            f"({e.get('folder')}, from {e.get('from') or '?'})")


# =============================================================================
# DOORS
# =============================================================================

@dossier_router.get("/orders/{order_id}/dossier")
def order_dossier(order_id: str, format: str = "md",
                  _: bool = Depends(require_admin)):
    """The order's generated mini-md [admin]: playbook header + facts +
    the full fire/email story. format=json returns the raw assembly."""
    d = build_dossier(order_id)
    if d.get("status") != "ok":
        raise HTTPException(status_code=404, detail=d.get("message"))
    if format == "json":
        return d
    return PlainTextResponse(render_dossier_md(d), media_type="text/markdown")


@dossier_router.get("/playbooks")
def list_playbooks(_: bool = Depends(require_admin)):
    with get_db() as conn:
        ensure_playbooks(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT supplier, playbook, updated_at
                           FROM supplier_playbooks ORDER BY supplier""")
            rows = [dict(r) for r in cur.fetchall()]
    return {"status": "ok", "playbooks": rows}


@dossier_router.get("/playbooks/{supplier}")
def read_playbook(supplier: str, _: bool = Depends(require_admin)):
    text = get_playbook(supplier)
    if text is None:
        raise HTTPException(status_code=404,
                            detail=f"no playbook for '{supplier}'")
    return {"status": "ok", "supplier": supplier, "playbook": text}


@dossier_router.put("/playbooks/{supplier}")
def write_playbook(supplier: str, body: Dict = Body(...),
                   _: bool = Depends(require_admin)):
    """Update a supplier's playbook [admin] — no deploys, the dossier and
    the machinery read the table."""
    text = (body or {}).get("text", "")
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="text is required")
    with get_db() as conn:
        ensure_playbooks(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO supplier_playbooks (supplier, playbook,
                                                           updated_at)
                           VALUES (%s, %s, NOW())
                           ON CONFLICT (supplier) DO UPDATE
                           SET playbook = EXCLUDED.playbook,
                               updated_at = NOW()""", (supplier, text.strip()))
        conn.commit()
    return {"status": "ok", "supplier": supplier, "saved": True}
