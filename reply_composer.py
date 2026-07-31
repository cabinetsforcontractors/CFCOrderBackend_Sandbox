"""
reply_composer.py — THE REPLY COMPOSER (Phase A, William-ruled 2026-07-30:
"I type wood, hit a button and you write 'hey bella we need wood trays,
thank you' and fire it off... a pop up shows me the email chain").

RULINGS BAKED IN:
  - PREVIEW LAW: /reply/compose NEVER sends — it returns the draft + the
    chain for the preview popup. /reply/send is the button.
  - VOICE: the William way; greeting "Hey There," / "-William here."
  - REPLY-ANCHOR LAW 7/30: replies go back to the OUTSIDE sender.
  - SEND-TO RESOLUTION 7/31: the intent (or the send_to field from the
    board's contact picker) names the recipient; CONTACT_BOOK resolves
    names, raw addresses win, partial addresses like "wpjob1@gmail" get
    completed, no-match warns loudly. Explicit recipients survive the send.
  - PLAYBOOK LESSONS 7/31: supplier playbook rides every compose;
    non-suppliers get CUSTOMERS.

Doors [admin]:
  POST /reply/compose {message_id, intent, order_id?, send_to?}
  POST /reply/send {message_id, body, to?, subject?}
  GET  /reply/contacts — the picker list for the board
"""

import base64
import json
import os
import re
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException

from auth import require_admin
from config import ANTHROPIC_API_KEY, SUPPLIER_INFO

reply_router = APIRouter(tags=["reply-composer"])

COMPOSER_MODEL = os.environ.get("REPLY_COMPOSER_MODEL",
                                "claude-sonnet-5").strip()
_OID_RE = re.compile(r"\b(5\d{3})\b")

# OUR mailboxes — a DERIVED reply must never be aimed at one of these
# (an EXPLICIT "send to 4wprince" is allowed — that's William's own call).
_OUR_ADDRS = {
    "orders@cabinetsforcontractors.com",
    "cabinetsforcontractors@gmail.com",
    "wpjob1@gmail.com",
    "contact@allprocabinetsandflooring.com",
}

# CONTACT BOOK — names William types resolve to real boxes (7/31 ruling).
# Longest keys are tried first; a raw email address in the intent wins.
CONTACT_BOOK = {
    "li yang":          "cabinetrydistribution@gmail.com",
    "li":               "cabinetrydistribution@gmail.com",
    "yang":             "cabinetrydistribution@gmail.com",
    "4wprince":         "4wprince@gmail.com",
    "william":          "4wprince@gmail.com",
    "wpjob1":           "wpjob1@gmail.com",
    "bella":            "csr4@milestonecabinetry.com",
    "thany":            "csr4@milestonecabinetry.com",
    "maria":            "csr4@milestonecabinetry.com",
    "milestone":        "csr4@milestonecabinetry.com",
    "lm":               "csr4@milestonecabinetry.com",
    "kathryn":          "orders@ghicabinets.com",
    "ghi":              "orders@ghicabinets.com",
    "todd":             "tgertz@ghicabinets.com",
    "jennifer":         "jennifer@cabinetstonellc.com",
    "cabinet & stone":  "jennifer@cabinetstonellc.com",
    "c&s":              "jennifer@cabinetstonellc.com",
    "roc":              "csr05@roccabinetry.com",
    "gerald":           "gbwoodcreations@gmail.com",
    "dominic":          "dgugliotti@nationwidecustomhomes.com",
    "dom":              "dgugliotti@nationwidecustomhomes.com",
    "connie":           "connie.prince08@gmail.com",
    "bill rhoads":      "wrhodes@aol.com",
    "bill":             "wrhodes@aol.com",
    "janine":           "office@pacificcoastcabinetry.com",
    "pacific coast":    "office@pacificcoastcabinetry.com",
}

# Labels for the board's picker (type a few letters, the list shrinks).
CONTACT_LABELS = [
    {"label": "William — 4wprince@gmail.com", "email": "4wprince@gmail.com"},
    {"label": "Safety inbox — wpjob1@gmail.com", "email": "wpjob1@gmail.com"},
    {"label": "Li Yang (LI / Cabinetry Distribution)", "email": "cabinetrydistribution@gmail.com"},
    {"label": "Milestone CSR (Bella / Thany / Maria)", "email": "csr4@milestonecabinetry.com"},
    {"label": "GHI orders (Kathryn — orders@ only)", "email": "orders@ghicabinets.com"},
    {"label": "Todd Gertz (GHI)", "email": "tgertz@ghicabinets.com"},
    {"label": "Jennifer (Cabinet & Stone)", "email": "jennifer@cabinetstonellc.com"},
    {"label": "ROC CSR (Liliexis)", "email": "csr05@roccabinetry.com"},
    {"label": "Gerald Thomas (G&B Wood Creations)", "email": "gbwoodcreations@gmail.com"},
    {"label": "Dominic Gugliotti (Nationwide)", "email": "dgugliotti@nationwidecustomhomes.com"},
    {"label": "Connie Prince", "email": "connie.prince08@gmail.com"},
    {"label": "Bill Rhoads", "email": "wrhodes@aol.com"},
    {"label": "Janine (Pacific Coast Cabinetry)", "email": "office@pacificcoastcabinetry.com"},
]

_SEND_TO_RE = re.compile(
    r"\bsend\s+(?:it\s+|this\s+|that\s+|a\s+copy\s+)?to\s+"
    r"([A-Za-z0-9@._%+&'\- ]{2,45})", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _complete_partial_email(word: str) -> Optional[str]:
    """'wpjob1@gmail' -> 'wpjob1@gmail.com' when that makes a valid address."""
    if "@" not in word:
        return None
    if _EMAIL_RE.fullmatch(word):
        return word.lower()
    candidate = word + ".com"
    if _EMAIL_RE.fullmatch(candidate):
        return candidate.lower()
    return None


def _resolve_token(token: str) -> Optional[Dict]:
    """One token/phrase -> a real address, or a loud no-match."""
    token = (token or "").strip()
    if not token:
        return None
    em = _EMAIL_RE.search(token)
    if em:
        return {"to": em.group(0).lower(), "matched": token,
                "via": "address typed"}
    words = token.lower().split()
    if words:
        completed = _complete_partial_email(words[0])
        if completed:
            return {"to": completed, "matched": token,
                    "via": "address completed"}
    tl = token.lower()
    for k in sorted(CONTACT_BOOK, key=len, reverse=True):
        if tl == k or tl.startswith(k + " "):
            return {"to": CONTACT_BOOK[k], "matched": token,
                    "via": f"contact '{k}'"}
    first = words[0] if words else ""
    if first in CONTACT_BOOK:
        return {"to": CONTACT_BOOK[first], "matched": token,
                "via": f"contact '{first}'"}
    return {"error": True, "matched": token}


def _resolve_send_to(intent: str, send_to: str = "") -> Optional[Dict]:
    """The picker field wins; else 'send to X' inside the intent."""
    if (send_to or "").strip():
        return _resolve_token(send_to)
    m = _SEND_TO_RE.search(intent or "")
    if not m:
        return None
    token = m.group(1)
    for stop in (",", ".", ";", " and ", " - ", " then ", "?"):
        if stop in token:
            token = token.split(stop)[0]
    return _resolve_token(token)


def _is_ours(addr: str) -> bool:
    a = (addr or "").lower()
    safety = os.environ.get("INTERNAL_SAFETY_EMAIL", "").strip().lower()
    if safety and safety in a:
        return True
    return any(x in a for x in _OUR_ADDRS)


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


def _reply_anchor(chain: Dict) -> Tuple[Dict, bool]:
    """REPLY-ANCHOR LAW: the message the reply answers. If the requested
    message is OURS (a forward William sent out for a look), walk backwards
    to the newest message from an OUTSIDE address."""
    target = chain["target"]
    if not _is_ours(target.get("from", "")):
        return target, False
    for c in reversed(chain["messages"]):
        if not _is_ours(c.get("from", "")):
            return c, True
    return target, False


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
- NEVER greet by name — several different people answer these supplier
  inboxes. Open with EXACTLY these two lines, with one blank line between
  them and one blank line after, then start the message:

Hey There,

-William here.

- Say the thing, then stop. No corporate filler, no "I hope this finds you
  well", no "please don't hesitate".
- If the instruction says to SEND TO a particular person, that part is
  routing — do not repeat it in the email body.
- Sign off exactly:

Thank you,
William

WILLIAM'S INSTRUCTION (what the reply must accomplish): {intent}

PLAYBOOK (how William handles this counterparty — follow it):
{playbook}

ORDER CONTEXT:
{order_context}

THE EMAIL THREAD (oldest first):
{chain}

Reply with ONLY the email body text (plain text, no subject line, no
commentary, no markdown)."""


def compose_reply(message_id: str, intent: str,
                  order_id: Optional[str] = None,
                  send_to: str = "") -> Dict:
    if not ANTHROPIC_API_KEY:
        return {"status": "error", "message": "ANTHROPIC_API_KEY not set"}
    chain = fetch_thread(message_id)
    if not chain:
        return {"status": "error", "message": "could not fetch thread"}
    anchor, re_anchored = _reply_anchor(chain)
    oid = order_id or _guess_order_id(chain)
    supplier = _supplier_for(anchor.get("from", ""), oid)

    # supplier playbook when we know the supplier; the distilled CUSTOMERS
    # playbook otherwise (7/31 — customer emails get lessons too)
    playbook = _playbook_text(supplier)
    if not playbook:
        playbook = _playbook_text("CUSTOMERS")

    chain_txt = "\n\n".join(
        f"--- {c['date']} | from {c['from']}\n{c['text']}"
        for c in chain["messages"][-8:])
    try:
        prompt = COMPOSE_PROMPT.format(
            intent=intent.strip(),
            playbook=playbook or "(none on file)",
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
        draft = "".join(b.get("text", "") for b in (result.get("content") or [])
                        if isinstance(b, dict)).strip()
    except urllib.error.HTTPError as e:
        return {"status": "error",
                "message": f"compose failed: {e.code} "
                           f"{e.read().decode()[:200] if e.fp else ''}"}
    except Exception as e:
        return {"status": "error", "message": f"compose failed: {e}"}
    if not draft:
        return {"status": "error", "message": "model returned nothing"}

    subject = anchor.get("subject") or ""
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    reply_to = anchor.get("from", "")
    m = re.search(r"<([^>]+)>", reply_to)
    to_addr = m.group(1) if m else reply_to.strip()

    # SEND-TO RESOLUTION (7/31): the picker field or the intent can name
    # the recipient.
    override = _resolve_send_to(intent, send_to)
    override_note = None
    explicit_to = False
    if override:
        if override.get("to"):
            to_addr = override["to"]
            explicit_to = True
            override_note = (f"recipient set by your instruction: "
                             f"'{override['matched']}' → {to_addr} "
                             f"({override['via']})")
        else:
            override_note = (f"⚠ couldn't match '{override.get('matched')}' "
                             f"to a known contact — using the thread's "
                             f"sender {to_addr}")

    return {"status": "ok", "message_id": message_id,
            "order_id": oid, "supplier": supplier,
            "to": to_addr, "subject": subject,
            "re_anchored": re_anchored,
            "anchor_from": anchor.get("from", ""),
            "explicit_to": explicit_to,
            "override_note": override_note,
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
    anchor, re_anchored = _reply_anchor(chain)

    explicit_to = bool((to or "").strip())
    if not to:
        m = re.search(r"<([^>]+)>", anchor.get("from", ""))
        to = m.group(1) if m else (anchor.get("from") or "").strip()
    # DERIVED recipients must never be our own box; an EXPLICIT one may be
    # (William deliberately sending to himself/4wprince).
    if _is_ours(to) and not explicit_to:
        return {"status": "error",
                "message": f"refusing to reply to our own address ({to}) — "
                           "no outside sender found in this thread"}
    if not subject:
        subject = anchor.get("subject") or ""
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
    rfc_id = anchor.get("rfc_message_id") or ""
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
                         "re_anchored": re_anchored,
                         "explicit_to": explicit_to,
                         "anchor_from": anchor.get("from", ""),
                         "gmail_message_id": sent.get("id"),
                         "in_reply_to_message": message_id,
                         "body_preview": body[:300]},
                        "reply_composer")
        except Exception as e:
            print(f"[REPLY] fire failed: {e}")

    return {"status": "ok", "sent_message_id": sent.get("id"),
            "to": to, "redirected": redirected,
            "original_to": original_to, "subject": subject,
            "re_anchored": re_anchored, "explicit_to": explicit_to,
            "order_id": oid, "thread_id": chain.get("thread_id")}


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
                             (payload or {}).get("order_id"),
                             send_to=(payload or {}).get("send_to", "") or "")
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


@reply_router.get("/reply/contacts")
def reply_contacts(_: bool = Depends(require_admin)):
    """The contact picker list [admin] — type a few letters, the board's
    datalist shrinks (William 7/31)."""
    return {"status": "ok", "contacts": CONTACT_LABELS}
