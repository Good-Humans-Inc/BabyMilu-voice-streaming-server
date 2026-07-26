# Codex Workflow For Shared Smoke Tests

This guide is for teammates who want their Codex to run BabyMilu smoke tests without rebuilding local scripts or guessing environment setup.

## Rule Zero

Before Codex runs any live smoke scenario, it should:

1. read this file
2. run the smoke preflight
3. stop immediately if the preflight reports failures

That keeps the workflow consistent and avoids half-configured local test runs.

## Choose The Right Mode First

Codex should decide two things before running smoke:

1. `environment_type`
   - `cloud`
   - `local-compose`
   - `external-dev`
2. `data_mode`
   - `live-shape`
   - `isolated`

Preferred defaults:

- PR branch validation: `local-compose` + `live-shape`
- deployed staging validation: `cloud` + `live-shape`
- safe offline iteration: `local-compose` + `isolated`

For `local-compose`, prefer:

- `scheduler_trigger = entrypoint` for the fastest local branch loop
- `scheduler_trigger = docker-exec` when the branch only runs correctly inside the compose container

## Required Auth

Codex inherits the auth available on the teammate machine. For `live-shape` smoke tests, the machine should already have:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project composed-augury-469200-g6
```

Why both matter:

- `gcloud auth login`
  gives Codex a token for invoking cloud-hosted scheduler targets
- `gcloud auth application-default login`
  gives the smoke harness Firestore access through the Python client

For `isolated` mode, ADC may not be needed if the environment is fully local.

## Required Local Check

Codex should run:

```bash
python3 tools/smoke/run.py preflight --env staging
```

Expected result:

- all checks are `OK`

If preflight shows failures, Codex should not continue. It should report the missing setup first.

## Recommended Codex Prompt

Use a prompt like this:

```text
Use /Users/yan/Desktop/BabyMilu/BabyMilu-voice-streaming-server/tools/smoke/CODEX_WORKFLOW.md.
Run the preflight first.
If preflight passes, run the shared smoke harness scenario I ask for.
Do not write one-off test scripts unless the shared harness is missing a required capability.
Clean up created docs unless I explicitly ask to keep them.
```

## Normal Operator Flow

### 1. Preflight

```bash
python3 tools/smoke/run.py preflight --env staging
```

### 2. Inspect available scenarios

```bash
python3 tools/smoke/run.py list-scenarios --env staging
```

### 3. Run a scenario

Reminder example:

```bash
python3 tools/smoke/run.py run \
  --env staging \
  --scenario scheduled.reminder \
  --uid +11551551551 \
  --device-id 90:e5:b1:d6:f8:58 \
  --channel both \
  --repeat weekly \
  --label "codex shared reminder smoke"
```

Alarm example:

```bash
python3 tools/smoke/run.py run \
  --env staging \
  --scenario scheduled.alarm \
  --uid +11551551551 \
  --device-id 90:e5:b1:d6:f8:58 \
  --repeat weekly \
  --label "codex shared alarm smoke"
```

Timezone recalculation example (local Firestore emulator only):

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
```

In a separate terminal, start the backend branch's guarded emulator worker:

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
python3 tools/smoke/run.py preflight --env timezone-local
python3 tools/smoke/run.py run \
  --env timezone-local \
  --scenario scheduled.timezone_recalculation \
  --uid smoke-timezone-user \
  --from-timezone America/Los_Angeles \
  --to-timezone America/New_York
```

This scenario hard-fails unless it sees `local-compose` + `isolated`,
`FIRESTORE_EMULATOR_HOST`, and a `demo-*` project ID. It verifies the Firestore
timezone-update worker only; it does not invoke a cloud endpoint, due-item
scheduler, MQTT, or websocket runtime.

The dual-database rollout requires direct worker coverage for both
`development` and `(default)`. Start a second development worker for the
phone-keyed Daily Call fixture:

```bash
cd /path/to/babymilu-backend
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
python3 src/commands/run-user-timezone-worker-local.py \
  --project demo-babymilu \
  --database development \
  --uid +15550001111
```

Back in the voice-server smoke terminal, run the Daily Call cursor contract
with the same fresh E.164 fixture:

```bash
python3 tools/smoke/run.py run \
  --env timezone-local \
  --scenario scheduled.daily_call_timezone_recalculation \
  --uid +15550001111 \
  --from-timezone America/Los_Angeles \
  --to-timezone America/New_York
```

Both development artifacts must report `database=development`. The Daily Call
artifact verifies that billing, consumed-day, character, retry, and
compensation fields are unchanged.

Start a third guarded worker against `(default)` with one fresh E.164 fixture
allowlisted for both default scenarios:

```bash
cd /path/to/babymilu-backend
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
python3 src/commands/run-user-timezone-worker-local.py \
  --project demo-babymilu \
  --database '(default)' \
  --uid +15550002222
```

Run both direct default-worker contracts:

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

The default artifacts must report `database=(default)` and have scenario names
that include `default`. All four timezone scenarios refuse cloud,
`live-shape`, non-`demo-*` projects, missing `FIRESTORE_EMULATOR_HOST`, and
occupied synthetic fixtures. These direct scenarios do not exercise the
cross-database UID/phone bridge; backend worker tests remain the contract for
that propagation.

Magic Camera example:

```bash
python3 tools/smoke/run.py run \
  --config /Users/yan/Desktop/BabyMilu/BabyMilu-voice-streaming-server/tools/smoke/environments/staging.local.json \
  --scenario interaction.magic_camera_photo \
  --uid +11551551551 \
  --device-id 90:e5:b1:d6:fb:0c \
  --label "codex magic camera smoke"
```

## Environment Config

The committed staging config is:

- `tools/smoke/environments/staging.json`

Codex should use that by default for deployed staging checks.

If a teammate needs a different local target, they can create or copy:

- `tools/smoke/environments/staging.local.json`
- `tools/smoke/environments/local-compose.local.json`
- `tools/smoke/environments/external-dev.local.json`

That file is gitignored.

## Artifacts

Every run writes a timestamped artifact folder under:

- `tools/smoke/artifacts/`

Codex should include the most relevant artifact files when reporting results:

- `result.json`
- `scenario-details.json`
- `*.wav` when plushie audio capture was available

## When To Extend Instead Of Rewriting

If a requested smoke test is close to existing behavior, Codex should extend the harness instead of creating a fresh standalone script.

Examples:

- memory recall probe
- first-response LLM probing
- backend-triggered user/device flows
- firmware compatibility checks

The shared framework is the default path now. One-off scripts should be the exception.
