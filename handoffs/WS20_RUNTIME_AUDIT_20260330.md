# WS20 RUNTIME AUDIT REPORT — CFC Order Backend Sandbox

**Date:** 2026-03-30
**Method:** Code audit only (no runtime traces or logs available)
**Repo:** CFCOrderBackend_Sandbox — FastAPI v6.2.0, 44 Python modules

---

## 1. EXECUTION MAP

### Critical distinction: This repo is NOT an MCP server

The `repo_read_file`, `repo_write_file`, `repo_diff`, `repo_read_multiple` tools referenced in the
audit scope are **external MCP tools** provided by MCP server `5f61d126-a30a-4cfd-9896-5073cfbe9b42`.
That MCP server wraps GitHub API access. Its code is **not in this repo**. This repo is the **target**
of those tools, not the implementer.

### What IS in this repo — actual execution paths

| Function | File:Line | Status | What it does |
|---|---|---|---|
| `b2bwave_api_request()` | `sync_service.py:42` | LIVE | HTTP Basic Auth to B2BWave, 30s timeout, no retry |
| `sync_order_from_b2bwave()` | `sync_service.py:68` | LIVE | Upsert order + line items + shipments to PostgreSQL |
| `run_auto_sync()` | `sync_service.py:248` | LIVE | `while True` daemon: B2BWave + Gmail + Square sync every 15min |
| `start_auto_sync_thread()` | `sync_service.py:311` | LIVE | Starts daemon thread on app startup (`main.py:200`) |
| `get_gmail_access_token()` | `gmail_sync.py:45` | LIVE | OAuth2 token refresh, 50min cache, global state |
| `gmail_api_request()` | `gmail_sync.py:78` | LIVE | Gmail API call, 30s timeout, returns None on failure |
| `_call_rl_sandbox()` | `rl_quote_proxy.py:63` | LIVE | HTTP to rl-quote-sandbox microservice, 30s timeout |
| `call_anthropic_api()` | `ai_summary.py:21` | LIVE | Claude API call, 60s timeout, up to 2048 tokens |
| `get_db()` | `db_helpers.py:18` | LIVE | Context manager: connect -> yield -> commit/rollback -> close |
| `_get_access_token()` | `invoice_routes.py:~162` | LIVE | Gmail OAuth for WS17 invoice scanner (separate from gmail_sync) |
| `_search_messages()` | `invoice_routes.py:~399` | LIVE | Gmail pagination loop (bounded by max_results) |
| `_audit_log` (in-memory list) | `routes/audit.py:31` | LIVE | Append-only list, lost on process restart |

No runner / local runner / tool runner exists in this codebase.
No MCP bridge, adapter, or tool execution layer exists here.
No backup-before-write exists. DB writes are upserts (ON CONFLICT DO UPDATE).
No approval/confirmation gates exist.

---

## 2. RETRY / LOOP RISKS

| Location | Trigger | What Repeats | Stop Condition | Risk |
|---|---|---|---|---|
| `sync_service.py:258` `while True` | App startup | Full B2BWave + Gmail + Square sync | NONE (infinite) | HIGH |
| `sync_service.py:278-282` per-order try/except | Order sync failure | Logs error, continues to next | Bounded by order count | LOW |
| `sync_service.py:289-294` gmail_sync_func | Gmail sync failure | Logs error, continues | Single attempt | LOW |
| `sync_service.py:298-303` square_sync_func | Square sync failure | Logs error, continues | Single attempt | LOW |
| `invoice_routes.py:~399` `while True` | Gmail API call | Fetches next page | max_results OR no nextPageToken OR API error | MEDIUM |
| `alerts_engine.py:133` `while current <= end` | Business day calc | Date increment | Date range boundary | LOW |
| All urllib calls (13 files) | HTTP request | Nothing (no retry) | Single attempt, 30s timeout | LOW |

Key finding: Zero retry/backoff mechanisms anywhere. Only loop risks are the infinite auto-sync
daemon and the Gmail pagination.

---

## 3. FAILURE HANDLING

| Failure Case | Where Handled | Explicit? | Stops Clean? | Retry/Burn Risk |
|---|---|---|---|---|
| Read timeout | All urllib calls | YES | YES | NO |
| Write timeout | `get_db()` context manager | YES | YES (rollback) | NO |
| B2BWave API failure | `b2bwave_api_request()` -> `B2BWaveAPIError` | YES | YES | NO (but auto-sync catches and loops) |
| Gmail API failure | `gmail_api_request()` -> returns None | INDIRECT | Partial | MEDIUM (no error signal) |
| Gmail auth failure (401) | `get_gmail_access_token()` -> returns None | INDIRECT | YES | MEDIUM (auto-sync retries every 15min) |
| Admin auth failure | `auth.py` -> HTTPException 401/403 | YES | YES | NO |
| Pydantic validation | 422 auto-response | YES | YES | NO |
| DB connection failure | `psycopg2.connect()` NOT wrapped | NO (crashes endpoint) | YES (500) | NO |
| Rate limit (429) | NOT HANDLED ANYWHERE | NO | NO | HIGH (could hammer APIs) |
| Bare except in migrations | `db_migrations.py` (9+ blocks) | NO (swallows all) | Continues silently | MEDIUM |

---

## 4. TOKEN-BURN CAUSES

### API token burn (actual cost)

| # | Cause | Evidence | Severity |
|---|---|---|---|
| 1 | `generate_comprehensive_summary()` loads full order history into Claude prompt | `ai_summary.py:159-283` - ALL snippets, ALL events, ALL shipments, max_tokens=2048 | HIGH |
| 2 | Auto-sync daemon calls Gmail + Square every 15 min | `sync_service.py:258-308` - runs even if nothing changed | MEDIUM |
| 3 | Gmail sync fetches up to 100 messages per scan | `invoice_routes.py:_search_messages()` - N+1 API pattern | MEDIUM |
| 4 | No caching on AI summaries | `ai_summary.py` - fresh Anthropic API call every time | HIGH |
| 5 | Email snippet context up to 500 chars x all emails | `ai_summary.py:246` | MEDIUM |

### LLM context token burn (Claude Code sessions)

| # | Cause | Evidence | Severity |
|---|---|---|---|
| 1 | Large route files | orders_routes (1113), shipping_routes (832), invoice_routes (718), rl_carriers (719) | HIGH |
| 2 | 14 handoff documents | `handoffs/` directory | MEDIUM |
| 3 | No .claudeignore or CLAUDE.md | No guidance for MCP tools to avoid large/irrelevant files | MEDIUM |

---

## 5. WS20 COMPLIANCE CHECK

| WS20 Rule | Status | Evidence |
|---|---|---|
| Inspect once | PARTIAL | API calls are single-attempt. But `generate_comprehensive_summary()` does 3 DB queries for same order. Auto-sync re-inspects all orders every 15min. |
| Decide path | YES | Route handlers have clear branching. `rl_quote_proxy.py:184-267` auto-quote is well-structured. |
| Perform one bounded action | PARTIAL | Individual API calls bounded (30s). But `run_auto_sync()` is unbounded infinite loop with 3 sync actions per iteration. |
| Stop on structured failure | NO | `run_auto_sync()` catches ALL exceptions and continues. `gmail_api_request()` returns None. `_search_messages()` exits silently on error. |
| Require explicit approval before repo write | N/A | Backend doesn't write to repos. DB writes use upserts without approval. |
| Restrict code writes to runner only | N/A | No runner exists. No code generation in this backend. |

---

## 6. TOP 5 FIXES

### Fix 1: Circuit breaker for auto-sync daemon
**File:** `sync_service.py:258-308`
**Problem:** `while True` + broad `except Exception` = runs forever, catches everything, no backoff.
**Fix:** Add `max_consecutive_failures` counter. After 5 failures, sleep 1hr. After 20, stop thread.

### Fix 2: Structured errors from Gmail functions (not None)
**Files:** `gmail_sync.py:78-99`, `invoice_routes.py:_search_messages()`
**Problem:** Returns None on failure. Callers can't distinguish "no data" from "service down."
**Fix:** Return error dicts or raise typed exceptions. Stop treating errors as empty results.

### Fix 3: Cache AI summaries in database
**File:** `ai_summary.py:58-283`
**Problem:** Every summary = fresh Anthropic API call with full context.
**Fix:** Cache in orders table with TTL. Invalidate on order update. ~90% cost reduction.

### Fix 4: Handle 429 rate-limit responses explicitly
**Files:** All urllib API clients (13 files)
**Problem:** 429 treated as generic error. No backoff.
**Fix:** Detect 429, extract Retry-After, return structured error with backoff hint.

### Fix 5: Add CLAUDE.md with file-size guidance
**Problem:** No config guiding MCP tools on which files to skip or how to scope reads.
**Fix:** Add CLAUDE.md with large file warnings, preferred read patterns, skip rules.

---

## BLUNT SUMMARY

* **Primary token-burn source:** `ai_summary.py` (uncached Anthropic calls with full order context) + auto-sync daemon (3 API services every 15min regardless of changes)
* **Primary control failure:** `sync_service.py:258` infinite `while True` loop with bare `except Exception` that swallows all errors
* **Most dangerous hidden behavior:** `gmail_api_request()` and `_search_messages()` return None/partial results on API failure with no error signal; auto-sync retries with dead credentials every 15 minutes
* **Safe to continue using as-is?** YES with caveats. Individual requests work correctly. Daemon loop is bounded by being a daemon thread (dies with process). API costs are the real concern.
* **Minimum fix before next serious run:** Cache AI summaries (fix #3) and add circuit breaker to auto-sync (fix #1)
