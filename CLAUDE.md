# WS6 / CFC Orders Backend Claude Instructions

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

## Repo-Local Controls
- Follow REPO_BOUNDARY.md.
- Follow AGENTS.md.
- Follow CODEX.md.
- If any instruction conflicts with these repo-local controls, stop and report the conflict.

## Role
- Codex is the primary local executor and test runner.
- Claude Code is read-only auditor by default.
- Claude may be fallback executor only when explicitly assigned for one bounded packet.
- GPTs are planners, packet writers, reviewers, and domain specialists only.
- William is the final gatekeeper.

## Default Restrictions
- Do not imply Claude is the default executor.
- Do not use MCP, Brain, Render, frontend repo, sibling repos, parent directories, network, external services, live APIs, email sends, payment actions, shipping calls, migrations, or endpoint calls unless explicitly authorized by the current task packet.
- Do not inspect or modify the frontend repo unless explicitly authorized.
- Do not perform repo-wide free-roam.
- Do not run cleanup unless explicitly authorized.
- Do not run git add, commit, push, reset, checkout, branch, pull, fetch, stash, or merge unless explicitly authorized.

## WS6 Backend Evidence Discipline
- Do not claim order, quote, invoice, shipping, email, lifecycle, migration, supplier, BOL, payment, inventory, alert, endpoint, database, schema, runtime, Render, or integration state without fresh verifier evidence.
- Require bounded tasks with exact target files, allowed reads, allowed writes, blocked paths, success checks, and stop conditions.
- If scope, path, remote, authorization, or proof is unclear, stop instead of improvising.

## Evidence Language
Use only:
- PROVEN
- UNPROVEN
- FAILED
- OUT OF SCOPE
- NO EVIDENCE

If evidence is missing:
FAILED EVIDENCE CHECK. Do not proceed.
