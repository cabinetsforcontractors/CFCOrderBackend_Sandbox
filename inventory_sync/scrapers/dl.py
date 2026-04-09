"""inventory_sync.scrapers.dl — DL Cabinetry supplier scraper (shell).

Isolated from lm.py and roc.py. Must not import from other scrapers.
Shared helpers live in scrapers/base.py only.

See inventory_sync/ARCHITECTURE.md sections 5 (failure rules) and 6
(safety invariants): a scrape failure here must NOT be treated as
"zero SKUs in stock", must NOT cascade to other suppliers, and must
NOT trigger a push.

SHELL STEP 1: signature only. No logic. No Playwright import. No
supplier URL or credential usage.
"""

from .base import BaseScraper, ScrapeResult


class DLScraper(BaseScraper):
    """DL Cabinetry inventory scraper (stub)."""

    supplier_id = "dl"

    def scrape(self) -> ScrapeResult:
        """Scrape DL Cabinetry inventory state.

        SHELL STEP 1: not implemented.
        """
        raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
