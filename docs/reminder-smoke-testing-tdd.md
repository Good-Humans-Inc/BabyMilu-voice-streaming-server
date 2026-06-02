# Reminder Smoke Testing TDD

## Purpose

This document describes the smoke-testing environment and workflow used for the
April 2026 reminder scheduler fixes.

The goals are:

- verify the new reminder scheduler behavior end to end
- isolate testing to allowlisted accounts and devices
- avoid sending duplicate or unexpected reminder traffic to non-test users
- provide a repeatable harness for both server-side validation and local mock
  device QA

## Scope

This smoke-testing setup is for:

- reminder delivery orchestration
- reminder plushie hydration
- app + plushie dual-channel reminder behavior
- stale overdue reminder protection

This is not intended to revalidate all legacy reminder infrastructure or serve
as a production load test.

## Key Fixes Under Test

The smoke environment validates the following V1 behaviors:

1. Dual-channel reminder race fix
   - `app`, `plushie`, and `app + plushie` reminders are handled in one
     orchestrated path
   - each reminder occurrence is finalized once

2. No lookahead / immediate firing
   - reminders are eligible when `nextOccurrenceUTC <= now`
   - no early plushie reminder firing

3. Reminder lateness cap
   - reminders more than `3 minutes` overdue are skipped by active delivery

4. Reminder hydration fallback
   - plushie reminder hydration uses:
     - `context`
     - else `title`
     - else `label`

5. Precomputed plushie reminder message
   - plushie reminder sessions carry `sessionConfig.firstMessage`
   - the reminder spoken on device is generated server-side before wake

## Environments

### Dev

Purpose:

- preserve the existing non-test environment
- prevent test users from receiving duplicate reminder traffic while staging
  smoke tests are running

Expected guardrail:

- the testing UIDs are blocked from dev reminder/alarm delivery

Testing UIDs:

- `+11551551551`
- `+11111111111`
- `+14244588253`

Associated server:

- `136.111.52.199`

### Staging

Purpose:

- run the new reminder scheduler behavior
- isolate delivery to only allowlisted test users

Expected guardrail:

- only allowlisted test users are processed by staging reminder/alarm delivery

Testing UIDs:

- `+11551551551`
- `+11111111111`
- `+14244588253`

Associated server:

- `34.30.176.148`

Expected staging function:

- `scan-due-alarms-staging`

Expected staging scheduler:

- `scan-due-alarms-staging-job`

## Scheduler Model Under Test

The smoke environment assumes the following reminder decision model:

- `app` only:
  - send app reminder
  - finalize once

- `plushie` only:
  - send plushie reminder
  - finalize once

- `app + plushie`:
  - send app reminder first
  - send plushie reminder second
  - finalize once

Reminder finalization writes:

- `lastDelivered.app.at`
- `lastDelivered.plushie.at`
- `lastDelivered.occurrenceUTC`

For recurring reminders:

- `nextOccurrenceUTC` is advanced once
- `nextTriggerUTC` is recomputed once

For one-time reminders:

- `status` becomes `off`

## Mock Plushie Harness

Branch includes a reusable local smoke-testing harness:

- [mock_plushie_reminder.py](/Users/yan/Desktop/BabyMilu/BabyMilu-voice-streaming-server-apr-reminder-racing-fix/scripts/mock_plushie_reminder.py)

This script:

1. creates a real recurring `app + plushie` reminder for a test user
2. subscribes to MQTT for the target device
3. waits for the real `ws_start`
4. opens websocket as a mock device
5. captures:
   - MQTT payloads
   - `tts` messages
   - streamed binary audio frames
6. decodes the Opus frames into a local WAV file
7. saves a JSON transcript/metadata artifact
8. optionally plays the audio locally
9. cleans up the reminder and session context after the run

This allows us to validate plushie reminder behavior without depending on a
physical device.

## Manual QA Workflow

### Physical-device QA

Use an allowlisted test user and device.

Recommended checks:

1. App-only reminder
   - app push appears once
   - plushie does not wake

2. Plushie-only reminder
   - plushie wakes once
   - app push does not appear

3. App + plushie one-time reminder
   - app push appears once
   - plushie wakes once
   - reminder turns `off`

4. App + plushie recurring reminder
   - app push appears once
   - plushie wakes once
   - reminder advances to next real occurrence

5. Stale reminder
   - reminder more than `3 minutes` overdue is skipped
   - reminder doc is not deleted

### Mock-device QA

Use the harness to validate the plushie leg directly:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
python scripts/mock_plushie_reminder.py \
  --uid +11551551551 \
  --device-id 90:e5:b1:d6:f8:58 \
  --play-audio
```

Expected outcomes:

- MQTT `ws_start` received
- websocket connection succeeds
- `sessionConfig.firstMessage` present
- TTS audio saved to a local WAV
- recurring reminder advances to next day

## Artifacts

The mock harness writes artifacts to:

- `artifacts/mock-plushie/*.wav`
- `artifacts/mock-plushie/*.json`

These are for local QA only and should not be committed.

## Reviewer Notes

When reviewing this reminder fix, the important questions are:

1. Is reminder delivery now finalized once per occurrence?
2. Is reminder lookahead fully removed?
3. Is the `3 minute` lateness cap enforced?
4. Does plushie reminder hydration fall back from `context` to `title/label`?
5. Is the mock-device harness sufficient to reproduce plushie reminder behavior
   without a physical device?

## Out of Scope

The smoke environment does not prove:

- large-scale reminder throughput behavior
- physical-device audio quality for all TTS providers
- long-horizon recurring reminder correctness across many days/weeks
- legacy reminder scheduler compatibility

Those require separate QA or follow-up validation.
