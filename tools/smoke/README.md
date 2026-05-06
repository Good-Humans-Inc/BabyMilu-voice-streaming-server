# BabyMilu Shared Smoke Harness

This folder holds the shared end-to-end smoke harness for BabyMilu features.

The goal is simple:

- reuse one testing workflow across the company
- avoid one-off local scripts per feature
- let humans and Codex run the same scenarios
- capture artifacts that make failures debuggable

## What It Covers Today

The harness currently ships with staged scenarios for:

- `scheduled.reminder`
- `scheduled.alarm`
- `interaction.magic_camera_photo`

Those scenarios already exercise the four core layers:

1. `scenario runner`
2. `data adapter`
3. `device simulator`
4. `assertion layer`

The framework is intentionally set up so we can add:

- memory write + recall smokes
- general websocket/LLM interaction probes
- backend API flows
- firmware compatibility checks

The harness now supports two orthogonal configuration axes:

- `environment_type`
  - `cloud`
  - `local-compose`
  - `external-dev`
- `data_mode`
  - `live-shape`
  - `isolated`

Codex-specific operator steps live in:

- `tools/smoke/CODEX_WORKFLOW.md`

## Directory Layout

```text
tools/smoke/
  run.py                  # entrypoint
  README.md               # teammate + Codex usage
  environments/           # committed env configs and local overrides
  harness/                # reusable framework code
```

## Teammate Quick Start

1. Pick the environment type and data mode you need.
2. Authenticate GCP if the environment uses `live-shape`.
3. Run the preflight before any live scenario.
3. Run a named scenario.
4. Review the artifacts in `tools/smoke/artifacts/`.

## Environment Types

### `cloud`

Use when validating deployed staging-style code.

- scheduler trigger usually goes through a Cloud Function URL
- MQTT and websocket point at a deployed VM or service
- best for rollout verification

### `local-compose`

Use when validating a branch under test locally.

- scheduler trigger can be:
  - direct Python entrypoint execution against the checked-out branch
  - `docker compose exec` against a running service
- MQTT and websocket usually point at localhost
- best for PR verification before merge

### `external-dev`

Use when a teammate already has a custom dev server running.

- scheduler trigger can be HTTP or manual
- MQTT and websocket point at that external dev environment
- best when we want real Firestore shape against a non-staging runtime

## Data Modes

### `live-shape`

Use real Firestore schema and dedicated test users.

- needs ADC and Firestore access
- best for catching real schema drift

### `isolated`

Use a local or emulator-backed dataset.

- safer and faster for iteration
- best when live cloud access is unavailable or undesirable

### Required Auth

This harness assumes the operator has:

- `gcloud` installed
- permission to run `gcloud auth print-access-token` for `cloud` or `live-shape` runs
- Firestore access to project `composed-augury-469200-g6` for `live-shape` runs
- permission to invoke scheduler targets when the environment trigger is HTTP

Recommended setup:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project composed-augury-469200-g6
```

### Preflight

Always run this first:

```bash
python3 tools/smoke/run.py preflight --env staging
```

If preflight fails, stop there and fix auth/tooling first.

### List Scenarios

```bash
python3 tools/smoke/run.py list-scenarios --env staging
```

### Example: Reminder Smoke

```bash
python3 tools/smoke/run.py run \
  --env staging \
  --scenario scheduled.reminder \
  --uid +11551551551 \
  --device-id 90:e5:b1:d6:f8:58 \
  --channel both \
  --repeat weekly \
  --label "shared smoke reminder"
```

### Example: Alarm Smoke

```bash
python3 tools/smoke/run.py run \
  --env staging \
  --scenario scheduled.alarm \
  --uid +11551551551 \
  --device-id 90:e5:b1:d6:f8:58 \
  --repeat weekly \
  --label "shared smoke alarm"
```

### Example: Magic Camera Smoke

```bash
python3 tools/smoke/run.py run \
  --config /Users/yan/Desktop/BabyMilu/BabyMilu-voice-streaming-server/tools/smoke/environments/staging.local.json \
  --scenario interaction.magic_camera_photo \
  --uid +11551551551 \
  --device-id 90:e5:b1:d6:fb:0c \
  --label "shared smoke magic camera"
```

## Codex Workflow

This is the workflow every teammate's Codex should follow before running live smoke tests:

1. Read this file.
2. Run:

```bash
python3 tools/smoke/run.py preflight --env staging
```

3. Only continue if the preflight shows no failures.
4. Use committed environment configs unless there is an explicit local override.
5. Prefer existing test users unless the scenario explicitly requires a synthetic user.
6. Clean up created docs unless the human operator asked to keep them.

Suggested prompt for teammate Codex:

```text
Use /Users/yan/Desktop/BabyMilu/BabyMilu-voice-streaming-server/tools/smoke/README.md.
Run the smoke preflight first. If it passes, run the requested scenario with the shared smoke harness.
Do not invent ad hoc scripts unless the harness is missing a needed capability.
```

## Environment Configs

Committed config:

- `tools/smoke/environments/staging.json`

Templates:

- `tools/smoke/environments/local-compose.example.json`
- `tools/smoke/environments/external-dev.example.json`

Optional local override:

- `tools/smoke/environments/staging.local.json`
- `tools/smoke/environments/<name>.local.json`

Use a local override when you need to point at a different VM, local branch runtime, or teammate-managed dev server. Local overrides are gitignored.

### `local-compose` config notes

For `local-compose`, set:

- `scheduler_trigger`
  - `entrypoint`
  - `docker-exec`
- `scheduler_entrypoint`
  - module:function form
  - example: `services.alarms.cloud.functions:scan_due_scheduled_items`
- `compose_project_dir`

Optional:

- `compose_file`
- `compose_service`
- `compose_workdir`

## Artifacts

Each run writes a new folder under:

- `tools/smoke/artifacts/`

Typical artifacts include:

- `result.json`
- `scenario-details.json`
- captured WAV file if plushie audio was decoded successfully

## Extending The Harness

To add a new feature smoke:

1. Add a scenario class under `tools/smoke/harness/scenarios/`
2. Register it in `tools/smoke/harness/registry.py`
3. Reuse the shared `FirestoreDataAdapter`, `DeviceSimulator`, and artifact writer
4. Document the scenario contract in `docs/testing/smoke-harness.md`

The design goal is to keep the framework core stable while feature-specific logic lives in small scenario modules.
