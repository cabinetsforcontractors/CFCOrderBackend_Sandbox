"""inventory_sync — bounded supplier inventory mirror (shell, no logic).

Isolated non-V6 subsystem inside the CFC Order sandbox repo. Mirrors
supplier stock state into the CFC B2BWave catalog so the website does
not sell what suppliers cannot ship.

SCOPE LOCK: not a pricing engine; not part of order-flow business logic;
does not read, write, or alter orders, shipments, quotes, webhooks, or
customer emails.

Mandatory first-read for any future session touching this module:
    inventory_sync/ARCHITECTURE.md

Drift rules, safety invariants, failure rules, and validation gates all
live in ARCHITECTURE.md. Any change to code in this module that touches
thresholds, boundaries, or safety rules must update ARCHITECTURE.md in
the same change set.

This is SHELL STEP 1. No logic. No routes. No DB migrations. No cron
wiring. No env edits. No Playwright install. Placeholder signatures
only.
"""
