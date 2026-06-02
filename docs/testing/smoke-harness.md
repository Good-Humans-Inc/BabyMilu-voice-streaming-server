# Shared Smoke Harness Architecture

## Why This Exists

We now rely on Codex and human operators to run holistic smoke tests against real staging-like environments. The system worked well enough that it should become shared infrastructure instead of a pile of one-off scripts.

This harness is designed to test more than reminders and alarms. The core abstraction is:

- user
- device
- environment
- trigger
- observable events
- assertions

That general shape supports:

- alarms
- reminders
- websocket/LLM interactions
- memory flows
- backend-triggered state changes
- future firmware probes

## Environment Matrix

The harness supports two axes:

### Environment type

- `cloud`
  - deployed staging or similar hosted runtime
- `local-compose`
  - local branch-under-test runtime
- `external-dev`
  - teammate-managed dev environment

### Data mode

- `live-shape`
  - real Firestore shape using dedicated test users
- `isolated`
  - emulator or local fixture dataset

These axes are intentionally independent. The same scenario should be able to run across different environment types while keeping the same assertion contract.

## Local-Compose Trigger Contract

`local-compose` supports two trigger styles:

1. `entrypoint`
   - executes a Python callable directly from the checked-out branch
   - expected format:
     - `services.alarms.cloud.functions:scan_due_scheduled_items`
   - best for fast PR iteration

2. `docker-exec`
   - executes the same callable inside a running compose service
   - best when the branch-under-test only behaves correctly inside the container runtime

Required `local-compose` config:

- `scheduler_trigger`
- `scheduler_entrypoint`
- `compose_project_dir`

Optional:

- `compose_file`
- `compose_service`
- `compose_workdir`

## The Four Layers

### 1. Scenario Runner

The scenario runner owns lifecycle:

1. setup
2. trigger
3. observe
4. assert
5. cleanup

Each scenario should be small and declarative. The runner should stay stable even as product features expand.

### 2. Data Adapter

The data adapter is the only layer that should know how to build or mutate Firestore documents.

Responsibilities:

- fetch user metadata
- create reminders/alarms in app-consistent shape
- seed test state
- read final state
- clean up test documents

Design rule:

- keep schema knowledge here, not scattered across scenarios

### 3. Device Simulator

The device simulator is the reusable engine for plushie-facing tests.

Responsibilities:

- subscribe to MQTT
- wait for `ws_start`
- open websocket as a mock device
- send `hello`
- optionally send scripted outbound websocket events
- capture:
  - MQTT
  - websocket JSON events
  - TTS
  - LLM
  - binary audio

Future features like memory recall or LLM probing should be built on top of this layer rather than writing separate mock-device logic.

### 4. Assertion Layer

Assertions must stay explicit and observable.

Examples:

- Firestore field changed
- app send marker written
- sessionContext created
- plushie TTS received
- next occurrence advanced
- memory fact mentioned in response

Design rule:

- assertions should describe user-visible or system-visible outcomes, not internal assumptions

## Extensibility Plan

### Server Repo

The server repo owns the framework core because it already contains:

- scheduler logic
- session logic
- websocket runtime
- MQTT-facing behavior

This repo should continue to host:

- `tools/smoke/harness/`
- staged environment configs
- scenario definitions for server-owned flows
- environment adapters for `cloud`, `local-compose`, and `external-dev`

### Backend Repo

When the backend needs its own testing environment, it should contribute:

- additional data adapters
- additional scenarios
- contract docs for backend-specific behavior

But it should reuse the same high-level smoke model where possible.

### Firmware Repo

Firmware-specific testing can either:

- reuse this simulator contract, or
- add a firmware adapter that speaks the same scenario language

That way the company keeps one shared mental model instead of three unrelated smoke systems.

## Contract-First Documentation

Every major feature should have a markdown contract that is easy for humans and Codex to consume.

Recommended sections:

1. user-visible behavior
2. trigger source
3. data shape
4. observable events
5. pass/fail rules
6. cleanup rules
7. known edge cases

That contract is what keeps the harness maintainable across repos.

## Required Operator Flow

Every Codex or human operator should:

1. read `tools/smoke/README.md`
2. choose `environment_type` and `data_mode`
3. run `python3 tools/smoke/run.py preflight --env <name>`
3. stop if preflight fails
4. run the named scenario
5. inspect artifacts
6. clean up or confirm automatic cleanup

## Next Scenarios To Add

The current framework ships with scheduled scenarios. The next good additions are:

- `interaction.first_response`
- `memory.write_then_recall`
- `tool.reminder_create`
- `task.daily_plushie`

Those should reuse the same framework core instead of spawning new standalone scripts.
