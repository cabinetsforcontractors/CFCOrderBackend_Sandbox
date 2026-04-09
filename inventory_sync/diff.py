"""inventory_sync.diff — pure diff between prior snapshot and new scrape.

Pure function. No I/O. No network. No database. No B2BWave call. The
diff module is the only place that computes "what changed" between two
snapshot states, and it never acts on the result — that is the engine's
job.

Per ARCHITECTURE.md section 6 (safety invariants):
    - Only diffs. Never returns a "full replacement" state.
    - Never infers out-of-stock from a missing scrape row. Missing rows
      in the new scrape are treated as "unknown", not "out of stock".
      The coverage gate in engine.py is responsible for rejecting
      low-coverage scrapes before they reach diff.

SHELL STEP 1: signature only. No logic.
"""

from typing import Any


def compute(previous: Any, current: Any) -> Any:
    """Compute the per-SKU stock-state diff between two snapshots.

    Intended behavior (see ARCHITECTURE.md section 3):
        - Returns only the SKU-level changes between `previous` and
          `current` for a single supplier.
        - Does NOT consult the ignore list. That happens after diff
          via inventory_sync.ignore_list.filter.
        - Does NOT read from disk, the network, or B2BWave.

    Args:
        previous: The prior snapshot for this supplier (from
            inventory_sync.snapshot.load_previous). May be empty on
            first run.
        current: The fresh ScrapeResult for this supplier.

    Returns:
        A diff object describing the per-SKU stock-state changes.
        Exact shape TBD when logic is implemented.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
