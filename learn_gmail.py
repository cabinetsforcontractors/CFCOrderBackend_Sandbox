"""
learn_gmail.py — THE LEARNING MACHINE'S GMAIL CONNECTION (William-ruled
2026-07-31: "we need to build a machine that will learn... the way to
learn is sweep the @gmail account and see my replies as the 1st step").

This module holds the SECOND Gmail connection — read-only, into
cabinetsforcontractors@gmail.com (William's historical mailbox, years of
his real replies). It is completely separate from the robot's orders@
connection in gmail_sync.py:
  - its own env trio: GMAIL_LEARN_CLIENT_ID / GMAIL_LEARN_CLIENT_SECRET /
    GMAIL_LEARN_REFRESH_TOKEN (minted via the Gmail Token Gen OAuth client,
    scope gmail.readonly — this token CANNOT send or delete anything)
  - touching this module can never break the orders@ machine

Doors [admin]:
  GET /learn/verify — whose mailbox does the token open, and how much
      history is there? Run this before trusting anything downstream.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional

from fastapi import APIRouter, Depends

from auth import require_admin

learn_router = APIRouter(tags=["learning"])

LEARN_CLIENT_ID = os.environ.get("GMAIL_LEARN_CLIENT_ID", "").strip()
LEARN_CLIENT_SECRET = os.environ.get("GMAIL_LEARN_CLIENT_SECRET", "").strip()
LEARN_REFRESH_TOKEN = os.environ.get("GMAIL_LEARN_REFRESH_TOKEN", "").strip()

EXPECTED_MAILBOX = "cabinetsforcontractors@gmail.com"

_token_cache = {"token": None, "expires": 0.0}


def learn_configured() -> bool:
    return bool(LEARN_CLIENT_ID and LEARN_CLIENT_SECRET
                and LEARN_REFRESH_TOKEN)


def get_learn_access_token() -> Optional[str]:
    """Refresh-token dance for the LEARN connection (cached ~1h)."""
    if not learn_configured():
        return None
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"] - 60:
        return _token_cache["token"]
    data = urllib.parse.urlencode({
        "client_id": LEARN_CLIENT_ID,
        "client_secret": LEARN_CLIENT_SECRET,
        "refresh_token": LEARN_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token",
                                 data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read().decode())
        _token_cache["token"] = tok.get("access_token")
        _token_cache["expires"] = now + int(tok.get("expires_in", 3600))
        return _token_cache["token"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300] if e.fp else ""
        print(f"[LEARN] token refresh failed: {e.code} {body}")
        return None
    except Exception as e:
        print(f"[LEARN] token refresh failed: {e}")
        return None


def learn_gmail_get(path: str) -> Optional[Dict]:
    """GET against the LEARN mailbox (read-only scope)."""
    token = get_learn_access_token()
    if not token:
        return None
    req = urllib.request.Request(
        f"https://gmail.googleapis.com/gmail/v1/users/me/{path}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# =============================================================================
# DOORS
# =============================================================================

@learn_router.get("/learn/verify")
def learn_verify(_: bool = Depends(require_admin)):
    """Whose mailbox does the LEARN token open [admin]? Expect the @gmail
    account; `match` must be true before the harvester runs."""
    if not learn_configured():
        return {"status": "error",
                "message": "GMAIL_LEARN_CLIENT_ID / _SECRET / _REFRESH_TOKEN "
                           "not all set on this service"}
    try:
        prof = learn_gmail_get("profile")
        if not prof:
            return {"status": "error",
                    "message": "token refresh failed — check the three "
                               "GMAIL_LEARN_* values (see server logs)"}
        sent = None
        try:
            s = learn_gmail_get("messages?labelIds=SENT&maxResults=1")
            sent = (s or {}).get("resultSizeEstimate")
        except Exception:
            pass
        mailbox = (prof.get("emailAddress") or "").lower()
        return {"status": "ok",
                "mailbox": prof.get("emailAddress"),
                "expected": EXPECTED_MAILBOX,
                "match": mailbox == EXPECTED_MAILBOX,
                "messages_total": prof.get("messagesTotal"),
                "threads_total": prof.get("threadsTotal"),
                "sent_estimate": sent,
                "scope_note": "read-only token — it cannot send or delete"}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300] if e.fp else ""
        return {"status": "error", "message": f"{e.code} {body}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
