"""
routes/audit.py
Audit log endpoints — /audit/log

POST /audit/log  — append an admin audit log entry (admin-protected, 60/min)
GET  /audit/log  — retrieve recent audit log entries (120/min)

Mount in main.py with:
    from routes.audit import audit_router
    app.include_router(audit_router)

Storage: in-memory list, process lifetime only.
Upgrade path: swap _audit_log for a DB-backed table when persistence is needed.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from starlette.requests import Request

from auth import require_admin
from rate_limit import limiter

audit_router = APIRouter(prefix="/audit", tags=["audit"])

# ---------------------------------------------------------------------------
# In-memory store (append-only, lives for process lifetime)
# ---------------------------------------------------------------------------
_audit_log: list = []


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class AuditEntry(BaseModel):
    action: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    detail: Optional[str] = None
    user: Optional[str] = "admin"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@audit_router.post("/log")
@limiter.limit("60/minute")
async def write_audit_log(
    request: Request,
    entry: AuditEntry,
    _: bool = Depends(require_admin),
):
    """
    Append an audit log entry.

    Admin-protected (X-Admin-Token or Bearer JWT required).
    Rate limited: 60 writes/minute per IP.
    Returns the assigned numeric ID.
    """
    record = {
        "id": len(_audit_log) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **entry.dict(),
    }
    _audit_log.append(record)
    return {"success": True, "id": record["id"]}


@audit_router.get("/log")
@limiter.limit("120/minute")
async def read_audit_log(
    request: Request,
    entity_type: Optional[str] = Query(None, description="Filter by entity_type"),
    entity_id: Optional[str] = Query(None, description="Filter by entity_id"),
    limit: int = Query(100, ge=1, le=1000, description="Max entries to return"),
):
    """
    Retrieve recent audit log entries, newest first.

    Optional filters: entity_type, entity_id.
    Limit defaults to 100, max 1000.
    Rate limited: 120 reads/minute per IP.
    """
    entries = list(reversed(_audit_log))

    if entity_type:
        entries = [e for e in entries if e.get("entity_type") == entity_type]
    if entity_id:
        entries = [e for e in entries if e.get("entity_id") == entity_id]

    sliced = entries[:limit]
    return {
        "success": True,
        "total": len(_audit_log),
        "count": len(sliced),
        "entries": sliced,
    }
