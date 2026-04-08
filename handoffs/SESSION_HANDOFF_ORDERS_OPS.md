# Platform Ops — Session Handoff
**Last Updated:** 2026-03-05
**Workstream:** CFC Orders
**Status:** Paused — needs health checks before any work

## What This Lane Covers
Sandbox-to-production promotion pipeline, repo cleanup, database migrations, Render deployment health monitoring, and environment variable management across all CFC Orders repos.

## Current State
- **Sandbox backend v6.0.0** deployed to Render (cfcorderbackend-sandbox.onrender.com) — modularized, 12+ files
- **Sandbox frontend v7.2.2 (App.jsx)** deployed to Vercel — Working — dark theme, alerts, lifecycle, api.js auth wrapper live.
- **Production backend** is 2 months behind sandbox — still monolithic main.py (3,113 lines)
- **Production frontend** is ~2 weeks behind sandbox
- **Dead files** need cleanup: main2.py, main4.py, main7.py, main8.py, rl_api_test_clean.py, desktop.ini
- **Frontend .gitignore broken** — node_modules committed to git
- **PostgreSQL is alive ✅** — 10 tables, lifecycle fields migrated, 15 orders backfilled.

## Key Files
- `CFCOrderBackend_Sandbox/config.py` — 155 lines, all env vars and constants
- `CFCOrderBackend_Sandbox/schema.py` — 298 lines, full DB schema SQL
- `CFCOrderBackend_Sandbox/db_helpers.py` — 287 lines, database connection management
- `CFCOrderBackend_Sandbox/db_migrations.py` — 255 lines, schema migration helpers
- `cfc-orders:handoffs/CFC_ORDERS_PLAN.md` — full upgrade plan with 5 steps

## Active Bugs / Blockers
1. **Render is on a PAID plan** — servers do NOT sleep. Never troubleshoot with cold start assumption.
2. **PostgreSQL is alive ✅** — 10 tables, lifecycle fields migrated, 15 orders backfilled.
3. **Dead files** in sandbox backend — 12K+ lines of dead code (main2/4/7/8.py)
4. **node_modules committed** to frontend git — .gitignore needs fix
5. **README.md** is UTF-16 "force rebuild" — needs real content

## Next Steps
1. Hit health endpoints on both Render services — verify alive
2. Check PostgreSQL database status
3. Delete dead files: main2.py, main4.py, main7.py, main8.py, rl_api_test_clean.py, desktop.ini
4. Fix frontend .gitignore (exclude node_modules, dist, zip)
5. Update README.md with actual documentation
6. After all fixes: promote sandbox → production (copy modules, update config, add env vars, deploy)

## Rules & Decisions
- Sandbox is ALWAYS promoted TO production — never edit production directly
- Render build command: `pip install -r requirements.txt && rm -rf __pycache__ *.pyc`
- All env vars documented in config.py — single source of truth
- Database schema lives in schema.py — migrations in db_migrations.py

## Repos
| Repo | URL | Version | Status |
|------|-----|---------|--------|
| Sandbox Backend | github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox | v6.0.0 | Working, needs cleanup |
| Sandbox Frontend | github.com/cabinetsforcontractors/CFCOrdersFrontend_Sandbox | v7.2.2 | Working — dark theme, alerts, lifecycle live |
| Prod Backend | github.com/cabinetsforcontractors/CFCOrderBackend | ~v5.7 | 2 months behind |
| Prod Frontend | github.com/cabinetsforcontractors/CFCOrdersFrontend | ~v5.10 | 2 weeks behind |
| RL Sandbox | github.com/cabinetsforcontractors/rl-quote-sandbox | v0.1.0 | PRIVATE |
| Sandbox Backend | github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox | v6.0.0 | Working, needs cleanup |
| Sandbox Frontend | github.com/cabinetsforcontractors/CFCOrdersFrontend_Sandbox | v5.10.1 | Working, .gitignore broken |
| Prod Backend | github.com/cabinetsforcontractors/CFCOrderBackend | ~v5.7 | 2 months behind |
| Prod Frontend | github.com/cabinetsforcontractors/CFCOrdersFrontend | ~v5.10 | 2 weeks behind |
| RL Sandbox | github.com/cabinetsforcontractors/rl-quote-sandbox | v0.1.0 | PRIVATE |

## Deploy URLs
- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- Production backend: cfc-backend-b83s.onrender.com
- Frontend: Vercel (both sandbox and production)
