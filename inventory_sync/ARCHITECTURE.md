# inventory_sync — ARCHITECTURE

**Status:** shell only. No logic, no routes, no DB migrations, no Playwright install.
**Scope:** intentionally isolated non-V6 tooling inside the CFC Order sandbox repo.
**Mandatory first-read** for any future session touching this module.

Any future change to thresholds, safety rules, or boundaries described here
must update this file in the same change set. See section 11.

---

## 1. Purpose

`inventory_sync` is a bounded subsystem that mirrors supplier stock state into
the CFC B2BWave catalog so that out-of-stock items are hidden and in-stock
items are restored. It exists so the website does not sell what suppliers
cannot ship.

### What this module does
- Scrape supplier inventory state from supported suppliers on a schedule.
- Diff the new scrape against the previous snapshot for the same supplier.
- Push only the difference to B2BWave.
- Maintain a per-supplier snapshot history as the only valid record of
  supplier stock state.
- Alert on repeated failure.
- Support a manual ignore list that excludes specific SKUs from any
  automatic state change.
- Support a dry-run mode that scrapes and diffs without pushing.

### What this module does NOT do
- **Not a pricing engine.** Pricing stays in the existing order flow.
- **Not a part of order-flow business logic.** It does not read, write,
  or alter orders, shipments, quotes, webhooks, or customer emails.
- **Not a source of truth for product catalog metadata.** It writes stock
  state only (available / out of stock). It does not touch SKU descriptions,
  images, categories, dimensions, or prices.
- **Not cross-supplier aggregation.** Each supplier snapshot is independent
  unless a future overlap rule is explicitly implemented and documented
  here first.
- **Not an ordering system.** It never creates, modifies, or cancels orders
  or shipments in any system.

---

## 2. Component Map

One-line responsibility for each file:

| File | Responsibility |
|---|---|
| `inventory_sync/__init__.py` | Package marker. No exports beyond what is explicitly listed here. |
| `inventory_sync/ARCHITECTURE.md` | This file. The locked, mandatory-first-read contract for the module. |
| `inventory_sync/engine.py` | The **only** orchestrator. Calls scrapers, diff, snapshot, push, alert in the documented order. |
| `inventory_sync/scrapers/__init__.py` | Scraper subpackage marker. No cross-scraper imports. |
| `inventory_sync/scrapers/base.py` | Abstract scraper interface. Defines the contract every supplier scraper must implement. |
| `inventory_sync/scrapers/lm.py` | Love-Milestone supplier scraper. |
| `inventory_sync/scrapers/dl.py` | DL Cabinetry supplier scraper. |
| `inventory_sync/scrapers/roc.py` | ROC Cabinetry supplier scraper. |
| `inventory_sync/b2bwave_push.py` | The **only** module allowed to write stock state to B2BWave. |
| `inventory_sync/diff.py` | Computes the diff between a new scrape result and the prior snapshot. Pure function; no I/O. |
| `inventory_sync/snapshot.py` | Reads and writes per-supplier scrape snapshots. The only record of supplier stock state. |
| `inventory_sync/alert.py` | Builds and sends failure alert emails through the existing Gmail send path. |
| `inventory_sync/ignore_list.py` | Loads the manual override / ignore list and exposes membership checks. |
| `inventory_sync/dry_run.py` | Runs the full scrape + diff path without invoking `b2bwave_push`. |

---

## 3. Data Flow (intended, not yet implemented)

This is the intended architecture. No code in this module performs any of
these steps today.

```
  [scheduled cron trigger]
            │
            ▼
  engine.run_once()
            │
            ▼
  scrapers.lm / dl / roc  (each returns a ScrapeResult)
            │
            ▼
  coverage + abnormal-change validation (engine.py)
            │
            ▼
  snapshot.load_previous(supplier)        ← prior state
            │
            ▼
  diff.compute(previous, current)         ← pure diff
            │
            ▼
  ignore_list.filter(diff)                ← manual overrides
            │
            ▼
  (dry-run mode branches out here: print/log only, return)
            │
            ▼
  b2bwave_push.apply(diff)                ← only writer
            │
            ▼
  snapshot.save(supplier, current)        ← only on push success
            │
            ▼
  alert.maybe_send(...)                   ← on repeated failure
```

Any deviation from this sequence is a drift bug and must be corrected or
the diagram must be updated in the same change set (see section 11).

---

## 4. Source-of-Truth Rules

1. **Supplier scrape snapshots are the only valid record of supplier stock
   state.** Not the B2BWave catalog. Not order flow state. Not cached
   in-memory globals.
2. **B2BWave is the push target, not the source of truth.** The module
   writes stock state INTO B2BWave; it never reads stock state FROM B2BWave
   to make decisions.
3. **Each supplier snapshot is independent.** A SKU that exists at more
   than one supplier is NOT automatically correlated. Cross-supplier
   overlap rules do not exist until they are explicitly implemented and
   documented here first. Until then, each supplier's stock state is
   tracked and pushed in isolation.
4. **The snapshot is authoritative for history.** Any recovery, rollback,
   or audit starts from the snapshot store.
5. **No module outside `inventory_sync` reads or writes snapshots.**

---

## 5. Failure Rules

1. **Partial scrape coverage must not trigger push.** If a scraper returns
   fewer SKUs than the configured coverage threshold for that supplier,
   the run is aborted for that supplier. No push, no snapshot save.
2. **Push failure must not mutate snapshot.** The snapshot is only saved
   after `b2bwave_push.apply` reports success for that supplier. A partial
   push failure leaves the prior snapshot intact so the next run re-attempts
   the same diff.
3. **Supplier scrape failure preserves prior state.** A supplier scraper
   that raises or returns an explicit failure marker does NOT mark its
   SKUs out of stock. The prior snapshot is kept and the next run retries.
4. **Two consecutive day-level failures trigger an email alert.** Repeated
   scrape or push failure for the same supplier across two consecutive
   scheduled runs triggers `alert.send` through the existing Gmail send
   path. A single transient failure does not alert.
5. **Failure in one supplier never cascades to another.** Suppliers are
   processed independently. `engine.run_once` catches and isolates
   per-supplier exceptions.

---

## 6. Safety Invariants

These are non-negotiable. They must hold at all times.

- **Never mark a SKU out of stock from a single supplier failure.** A failed
  scrape is not a "zero SKUs in stock" signal.
- **Never mass-mark SKUs out of stock when scrape coverage is abnormal.**
  If the fraction of scraped rows drops below the coverage threshold, the
  run aborts for that supplier before any push.
- **Default state on ambiguity or failure is leave products available /
  in stock.** Silence from a scraper is not evidence of absence.
- **Only push diffs.** The module never sends a full catalog state as a
  push. It only pushes the SKU-level changes between the prior snapshot
  and the new scrape.
- **B2BWave writes are confined to `b2bwave_push.py`.** No other file in
  this module, and no file outside this module, writes stock state to
  B2BWave.
- **Scraper modules do not import from each other.** Each scraper is
  isolated. Any shared helper must live in `scrapers/base.py` or in a
  future dedicated helper module documented here.
- **`engine.py` is the only orchestrator.** No other module sequences
  scrape → diff → push.

---

## 7. Validation Gates

Three gates protect against bad runs. All three must pass before any push
to B2BWave for a given supplier.

1. **Coverage threshold.** The scrape must return at least the configured
   minimum fraction of SKUs for that supplier. Below-threshold results
   abort the run for that supplier with no push and no snapshot save.
2. **Abnormal change threshold.** If the computed diff would flip more
   than the configured maximum fraction of SKUs (for example, 25% of all
   known SKUs going out of stock in a single run), the run aborts for
   that supplier and emits a cautious alert. This guards against silent
   supplier-site outages that return empty inventory tables.
3. **Dry-run-before-live.** A supplier scraper is not allowed to transition
   from development to live push until it has produced a clean dry-run
   result that matches expectations. See section 9.

---

## 8. Ignore List Format

The module must support a manual override / ignore list so a human can
exclude specific SKUs from any automatic stock-state change.

- The ignore list is read every run by `ignore_list.load()`.
- Entries on the list are filtered out of the diff BEFORE `b2bwave_push`
  sees it.
- The format, storage location, and entry semantics are not yet chosen;
  they are an implementation-step-2 decision. The only requirement right
  now is that every downstream module that acts on the diff does so
  through `ignore_list.filter(diff)`, not by inspecting raw scrape output.
- Any SKU on the ignore list is always left in its current B2BWave state
  regardless of scrape results.

---

## 9. Dry-Run Protocol

`dry_run.py` runs the full scrape + diff path without live push.

- Executes every configured scraper.
- Runs diff, validation gates, and ignore-list filtering.
- Logs what a live run WOULD push, including per-supplier counts, diffed
  SKUs, and which gate (if any) would have aborted the run.
- Never calls `b2bwave_push.apply`.
- Never writes a new snapshot (dry-runs do not mutate stored state).
- Must be run successfully before the first live push for a new supplier
  or after any scraper behavior change.

---

## 10. Env Vars Required

Placeholder only. No environment variable is added or read in this step.
This section will be populated when scraper, push, and alert logic are
implemented.

- (supplier credentials — TBD per scraper)
- (B2BWave push credentials — reuse existing `config.py`)
- (Gmail alert path — reuse existing Gmail send path)
- (coverage threshold — TBD)
- (abnormal change threshold — TBD)
- (dry-run flag — TBD)

---

## 11. Drift-Prevention Rules

These rules keep this module from drifting into the order flow or
silently breaking its own invariants.

1. **This file is the mandatory first-read** for any future session
   touching `inventory_sync/`. A session that does not read this file
   must not modify any file in this module.
2. **Any change to thresholds, failure rules, safety invariants, the
   component map, the data flow diagram, or the source-of-truth rules
   MUST update this file in the same change set.** A code change that
   contradicts this document is a drift bug and must be reverted or
   followed by an immediate doc update.
3. **`inventory_sync` stays isolated** from order-flow logic. It may
   import from `config.py` for shared env/credentials and from the
   existing Gmail send path for alert delivery. It may (later) import
   shared DB connection helpers. Anything beyond that is out of scope
   for this module and must be justified in this file before it happens.
4. **Scraper modules may not import from each other.** Any shared
   scraper helper lives in `scrapers/base.py`.
5. **`b2bwave_push.py` is the only future module allowed to write stock
   state to B2BWave.** No other file in this module, and no file outside
   this module, may do so.
6. **`engine.py` is the only orchestrator.** Cron wiring and route
   wiring (when added) call into `engine.run_once` (or an equivalent
   single entry point) and nothing else.
7. **Placeholder: future run-lock / no-overlap execution rule.** When
   implemented, only one `engine.run_once` may be in-flight at a time.
   Concurrent invocations must be short-circuited. Design and semantics
   TBD.
8. **Placeholder: future SKU normalization layer.** When implemented, a
   normalization layer will canonicalize supplier SKU strings before
   diff is computed. Until it exists, scraper output is diffed as-is
   and any supplier-side SKU renames are manually captured in the
   ignore list.
9. **Placeholder: future abnormal-change cap.** In addition to the
   abnormal change threshold (section 7), a hard cap on absolute SKU
   state changes per run may be added. Design TBD.

---

## 12. Deployment Notes

- **Playwright on Render is unresolved.** Supplier scrapers likely need
  headless browser automation (Playwright or equivalent). Running
  Playwright reliably on Render's build and runtime environment is an
  open question and must be solved before any scraper implementation
  beyond the abstract `scrapers/base.py` interface.
- This module is not wired into any runtime, route, or cron at this
  step. Shell files exist; no code runs.
- Cron entry point and route wiring are a separate, later step, not
  part of shell creation.
- DB tables, migrations, and schema choices are a separate, later step,
  not part of shell creation.
- `requirements.txt` changes, Dockerfile changes, and Render service
  changes are a separate, later step, not part of shell creation.
