# E-Commerce Platform Build — Session Handoff
**Last Updated:** 2026-02-28
**Workstream:** E-Commerce Website
**Status:** Not Started — spec exists (BOOTSTRAP_PLATFORM_SPEC.md planned)

## What This Lane Covers
The Design-to-Doorstep bootstrap e-commerce platform. BOM-to-cart conversion from CabinetCloudAI designs, storefront for direct cabinet sales, checkout flow, and the eventual multi-seller SaaS vision. This is the consumer-facing sales channel that complements the B2B wholesale (CFC) business.

## Current State
- **Concept defined** — "Design-to-Doorstep" vision
- **BOOTSTRAP_PLATFORM_SPEC.md** referenced in manifest but not yet created
- **No code exists** — this is greenfield development
- **CFC pricing infrastructure** is the foundation — MSRP = COGS / 0.38
- **CabinetCloudAI engine** provides BOM output that would feed the cart

## Key Files
- `brain:BOOTSTRAP_PLATFORM_SPEC.md` — TO BE CREATED. Full 6-phase plan for platform build.
- `v5:engine/solve_kitchen_v5.py` — Produces BOM output (upstream of cart)
- `brain:WILLIAM_BRAIN/CFC_BRAIN/rules.md` — Pricing rules that feed product pricing ⚠️ pending migration from WILLIAM_BRAIN

## Active Bugs / Blockers
- BOOTSTRAP_PLATFORM_SPEC.md not yet written
- No technology stack decision made (Shopify? Custom? Headless CMS?)
- No payment processor selected for consumer sales (CFC uses Square for B2B)
- BOM-to-cart conversion logic not designed
- CFC tier rebuild must complete before pricing can flow to consumer storefront

## Next Steps
1. Write BOOTSTRAP_PLATFORM_SPEC.md with full 6-phase plan
2. Decide technology stack for storefront
3. Design BOM-to-cart conversion (engine output → shopping cart items)
4. Decide: separate brand from CFC, or extension of CFC?
5. Design multi-seller SaaS architecture (long-term vision)

## Rules & Decisions
- Design-to-Doorstep is the brand concept — design your kitchen, buy the cabinets, get them delivered
- Must maintain clear separation between wholesale (CFC/B2B) and retail (consumer) pricing
- CFC pricing integrity must be protected — consumer prices derived from MSRP, never expose COGS
- BOM output from CabinetCloudAI engine is the bridge between design tool and e-commerce
- Multi-seller SaaS is the long-term vision — start with single-seller (William's cabinets) first
