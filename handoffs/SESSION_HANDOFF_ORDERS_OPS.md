# Platform Ops — Session Handoff
**Last Updated:** 2026-02-28
**Workstream:** CFC Orders
**Status:** Paused — needs health checks before any work

## What This Lane Covers
Sandbox-to-production promotion pipeline, repo cleanup, database migrations, Render deployment health monitoring, and environment variable management across all CFC Orders repos.

## Current State
- **Sandbox backend v6.0.0** deployed to Render (cfcorderbackend-sandbox.onrender.com) — modularized, 12+ files
- **Sandbox frontend v5.10.1** deployed to Vercel — 8 JSX components
- **Production backend** is 2 months behind sandbox — still monolithic main.py (3,113 lines)
- **Production frontend** is ~2 weeks behind sandbox
- **Dead files** need cleanup: main2.py, main4.py, main7.py, main8.py, rl_api_test_clean.py, desktop.ini
- **Frontend .gitignore broken** — node_modules committed to git
- **PostgreSQL may be expired** — Render free-tier 90-day limit, 2 months idle

## Key Files
- `CFCOrderBackend_Sandbox/config.py` — 155 lines, all env vars and constants
- `CFCOrderBackend_Sandbox/schema.py` — 298 lines, full DB schema SQL
- `CFCOrderBackend_Sandbox/db_helpers.py` — 287 lines, database connection management
- `CFCOrderBackend_Sandbox/db_migrations.py` — 255 lines, schema migration helpers
- `cfc-orders:handoffs/CFC_ORDERS_PLAN.md` — full upgrade plan with 5 steps

## Active Bugs / Blockers
1. **Render services may be dead** — 2 months idle, need to hit health endpoints
2. **PostgreSQL may be expired** — Render free-tier 90-day limit
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
| Sandbox Backend | github.com/4wprince/CFCOrderBackend_Sandbox | v6.0.0 | Working, needs cleanup |
| Sandbox Frontend | github.com/4wprince/CFCOrdersFrontend_Sandbox | v5.10.1 | Working, .gitignore broken |
| Prod Backend | github.com/4wprince/CFCOrderBackend | ~v5.7 | 2 months behind |
| Prod Frontend | github.com/4wprince/CFCOrdersFrontend | ~v5.10 | 2 weeks behind |
| RL Sandbox | github.com/4wprince/rl-quote-sandbox | v0.1.0 | PRIVATE |

## Deploy URLs
- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- Production backend: cfc-backend-b83s.onrender.com
- Frontend: Vercel (both sandbox and production)
