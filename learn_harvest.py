"""
learn_harvest.py — THE HARVESTER (learning machine step 1, William-ruled
2026-07-31: "the way to learn is sweep the @gmail account and see my
replies as the 1st step").

A LESSON'S RAW MATERIAL IS A PAIR: the email that came in + what William
did about it. This module walks the @gmail SENT history (read-only, via
learn_gmail's separate token) and stores one row per William reply:

  learn_pairs:
    reply_msg_id (PK) — dedupe = resumability: re-running skips known rows
    thread_id, subject
    counterparty_addr / _domain — WHO he was dealing with
    inbound_msg_id / inbound_date / inbound_text — what they said
    reply_date / reply_text — what William answered
    has_inbound — false = a cold outbound HE started (also a lesson:
                  how he opens conversations)
    order_ids — any 5xxx numbers seen in subject/bodies
    harvested_at

  learn_skips: messages judged NOT-A-LESSON (self-forwards, notes to our
    own boxes). Remembered so batches never re-read them — without this
    the sweep stalls on thick blocks of self-forwards (7/31 stall bug).

Batches are small on purpose (Render request timeout) — call the door
repeatedly; each run picks up where the last left off via the dedupe.

Doors [admin]:
  POST /learn/harvest?max_messages=100&dry_run=false
  GET  /learn/harvest/status — rows stored, by counterparty domain
"""

import base64
import json
import re
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends

from auth import require_admin
from db_helpers import get_db
from learn_gmail import learn_configured, learn_gmail_get

harvest_router = APIRouter(tags=["learning"])

_OID_RE = re.compile(r"\b(5\d{3})\b")

# addresses that count as "us" — a reply TO these is not a counterparty pair
_OUR_RE = re.compile(
    r"orders@cabinetsforcontractors\.com|cabinetsforcontractors@gmail\.com|"
    r"wpjob1@gmail\.com|contact@allprocabinetsandflooring\.com|"
    r"4wprince@gmail\.com", re.I)


# =============================================================================
# TABLES
# =============================================================================

def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS learn_pairs (
                reply_msg_id VARCHAR(120) PRIMARY KEY,
                mailbox VARCHAR(80),
                thread_id VARCHAR(120),
                subject TEXT,
                counterparty_addr TEXT,
                counterparty_domain VARCHAR(120),
                inbound_msg_id VARCHAR(120),
                inbound_date TIMESTAMP WITH TIME ZONE,
                inbound_text TEXT,
                reply_date TIMESTAMP WITH TIME ZONE,
                reply_text TEXT,
                has_inbound BOOLEAN DEFAULT FALSE,
                order_ids TEXT,
                harvested_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_learn_pairs_domain
                       ON learn_pairs(counterparty_domain)""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS learn_skips (
                msg_id VARCHAR(120) PRIMARY KEY,
                reason VARCHAR(60),
                at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        conn.commit()


# =============================================================================
# GMAIL HELPERS (payload parsing mirrors reply_composer)
# =============================================================================

def _header(msg: Dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _body_text(payload: Dict) -> str:
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data and mime.startswith("text/plain"):
        try:
            return base64.urlsafe_b64decode(data + "==").decode(
                "utf-8", errors="replace")
        except Exception:
            return ""
    for part in payload.get("parts") or []:
        t = _body_text(part)
        if t:
            return t
    if data and mime.startswith("text/html"):
        try:
            html = base64.urlsafe_b64decode(data + "==").decode(
                "utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html)
        except Exception:
            return ""
    return ""


def _addr_only(from_header: str) -> str:
    m = re.search(r"<([^>]+)>", from_header or "")
    return (m.group(1) if m else (from_header or "")).strip().lower()


def _strip_quoted(text: str) -> str:
    """Drop quoted history — keep only what William actually typed."""
    lines = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s.startswith(">"):
            continue
        if re.match(r"On .{5,80} wrote:\s*$", s):
            break
        if s.startswith("-----Original Message-----"):
            break
        if re.match(r"From:\s?.+", s) and lines and not lines[-1].strip():
            break
        lines.append(ln)
    return "\n".join(lines).strip()


# =============================================================================
# THE SWEEP
# =============================================================================

def harvest_batch(max_messages: int = 100, dry_run: bool = False) -> Dict:
    if not learn_configured():
        return {"status": "error", "message": "GMAIL_LEARN_* not configured"}

    out = {"status": "ok", "dry_run": dry_run, "scanned": 0, "stored": 0,
           "not_lesson": 0, "skipped_known": 0, "no_inbound": 0,
           "errors": []}

    with get_db() as conn:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT reply_msg_id FROM learn_pairs")
            known = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT msg_id FROM learn_skips")
            known |= {r[0] for r in cur.fetchall()}

        page_token = None
        processed = 0
        while processed < max_messages:
            path = f"messages?labelIds=SENT&maxResults=50"
            if page_token:
                path += f"&pageToken={page_token}"
            try:
                page = learn_gmail_get(path)
            except Exception as e:
                out["errors"].append(f"list failed: {e}")
                break
            if not page:
                out["errors"].append("list returned nothing (token?)")
                break
            ids = [m["id"] for m in page.get("messages", [])]
            page_token = page.get("nextPageToken")
            if not ids:
                break

            fresh = [i for i in ids if i not in known]
            out["skipped_known"] += len(ids) - len(fresh)

            for mid in fresh:
                if processed >= max_messages:
                    break
                processed += 1
                out["scanned"] += 1
                try:
                    row = _build_pair(mid)
                    if row is None:
                        # judged, remembered, never re-read
                        out["not_lesson"] += 1
                        if not dry_run:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO learn_skips (msg_id, reason)
                                    VALUES (%s, 'not-a-lesson')
                                    ON CONFLICT (msg_id) DO NOTHING
                                """, (mid,))
                        known.add(mid)
                        continue
                    if not row["has_inbound"]:
                        out["no_inbound"] += 1
                    if not dry_run:
                        _store(conn, row)
                    known.add(mid)
                    out["stored"] += 1
                except Exception as e:
                    out["errors"].append(f"{mid}: {str(e)[:120]}")

            if not page_token:
                out["complete"] = True
                break
        if not dry_run:
            conn.commit()

    out["errors"] = out["errors"][:10]
    return out


def _build_pair(reply_msg_id: str) -> Optional[Dict]:
    msg = learn_gmail_get(f"messages/{reply_msg_id}?format=full")
    if not msg:
        return None
    thread_id = msg.get("threadId")
    reply_text = _strip_quoted(_body_text(msg.get("payload")))[:6000]
    subject = _header(msg, "Subject")
    reply_date = None
    try:
        reply_date_ms = int(msg.get("internalDate", "0"))
        from datetime import datetime, timezone
        reply_date = datetime.fromtimestamp(reply_date_ms / 1000,
                                            tz=timezone.utc)
    except Exception:
        pass

    # walk the thread: the newest OUTSIDE message BEFORE this reply
    inbound = None
    to_addr = _addr_only(_header(msg, "To"))
    try:
        thread = learn_gmail_get(f"threads/{thread_id}?format=full")
        msgs = (thread or {}).get("messages", [])
        my_ts = int(msg.get("internalDate", "0"))
        for m in msgs:
            if m.get("id") == reply_msg_id:
                continue
            ts = int(m.get("internalDate", "0"))
            frm = _addr_only(_header(m, "From"))
            if ts < my_ts and frm and not _OUR_RE.search(frm):
                if inbound is None or ts > inbound["ts"]:
                    inbound = {"id": m.get("id"), "ts": ts, "from": frm,
                               "text": _body_text(m.get("payload"))[:6000]}
    except Exception:
        pass

    counterparty = (inbound or {}).get("from") or to_addr
    if not counterparty or _OUR_RE.search(counterparty):
        # a note to ourselves — not a lesson
        return None
    domain = counterparty.split("@")[-1][:120]

    oid_blob = " ".join([subject or "", reply_text[:500],
                         (inbound or {}).get("text", "")[:500]])
    oids = sorted(set(_OID_RE.findall(oid_blob)))

    inbound_date = None
    if inbound:
        try:
            from datetime import datetime, timezone
            inbound_date = datetime.fromtimestamp(inbound["ts"] / 1000,
                                                  tz=timezone.utc)
        except Exception:
            pass

    return {
        "reply_msg_id": reply_msg_id,
        "mailbox": "cabinetsforcontractors@gmail.com",
        "thread_id": thread_id,
        "subject": (subject or "")[:500],
        "counterparty_addr": counterparty[:300],
        "counterparty_domain": domain,
        "inbound_msg_id": (inbound or {}).get("id"),
        "inbound_date": inbound_date,
        "inbound_text": (inbound or {}).get("text", "")[:6000] or None,
        "reply_date": reply_date,
        "reply_text": reply_text or None,
        "has_inbound": inbound is not None,
        "order_ids": ",".join(oids) or None,
    }


def _store(conn, row: Dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO learn_pairs
                (reply_msg_id, mailbox, thread_id, subject,
                 counterparty_addr, counterparty_domain,
                 inbound_msg_id, inbound_date, inbound_text,
                 reply_date, reply_text, has_inbound, order_ids)
            VALUES (%(reply_msg_id)s, %(mailbox)s, %(thread_id)s,
                    %(subject)s, %(counterparty_addr)s,
                    %(counterparty_domain)s, %(inbound_msg_id)s,
                    %(inbound_date)s, %(inbound_text)s, %(reply_date)s,
                    %(reply_text)s, %(has_inbound)s, %(order_ids)s)
            ON CONFLICT (reply_msg_id) DO NOTHING
        """, row)


# =============================================================================
# DOORS
# =============================================================================

@harvest_router.post("/learn/harvest")
def learn_harvest(max_messages: int = 100, dry_run: bool = False,
                  _: bool = Depends(require_admin)):
    """Sweep a batch of @gmail SENT history into learn_pairs [admin].
    Resumable — re-running skips rows already stored or judged."""
    if max_messages > 300:
        max_messages = 300
    return harvest_batch(max_messages=max_messages, dry_run=dry_run)


@harvest_router.get("/learn/harvest/status")
def learn_harvest_status(_: bool = Depends(require_admin)):
    """What the harvester holds so far [admin] — totals + by counterparty."""
    with get_db() as conn:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*),
                                  COUNT(*) FILTER (WHERE has_inbound),
                                  MIN(reply_date), MAX(reply_date)
                           FROM learn_pairs""")
            total, paired, oldest, newest = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM learn_skips")
            skips = cur.fetchone()[0]
            cur.execute("""
                SELECT counterparty_domain, COUNT(*)
                FROM learn_pairs
                GROUP BY counterparty_domain
                ORDER BY COUNT(*) DESC LIMIT 25
            """)
            domains = [{"domain": d, "replies": n} for d, n in cur.fetchall()]
    return {"status": "ok", "total_replies": total,
            "with_inbound_pair": paired,
            "judged_not_lessons": skips,
            "oldest": oldest.isoformat() if oldest else None,
            "newest": newest.isoformat() if newest else None,
            "by_domain": domains}
