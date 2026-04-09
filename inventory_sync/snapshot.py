"""inventory_sync.snapshot — per-supplier snapshot read/write.

Snapshots are the ONLY valid record of supplier stock state (see
ARCHITECTURE.md section 4). Not the B2BWave catalog. Not order flow
state. Not cached in-memory globals.

Per ARCHITECTURE.md section 5:
    - The snapshot is saved ONLY after b2bwave_push.apply reports
      success for that supplier. A partial push failure leaves the
      prior snapshot intact.

Per ARCHITECTURE.md section 9 (dry-run protocol):
    - Dry-runs do NOT call save(). They read via load_previous() and
      leave stored state untouched.

Per ARCHITECTURE.md section 11 (drift-prevention):
    - No module outside inventory_sync reads or writes snapshots.

SHELL STEP 1: signatures only. No storage backend chosen. No DB table,
no file I/O, no JSON schema.
"""

from typing import Any, Optional


def load_previous(supplier_id: str) -> Optional[Any]:
    """Load the most recent saved snapshot for a supplier.

    Returns None on first run (no prior snapshot).

    Args:
        supplier_id: The stable supplier identifier (e.g. "lm", "dl",
            "roc") from the scraper's supplier_id attribute.

    Returns:
        The prior snapshot object, or None if no snapshot exists yet.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")


def save(supplier_id: str, current: Any) -> None:
    """Persist a new snapshot for a supplier.

    The engine MUST NOT call this until b2bwave_push.apply has
    reported success. Dry-runs MUST NOT call this at all.

    Args:
        supplier_id: The stable supplier identifier.
        current: The ScrapeResult that was successfully pushed.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
