# WS6 / CFC Orders Backend Agent Instructions

## Repo Identity
- This repo is CFCOrderBackend_Sandbox.
- This repo supports WS6 / CFC Orders backend work.
- Active local workspace is C:\dev\CFCOrderBackend_Sandbox.
- Expected remote is https://github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox.git.
- Folder names alone are not authority.

## Authority Model
- WS20 / Brain is the governance authority and evidence layer.
- WS6 / CFC Orders backend is subordinate to WS20 / Brain.
- WS20 authority is not mirrored here.
- WS20 directives reach this repo only through operator/GPT task packets.

## Roles
- Codex is the primary local executor and test runner.
- GPTs are planners, packet writers, reviewers, and domain specialists only.
- Claude Code is read-only auditor by default.
- Claude may be fallback executor only when explicitly assigned for one bounded packet.
- William is the final gatekeeper.

## Codex Behavior
- Confirm repo identity before work.
- Follow REPO_BOUNDARY.md literally.
- Do not expand scope beyond the current task packet.
- Stop instead of improvising when scope, path, remote, authorization, or proof is unclear.
- Do not use MCP, Brain, Render, frontend repo, sibling repos, parent directories, network, external services, or live APIs unless explicitly authorized by the current task packet.
- Do not perform repo-wide free-roam.
- Do not run cleanup unless explicitly authorized.

## WS6 Backend Domain Discipline
- Do not claim order, quote, invoice, shipping, email, lifecycle, migration, supplier, BOL, payment, inventory, alert, endpoint, database, schema, runtime, Render, or integration state without fresh verifier evidence.
- Backend and frontend are separate repos; do not inspect or modify the frontend repo unless explicitly authorized.
- Require bounded tasks with exact target files, allowed reads, allowed writes, blocked paths, success checks, and stop conditions.
- Do not run live API calls, email sends, payment actions, shipping calls, migrations, or endpoint calls unless explicitly authorized.

## Evidence Language
Use only:
- PROVEN
- UNPROVEN
- FAILED
- OUT OF SCOPE
- NO EVIDENCE

If evidence is missing, say:
FAILED EVIDENCE CHECK. Do not proceed.
