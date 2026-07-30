"""
reply_composer.py — THE REPLY COMPOSER (Phase A, William-ruled 2026-07-30:
"I type wood, hit a button and you write 'hey bella we need wood trays,
thank you' and fire it off... a pop up shows me the email chain").

RULINGS BAKED IN:
  - PREVIEW LAW: /reply/compose NEVER sends — it returns the draft + the
    chain for the preview popup. /reply/send is the button (one-click for
    now; auto later "as it learns").
  - VOICE: the William way — casual, direct, short sentences, "Hey {first
    name}", signs "William". No corporate fluff.
  - Context = the whole thread + the order's dossier facts + the supplier
    playbook, so the reply knows the deal, not just the last message.

Doors [admin]:
  POST /reply/compose {message_id, intent, order_id?}
       -> {draft_body, subject, to, order_id, supplier, chain[]}
  POST /reply/send {message_id, body, to?, subject?}
       -> sends as a real REPLY in the same thread (In-Reply-To/References
          + Gmail threadId so it threads); EMAIL_ALLOWLIST redirect applies
          exactly like every other send; records a reply_sent fire.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from auth import require_admin
from config import ANTHROPIC_API_KEY, SUPPLIER_INFO

reply_router = APIRouter(tags=["reply-composer"])

COMPOSER_MODEL = os.environ.get("REPLY_COMPOSER_MODEL",
                                "claude-sonnet-5").strip()
_OID_RE = re.compile(r"\b(5\d{3})\b")


# =============================================================================
# GMAIL THREAD FETCH
# =============================================================================

def _gmail_get(path: str) -> Optional[Dict]:
    from gmail_sync import get_gmail_access_token
    token = get_gmail_access_token()
    if not token:
        return None
    req = urllib.request.Request(
        f"https://gmail.googleapis.com/gmail/v1/users/me/{path}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[REPLY] gmail get {path} failed: {e}")
        return None


def _header(msg: Dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _body_text(payload: Dict) -> str:
    """Best-effort plain text from a Gmail payload tree."""
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
    # html fallback, tags stripped
    if data and mime.startswith("text/html"):
        try:
            html = base64.urlsafe_b64decode(data + "==").decode(
                "utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html)
        except Exception:
            return ""
    return ""


def fetch_thread(message_id: str) -> Optional[Dict]:
    """The message + its whole thread, oldest first."""
    msg = _gmail_get(f"messages/{message_id}?format=full")
    if not msg:
        return None
    thread_id = msg.get("threadId")
    thread = _gmail_get(f"threads/{thread_id}?format=full") if thread_id else None
    messages = (thread or {}).get("messages") or [msg]
    chain = []
    for m in messages:
        chain.append({
            "message_id": m.get("id"),
            "from": _header(m, "From"),
            "to": _header(m, "To"),
            "date": _header(m, "Date"),
            "subject": _header(m, "Subject"),
            "rfc_message_id": _header(m, "Message-ID"),
            "text": _body_text(m.get("payload"))[:4000].strip(),
        })
    return {"thread_id": thread_id, "messages": chain,
            "target": next((c for c in chain
                            if c["message_id"] == message_id), chain[-1])}


# =============================================================================
# CONTEXT
# =============================================================================

def _guess_order_id(chain: Dict) -> Optional[str]:
    for c in reversed(chain["messages"]):
        m = _OID_RE.search((c.get("subject") or "") + " " + (c.get("text") or ""))
        if m:
            return m.group(1)
    return None


def _supplier_for(from_addr: str, order_id: Optional[str]) -> Optional[str]:
    fa = (from_addr or "").lower()
    for sup, info in SUPPLIER_INFO.items():
        dom = (info.get("email") or "").split("@")[-1].lower()
        if dom and dom not in ("gmail.com",) and dom in fa:
            return sup
    if "milestonecabinetry" in fa:
        return "Love-Milestone"
    if "ghicabinets" in fa:
        return "GHI"
    if "roccabinetry" in fa:
        return "ROC"
    if "cabinetstonellc" in fa:
        return "Cabinet & Stone"
    if order_id:
        try:
            from dossier import build_dossier
            d = build_dossier(order_id)
            whs = d.get("warehouses") or []
            if len(whs) == 1:
                return whs[0]
        except Exception:
            pass
    return None


def _order_context(order_id: Optional[str]) -> str:
    if not order_id:
        return ""
    try:
        from dossier import build_dossier
        d = build_dossier(order_id)
        if d.get("status") != "ok":
            return ""
        o = d["order"]
        lines = [f"ORDER {order_id}: {o.get('company_name')} "
                 f"({o.get('customer_name')}), items "
                 f"${float(o.get('order_total') or 0):,.2f}, "
                 f"{'PAID' if o.get('payment_received') else 'unpaid'}, "
                 f"ship-to {o.get('street')}, {o.get('city')} "
                 f"{o.get('state')} {o.get('zip_code')}"]
        for f in (d.get("fires") or [])[-8:]:
            data = {k: v for k, v in (f.get("data") or {}).items()
                    if k != "_fire"}
            lines.append(f"- {f['at'][:16]} {f['kind']}: "
                         f"{json.dumps(data, default=str)[:150]}")
        return "\n".join(lines)
    except Exception as e:
        return f"(order context unavailable: {e})"


def _playbook_text(supplier: Optional[str]) -> str:
    if not supplier:
        return ""
    try:
        from dossier import get_playbook
        return get_playbook(supplier) or ""
    except Exception:
        return ""


# =============================================================================
# COMPOSE
# =============================================================================

# NOTE: literal braces are doubled — this template goes through .format().
COMPOSE_PROMPT = """You write emails AS William Prince of Cabinets For \
Contractors (a wholesale RTA cabinet business). Write his reply to the \
LATEST message in the thread below, doing exactly what his instruction says.

WILLIAM'S VOICE — follow it exactly:
- Casual and direct. Short sentences. Plain words.
- Greeting: "Hey" plus the first name (or "Good morning" when it fits).
- Say the thing, then stop. No corporate filler, no "I hope this finds you
  well", no "please don't hesitate".
- Sign off exactly:

Thank you,
William

WILLIAM'S INSTRUCTION (what the reply must accomplish): {intent}

SUPPLIER PLAYBOOK (rules for dealing with this company):
{playbook}

ORDER CONTEXT:
{order_context}

THE EMAIL THREAD (oldest first):
{chain}

Reply with ONLY the email body text (plain text, no subject line, no
commentary, no markdown)."""


def compose_reply(message_id: str, intent: str,
                  order_id: Optional[str] = None) -> Dict:
    if not ANTHROPIC_API_KEY:
        return {"status": "error", "message": "ANTHROPIC_API_KEY not set"}
    chain = fetch_thread(message_id)
    if not chain:
        return {"status": "error", "message": "could not fetch thread"}
    target = chain["target"]
    oid = order_id or _guess_order_id(chain)
    supplier = _supplier_for(target.get("from", ""), oid)

    chain_txt = "\n\n".join(
        f"--- {c['date']} | from {c['from']}\n{c['text']}"
        for c in chain["messages"][-8:])
    try:
        prompt = COMPOSE_PROMPT.format(
            intent=intent.strip(),
            playbook=_playbook_text(supplier) or "(none on file)",
            order_context=_order_context(oid) or "(none)",
            chain=chain_txt[:14000])
    except Exception as e:
        return {"status": "error", "message": f"prompt build failed: {e}"}

    payload = {"model": COMPOSER_MODEL, "max_tokens": 700,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", ANTHROPIC_API_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read().decode())
        draft = (result.get("content") or [{}])[0].get("text", "").strip()
    except urllib.error.HTTPError as e:
        return {"status": "error",
                "message": f"compose failed: {e.code} "
                           f"{e.read().decode()[:200] if e.fp else ''}"}
    except Exception as e:
        return {"status": "error", "message": f"compose failed: {e}"}
    if not draft:
        return {"status": "error", "message": "model returned nothing"}

    subject = target.get("subject") or ""
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    reply_to = target.get("from", "")
    m = re.search(r"<([^>]+)>", reply_to)
    to_addr = m.group(1) if m else reply_to.strip()

    return {"status": "ok", "message_id": message_id,
            "order_id": oid, "supplier": supplier,
            "to": to_addr, "subject": subject,
            "draft_body": draft,
            "chain": [{"from": c["from"], "date": c["date"],
                       "snippet": (c["text"] or "")[:200]}
                      for c in chain["messages"]]}


# =============================================================================
# SEND (the button after the preview)
# =============================================================================

def send_reply(message_id: str, body: str, to: str = "",
               subject: str = "") -> Dict:
    from gmail_sync import get_gmail_access_token
    from email_identity import apply_from
    chain = fetch_thread(message_id)
    if not chain:
        return {"status": "error", "message": "could not fetch thread"}
    target = chain["target"]

    if not to:
        m = re.search(r"<([^>]+)>", target.get("from", ""))
        to = m.group(1) if m else (target.get("from") or "").strip()
    if not subject:
        subject = target.get("subject") or ""
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

    # EMAIL_ALLOWLIST guard — identical semantics to every other send.
    original_to = to
    allow = os.environ.get("EMAIL_ALLOWLIST", "").strip()
    redirected = False
    if allow:
        allowed = {e.strip().lower() for e in allow.split(",") if e.strip()}
        if to.lower() not in allowed:
            redirect = os.environ.get("INTERNAL_SAFETY_EMAIL", "").strip()
            if redirect:
                to = redirect
                redirected = True
            else:
                return {"status": "error",
                        "message": "recipient not in EMAIL_ALLOWLIST"}

    token = get_gmail_access_token()
    if not token:
        return {"status": "error", "message": "Gmail OAuth not configured"}

    msg = MIMEMultipart("alternative")
    apply_from(msg)
    msg["To"] = to
    msg["Subject"] = subject
    rfc_id = target.get("rfc_message_id") or ""
    if rfc_id:
        msg["In-Reply-To"] = rfc_id
        msg["References"] = rfc_id
    msg.attach(MIMEText(body, "plain"))
    try:
        import html as _html
        esc = _html.escape(body)
        esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
        html_body = ("<div style='font-family:Arial,sans-serif;font-size:14px;"
                     "line-height:1.5;'>" + esc.replace("\n", "<br>\n") + "</div>")
        msg.attach(MIMEText(html_body, "html"))
    except Exception:
        pass

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if chain.get("thread_id"):
        payload["threadId"] = chain["thread_id"]
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=json.dumps(payload).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            sent = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"status": "error",
                "message": f"Gmail send failed: {e.code} "
                           f"{e.read().decode()[:200] if e.fp else ''}"}

    oid = _guess_order_id(chain)
    if oid:
        try:
            from fire_log import record_fire
            record_fire(oid, "reply_sent",
                        {"to": to, "original_to": original_to,
                         "redirected": redirected, "subject": subject,
                         "gmail_message_id": sent.get("id"),
                         "in_reply_to_message": message_id,
                         "body_preview": body[:300]},
                        "reply_composer")
        except Exception as e:
            print(f"[REPLY] fire failed: {e}")

    return {"status": "ok", "sent_message_id": sent.get("id"),
            "to": to, "redirected": redirected,
            "original_to": original_to, "subject": subject,
            "order_id": oid}


# =============================================================================
# DOORS
# =============================================================================

@reply_router.post("/reply/compose")
def reply_compose(payload: Dict = Body(...), _: bool = Depends(require_admin)):
    """Compose a William-voiced reply [admin]. NEVER sends — returns the
    draft + chain for the preview popup (PREVIEW LAW 2026-07-30)."""
    message_id = (payload or {}).get("message_id", "").strip()
    intent = (payload or {}).get("intent", "").strip()
    if not message_id or not intent:
        raise HTTPException(status_code=400,
                            detail="message_id and intent are required")
    try:
        return compose_reply(message_id, intent,
                             (payload or {}).get("order_id"))
    except Exception as e:
        return {"status": "error", "message": f"compose crashed: {e}"}


@reply_router.post("/reply/send")
def reply_send(payload: Dict = Body(...), _: bool = Depends(require_admin)):
    """Send a (previewed) reply into the original thread [admin].
    Allowlist redirect applies; records a reply_sent fire."""
    message_id = (payload or {}).get("message_id", "").strip()
    body = (payload or {}).get("body", "").strip()
    if not message_id or not body:
        raise HTTPException(status_code=400,
                            detail="message_id and body are required")
    try:
        return send_reply(message_id, body,
                          to=(payload or {}).get("to", ""),
                          subject=(payload or {}).get("subject", ""))
    except Exception as e:
        return {"status": "error", "message": f"send crashed: {e}"}
