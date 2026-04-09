"""inventory_sync.scrapers.roc — ROC Cabinetry supplier scraper (shell).

Isolated from lm.py and dl.py. Must not import from other scrapers.
Shared helpers live in scrapers/base.py only.

See inventory_sync/ARCHITECTURE.md sections 5 (failure rules) and 6
(safety invariants): a scrape failure here must NOT be treated as
"zero SKUs in stock", must NOT cascade to other suppliers, and must
NOT trigger a push.

SHELL STEP 1: signature only. No logic. No Playwright import. No
supplier URL or credential usage.
"""

from .base import BaseScraper, ScrapeResult


class ROCScraper(BaseScraper):
    """ROC Cabinetry inventory scraper (stub)."""

    supplier_id = "roc"

    def scrape(self) -> ScrapeResult:
        """Scrape ROC Cabinetry inventory state.

        SHELL STEP 1: not implemented.
        """
        raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
