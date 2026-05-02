# BabyMilu Voice Server AGENTS

This file is the repo table of contents. Keep it short. Put detailed guidance in repo docs, TDDs, and runbooks.

## Start Here

1. Read `tools/smoke/README.md` and `tools/smoke/CODEX_WORKFLOW.md` before live smoke work.
2. Run `python3 tools/smoke/run.py preflight --env <name>` before any shared smoke scenario.
3. Stop if preflight fails.
4. Use the smallest deterministic check before live smoke.

## Core Runbooks

- Shared smoke workflow: `tools/smoke/CODEX_WORKFLOW.md`
- Shared smoke architecture: `docs/testing/smoke-harness.md`
- Shared smoke TDD: `docs/testing/smoke-harness-tdd.md`
- Next-starter latency smoke: `docs/testing/next-starter-latency-smoke.md`
- Legacy reminder smoke notes: `docs/reminder-smoke-testing-tdd.md`
- Legacy daily task VM testing: `docs/daily-tasks-vm-testing-guide.md`
- Repo-local skill: `.codex/skills/shared-smoke/SKILL.md`

## Canonical Domains

- Scheduler and alarm/reminder runtime: `main/xiaozhi-server/services/alarms`
- Websocket runtime: `main/xiaozhi-server/core/connection.py`
- Listen-start playback path: `main/xiaozhi-server/core/handle/textHandler/listenMessageHandler.py`
- Character memory fetch path: `main/xiaozhi-server/core/utils/next_starter_client.py`
- Shared smoke harness core: `tools/smoke/harness/`
- Temporary latency wrapper: `tools/smoke/run_next_starter_latency.py`

## Default Change Workflow

1. Change code in this repo.
2. Run the smallest useful deterministic validation after each attempt.
3. If behavior changed and a shared smoke scenario exists, run shared smoke.
4. If behavior changed and no shared smoke scenario exists yet, run the legacy harness or report the exact gap.
5. Update docs, TDDs, and runbooks in the same change when behavior changes.

## Boundaries

- Do not push from a VM. Local git is the source of truth.
- Do not mutate live cloud resources or test users without explicit confirmation.
- Do not invent one-off smoke scripts when `tools/smoke/` can be extended instead.
- Do not claim end-to-end behavior from code inspection alone.
- Do not keep going after a failed preflight; fix setup first.

## Failure Rules

- Add new `DO NOT` rules here when a failure repeats.
- If context gets polluted, fork a fresh agent or thread.
- If `next_starter` or latency behavior is involved, note that the current validated harness still lives in `memory-worker` until it is migrated into `tools/smoke/`.
