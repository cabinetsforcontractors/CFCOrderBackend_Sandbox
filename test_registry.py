"""
test_registry.py — ONE source of truth for test/noise order ids (Beat 1,
William 2026-07-25).

Problem it kills: test rows (contract tests, smoke orders, sandbox
acceptance rows) polluted every surface — dashboard, task board, unpaid
reports — and each module kept its own hardcoded list. Now there is one
DB-backed registry that every surface asks.

Table: test_orders(order_id PK, reason, created_at). Seeded once with the
known 2026-07-24 fit-state list; after that the registry is admin-managed.

Endpoints [admin]:
  GET    /test-orders                 — list the registry
  POST   /test-orders                 — {"order_id": "...", "reason": "..."}
  DELETE /test-orders/{order_id}      — remove (order shows everywhere again)

Consumers call test_order_ids() — a plain set of strings. On any DB
hiccup it falls back to the seed list so surfaces never unfilter.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Body

from auth import require_admin
from db_helpers import get_db

test_registry_router = APIRouter(tags=["test-registry"])

# The known list as of the 2026-07-24 fit-state data notes (+ 5710, the
# canceled ROC round-trip test). Seeded into the table on first ensure;
# also the fail-safe fallback if the table can't be read.
SEED = {
    "1": "legacy placeholder row",
    "4859": "C4C test account order",
    "5706": "C4C test order (substitution drills)",
    "5710": "ROC round-trip test (canceled)",
    "5716": "automated contract test",
    "5717": "storefront push loop test",
    "5718": "storefront crash-dup test",
    "5719": "storefront crash-dup test",
    "5720": "storefront phase-1 e2e test",
    "100001": "storefront smoke test",
    "100002": "storefront smoke test",
    "100003": "storefront smoke test",
    "100004": "sandbox acceptance test",
}

_table_ready = False


def _ensure_table():
    global _table_ready
    if _table_ready:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS test_orders (
                    order_id   VARCHAR(50) PRIMARY KEY,
                    reason     TEXT DEFAULT '',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            cur.execute("SELECT COUNT(*) FROM test_orders")
            if cur.fetchone()[0] == 0:
                for oid, reason in SEED.items():
                    cur.execute(
                        "INSERT INTO test_orders (order_id, reason) VALUES (%s, %s) "
                        "ON CONFLICT (order_id) DO NOTHING", (oid, reason))
        conn.commit()
    _table_ready = True


def test_order_ids() -> set:
    """The registry as a set of order-id strings, PLUS the storefront
    quote CLASS (storefront note 2026-07-29, William accepted: any record
    whose comments carry the '[CFC-COM] … saved quote' marker is a dealer's
    saved-quote Temporary consuming a number — they mint continuously, so a
    rule beats a list). Never raises."""
    try:
        _ensure_table()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT order_id FROM test_orders")
                ids = {str(r[0]) for r in cur.fetchall()}
                cur.execute("""SELECT order_id FROM orders
                               WHERE comments ILIKE '%%[CFC-COM]%%'
                                 AND comments ILIKE '%%saved quote%%'""")
                ids |= {str(r[0]) for r in cur.fetchall()}
                return ids
    except Exception as e:
        print(f"[TEST-REGISTRY] read failed ({e}) — using seed fallback")
        return set(SEED)


@test_registry_router.get("/test-orders")
def list_test_orders(_: bool = Depends(require_admin)):
    _ensure_table()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id, reason, created_at FROM test_orders ORDER BY order_id")
            rows = [{"order_id": r[0], "reason": r[1] or "",
                     "created_at": r[2].isoformat() if r[2] else ""}
                    for r in cur.fetchall()]
    return {"status": "ok", "count": len(rows), "test_orders": rows}


@test_registry_router.post("/test-orders")
def add_test_order(payload: dict = Body(...), _: bool = Depends(require_admin)):
    _ensure_table()
    oid = str(payload.get("order_id") or "").strip()
    reason = (payload.get("reason") or "").strip()
    if not oid:
        return {"status": "error", "message": "order_id required"}
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO test_orders (order_id, reason) VALUES (%s, %s)
                ON CONFLICT (order_id) DO UPDATE SET reason = EXCLUDED.reason
            """, (oid, reason))
        conn.commit()
    return {"status": "ok", "order_id": oid, "action": "registered",
            "at": datetime.now(timezone.utc).isoformat()}


@test_registry_router.delete("/test-orders/{order_id}")
def remove_test_order(order_id: str, _: bool = Depends(require_admin)):
    _ensure_table()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM test_orders WHERE order_id = %s", (order_id,))
            gone = cur.rowcount
        conn.commit()
    return {"status": "ok", "order_id": order_id,
            "action": "removed" if gone else "was_not_registered"}
