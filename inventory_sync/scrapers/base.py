"""inventory_sync.scrapers.base — abstract scraper contract.

Defines the interface every supplier scraper must implement. This is
the only allowed location for shared scraper helpers; scraper modules
may not import from each other (see ARCHITECTURE.md section 6).

SHELL STEP 1: interface sketch only. No logic. No dependencies on
Playwright, requests, or any scraping library.
"""

from typing import Any


class ScrapeResult:
    """Per-supplier scrape result (stub).

    Intended shape (TBD in a later step):
        - supplier_id: str
        - scraped_at: datetime
        - rows: tuple of (sku, in_stock_bool) pairs
        - coverage: float (fraction of known SKUs found)
        - status: "ok" | "partial" | "failed"
        - error: optional string

    The real shape will be locked when logic is implemented. Until then,
    this is a placeholder so type references elsewhere in the module
    don't break.
    """


class BaseScraper:
    """Abstract supplier scraper.

    Every supplier scraper module defines exactly one subclass that
    implements `scrape()` and reports a `ScrapeResult`. The scraper
    never writes anywhere — it only returns the result. The engine
    decides what to do with it.

    Per ARCHITECTURE.md section 5, a scraper that fails must preserve
    prior state (no out-of-stock inference) and must not cascade to
    other suppliers.
    """

    #: Stable supplier identifier (e.g. "lm", "dl", "roc").
    supplier_id: str = ""

    def scrape(self) -> ScrapeResult:
        """Return a ScrapeResult for this supplier.

        Must not write snapshots, push to B2BWave, send alerts, or
        invoke any other scraper. Side-effect-free except for the
        network reads needed to scrape.

        SHELL STEP 1: not implemented.
        """
        raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
