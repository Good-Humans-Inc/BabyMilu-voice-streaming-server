# Smoke Harness TDD

## Goal

Create one shared smoke-testing system for BabyMilu that:

- validates runtime behavior end to end
- is reusable by humans and Codex
- works against deployed environments and local branch-under-test environments
- leaves artifacts and cleanup state in a predictable way

## Framework Shape

The harness is built around four layers:

1. scenario runner
2. data adapter
3. device simulator
4. assertion layer

## Environment Model

The harness supports two independent axes:

### `environment_type`

- `cloud`
- `local-compose`
- `external-dev`

### `data_mode`

- `live-shape`
- `isolated`

## Current Implemented Behavior

### Scenarios

- `scheduled.reminder`
- `scheduled.alarm`

### Trigger support

- `cloud`
  - HTTP scheduler trigger
- `local-compose`
  - direct Python entrypoint trigger
  - `docker compose exec` trigger
- `external-dev`
  - HTTP trigger
  - manual trigger

### Data support

- `live-shape`
  - real Firestore shape with dedicated test users
- `isolated`
  - reserved for emulator or local fixture-backed workflows

## Local-Compose Decision

The first branch-under-test implementation uses:

1. `entrypoint`
   - fastest path
   - runs the checked-out branch directly
2. `docker-exec`
   - fallback when the runtime only works inside compose

This keeps the same scenarios reusable across deployed staging and local PR validation.

## Pass Criteria

The smoke environment is considered usable when:

1. preflight reports the real runnable state clearly
2. the harness can create app-consistent scheduled docs
3. the harness can trigger the scheduler in the chosen environment
4. the harness can observe plushie websocket/audio behavior when relevant
5. the harness can assert final Firestore state
6. the harness can clean up temporary docs automatically

## Codex Contract

Codex should:

1. choose `environment_type` and `data_mode`
2. run preflight first
3. stop if preflight has real failures
4. use the shared harness instead of ad hoc smoke scripts
5. report artifacts and cleanup state
6. update docs and TDDs when behavior changes

## Next Additions

- `interaction.first_response`
- `memory.write_then_recall`
- backend API adapters
- emulator-backed `isolated` fixtures
- PR review runbook for coworker branch validation
