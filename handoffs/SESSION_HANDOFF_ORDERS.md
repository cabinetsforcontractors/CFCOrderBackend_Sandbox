# SESSION HANDOFF — CFC Orders (General)

**Last Updated:** 2026-03-02 (Session 5)
**Last Session:** Mar 2, 2026 — RL-Quote API testing + MCP bridge rl-quote repo added
**Session Before That:** Mar 1, 2026 — Phase 2 RL-Quote Integration COMPLETE + blink bug fix

---

## WHAT HAPPENED THIS SESSION (Mar 2 — Session 5)

### RL-Quote API Testing
1. ✅ **rl-quote-sandbox service confirmed healthy** — v0.1.0 running
2. ✅ **API pipeline validated end-to-end** — `/quote/simple` hit R+L API successfully (returned scheduled maintenance window response, proving full connectivity: sandbox → rl-quote-sandbox → R+L Carriers API)
3. ⚠️ **Warehouse data wrong** — only 1 warehouse defined (LI), and LI has ROC's address (30071/Norcross GA instead of 32148/Interlachen FL)
4. ✅ **MCP bridge v2.6 deployed** — added `rl-quote` repo alias → `4wprince/rl-quote-sandbox`. Claude can now read/write/search the rl-quote-sandbox repo directly.

### Warehouse Data Fix Needed (rl-quote-sandbox repo: models.py)
Current: Only LI defined with WRONG zip (30071)
Correct data:

| Code | Name | City | State | ZIP |
|------|------|------|-------|-----|
| LI | Li | Interlachen | FL | 32148 |
| DL | DL Cabinetry | Jacksonville | FL | 32256 |
| ROC | ROC Cabinetry | Norcross | GA | 30071 |
| GHI | GHI | Palmetto | FL | 34221 |
| LC | L&C Cabinetry | Virginia Beach | VA | 23454 |
| CS | Cabinet & Stone | Houston | TX | 77043 |

### FUTURE: Additional Warehouses to Add
These suppliers need warehouse entries added to the WarehouseCode enum + WAREHOUSES dict:
- **DuraStone** — 9815 North Fwy, Houston TX 77037
- **Love-Milestone** — 10963 Florida Crown Dr STE 100, Orlando FL 32824
- **Linda / Dealer Cabinetry** — 202 West Georgia Ave, Bremen GA 30110
- **Go Bravura** — 14200 Hollister Street Suite 200, Houston TX 77066

### FUTURE: Additional Carrier APIs
Currently only R+L Carriers is integrated. Need to add:
- **SAIA** — LTL carrier API integration
- **Daylight Transport** — LTL carrier API integration
These will follow the same proxy pattern (rl-quote-sandbox calls carrier API, CFC sandbox backend proxies to rl-quote-sandbox).

## BLOCKER STATUS (updated Mar 2)

| # | Blocker | Status |
|---|---------|--------|
| 1 | rl-quote-sandbox private | ✅ RESOLVED — MCP bridge v2.6 has `rl-quote` alias |
| 2 | Render services dead | ✅ RESOLVED — paid tier, no sleep |
| 3 | PostgreSQL expired | ✅ RESOLVED |
| 4 | Hardcoded API key | ✅ RESOLVED |
| 5 | Frontend junk in repo | OPEN — needs William local git rm commands |
| 6 | Warehouse data wrong | OPEN — fix models.py in rl-quote repo (6 warehouses + correct LI zip) |

## BATTLE PLAN STATUS

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Cleanup & Hygiene | ✅ DONE (local git cleanup remaining) |
| 2 | RL-Quote Integration | ✅ DONE — deployed and tested |
| 3 | AlertsEngine | NOT STARTED |
| 4 | Customer Communications | NOT STARTED |
| 5 | Backend Hardening | NOT STARTED |
| 6 | Frontend Polish | NOT STARTED |
| 7 | Production Promotion | NOT STARTED |

## REMAINING CLEANUP (Phase 1 leftover)

Frontend repo still has committed junk (node_modules, dist, cfc-frontend.zip):
```
cd C:\dev\CFCOrdersFrontend_Sandbox
git rm -r --cached node_modules
git rm -r --cached dist
git rm --cached cfc-frontend.zip
git commit -m "Remove committed junk: node_modules, dist, cfc-frontend.zip"
git push origin main
```

## NEXT SESSION SHOULD

1. **Fix warehouse data** — Now that rl-quote repo is in MCP, edit `backend/models.py` to add all 6 warehouses with correct zips
2. **Re-test R+L quote** — 32148→32176, class 85, 1600 lbs, business-to-business (R+L maintenance ends 5:30 AM EST)
3. Start **Phase 3 (AlertsEngine)** — 8 alert rules, business hours calculator, cron endpoint `POST /alerts/check-all`
4. Read rules from brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md for ORD-A1 spec
5. Read battle plan: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md (Phase 3 section)

## KEY REFERENCE FILES

- **Battle plan**: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md
- **Original upgrade plan**: cfc-orders:handoffs/CFC_ORDERS_PLAN.md
- **Rules**: brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- **Master status**: brain:MASTER_STATUS.md (Workstream 6)

## REPOS

- Sandbox backend: github.com/4wprince/CFCOrderBackend_Sandbox (v6.0.0)
- Sandbox frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox (v5.10.1)
- RL quote sandbox: github.com/4wprince/rl-quote-sandbox (MCP alias: `rl-quote`)
- Prod backend: github.com/4wprince/CFCOrderBackend (outdated monolithic)
- Prod frontend: github.com/4wprince/CFCOrdersFrontend (behind sandbox)

## DEPLOY URLS

- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- RL-quote sandbox: rl-quote-sandbox.onrender.com
- Sandbox frontend: cfcordersfrontend-sandbox.vercel.app
- Production frontend: cfc-orders-frontend.vercel.app

## LOCAL REPOS ON WILLIAM'S MACHINE

- `C:\dev\CFCOrderBackend_Sandbox` — backend
- `C:\dev\CFCOrdersFrontend_Sandbox` — frontend
