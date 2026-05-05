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
