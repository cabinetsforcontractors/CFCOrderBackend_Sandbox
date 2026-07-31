"""
order_analysis.py — FULL ANALYSIS + THE LAST EXCHANGE (William 2026-07-31:
"I need a full analysis rundown... I need to see the email I sent and the
response, just 1 back — the words I sent and their reply" + the queue-card
ruling: "email summary, always the last two messages until archived...
and under that the tell-the-robot box — receive all the relevant info and
respond, set a future task, or tell the robot to do something").

Doors [admin]:
  POST /orders/{order_id}/comprehensive-summary -> {"summary": markdown}
       (the app's Full Analysis Generate button — exchange verbatim first,
        then the AI rundown from dossier + fires + legs + playbooks)
  GET  /orders/{order_id}/last-exchange
  POST /queue/exchanges {"order_ids": [...]} -> {"exchanges": {oid: {...}}}
       (batch feed for the queue cards; bodies come from the email body
        cache — each Gmail message is fetched ONCE ever, then served from
        the table, so the board load stays fast)
"""

import json
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends

from auth import require_admin
from db_helpers import get_db

analysis_router = APIRouter(tags=["analysis"])

ANALYSIS_MODEL = os.environ.get("ANALYSIS_MODEL", "claude-sonnet-5").strip()


# =============================================================================
# EMAIL BODY CACHE (bodies are immutable — fetch once, keep forever)
# =============================================================================

def _ensure_body_cache(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_body_cache (
                message_id VARCHAR(120) PRIMARY KEY,
                body TEXT,
                fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        conn.commit()


def _fetch_body(message_id: str) -> str:
    if not message_id:
        return ""
    try:
        with get_db() as conn:
            _ensure_body_cache(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT body FROM email_body_cache "
                            "WHERE message_id = %s", (message_id,))
                row = cur.fetchone()
                if row is not None:
                    return row[0] or ""
    except Exception:
        pass
    body = ""
    try:
        from reply_composer import _gmail_get, _body_text
        msg = _gmail_get(f"messages/{message_id}?format=full")
        if msg:
            body = _body_text(msg.get("payload")) or ""
    except Exception:
        body = ""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO email_body_cache (message_id, body)
                    VALUES (%s, %s) ON CONFLICT (message_id) DO NOTHING
                """, (message_id, body))
            conn.commit()
    except Exception:
        pass
    return body


def _strip_quoted(text: str) -> str:
    lines = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s.startswith(">"):
            continue
        if re.match(r"On .{5,80} wrote:\s*$", s):
            break
        if s.startswith("-----Original Message-----"):
            break
        lines.append(ln)
    return "\n".join(lines).strip()


# =============================================================================
# THE LAST EXCHANGE (one back — his words + their reply)
# =============================================================================

def _latest_ledger_msgs(order_id: str) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Newest inbox row + newest sent row attributed to this order."""
    from psycopg2.extras import RealDictCursor
    latest = {"inbox": None, "sent": None}
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(r"""
                SELECT message_id, folder, from_addr, to_addr, subject,
                       email_date
                FROM email_ledger
                WHERE order_ids ~ ('\m' || %s || '\M')
                  AND folder IN ('inbox', 'sent')
                ORDER BY email_date DESC
                LIMIT 40
            """, (str(order_id),))
            for r in cur.fetchall():
                f = r["folder"]
                if latest.get(f) is None:
                    latest[f] = dict(r)
                if latest["inbox"] and latest["sent"]:
                    break
    return latest["inbox"], latest["sent"]


def last_exchange(order_id: str) -> Dict:
    """The newest message each direction, bodies included, quoted history
    stripped — 'the words I sent and their reply', nothing more."""
    inbox, sent = _latest_ledger_msgs(order_id)
    out = {"has_exchange": bool(inbox or sent)}
    if sent:
        out["you"] = {
            "message_id": sent["message_id"],
            "at": sent["email_date"].isoformat() if sent.get("email_date") else "",
            "to": sent.get("to_addr") or "",
            "subject": sent.get("subject") or "",
            "body": _strip_quoted(_fetch_body(sent["message_id"]))[:2500],
        }
    if inbox:
        out["them"] = {
            "message_id": inbox["message_id"],
            "at": inbox["email_date"].isoformat() if inbox.get("email_date") else "",
            "from": inbox.get("from_addr") or "",
            "subject": inbox.get("subject") or "",
            "body": _strip_quoted(_fetch_body(inbox["message_id"]))[:2500],
        }
    # the composer anchor: the newest OUTSIDE message wins; fall back to ours
    out["anchor_id"] = ((inbox or {}).get("message_id")
                        or (sent or {}).get("message_id"))
    return out


def _exchange_md(ex: Dict) -> str:
    """Render the exchange verbatim, chronological, clearly labeled."""
    if not ex.get("has_exchange"):
        return ("## THE LAST EXCHANGE\n\n_No emails on record for this "
                "order yet._\n")
    blocks = []
    for key, label in (("you", "**YOU sent**"), ("them", "**THEY replied**")):
        m = ex.get(key)
        if not m:
            continue
        when = (m.get("at") or "")[:16].replace("T", " ")
        who = m.get("to") or m.get("from") or ""
        body = (m.get("body") or "").strip() or "_(empty body)_"
        blocks.append((m.get("at") or "", f"{label} — {when} — {who}\n\n"
                       f"> {body.replace(chr(10), chr(10) + '> ')}\n"))
    blocks.sort(key=lambda b: b[0])
    return "## THE LAST EXCHANGE (one back, verbatim)\n\n" + \
        "\n".join(b[1] for b in blocks)


# =============================================================================
# THE RUNDOWN
# =============================================================================

def _supplier_legs(order_id: str) -> List[Dict]:
    from psycopg2.extras import RealDictCursor
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT warehouse, status, supplier_doc_ref,
                           revision_requested_at, confirmed_at, updated_at
                    FROM supplier_orders WHERE order_id = %s
                """, (str(order_id),))
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


_ANALYSIS_PROMPT = """You are the operations analyst for Cabinets For \
Contractors (wholesale RTA cabinets). Write the FULL RUNDOWN of this order \
for William, the owner. He is dyslexic: plain detailed English, short \
lines, any numbers in simple markdown tables. Never invent — only what the \
evidence below supports.

Use these sections (markdown ## headers):
WHERE THIS ORDER STANDS — two or three plain sentences, the state right now
MONEY — table: items / shipping / tariff / paid or owed / anything off
SUPPLIER SIDE — each leg: who, status, doc, anything unresolved
SHIPPING — quotes, BOL, tracking, anything at risk
WHAT NEEDS A HUMAN NEXT — numbered, most urgent first (say NONE if none)
RISKS — anything in the record that smells like money loss or a mistake
(reference the playbook's known supplier mistakes when relevant)

ORDER FACTS:
{facts}

THE FIRE-LOG STORY (chronological, every recorded action):
{fires}

SUPPLIER LEGS:
{legs}

SUPPLIER PLAYBOOK (how this supplier behaves; known mistakes):
{playbook}

THE LAST EMAIL EXCHANGE:
{exchange}

Answer with ONLY the markdown sections, max ~70 lines."""


def _call_model(prompt: str) -> Optional[str]:
    from config import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        return None
    payload = {"model": ANALYSIS_MODEL, "max_tokens": 2500,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", ANTHROPIC_API_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    with urllib.request.urlopen(req, timeout=120) as r:
        result = json.loads(r.read().decode())
    return "".join(b.get("text", "") for b in (result.get("content") or [])
                   if isinstance(b, dict)).strip() or None


def comprehensive_summary(order_id: str) -> Dict:
    # the exchange comes first and VERBATIM — even if the AI call fails,
    # William still sees the words
    try:
        ex = last_exchange(order_id)
    except Exception as e:
        ex = {"has_exchange": False, "error": str(e)}
    exchange_block = _exchange_md(ex)

    facts, fires_txt, playbook = "", "", ""
    try:
        from dossier import build_dossier, get_playbook
        d = build_dossier(order_id)
        if d.get("status") == "ok":
            o = d["order"]
            facts = json.dumps({k: str(v) for k, v in o.items()
                                if v is not None}, indent=1, default=str)[:3000]
            fires_txt = "\n".join(
                f"- {f.get('at', '')[:16]} {f.get('kind')}: "
                f"{json.dumps({k: v for k, v in (f.get('data') or {}).items() if k != '_fire'}, default=str)[:200]}"
                for f in (d.get("fires") or [])[-40:])
            for wh in (d.get("warehouses") or []):
                pb = get_playbook(wh)
                if pb:
                    playbook += f"\n--- {wh} ---\n{pb[:4000]}\n"
    except Exception as e:
        facts = f"(dossier unavailable: {e})"

    legs = _supplier_legs(order_id)
    legs_txt = "\n".join(
        f"- {l['warehouse']}: {l['status']} (doc {l.get('supplier_doc_ref') or '?'}"
        f"{', revision asked ' + str(l['revision_requested_at'])[:16] if l.get('revision_requested_at') else ''})"
        for l in legs) or "(no supplier legs on record)"

    ex_for_ai = ""
    for key in ("you", "them"):
        m = ex.get(key) or {}
        if m:
            ex_for_ai += (f"{'WILLIAM SENT' if key == 'you' else 'THEY SAID'} "
                          f"({(m.get('at') or '')[:16]}): "
                          f"{(m.get('body') or '')[:800]}\n\n")

    prompt = _ANALYSIS_PROMPT.format(
        facts=facts or "(none)", fires=fires_txt or "(no fires recorded)",
        legs=legs_txt, playbook=playbook or "(none on file)",
        exchange=ex_for_ai or "(no emails on record)")

    try:
        rundown = _call_model(prompt)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200] if e.fp else ""
        rundown = f"_AI rundown failed: {e.code} {body}_"
    except Exception as e:
        rundown = f"_AI rundown failed: {e}_"
    if not rundown:
        rundown = "_AI rundown unavailable (ANTHROPIC_API_KEY not set?)_"

    return {"status": "ok", "order_id": str(order_id),
            "summary": f"{exchange_block}\n\n---\n\n{rundown}"}


# =============================================================================
# DOORS
# =============================================================================

@analysis_router.post("/orders/{order_id}/comprehensive-summary")
def order_comprehensive_summary(order_id: str,
                                _: bool = Depends(require_admin)):
    """Full Analysis [admin]: THE LAST EXCHANGE verbatim first, then the AI
    rundown from dossier + fires + playbooks. The app's Generate button."""
    try:
        return comprehensive_summary(order_id)
    except Exception as e:
        return {"status": "error", "summary": f"analysis crashed: {e}"}


@analysis_router.get("/orders/{order_id}/last-exchange")
def order_last_exchange(order_id: str, _: bool = Depends(require_admin)):
    """Just the last exchange [admin] — his words + their reply, one back."""
    try:
        return {"status": "ok", "order_id": str(order_id),
                **last_exchange(order_id)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@analysis_router.post("/queue/exchanges")
def queue_exchanges(payload: Dict = Body(...),
                    _: bool = Depends(require_admin)):
    """Batch last-exchange feed for the queue cards [admin]. Bodies ride
    the cache — each Gmail message is fetched once ever."""
    ids = [str(i) for i in ((payload or {}).get("order_ids") or [])][:60]
    out = {}
    for oid in ids:
        try:
            out[oid] = last_exchange(oid)
        except Exception as e:
            out[oid] = {"has_exchange": False, "error": str(e)[:100]}
    return {"status": "ok", "exchanges": out}
