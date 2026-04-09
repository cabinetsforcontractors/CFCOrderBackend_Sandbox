"""inventory_sync.engine — the only orchestrator for the inventory sync module.

Calls scrapers -> validates coverage and abnormal-change -> loads prior
snapshot -> computes diff -> filters through ignore list -> pushes via
b2bwave_push -> saves snapshot on push success -> maybe alerts on
repeated failure.

This is the ONLY module allowed to sequence scrape -> diff -> push in
the inventory_sync subsystem. Scraper, diff, snapshot, push, and alert
modules must not call each other directly; everything flows through
engine.run_once (or the equivalent single entry point introduced in a
later step).

See inventory_sync/ARCHITECTURE.md sections 3 (data flow), 5 (failure
rules), 6 (safety invariants), and 7 (validation gates).

SHELL STEP 1: signatures only. No logic.
"""

from typing import Any


def run_once(*, dry_run: bool = False) -> Any:
    """Run one full inventory sync pass across all configured suppliers.

    Intended flow (see ARCHITECTURE.md section 3):
        1. Iterate configured scrapers in isolation.
        2. For each supplier: scrape, validate coverage gate, load prior
           snapshot, compute diff, validate abnormal-change gate, filter
           ignore list, push via b2bwave_push (unless dry_run), save
           snapshot on push success.
        3. Track per-supplier success/failure state for the alert path.
        4. On two consecutive day-level failures for the same supplier,
           trigger alert.maybe_send.

    Args:
        dry_run: If True, all steps run except the final push and
            snapshot save. See inventory_sync/dry_run.py for the
            dedicated dry-run entry point.

    Returns:
        A per-supplier result summary. Exact shape TBD when logic is
        implemented in a later step.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
