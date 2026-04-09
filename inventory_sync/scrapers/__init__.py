"""inventory_sync.scrapers — supplier scrapers subpackage (shell, no logic).

Each supplier has its own scraper module. Per ARCHITECTURE.md section 6:

    - Scraper modules may NOT import from each other.
    - Shared scraper helpers live in `base.py`.
    - Each scraper is independent.

The abstract contract every scraper must implement lives in `base.py`.
Supplier modules:

    - lm.py   — Love-Milestone
    - dl.py   — DL Cabinetry
    - roc.py  — ROC Cabinetry

SHELL STEP 1: no logic. Placeholder signatures only.
"""
