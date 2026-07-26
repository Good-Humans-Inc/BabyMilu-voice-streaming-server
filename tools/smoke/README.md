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
- `scheduled.timezone_recalculation`
- `scheduled.daily_call_timezone_recalculation`
- `scheduled.default_timezone_recalculation`
- `scheduled.default_daily_call_timezone_recalculation`
- `scheduled.cloud_timezone_worker_recalculation`
- `interaction.magic_camera_photo`
- `interaction.daycare_food_gift`

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

### Example: Timezone Recalculation Smoke (Local Emulator Only)

`scheduled.timezone_recalculation` is intentionally unable to run against cloud
Firestore. It seeds one weekly reminder and one weekly alarm in the Firestore
emulator, changes the user's IANA timezone, and waits for the Firestore worker to
rebase both `nextOccurrenceUTC` values while preserving `schedule.timeLocal` and
`schedule.days`.

Start the Firestore emulator and the timezone-change worker/function emulator
first. The backend branch provides the guarded local runner:

```bash
cd /path/to/babymilu-backend
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
python3 src/commands/run-user-timezone-worker-local.py \
  --project demo-babymilu \
  --database development \
  --uid smoke-timezone-user
```

Then run:

```bash
cd /Users/yan/Desktop/BabyMilu/.worktrees/voice-server-timezone-schedule-recalculation
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
export BABYMILU_SMOKE_ENVIRONMENT_TYPE=local-compose
export BABYMILU_SMOKE_DATA_MODE=isolated
export BABYMILU_SMOKE_PROJECT=demo-babymilu
export BABYMILU_SMOKE_MQTT_HOST=127.0.0.1
export BABYMILU_SMOKE_WS_URL=ws://127.0.0.1:8000
export BABYMILU_SMOKE_COMPOSE_PROJECT_DIR="$PWD"
export BABYMILU_SMOKE_SCHEDULER_TRIGGER=entrypoint
export BABYMILU_SMOKE_SCHEDULER_ENTRYPOINT=services.alarms.cloud.functions:scan_due_scheduled_items

python3 tools/smoke/run.py preflight --env timezone-local
python3 tools/smoke/run.py run \
  --env timezone-local \
  --scenario scheduled.timezone_recalculation \
  --uid smoke-timezone-user \
  --from-timezone America/Los_Angeles \
  --to-timezone America/New_York
```

The scenario does not invoke the due-item scheduler or a device simulator. It
expects the local Firestore update trigger to perform the recalculation. Created
schedule documents and the synthetic user are removed automatically unless
`--keep-docs` is supplied.

### Dual-Database Worker Coverage

The rollout has separate workers for `development` and `(default)`. Keep the
existing development coverage, including its phone-keyed Daily Call shape, and
run the explicitly named default-database scenarios before releasing either
worker.

Start a second guarded development worker for the phone-keyed Daily Call
fixture:

```bash
cd /path/to/babymilu-backend
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
python3 src/commands/run-user-timezone-worker-local.py \
  --project demo-babymilu \
  --database development \
  --uid +15550001111
```

Back in the voice-server smoke terminal, run the phone-keyed Daily Call contract:

```bash
python3 tools/smoke/run.py run \
  --env timezone-local \
  --scenario scheduled.daily_call_timezone_recalculation \
  --uid +15550001111 \
  --from-timezone America/Los_Angeles \
  --to-timezone America/New_York
```

This scenario validates
`development/users/{E.164 phone}/miluCall/dailyCall`, including its
seven-day `times` map, billing/character/retry/compensation preservation, and
no-claim/no-dispatch invariant.

In another terminal, start the guarded `(default)` worker with one fresh E.164
fixture allowlisted for both default-database scenarios:

```bash
cd /path/to/babymilu-backend
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
python3 src/commands/run-user-timezone-worker-local.py \
  --project demo-babymilu \
  --database '(default)' \
  --uid +15550002222
```

Then run both direct `(default)` worker contracts:

```bash
python3 tools/smoke/run.py run \
  --env timezone-local \
  --scenario scheduled.default_timezone_recalculation \
  --uid +15550002222 \
  --from-timezone America/Los_Angeles \
  --to-timezone America/New_York

python3 tools/smoke/run.py run \
  --env timezone-local \
  --scenario scheduled.default_daily_call_timezone_recalculation \
  --uid +15550002222 \
  --from-timezone America/Los_Angeles \
  --to-timezone America/New_York
```

The first default scenario covers recurring reminder and alarm
`nextOccurrenceUTC` plus `nextTriggerUTC`; the second covers
`users/{phone}/miluCall/dailyCall`. Artifact directory names include
`default_timezone_recalculation` or
`default_daily_call_timezone_recalculation`, and each
`scenario-details.json` records `database=(default)`.

All four timezone scenarios hard-refuse cloud and `live-shape` environments,
require `FIRESTORE_EMULATOR_HOST` plus a `demo-*` project, and refuse occupied
synthetic fixtures. Cleanup therefore cannot overwrite a pre-existing
emulator user or schedule. The direct default-worker scenarios do not prove
the development-to-default UID/phone bridge; keep that identity propagation
covered by backend worker tests.

### Deployed Dual-Database Worker Smoke (Explicit Live Approval Only)

`scheduled.cloud_timezone_worker_recalculation` is the guarded rollout smoke
for the deployed Eventarc workers. It is intentionally separate from the four
local-emulator contracts and runs only with `cloud` + `live-shape`.

Before any Firestore write it verifies:

- project `composed-augury-469200-g6`;
- active development and `(default)` function contracts, exact database/path
  filters, runtime/build/trigger service accounts, and `nam5` trigger region;
- one service-level Run Invoker per worker: its exact Eventarc identity,
  an audit of inherited project-level Run Invokers, no public project member,
  and an effective unauthenticated HTTP denial;
- complete absence of both disposable user trees and any matching top-level
  legacy reminder/alarm/schedule.

The only write allowlist is:

```text
development/users/codex-timezone-live-smoke-20260726
development/users/codex-timezone-live-smoke-20260726/reminders/codex-timezone-live-smoke-reminder-20260726
development/users/codex-timezone-live-smoke-20260726/schedules/codex-timezone-live-smoke-schedule-20260726
(default)/users/+15550003333
(default)/users/+15550003333/miluCall/dailyCall
```

Run it only after an operator explicitly approves those exact disposable
documents and confirms both workers are deployed:

```bash
python3 tools/smoke/run.py preflight --env staging

python3 tools/smoke/run.py run \
  --env staging \
  --scenario scheduled.cloud_timezone_worker_recalculation \
  --uid codex-timezone-live-smoke-20260726 \
  --from-timezone America/Los_Angeles \
  --to-timezone America/New_York \
  --direct-default-timezone America/Chicago \
  --confirm-live-timezone-smoke RUN_LIVE_TIMEZONE_WORKER_SMOKE_20260726 \
  --timeout-seconds 180
```

The scenario proves the development update, reminder/schedule cursor and audit
recalculation, guarded UID-to-E.164 bridge, bridged Daily Call recalculation,
and a second direct `(default)` timezone update. It also asserts no schedule or
Daily Call delivery/claim marker is written. It never invokes the due-item
scheduler, MQTT, or websocket runtime.

`--keep-docs` and `--skip-preflight` are refused. Every fixture write is
create-only, timezone updates
are transactionally conditioned on this run's exact marker, and cleanup only
deletes a document while that same marker still owns it. Cleanup runs in
`finally`, processes exact child documents before their parents, then
repeatedly verifies that both user trees and matching top-level legacy queries
are empty. The pre-run snapshot,
deployed-contract evidence, both Eventarc audit results, no-delivery fields,
and cleanup proof are written to `scenario-details.json`.

### Example: Magic Camera Smoke

```bash
python3 tools/smoke/run.py run \
  --config /Users/yan/Desktop/BabyMilu/BabyMilu-voice-streaming-server/tools/smoke/environments/staging.local.json \
  --scenario interaction.magic_camera_photo \
  --uid +11551551551 \
  --device-id 90:e5:b1:d6:fb:0c \
  --label "shared smoke magic camera"
```

### Example: Daycare Food + Gift Smoke

The environment config must select Firestore `development` and provide the
deployed Daycare URL plus the Firebase web API key. The API key is public
Firebase client configuration, not a server credential.

```bash
/Users/yan/Desktop/BabyMilu/.venv/bin/python tools/smoke/run.py run \
  --config tools/smoke/environments/daycare-miffy-dev.local.json \
  --scenario interaction.daycare_food_gift \
  --uid auto-daycare \
  --timeout-seconds 300 \
  --label "miffy-dev Daycare release smoke"
```

The scenario creates a disposable Firebase email/password identity, seeds an
isolated user and character in the named database, runs authenticated Food and
Gift text actions through preview and Send, validates each signed private
image, reverses its synthetic economy counters, and removes Firebase Auth,
Firestore, and Storage test data.

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
