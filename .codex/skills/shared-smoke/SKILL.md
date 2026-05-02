---
name: "shared-smoke"
description: "Use when work touches reminder, alarm, websocket delivery, or other server-owned behavior that should go through the shared smoke harness in tools/smoke."
---

# Shared Smoke Skill

## When to use

- reminder or alarm scheduling changes
- websocket delivery behavior changes
- plushie-facing playback changes
- scheduler trigger behavior changes
- behavior that should be validated through `tools/smoke/`

## First read

- `tools/smoke/README.md`
- `tools/smoke/CODEX_WORKFLOW.md`
- `docs/testing/smoke-harness.md`

## Required behavior

1. Run `python3 tools/smoke/run.py preflight --env <name>` first.
2. Stop if preflight has real failures.
3. Run an existing shared smoke scenario when one matches the changed behavior.
4. If no scenario exists yet, say that explicitly and either:
   - run the legacy harness that currently covers the behavior, or
   - extend `tools/smoke/` instead of writing another standalone test path.
5. Report artifacts, cleanup state, and blockers.

## Current shared scenarios

- `scheduled.reminder`
- `scheduled.alarm`

Inspect:

```bash
python3 tools/smoke/run.py list-scenarios --env staging
```

## Current gap: next_starter latency flow

The validated `next_starter` latency harness is not yet migrated into `tools/smoke/`.

For now, the known regression path is the legacy e2e script in `memory-worker`:

- `memory-worker/scripts/run_next_starter_e2e.py`
- `memory-worker/docs/next_starter_e2e_harness.md`
- `tools/smoke/run_next_starter_latency.py`

Use that path when:

- `next_starter` generation changes
- `listen start` starter playback changes
- character-scoped starter consume behavior changes

But treat it as a migration target for the shared harness, not the final long-term shape.

## Do Not

- Do not skip preflight.
- Do not claim a shared smoke setup is complete when env config or auth is still missing.
- Do not add another ad hoc reminder/alarm smoke script if a shared scenario can be extended.
- Do not push VM-side edits instead of syncing from local source of truth.
