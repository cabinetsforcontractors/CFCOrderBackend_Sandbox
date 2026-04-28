# WS6 / CFC Orders Backend Repo Boundary

## Repo Identity
- This repo is CFCOrderBackend_Sandbox.
- This repo supports WS6 / CFC Orders backend work.
- Active local workspace: C:\dev\CFCOrderBackend_Sandbox
- Expected remote: https://github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox.git
- Folder names alone are not authority. Authority is determined by the task packet, git remote, expected path, and any expected commit or base ref supplied by the current task packet.

## Authority Model
- WS20 / Brain is the governance authority and evidence layer.
- WS6 / CFC Orders backend is subordinate to WS20 / Brain.
- WS20 authority is not mirrored here.
- WS20 directives reach this repo through operator/GPT task packets.

## Execution Roles
- Codex is the primary local executor and test runner.
- Claude Code is read-only auditor by default.
- Claude may be fallback executor only when explicitly assigned for one bounded packet.
- GPTs are planners, packet writers, reviewers, and domain specialists only.

## Repo Identity Check
Before any task, Codex must report:
- current working directory
- git repo root
- git remote
- branch
- current HEAD
- git status

STOP if:
- current working directory is not C:\dev\CFCOrderBackend_Sandbox
- git remote is not https://github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox.git
- any provided expected commit/base ref does not match
- task scope conflicts with this boundary file

Return FAILED ACTIVE WORKSPACE CHECK or FAILED REPO BOUNDARY CHECK as appropriate.

## Read Scope
Allowed by default:
- files inside this repo only
- files explicitly named by the current task packet

Forbidden unless explicitly authorized:
- sibling repos
- frontend repo
- Brain repo
- MCP bridge calls
- live Render endpoint calls
- external service/API calls
- parent directory scanning
- network access

## Write Scope
Allowed only when the current task packet explicitly authorizes edits.

If edits are authorized:
- write only inside this repo
- modify only files explicitly allowed by the current task packet
- do not create scratch/helper/temp files unless explicitly allowed
- do not run cleanup unless explicitly authorized
- do not run git add, commit, push, reset, checkout, branch, pull, fetch, stash, or merge unless explicitly authorized

## Locked Files
These files are locked unless the task packet explicitly says BOUNDARY MAINTENANCE:
- AGENTS.md
- CODEX.md
- REPO_BOUNDARY.md
- CLAUDE.md

## WS6 Backend Domain Guardrails
- Do not claim order, quote, invoice, shipping, email, lifecycle, migration, supplier, BOL, payment, inventory, alert, endpoint, database, schema, runtime, Render, or integration state without fresh verifier evidence.
- Backend and frontend are separate repos; do not inspect or modify the frontend repo unless explicitly authorized.
- Do not perform repo-wide free-roam.
- Require bounded tasks with exact target files, allowed reads, allowed writes, blocked paths, success checks, and stop conditions.
- Do not run live API calls, email sends, payment actions, shipping calls, migrations, or endpoint calls unless explicitly authorized.

## Evidence Rule
Every claim must be backed by:
- file path
- command output
- exact diff
- exit code/result
- test/check result
- hash
- commit history
- explicit user approval

or labeled UNPROVEN / NO EVIDENCE.

Use only:
- PROVEN
- UNPROVEN
- FAILED
- OUT OF SCOPE
- NO EVIDENCE

If evidence is missing:
FAILED EVIDENCE CHECK. Do not proceed.
