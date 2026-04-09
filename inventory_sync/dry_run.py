"""inventory_sync.dry_run — full scrape + diff path with no live push.

Per ARCHITECTURE.md section 9 (dry-run protocol):
    - Executes every configured scraper.
    - Runs diff, validation gates, and ignore-list filtering.
    - Logs what a live run WOULD push, including per-supplier counts,
      diffed SKUs, and which gate (if any) would have aborted the run.
    - NEVER calls b2bwave_push.apply.
    - NEVER writes a new snapshot (dry-runs do not mutate stored state).
    - Must be run successfully before the first live push for a new
      supplier or after any scraper behavior change.

SHELL STEP 1: signature only. No logic. No scrape calls.
"""

from typing import Any


def run() -> Any:
    """Run one full dry-run pass across all configured suppliers.

    Intended behavior:
        - Delegates to the same pipeline as engine.run_once, but with
          push and snapshot save short-circuited out.
        - Returns a per-supplier summary of what would have been
          pushed, including any validation-gate failures.

    Returns:
        A per-supplier dry-run summary. Exact shape TBD.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
