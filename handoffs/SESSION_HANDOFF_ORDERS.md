# SESSION HANDOFF — CFC Orders (General)

**Last Updated:** 2026-03-02 (Session 7B — AI Config Panel + Sandbox Link)
**Last Session:** Mar 2, 2026 — AI Config Panel (Connie's NLP UI customizer) + sandbox link for production
**Session Before That:** Mar 2, 2026 — Phase 3B lifecycle wiring + bug documentation

---

## WHAT HAPPENED THIS SESSION (Mar 2 — Session 7B)

### Feature 1: AI Config Panel (Sandbox Frontend)
Built a floating AI-powered configuration panel for the sandbox frontend. Connie can type natural language commands like "make awaiting payment pink" and Claude interprets them into live UI changes.

**Backend — 2 new files committed to cfc-orders:**
| File | Purpose |
|------|---------|
| `ai_configure.py` | FastAPI router: `POST /ai/configure` + `GET /ai/ui-schema` |
| `ai_configure_wiring.py` | Mount helper (2-line wire into main.py) |

How it works:
1. Frontend sends `{ prompt: "make awaiting payment pink" }` to `POST /ai/configure`
2. Backend sends prompt to Claude Sonnet with system prompt describing the UI schema
3. Claude returns structured JSON: `{ statusColors: { awaiting_payment: "#ff69b4" }, ... }`
4. Frontend applies changes live via injected CSS

Supported config changes:
- `statusColors` — change any status badge/button color
- `statusLabels` — rename status labels
- `theme` — "light" or "dark"
- `headerColor` — header background color
- `fontSize` — base font size
- `accentColor` — primary accent color
- `customCSS` — raw CSS for advanced tweaks

**Frontend — 2 files committed to cfc-orders-frontend:**
| File | Purpose |
|------|---------|
| `src/components/AiConfigPanel.jsx` | Floating 🤖 button → expandable chat panel |
| `src/App.jsx` | Updated to v5.10.0 — wires panel, dynamic CSS injection |

App.jsx changes:
- Added `useMemo` import
- Added `AiConfigPanel` import
- `BASE_STATUS_MAP` const (was `STATUS_MAP`) — labels now overridable
- `STATUS_MAP` is now a `useMemo` that merges base + AI label overrides
- `dynamicCSS` useMemo generates CSS from aiConfig state
- `handleConfigChange` merges incoming changes into aiConfig state
- `<style>{dynamicCSS}</style>` injected into render
- `<AiConfigPanel>` rendered at bottom of app
- Header now shows "SANDBOX" badge

### Feature 2: Sandbox Link for Production Frontend
**NOT YET APPLIED** — production frontend repo not in MCP aliases.
Code changes ready — see wiring instructions below.

---

## WIRING INSTRUCTIONS FOR WILLIAM

### Step 1: Wire AI Configure into sandbox backend main.py (2 lines)

Add after the lifecycle/alerts router mount (around line 175):

```python
# Session 7: AI Config Panel
from ai_configure_wiring import wire_ai_configure
wire_ai_configure(app)
```

### Step 2: Wire lifecycle into main.py (2 lines — from Session 7)

Add these 2 lines AFTER the alerts router mount:

```python
# Phase 3B: Lifecycle Engine wiring
from lifecycle_wiring import wire_lifecycle
wire_lifecycle(app)
```

### Step 3: Fix freight class bug (3 locations in main.py)

Line 598: Change `freight_class: str = "70"` → `freight_class: str = "85"`
Line 675: Change `freight_class: str = "70"` → `freight_class: str = "85"` 
Line 1079: Change `freight_class="70",` → `freight_class="85",`

### Step 4: Add lifecycle to root endpoint (main.py root() function)

After the `"alerts_engine"` dict, add:
```python
        "lifecycle_engine": {
            "enabled": True
        },
        "ai_configure": {
            "enabled": True
        }
```

### Step 5: Git push sandbox backend

```
cd C:\dev\CFCOrderBackend_Sandbox
git add -A
git commit -m "Session 7: Wire AI config + lifecycle + fix freight class"
git push
```

### Step 6: Add sandbox link to PRODUCTION frontend

Edit `C:\dev\CFCOrdersFrontend\src\App.jsx`.

In the header section, add a sandbox link button in `header-actions`:

```jsx
<div className="header-actions">
  <a
    href="https://cfcordersfrontend-sandbox.vercel.app"
    target="_blank"
    rel="noopener noreferrer"
    title="Open Sandbox"
    style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: '4px',
      padding: '6px 12px',
      backgroundColor: '#ff9800',
      color: '#fff',
      borderRadius: '6px',
      textDecoration: 'none',
      fontSize: '12px',
      fontWeight: 600,
    }}
  >
    🧪 Sandbox
  </a>
  <button onClick={loadOrders} disabled={loading}>
    {loading ? 'Loading...' : 'Refresh'}
  </button>
  <button onClick={handleLogout}>Logout</button>
</div>
```

Then push:
```
cd C:\dev\CFCOrdersFrontend
git add -A
git commit -m "Add sandbox link to production header"
git push
```

### Step 7: Run DB migration (after sandbox backend deploy)

```
POST https://cfcorderbackend-sandbox.onrender.com/add-lifecycle-fields
POST https://cfcorderbackend-sandbox.onrender.com/backfill-lifecycle
```

---

## BLOCKER STATUS

| # | Blocker | Status |
|---|---------|--------|
| 1 | rl-quote-sandbox private | ✅ RESOLVED |
| 2 | Render services dead | ✅ RESOLVED |
| 3 | PostgreSQL expired | ✅ RESOLVED |
| 4 | Hardcoded API key | ✅ RESOLVED |
| 5 | Frontend junk in repo | OPEN — needs William local git rm |
| 6 | Warehouse data wrong | OPEN — fix models.py (6 warehouses) |
| 7 | Duplicate endpoint | OPEN — merge POST /rl/pickup/pro |
| 8 | Freight class bug | **DOCUMENTED** — 3 lines in main.py |
| 9 | No authentication | OPEN — Phase 5 |
| 10 | Lifecycle not wired | **DOCUMENTED** — 2 lines in main.py |
| 11 | AI config not wired | **DOCUMENTED** — 2 lines in main.py |

## BATTLE PLAN STATUS

| Phase | Focus | Status |
|-------|-------|--------|
| 1 | Cleanup & Hygiene | ✅ DONE |
| 2 | RL-Quote Integration | ✅ DONE |
| Audit | Full-stack audit + UI mockup | ✅ DONE |
| 3A | AlertsEngine | ✅ DONE — wired in main.py |
| 3B | Order Lifecycle | ✅ CODE COMPLETE — needs main.py wiring (2 lines) |
| — | AI Config Panel | ✅ CODE COMPLETE — needs main.py wiring (2 lines) |
| 4 | Customer Communications | **NEXT** |
| 5 | Backend Hardening | NOT STARTED |
| 6 | Frontend Redesign | NOT STARTED |
| 7 | Production Promotion | NOT STARTED |

## NEXT SESSION SHOULD

1. **Wire everything** — Run Steps 1-7 above (William local, ~10 min)
2. **Test AI Config Panel** — Open sandbox frontend, click 🤖 button, try:
   - "make awaiting payment pink"
   - "switch to dark mode"
   - "make the font bigger"
   - "rename Need BOL to Get BOL"
3. **Test lifecycle endpoints** — POST /lifecycle/check-all, GET /lifecycle/summary
4. **Start Phase 4** — Customer Communications (email templates, lifecycle emails)
5. **Consider**: Add production frontend to MCP aliases for future sessions

## KEY REFERENCE FILES

- **Battle plan**: cfc-orders:handoffs/CFC_ORDERS_BATTLE_PLAN.md
- **Rules**: brain:WILLIAM_BRAIN/ORDERS_BRAIN/rules.md (v1.2)
- **AI configure**: cfc-orders:ai_configure.py + ai_configure_wiring.py
- **AI config panel**: cfc-orders-frontend:src/components/AiConfigPanel.jsx
- **Lifecycle engine**: cfc-orders:lifecycle_engine.py
- **Lifecycle wiring**: cfc-orders:lifecycle_wiring.py

## REPOS

- Sandbox backend: github.com/4wprince/CFCOrderBackend_Sandbox (v6.0.0)
- Sandbox frontend: github.com/4wprince/CFCOrdersFrontend_Sandbox (v5.10.0)
- Prod frontend: github.com/4wprince/CFCOrdersFrontend (NOT in MCP)
- RL quote sandbox: github.com/4wprince/rl-quote-sandbox (MCP alias: `rl-quote`)

## DEPLOY URLS

- Sandbox backend: cfcorderbackend-sandbox.onrender.com
- RL-quote sandbox: rl-quote-sandbox.onrender.com
- Sandbox frontend: cfcordersfrontend-sandbox.vercel.app
- Prod frontend: cfc-orders-frontend.vercel.app

## LOCAL REPOS

- `C:\dev\CFCOrderBackend_Sandbox` — backend
- `C:\dev\CFCOrdersFrontend_Sandbox` — sandbox frontend
- `C:\dev\CFCOrdersFrontend` — production frontend
