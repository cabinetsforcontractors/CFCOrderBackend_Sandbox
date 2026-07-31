"""
learn_distill.py — THE DISTILLER (learning machine Lane 1, William-ruled
2026-07-31: "run the distiller... I don't want to hand build anything or
make it work, I want it to work so that we know that when its used it does
what its supposed to do").

WHAT IT DOES: reads a counterparty's bucket of harvested pairs (their email
+ William's reply, from learn_pairs) and has an AI write the LESSONS in
plain English — how the relationship works, what William does in which
situation, what mistakes this counterparty makes, tone notes. The lessons
land in the SAME playbooks the machinery already reads:
  - the reply composer injects the playbook into every draft
  - Checker B reads it as the vigilance list on every document check
so a distill run makes both machines smarter with ZERO extra wiring.

THE MERGE LAW: William's hand-written rules at the top of a playbook are
NEVER touched. The distiller owns only the section below the marker line —
re-running replaces that section cleanly (idempotent). William can read
and strike any learned line via the existing GET/PUT /playbooks doors.

DATA HYGIENE: only has_inbound pairs (campaign blasts have no inbound);
near-duplicate replies deduped; newest-first sampling.

Doors [admin]:
  GET  /learn/distill/targets — buckets + pair counts
  POST /learn/distill/{target}?dry_run=false — distill one bucket
       (dry_run returns the lessons WITHOUT writing the playbook)
"""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends

from auth import require_admin
from db_helpers import get_db

distill_router = APIRouter(tags=["learning"])

DISTILL_MODEL = os.environ.get("DISTILL_MODEL", "claude-sonnet-5").strip()

LEARNED_MARK = "===== LEARNED FROM HISTORY"

# bucket key -> matcher. Keys = the EXACT playbook keys the machinery uses.
BUCKETS = {
    "Love-Milestone":  {"domains": ["milestonecabinetry.com"]},
    "GHI":             {"domains": ["ghicabinets.com"]},
    "Cabinet & Stone": {"domains": ["cabinetstonellc.com"]},
    "ROC":             {"domains": ["roccabinetry.com",
                                    "roccabinetrytampa.com"]},
    "DL":              {"domains": ["dlcabinetry.com",
                                    "emails.dlcabinetry.com"]},
    "DuraStone":       {"domains": ["durastoneusa.com"]},
    "GoBravura":       {"domains": ["gobravura.com"]},
    "LI":              {"addrs":   ["cabinetrydistribution@gmail.com"]},
    "L&C Cabinetry":   {"like":    "%lnccabinetry%"},
    "CUSTOMERS":       {"special": "customers"},
}

# senders that are NOT customers (suppliers above + logistics + robots)
_NOT_CUSTOMER_DOMAINS = [
    "milestonecabinetry.com", "ghicabinets.com", "cabinetstonellc.com",
    "roccabinetry.com", "roccabinetrytampa.com", "dlcabinetry.com",
    "emails.dlcabinetry.com", "durastoneusa.com", "gobravura.com",
    "b2bemailservice.com", "kchtrans.com", "ufpdllc.com", "rlcarriers.com",
    "echo.com", "notification.intuit.com", "eq.intuit.com",
]


# =============================================================================
# PAIR SELECTION
# =============================================================================

def _fetch_pairs(target: str, limit: int = 300) -> List[Dict]:
    from psycopg2.extras import RealDictCursor
    spec = BUCKETS.get(target)
    if not spec:
        return []
    where, params = "", []
    if spec.get("domains"):
        where = "counterparty_domain = ANY(%s)"
        params = [spec["domains"]]
    elif spec.get("addrs"):
        where = "LOWER(counterparty_addr) = ANY(%s)"
        params = [[a.lower() for a in spec["addrs"]]]
    elif spec.get("like"):
        where = "counterparty_addr ILIKE %s"
        params = [spec["like"]]
    elif spec.get("special") == "customers":
        where = ("NOT (counterparty_domain = ANY(%s)) "
                 "AND LOWER(counterparty_addr) != %s")
        params = [_NOT_CUSTOMER_DOMAINS, "cabinetrydistribution@gmail.com"]
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT subject, counterparty_addr, inbound_text, reply_text,
                       reply_date, order_ids
                FROM learn_pairs
                WHERE has_inbound = TRUE
                  AND reply_text IS NOT NULL AND reply_text != ''
                  AND {where}
                ORDER BY reply_date DESC
                LIMIT %s
            """, params + [limit])
            rows = [dict(r) for r in cur.fetchall()]
    # dedupe near-identical replies (campaign echoes, canned sends)
    seen, out = set(), []
    for r in rows:
        key = re.sub(r"\s+", " ", (r["reply_text"] or "")[:120]).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _bucket_counts() -> List[Dict]:
    out = []
    for name in BUCKETS:
        pairs = _fetch_pairs(name, limit=500)
        out.append({"target": name, "usable_pairs": len(pairs)})
    return out


# =============================================================================
# THE DISTILL PROMPT
# =============================================================================

_DISTILL_PROMPT = """You are distilling how William Prince (owner, Cabinets \
For Contractors — wholesale RTA cabinets) handles {who}. Below are real \
email exchanges: what {who} wrote, and what William actually did/said back.

Write the LESSONS a robot assistant needs so it can handle {who} the way \
William does. Rules:
- Plain English. Short, concrete, numbered lines. No filler.
- ONLY what the evidence supports — never invent a rule from one example
  unless it is clearly a standing practice.
- Focus on WHAT WILLIAM DOES in each situation, not how he phrases things.

Use exactly these section headers:

HOW THIS RELATIONSHIP WORKS
(who they are to the business, cadence, channels, who answers)

SITUATIONS -> WHAT WILLIAM DOES
(the repeating situations and his standard move in each: quotes arriving,
confirmations, stock issues, damage, payment, scheduling, changes)

MISTAKES THEY MAKE + HOW WILLIAM CATCHES THEM
(missed skus, wrong qtys, wrong colors, pricing slips — and the check or
fix William applies; if none appear in evidence, write "none observed")

TONE + BOUNDARIES
(how direct, what he never does, what he always insists on)

THE EXCHANGES ({n} of them, newest first):
{pairs}

Answer with ONLY the four sections, max ~60 lines total."""


def _fmt_pairs(pairs: List[Dict], cap: int) -> str:
    chunks = []
    for p in pairs[:cap]:
        d = ""
        try:
            d = p["reply_date"].strftime("%Y-%m-%d")
        except Exception:
            pass
        inbound = re.sub(r"\s+", " ", (p.get("inbound_text") or ""))[:500]
        reply = re.sub(r"\s+", " ", (p.get("reply_text") or ""))[:500]
        chunks.append(f"--- {d} | {p.get('counterparty_addr')} | "
                      f"{(p.get('subject') or '')[:80]}\n"
                      f"THEY: {inbound}\nWILLIAM: {reply}")
    return "\n\n".join(chunks)


def _call_model(prompt: str, max_tokens: int = 3000) -> Optional[str]:
    from config import ANTHROPIC_API_KEY
    if not ANTHROPIC_API_KEY:
        return None
    payload = {"model": DISTILL_MODEL, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", ANTHROPIC_API_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    with urllib.request.urlopen(req, timeout=180) as r:
        result = json.loads(r.read().decode())
    return "".join(b.get("text", "") for b in (result.get("content") or [])
                   if isinstance(b, dict)).strip() or None


# =============================================================================
# THE MERGE LAW
# =============================================================================

def _merge_playbook(existing: Optional[str], lessons: str) -> str:
    """Hand rules stay the law; the LEARNED section below the marker is
    replaced cleanly on every run."""
    base = (existing or "").split(LEARNED_MARK)[0].rstrip()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (f"{base}\n\n{LEARNED_MARK} (auto-distilled {stamp}) =====\n"
            f"(distilled from William's real replies — hand rules above stay "
            f"the law; edit or strike any line via PUT /playbooks)\n\n"
            f"{lessons.strip()}\n")


def _write_playbook(target: str, merged: str):
    from dossier import ensure_playbooks
    with get_db() as conn:
        ensure_playbooks(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO supplier_playbooks (supplier, playbook, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (supplier) DO UPDATE
                SET playbook = EXCLUDED.playbook, updated_at = NOW()
            """, (target, merged))
        conn.commit()


# =============================================================================
# DOORS
# =============================================================================

@distill_router.get("/learn/distill/targets")
def distill_targets(_: bool = Depends(require_admin)):
    """The buckets and how many usable pairs each holds [admin]."""
    return {"status": "ok", "targets": _bucket_counts()}


@distill_router.post("/learn/distill/{target}")
def distill_target(target: str, dry_run: bool = False, max_pairs: int = 60,
                   _: bool = Depends(require_admin)):
    """Distill one bucket into its playbook [admin]. dry_run returns the
    lessons WITHOUT writing. Hand rules are never touched either way."""
    if target not in BUCKETS:
        return {"status": "error",
                "message": f"unknown target — one of {sorted(BUCKETS)}"}
    pairs = _fetch_pairs(target)
    if len(pairs) < 5:
        return {"status": "skipped", "target": target,
                "usable_pairs": len(pairs),
                "message": "fewer than 5 usable pairs — not enough evidence"}
    cap = min(max_pairs, 80)
    prompt = _DISTILL_PROMPT.format(
        who=target if target != "CUSTOMERS" else "our CUSTOMERS (contractors "
        "and builders buying cabinets)",
        n=min(len(pairs), cap), pairs=_fmt_pairs(pairs, cap))
    try:
        lessons = _call_model(prompt)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200] if e.fp else ""
        return {"status": "error", "target": target,
                "message": f"api {e.code} {body}"}
    except Exception as e:
        return {"status": "error", "target": target, "message": str(e)}
    if not lessons:
        return {"status": "error", "target": target,
                "message": "model returned nothing"}

    if dry_run:
        return {"status": "dry_run", "target": target,
                "pairs_used": min(len(pairs), cap), "lessons": lessons}

    try:
        from dossier import get_playbook
        existing = get_playbook(target)
    except Exception:
        existing = None
    merged = _merge_playbook(existing, lessons)
    _write_playbook(target, merged)
    return {"status": "ok", "target": target,
            "pairs_used": min(len(pairs), cap),
            "lessons_chars": len(lessons),
            "playbook_chars": len(merged),
            "note": "live immediately — composer and Checker B read this "
                    "playbook on their next job"}
