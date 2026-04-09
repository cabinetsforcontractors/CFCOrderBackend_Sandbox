"""inventory_sync.b2bwave_push — the ONLY module that writes stock state to B2BWave.

Per ARCHITECTURE.md section 6, no other file in this module — and no
file outside this module — may write stock state to B2BWave. This is
the enforced single writer.

Per ARCHITECTURE.md section 5 and section 6:
    - Only pushes diffs. Never sends a full catalog state.
    - Push failure must NOT mutate the snapshot. The engine only saves
      the snapshot after apply() reports success.
    - A failed push preserves the prior snapshot so the next run
      re-attempts the same diff.

SHELL STEP 1: signatures only. No B2BWave API calls. No authentication.
No network. No dependency on b2bwave_api.py.
"""

from typing import Any


def apply(diff: Any) -> Any:
    """Apply a computed diff to the B2BWave catalog.

    Intended behavior (see ARCHITECTURE.md sections 3, 5, 6):
        - Writes only the stock state changes contained in `diff`.
        - Writes stock state only (available / out of stock). Does NOT
          touch SKU descriptions, images, categories, dimensions, or
          prices.
        - Returns a success/failure summary suitable for the engine to
          decide whether to save the snapshot.

    Args:
        diff: The output of inventory_sync.diff.compute, filtered
            through inventory_sync.ignore_list.filter. Shape TBD.

    Returns:
        A per-SKU push result summary. Exact shape TBD.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
