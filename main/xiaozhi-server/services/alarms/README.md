# Alarm Service

This package contains the backend-facing alarm/reminder logic that will
eventually power Cloud Functions / Cloud Tasks. It deliberately avoids importing
from `core.*` so it can be lifted into a standalone service later.

## Contents

- `models.py` – dataclasses describing alarm docs, targets, logs.
- `firestore_client.py` – read/write helpers scoped to alarm collections.
- `scheduler.py` – logic to discover which alarms should fire and create mode
  sessions via `services.session_context.store`.
- `config.py` – default mode configuration (instructions, follow-up behavior)
  plus shared alarm timing knobs (scheduler lookahead, session TTL) consumed by
  the runtime when no per-session overrides exist.
- `tool_handlers.py` – server-side implementations for snooze/dismiss/list
  tools (currently stubs).
- `tasks.py` – lightweight wake-request wrapper + payload helper that forwards
  session metadata to downstream workers.
- `cloud/functions.py` – Cloud Scheduler entrypoint that prepares wake requests
  and publishes MQTT `ws_start`.

## Cloud Function deployment

The HTTP entrypoint `services.alarms.cloud.functions.scan_due_alarms` is deployed
as separate first-gen Cloud Functions per environment:

- `scan-due-alarms-dev`
- `scan-due-alarms-staging`
- `scan-due-alarms-prod`

Each deployment re-exports the same handler via a minimal `main.py` wrapper in
the upload bundle, keeping the source of truth under `services/alarms/cloud/`.

## Session-Centered Architecture

Alarm wake-ups now rely on the shared `services.session_context` package. When a
device is scheduled to wake up, the scheduler creates a `sessionContexts`
document that records the trigger timestamp, TTL, and a normalized
`session_config` (including the selected mode). The websocket runtime hydrates
this session on connect and ignores firmware-provided mode hints, ensuring that
alarms remain a fully server-owned workflow.

