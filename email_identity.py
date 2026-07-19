"""
email_identity.py
THE OUTBOUND FROM-ADDRESS SWITCH (William 2026-07-19, deliverability lane).

Problem: mail sent as cabinetsforcontractors@gmail.com gets spam-filtered by
some receivers (free-mail business sender profile). Fix: William stood up
orders@cabinetsforcontractors.net (GoDaddy/M365 mailbox, forwards into the
same Gmail the robot reads) and verified it as a Gmail "Send mail as" alias
routed through smtp.office365.com — so mail with that From is physically
transmitted by Microsoft's servers with SPF/DKIM alignment on the domain.

This module is the single switch:
  EMAIL_FROM_ADDRESS unset/empty  -> NO From header is added anywhere; Gmail
                                     stamps the authenticated account exactly
                                     as before. Zero behavior change.
  EMAIL_FROM_ADDRESS=orders@...   -> every robot send AND draft carries
                                     From: "Cabinets For Contractors <orders@...>"
                                     (name via EMAIL_FROM_NAME, default set).

RULES:
- Do NOT set EMAIL_FROM_ADDRESS until the alias is VERIFIED in Gmail
  ("Send mail as" list) — Gmail rewrites/rejects unverified From addresses.
- Flip = set the env var in Render, redeploy. Rollback = unset it.
- Every MIME builder in the repo calls apply_from(msg) right after
  construction; new senders must do the same.
"""

import os


def from_header() -> str:
    """The configured From header value, or '' when the switch is off."""
    addr = os.environ.get("EMAIL_FROM_ADDRESS", "").strip()
    if not addr:
        return ""
    name = os.environ.get("EMAIL_FROM_NAME", "Cabinets For Contractors").strip()
    return f"{name} <{addr}>" if name else addr


def apply_from(msg):
    """Stamp the configured From onto a MIME message (no-op when switch off).
    Replaces any existing From header so the switch always wins."""
    fh = from_header()
    if fh:
        try:
            del msg["From"]
        except Exception:
            pass
        msg["From"] = fh
    return msg
