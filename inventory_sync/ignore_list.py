"""inventory_sync.ignore_list — manual override / ignore list for stock-state changes.

Per ARCHITECTURE.md section 8:
    - The ignore list is read every run.
    - Entries on the list are filtered out of the diff BEFORE
      b2bwave_push sees it.
    - Any SKU on the ignore list is always left in its current B2BWave
      state regardless of scrape results.
    - The format, storage location, and entry semantics are a later
      implementation decision. The only shell-step-1 requirement is
      that every caller that acts on the diff does so through
      ignore_list.filter(diff), not by inspecting raw scrape output.

SHELL STEP 1: signatures only. No list loaded. No storage backend
chosen.
"""

from typing import Any


def load() -> Any:
    """Load the current manual ignore list.

    Intended behavior: return a lookup structure (e.g. a set of SKUs or
    a per-supplier mapping) usable by filter(). The exact shape is TBD.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")


def filter(diff: Any) -> Any:
    """Return a copy of `diff` with any ignore-list SKUs removed.

    Intended behavior (see ARCHITECTURE.md section 8):
        - Loads the ignore list via load() (or is passed a cached copy
          by the engine; exact interface TBD).
        - Produces a new diff object with the ignored SKUs stripped.
        - Does NOT mutate the input diff.
        - Does NOT call b2bwave_push or snapshot.

    Args:
        diff: The output of inventory_sync.diff.compute.

    Returns:
        A filtered diff object ready for b2bwave_push.apply.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
