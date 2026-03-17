"""
invoice_routes.py
WS17 — Invoice Intelligence System
CFC Orders backend route module — Phase 1 Gmail scanner.

Execution lives here because cfc-orders already has:
  - DATABASE_URL (Render PostgreSQL)
  - GMAIL_CLIENT_ID / SECRET / REFRESH_TOKEN
  - psycopg2, httpx, BeautifulSoup (or graceful fallback)

Endpoints:
  POST /invoice/migrate — one-shot DB migration (tables for WS17)
  POST /invoice/scan    — run Phase 1 Gmail scan (classify + write to DB)
  GET  /invoice/status  — summary counts per email_type + flag counts
  GET  /invoice/flags   — unresolved invoice_flags rows
  GET  /invoice/emails  — recent invoice_emails rows (for UI table)

Supplier config (confirmed 2026-03-17):
  LI invoice flow: LI -> QuickBooks -> cfcinvoices42@gmail.com -> cabinetsforcontractors@gmail.com
  OWN_EMAIL: cabinetsforcontractors@gmail.com
  cfcinvoices42 is a SUPPLIER address, never OWN_EMAIL.
"""

import re
import json
import base64
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
import psycopg2
import psycopg2.extras

from db_helpers import get_db
from auth import require_admin

invoice_router = APIRouter(prefix="/invoice", tags=["WS17 Invoice Intelligence"])

# =============================================================================
# MIGRATION STATEMENTS
# Each statement is a separate string — avoids semicolon-splitting bugs
# that occur when SQL string literals contain semicolons.
# Safe to run multiple times (IF NOT EXISTS / ON CONFLICT DO NOTHING).
# =============================================================================

_MIGRATION_STATEMENTS = [
    # invoice_emails
    """
    CREATE TABLE IF NOT EXISTS invoice_emails (
        id                  SERIAL PRIMARY KEY,
        gmail_message_id    VARCHAR(255) UNIQUE NOT NULL,
        supplier            VARCHAR(100),
        sender_email        VARCHAR(255),
        subject             TEXT,
        received_at         TIMESTAMPTZ,
        email_type          VARCHAR(50),
        classifier_score    INTEGER,
        has_attachment      BOOLEAN DEFAULT FALSE,
        processed           BOOLEAN DEFAULT FALSE,
        created_at          TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_invoice_emails_type     ON invoice_emails(email_type)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_emails_supplier ON invoice_emails(supplier)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_emails_received ON invoice_emails(received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_emails_unproc   ON invoice_emails(processed) WHERE NOT processed",

    # invoice_attachments
    """
    CREATE TABLE IF NOT EXISTS invoice_attachments (
        id              SERIAL PRIMARY KEY,
        email_id        INTEGER REFERENCES invoice_emails(id) ON DELETE CASCADE,
        filename        VARCHAR(255),
        file_type       VARCHAR(20),
        storage_path    TEXT,
        parsed          BOOLEAN DEFAULT FALSE,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_invoice_att_email    ON invoice_attachments(email_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_att_unparsed ON invoice_attachments(parsed) WHERE NOT parsed",

    # invoice_line_items
    """
    CREATE TABLE IF NOT EXISTS invoice_line_items (
        id                  SERIAL PRIMARY KEY,
        attachment_id       INTEGER REFERENCES invoice_attachments(id) ON DELETE CASCADE,
        supplier            VARCHAR(100) NOT NULL,
        invoice_number      VARCHAR(100),
        invoice_date        DATE,
        sku                 VARCHAR(100),
        description         TEXT,
        quantity            NUMERIC(10,2),
        unit_price_raw      NUMERIC(12,4),
        net_cost            NUMERIC(12,4),
        pricing_method      VARCHAR(50),
        created_at          TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_invoice_li_supplier ON invoice_line_items(supplier)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_li_sku      ON invoice_line_items(sku)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_li_att      ON invoice_line_items(attachment_id)",

    # supplier_pricing_rules
    """
    CREATE TABLE IF NOT EXISTS supplier_pricing_rules (
        id              SERIAL PRIMARY KEY,
        supplier        VARCHAR(100) UNIQUE NOT NULL,
        method          VARCHAR(50) NOT NULL,
        multiplier      NUMERIC(6,4),
        discount_pct    NUMERIC(6,4),
        notes           TEXT,
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "INSERT INTO supplier_pricing_rules (supplier, method, notes) VALUES ('GHI', 'net', 'Use invoice price directly') ON CONFLICT (supplier) DO NOTHING",
    "INSERT INTO supplier_pricing_rules (supplier, method, notes) VALUES ('LI', 'wsf_floor', 'WSP baseline - floor for all LI door lines') ON CONFLICT (supplier) DO NOTHING",
    "INSERT INTO supplier_pricing_rules (supplier, method, notes) VALUES ('Go Bravura', 'net', 'COGS = box+door combined - each door line has own SKU list and own COGS') ON CONFLICT (supplier) DO NOTHING",
    "INSERT INTO supplier_pricing_rules (supplier, method, notes) VALUES ('DL', 'tbd', 'SKUs in body HTML no PDF - verify pricing from first invoice') ON CONFLICT (supplier) DO NOTHING",
    "INSERT INTO supplier_pricing_rules (supplier, method, notes) VALUES ('ROC', 'tbd', 'Verify pricing method from first invoice') ON CONFLICT (supplier) DO NOTHING",
    "INSERT INTO supplier_pricing_rules (supplier, method, notes) VALUES ('Cabinet & Stone', 'tbd', 'Verify pricing method from first invoice') ON CONFLICT (supplier) DO NOTHING",
    "INSERT INTO supplier_pricing_rules (supplier, method, notes) VALUES ('DuraStone', 'tbd', 'Verify pricing method from first invoice') ON CONFLICT (supplier) DO NOTHING",
    "INSERT INTO supplier_pricing_rules (supplier, method, notes) VALUES ('Love-Milestone', 'tbd', 'Verify pricing method from first invoice') ON CONFLICT (supplier) DO NOTHING",

    # invoice_flags
    """
    CREATE TABLE IF NOT EXISTS invoice_flags (
        id              SERIAL PRIMARY KEY,
        line_item_id    INTEGER REFERENCES invoice_line_items(id) ON DELETE CASCADE,
        flag_type       VARCHAR(50) NOT NULL,
        sku             VARCHAR(100),
        supplier        VARCHAR(100),
        invoice_number  VARCHAR(100),
        order_id        VARCHAR(100),
        master_cogs     NUMERIC(12,4),
        invoice_cost    NUMERIC(12,4),
        delta_pct       NUMERIC(8,4),
        detail          TEXT,
        resolved        BOOLEAN DEFAULT FALSE,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_flags_unresolved ON invoice_flags(resolved) WHERE NOT resolved",
    "CREATE INDEX IF NOT EXISTS idx_flags_supplier   ON invoice_flags(supplier)",
    "CREATE INDEX IF NOT EXISTS idx_flags_sku        ON invoice_flags(sku)",
    "CREATE INDEX IF NOT EXISTS idx_flags_type       ON invoice_flags(flag_type)",
]

# =============================================================================
# GMAIL AUTH — reuses same env vars as gmail_sync.py
# =============================================================================

GMAIL_CLIENT_ID     = os.environ.get("GMAIL_CLIENT_ID", "").strip()
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()

_access_token: Optional[str] = None
_token_expires: Optional[datetime] = None


def _get_access_token() -> str:
    """Refresh and return a Gmail API access token. Raises RuntimeError on failure."""
    global _access_token, _token_expires
    if _access_token and _token_expires and datetime.now(timezone.utc) < _token_expires:
        return _access_token
    if not (GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN):
        raise RuntimeError(
            "Gmail not configured — GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / "
            "GMAIL_REFRESH_TOKEN env vars not set on cfc-orders"
        )

    import urllib.request, urllib.parse, urllib.error
    data = urllib.parse.urlencode({
        "client_id":     GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "refresh_token": GMAIL_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        raise RuntimeError(
            f"Gmail token refresh failed (HTTP {e.code}): {body[:200]}"
        )
    except Exception as e:
        raise RuntimeError(f"Gmail token refresh error: {e}")

    if "access_token" not in payload:
        raise RuntimeError(f"Gmail token refresh: no access_token in response: {payload}")

    _access_token  = payload["access_token"]
    _token_expires = datetime.now(timezone.utc) + timedelta(minutes=50)
    return _access_token


def _gmail_get(endpoint: str, params: dict = None) -> Optional[dict]:
    """Make an authenticated GET to the Gmail API. Returns None on any failure."""
    import urllib.request, urllib.parse, urllib.error
    try:
        token = _get_access_token()
    except RuntimeError as e:
        print(f"[WS17:invoice] Auth error: {e}")
        raise   # re-raise so caller can surface the real error message
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        print(f"[WS17:invoice] Gmail API HTTP {e.code} on /{endpoint}: {body[:300]}")
        raise RuntimeError(f"Gmail API error {e.code} on /{endpoint}: {body[:200]}")
    except Exception as e:
        print(f"[WS17:invoice] Gmail API request error: {e}")
        raise RuntimeError(f"Gmail API request error: {e}")

# =============================================================================
# SUPPLIER CONFIG (confirmed 2026-03-17)
# =============================================================================

SUPPLIER_DOMAINS = {
    "GHI":             ["ghicabinets.com"],
    "DL":              ["dlcabinetry.com"],
    "ROC":             ["roccabinetry.com"],
    "Go Bravura":      ["gobravura.com"],
    "Cabinet & Stone": ["cabinetstonellc.com"],
    "DuraStone":       ["durastoneusa.com"],
}

SUPPLIER_FULL_ADDRESSES = {
    "LI":             ["cfcinvoices42@gmail.com", "cabinetrydistribution@gmail.com"],
    "Love-Milestone": ["lovetoucheskitchen@gmail.com"],
}

OWN_EMAILS = {"cabinetsforcontractors@gmail.com"}
OWN_EMAIL_NEVER = {"cfcinvoices42@gmail.com", "cabinetrydistribution@gmail.com", "lovetoucheskitchen@gmail.com"}
OWN_EMAIL_PATTERNS = ["cabinetsforcontractors.net", "4wprince", "cabinetcloudai"]


def _identify_supplier(sender: str) -> Optional[str]:
    sl = sender.lower().strip()
    if "<" in sl:
        sl = sl.split("<")[-1].rstrip(">").strip()
    for sup, addrs in SUPPLIER_FULL_ADDRESSES.items():
        if sl in addrs:
            return sup
    domain = sl.split("@")[-1] if "@" in sl else ""
    for sup, domains in SUPPLIER_DOMAINS.items():
        if domain in domains:
            return sup
    return None


def _is_own_email(sender: str) -> bool:
    sl = sender.lower().strip()
    addr = sl.split("<")[-1].rstrip(">").strip() if "<" in sl else sl
    if addr in OWN_EMAIL_NEVER:
        return False
    if addr in OWN_EMAILS:
        return True
    return any(p in addr for p in OWN_EMAIL_PATTERNS)

# =============================================================================
# CLASSIFIER
# =============================================================================

_SKU_RE = re.compile(r'\b[A-Z]{1,5}\d{2,4}[A-Z0-9_\-]*\b')

SCORE_INVOICE = 5
SCORE_ORDER_CONFIRMATION = 3
SCORE_ESTIMATE = 1


def _has_sku_table(text: str) -> bool:
    return len(set(_SKU_RE.findall(text.upper()))) >= 3


def _classify(msg: dict) -> tuple:
    subject   = msg["subject"].lower()
    sender    = msg["from"].lower()
    body      = (msg["body_plain"] + " " + msg["body_html"]).lower()
    atts      = msg["attachments"]
    has_pdf   = any(a["type"] == "pdf"  for a in atts)
    has_excel = any(a["type"] == "xlsx" for a in atts)

    if ("square.link" in body or "squareup.com/pay" in body or
            "noreply@messaging.squareup.com" in sender):
        return "PAYMENT_LINK", 0

    if "fwd: order" in subject and _is_own_email(sender):
        return "INTERNAL_ORDER", 0

    tracking_kw = ("tracking", "has shipped", "delivery confirmation",
                   "pro number", "freight tracking", "shipment update")
    if any(k in subject for k in tracking_kw):
        if not any(k in body for k in ("invoice", "unit price", "unit cost", "sku")):
            return "TRACKING", 0

    score = 0
    if has_pdf:                                                           score += 2
    if "invoice" in subject or "invoice" in body:                         score += 2
    if _has_sku_table(body):                                              score += 2
    if _identify_supplier(sender):                                        score += 1
    if has_excel:                                                         score += 1
    if any(k in body for k in ("unit price", "unit cost", "per unit",
                                "net price", "net cost")):                score += 1
    if any(k in body for k in ("order #", "order number", "sales order",
                                "po ", "purchase order")):                score += 1
    if any(k in subject for k in ("order confirmation", "your order",
                                   "sales order", "re: po",
                                   "invoice attached", "new invoice")):   score += 1
    if "intuit" in body or "quickbooks" in body:                          score += 1
    if "cfcinvoices42" in sender:                                         score += 1

    if "sample" in subject:           score -= 2
    if re.search(r'\$[0-9]\b', body): score -= 1
    if "unsubscribe" in body:         score -= 1
    if "free shipping" in body:       score -= 1

    if score >= SCORE_INVOICE:            return "INVOICE", score
    if score >= SCORE_ORDER_CONFIRMATION: return "ORDER_CONFIRMATION", score
    if score >= SCORE_ESTIMATE:           return "ESTIMATE", score
    return "IGNORE", score

# =============================================================================
# MESSAGE FETCH
# =============================================================================

def _walk_parts(part: dict, state: dict):
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    if mime == "text/plain" and body.get("data") and not state["body_plain"]:
        state["body_plain"] = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="ignore")
    elif mime == "text/html" and body.get("data") and not state["body_html"]:
        state["body_html"] = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="ignore")
    elif mime.startswith("multipart/"):
        for sub in part.get("parts", []):
            _walk_parts(sub, state)
    elif body.get("attachmentId"):
        filename = part.get("filename", "")
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        att_type = {"pdf": "pdf", "xlsx": "xlsx", "xls": "xlsx", "csv": "csv"}.get(ext, "other")
        if att_type != "other":
            state["attachments"].append({
                "filename": filename,
                "attachment_id": body["attachmentId"],
                "type": att_type,
            })


def _fetch_message(message_id: str) -> Optional[dict]:
    data = _gmail_get(f"messages/{message_id}", {"format": "full"})
    if not data:
        return None
    headers = {h["name"].lower(): h["value"]
               for h in data.get("payload", {}).get("headers", [])}
    state = {"body_plain": "", "body_html": "", "attachments": []}
    flat = data.get("payload", {}).get("body", {})
    if flat.get("data"):
        decoded = base64.urlsafe_b64decode(flat["data"]).decode("utf-8", errors="ignore")
        if data["payload"].get("mimeType") == "text/plain":
            state["body_plain"] = decoded
        else:
            state["body_html"] = decoded
    for part in data.get("payload", {}).get("parts", []):
        _walk_parts(part, state)

    received_at = None
    if data.get("internalDate"):
        try:
            received_at = datetime.fromtimestamp(int(data["internalDate"]) / 1000, tz=timezone.utc)
        except Exception:
            pass

    return {
        "id":          message_id,
        "subject":     headers.get("subject", ""),
        "from":        headers.get("from", ""),
        "received_at": received_at,
        **state,
    }


def _search_messages(query: str, max_results: int = 100) -> list:
    """Search Gmail. Returns empty list (not raises) if query returns no results."""
    results, page_token = [], None
    while True:
        params = {"q": query, "maxResults": min(max_results - len(results), 100)}
        if page_token:
            params["pageToken"] = page_token
        data = _gmail_get("messages", params)
        if not data:
            break
        results.extend(data.get("messages", []))
        page_token = data.get("nextPageToken")
        if not page_token or len(results) >= max_results:
            break
    return results

# =============================================================================
# DB HELPERS
# =============================================================================

def _upsert_email(conn, gmail_id, **fields) -> int:
    cols  = ["gmail_message_id"] + list(fields.keys())
    vals  = [gmail_id] + list(fields.values())
    ph    = ", ".join(["%s"] * len(cols))
    names = ", ".join(cols)
    upd   = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO invoice_emails ({names}) VALUES ({ph}) "
            f"ON CONFLICT (gmail_message_id) DO UPDATE SET {upd} RETURNING id",
            vals
        )
        return cur.fetchone()[0]


def _insert_attachment(conn, email_id, filename, file_type):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO invoice_attachments (email_id, filename, file_type, storage_path) "
            "VALUES (%s, %s, %s, '') RETURNING id",
            (email_id, filename, file_type)
        )
        return cur.fetchone()[0]

# =============================================================================
# PHASE 1 CORE
# =============================================================================

def _run_phase1(days_back: int = 30, hours_back: int = None, dry_run: bool = False) -> dict:
    time_filter = f"newer_than:{hours_back}h" if hours_back else f"newer_than:{days_back}d"

    # Gmail search query rules:
    # - No trailing @ in -from: operator (invalid syntax -> 400)
    # - Quoted phrases use plain double quotes; no backslash escaping needed
    # - Each query is independently safe to fail (try/except per query)
    queries = [
        # Confirmed supplier addresses — domain-based and full Gmail addresses
        ("supplier_addresses",
         f"{time_filter} "
         f"(from:ghicabinets.com OR from:dlcabinetry.com OR from:roccabinetry.com "
         f"OR from:gobravura.com OR from:cabinetstonellc.com OR from:durastoneusa.com "
         f"OR from:cabinetrydistribution@gmail.com OR from:lovetoucheskitchen@gmail.com "
         f"OR from:cfcinvoices42@gmail.com)"),

        # Keyword catch-all for any supplier not yet in address list
        # Note: -from:squareup.com only, no trailing-@ operators
        ("keyword_subject",
         f"{time_filter} "
         f"(subject:invoice OR subject:\"order confirmation\" OR subject:\"sales order\" "
         f"OR subject:\"re: po\" OR subject:\"invoice attached\") "
         f"-from:squareup.com"),

        # Internal forwarded order emails (Type 4 — INTERNAL_ORDER)
        ("internal_fwd",
         f"{time_filter} subject:\"fwd: order\""),
    ]

    seen: set = set()
    all_msgs: list = []
    query_errors: list = []

    for qname, q in queries:
        try:
            msgs = _search_messages(q)
            new  = [m for m in msgs if m["id"] not in seen]
            print(f"[WS17:scan] {qname}: {len(msgs)} found, {len(new)} new")
            for m in new:
                seen.add(m["id"])
                all_msgs.append(m)
        except RuntimeError as e:
            # Log query error but continue with remaining queries
            msg = f"{qname}: {e}"
            print(f"[WS17:scan] QUERY ERROR — {msg}")
            query_errors.append(msg)

    print(f"[WS17:scan] Total unique messages: {len(all_msgs)}")

    if not all_msgs and query_errors:
        # All queries failed — surface the first error clearly
        raise RuntimeError(f"All Gmail queries failed. First error: {query_errors[0]}")

    counts = {
        "INVOICE": 0, "ORDER_CONFIRMATION": 0, "ESTIMATE": 0,
        "INTERNAL_ORDER": 0, "TRACKING": 0, "PAYMENT_LINK": 0,
        "IGNORE": 0, "errors": 0,
    }

    if dry_run:
        results = []
        for m in all_msgs:
            try:
                msg = _fetch_message(m["id"])
                if not msg:
                    counts["errors"] += 1
                    continue
                email_type, score = _classify(msg)
                supplier = _identify_supplier(msg["from"])
                counts[email_type] = counts.get(email_type, 0) + 1
                results.append({
                    "email_type": email_type,
                    "score":      score,
                    "supplier":   supplier,
                    "from":       msg["from"][:60],
                    "subject":    msg["subject"][:80],
                })
            except Exception as e:
                counts["errors"] += 1
        return {"dry_run": True, "counts": counts, "results": results,
                "query_errors": query_errors}

    with get_db() as conn:
        for m in all_msgs:
            try:
                msg = _fetch_message(m["id"])
                if not msg:
                    counts["errors"] += 1
                    continue
                email_type, score = _classify(msg)
                supplier  = _identify_supplier(msg["from"])
                has_att   = len(msg["attachments"]) > 0

                counts[email_type] = counts.get(email_type, 0) + 1

                if email_type == "PAYMENT_LINK":
                    continue

                email_id = _upsert_email(
                    conn,
                    msg["id"],
                    supplier         = supplier,
                    sender_email     = msg["from"],
                    subject          = msg["subject"],
                    received_at      = msg["received_at"],
                    email_type       = email_type,
                    classifier_score = score,
                    has_attachment   = has_att,
                )
                conn.commit()

                if email_type in ("INVOICE", "ORDER_CONFIRMATION", "ESTIMATE"):
                    for att in msg["attachments"]:
                        _insert_attachment(conn, email_id, att["filename"], att["type"])
                    if not has_att and (msg["body_html"] or msg["body_plain"]):
                        _insert_attachment(conn, email_id, f"body_{msg['id']}.html", "body_html")
                    conn.commit()

            except Exception as e:
                print(f"[WS17:scan] ERROR on {m['id']}: {e}")
                counts["errors"] += 1

    return {"dry_run": False, "counts": counts, "query_errors": query_errors}

# =============================================================================
# ROUTES
# =============================================================================

@invoice_router.post("/migrate")
def migrate(admin=require_admin):
    """
    Run WS17 DB migration — creates 5 invoice intelligence tables.
    Each SQL statement is a separate Python string — no semicolon splitting.
    Safe to run multiple times.
    """
    executed = 0
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                for stmt in _MIGRATION_STATEMENTS:
                    cur.execute(stmt)
                    executed += 1
            conn.commit()
    except Exception as e:
        raise HTTPException(500, f"Migration failed at statement {executed + 1}: {e}")

    return {
        "status": "ok",
        "message": f"WS17 migration complete — {executed} statements executed",
        "tables": [
            "invoice_emails",
            "invoice_attachments",
            "invoice_line_items",
            "supplier_pricing_rules",
            "invoice_flags",
        ],
        "note": "Safe to run again — IF NOT EXISTS / ON CONFLICT DO NOTHING everywhere",
    }


@invoice_router.post("/scan")
def scan(
    days:    int  = Query(30,    description="Days back to scan"),
    hours:   int  = Query(None,  description="Hours back (overrides days)"),
    dry_run: bool = Query(False, description="Classify without writing to DB"),
    admin=require_admin,
):
    """Run Phase 1 Gmail scan — classify supplier invoice emails and write to DB."""
    try:
        result = _run_phase1(days_back=days, hours_back=hours, dry_run=dry_run)
        return {"status": "ok", **result}
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(500, f"Scan failed: {e}")


@invoice_router.get("/status")
def status(admin=require_admin):
    """Invoice pipeline summary — email type counts, flag counts, last scan."""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'invoice_emails'
        """)
        if cur.fetchone()["count"] == 0:
            return {"status": "migration_not_run",
                    "message": "Run POST /invoice/migrate first"}

        cur.execute("SELECT email_type, COUNT(*) cnt FROM invoice_emails GROUP BY email_type ORDER BY cnt DESC")
        email_counts = {r["email_type"]: r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT COUNT(*) total FROM invoice_emails")
        total = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) cnt FROM invoice_flags WHERE NOT resolved")
        flags = cur.fetchone()["cnt"]

        cur.execute("SELECT flag_type, COUNT(*) cnt FROM invoice_flags WHERE NOT resolved GROUP BY flag_type")
        flag_breakdown = {r["flag_type"]: r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT MAX(created_at) last FROM invoice_emails")
        last_scan = cur.fetchone()["last"]

        cur.execute("SELECT COUNT(*) cnt FROM invoice_attachments WHERE storage_path = '' OR storage_path IS NULL")
        pending = cur.fetchone()["cnt"]

    return {
        "status":            "ok",
        "total_emails":      total,
        "email_type_counts": email_counts,
        "unresolved_flags":  flags,
        "flag_breakdown":    flag_breakdown,
        "pending_downloads": pending,
        "last_scan":         last_scan.isoformat() if last_scan else None,
    }


@invoice_router.get("/emails")
def list_emails(
    email_type: Optional[str] = None,
    supplier:   Optional[str] = None,
    limit:      int = 50,
    admin=require_admin,
):
    """Recent invoice_emails rows — for Brain UI table view."""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        where_parts, params = ["1=1"], []
        if email_type:
            where_parts.append("email_type = %s"); params.append(email_type)
        if supplier:
            where_parts.append("supplier = %s"); params.append(supplier)
        params.append(limit)
        cur.execute(
            f"SELECT id, gmail_message_id, supplier, sender_email, subject, "
            f"received_at, email_type, classifier_score, has_attachment, processed "
            f"FROM invoice_emails WHERE {' AND '.join(where_parts)} "
            f"ORDER BY received_at DESC NULLS LAST LIMIT %s",
            params
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"status": "ok", "count": len(rows), "emails": rows}


@invoice_router.get("/flags")
def list_flags(
    supplier:  Optional[str] = None,
    flag_type: Optional[str] = None,
    limit:     int = 100,
    admin=require_admin,
):
    """Unresolved invoice_flags — COGS mismatches, overcharges, missing SKUs."""
    with get_db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        where_parts, params = ["NOT resolved"], []
        if supplier:
            where_parts.append("supplier = %s"); params.append(supplier)
        if flag_type:
            where_parts.append("flag_type = %s"); params.append(flag_type)
        params.append(limit)
        cur.execute(
            f"SELECT id, flag_type, sku, supplier, invoice_number, "
            f"master_cogs, invoice_cost, delta_pct, detail, created_at "
            f"FROM invoice_flags WHERE {' AND '.join(where_parts)} "
            f"ORDER BY created_at DESC LIMIT %s",
            params
        )
        flags = [dict(r) for r in cur.fetchall()]
    return {"status": "ok", "count": len(flags), "flags": flags}
