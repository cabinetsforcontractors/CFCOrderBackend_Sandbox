"""inventory_sync.alert — failure alert emails via existing Gmail send path.

Per ARCHITECTURE.md section 5:
    - Two consecutive day-level failures for the same supplier trigger
      an email alert. A single transient failure does NOT alert.

Per ARCHITECTURE.md section 11:
    - This module is allowed to use the existing Gmail send path
      (already present elsewhere in the sandbox repo). It does not
      introduce new mail infrastructure.

SHELL STEP 1: signatures only. No Gmail API calls. No template strings.
No credential use. No import of existing gmail_sync / email_sender
modules yet.
"""

from typing import Any


def maybe_send(*, supplier_id: str, failure_context: Any) -> bool:
    """Decide whether a failure alert is warranted and send it if so.

    Intended behavior (see ARCHITECTURE.md section 5):
        - Inspects the per-supplier failure history tracked by the
          engine.
        - Sends an email alert only on two consecutive day-level
          failures for the same supplier.
        - Routes through the existing Gmail send path already present
          in the sandbox repo.
        - Returns True if an alert was sent, False otherwise.

    Args:
        supplier_id: The stable supplier identifier.
        failure_context: Per-run failure state used to decide whether
            the "two consecutive day-level failures" condition is met.
            Exact shape TBD.

    Returns:
        True if an alert email was sent; False otherwise.

    SHELL STEP 1: not implemented.
    """
    raise NotImplementedError("inventory_sync shell step 1 — no logic yet")
