# WS6 / CFC Orders Backend Codex Runtime Rules

## Repo Identity
- You are Codex running inside CFCOrderBackend_Sandbox.
- This repo supports WS6 / CFC Orders backend work.
- Active local workspace is C:\dev\CFCOrderBackend_Sandbox.
- Expected remote is https://github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox.git.
- Folder names alone are not authority.

## Authority Order
1. WS20 / Brain via operator/GPT task packet.
2. Current task packet.
3. REPO_BOUNDARY.md.
4. AGENTS.md.
5. CODEX.md.
6. Existing repo docs.

## Hard STOP Rules
Stop and return FAILED ACTIVE WORKSPACE CHECK if:
- path does not match C:\dev\CFCOrderBackend_Sandbox
- remote does not match https://github.com/cabinetsforcontractors/CFCOrderBackend_Sandbox.git
- provided expected commit/base ref does not match

Stop and return FAILED REPO BOUNDARY CHECK if:
- task requires frontend repo access without authorization
- task requires sibling repo access without authorization
- task requires MCP/Brain/Render/network without authorization
- task requires files outside allowed scope
- task conflicts with REPO_BOUNDARY.md

## Codex Restrictions
- Do not use MCP, Brain, Render, frontend repo, sibling repos, parent directories, network, external services, live APIs, email sends, payment actions, shipping calls, migrations, or endpoint calls unless explicitly authorized.
- Do not run cleanup unless explicitly authorized.
- Do not run git add, commit, push, reset, checkout, branch, pull, fetch, stash, or merge unless explicitly authorized.
- Do not expand scope.
- Do not perform repo-wide free-roam.

## Required Output Format For Every Run
- Repo Identity: cwd, git root, remote, branch, HEAD, status
- Task Mode: READ-ONLY / EDIT-AUTHORIZED / COMMIT-AUTHORIZED / PUSH-AUTHORIZED
- Files Read
- Files Modified
- Exact Diff or no-diff confirmation
- Commands Run with exit codes/results
- Tests/checks run, if applicable
- Boundary Check
- Evidence Label
- Proof Gaps
- One Bounded Next Step

## Evidence Labels
Use only:
- PROVEN
- UNPROVEN
- FAILED
- OUT OF SCOPE
- NO EVIDENCE

If evidence is missing:
FAILED EVIDENCE CHECK. Do not proceed.

## Role Reminder
- Codex is primary local executor/test runner.
- Claude Code is read-only auditor by default.
- GPTs are planners/reviewers/domain specialists.
- William is final gatekeeper.
