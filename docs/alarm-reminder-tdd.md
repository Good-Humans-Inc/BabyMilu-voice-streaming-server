# Alarm / Reminder TDD

Linked PRD:

- Alarm PRD: `https://www.notion.so/Alarm-25b2b144ff4c8017b225ca4fd6e713f9`

## V1 - Apr 2026

### Summary

V1 keeps the same server-driven architecture for alarms and reminders, but
changes the reminder delivery model to remove race conditions, remove
lookahead-based early firing, and improve plushie reminder hydration for
app-created reminders. V1 also standardizes timezone resolution so both alarm
and reminder scheduling read from `users/{uid}.timezone` as the only ground
truth.

### What Changed From V0

#### 1. Reminder delivery is unified and finalized once per occurrence

Reminder delivery now uses one orchestrated scheduler path:

- `app` only: send app notification, then finalize once
- `plushie` only: send plushie reminder, then finalize once
- `app + plushie`: send app first, then plushie, then finalize once

This removes the earlier race where different channels could mutate the same
`nextOccurrenceUTC` independently.

#### 2. Lookahead is removed

V1 removes lookahead for active delivery.

Due-item eligibility is now:

- alarms: `nextOccurrenceUTC <= now`
- reminders: `nextOccurrenceUTC <= now`

This means reminders no longer fire early on plushie.

#### 3. Reminders have a lateness cap

Reminder delivery now ignores stale overdue reminders for active firing.

- reminders more than `3 minutes` overdue are skipped by the active scheduler
- reminder docs are not deleted
- this is a delivery guardrail, not a data-deletion rule

#### 4. Plushie reminder hydration falls back to title / label

If a reminder does not have rich `context`, plushie hydration now uses:

1. `context`
2. otherwise `title`
3. otherwise `label`

This ensures app-created reminders still carry enough meaning into the plushie
session.

#### 5. Plushie reminder sessions carry a precomputed first message

For plushie reminder delivery, the server stores a precomputed
`sessionConfig.firstMessage` in the session context.

This means the reminder that the plushie speaks is generated server-side before
device wake and then hydrated directly into the session.

#### 6. Smoke-testing environment is formalized

V1 includes a documented smoke-testing setup:

- staging is restricted to allowlisted test users
- dev blocks those same test users to avoid duplicate reminders
- a local mock plushie harness can subscribe to MQTT, open websocket, capture
  streamed TTS audio, and save a playable WAV

See:

- [Reminder Smoke Testing TDD](./reminder-smoke-testing-tdd.md)

#### 7. `users/{uid}.timezone` is the only schedule timezone source of truth

Alarm and reminder recurrence now resolve timezone from the user document, not
from the scheduled item itself.

- alarms advance using `users/{uid}.timezone`
- reminders advance using `users/{uid}.timezone`
- legacy `timezone` fields that may still exist on individual alarm/reminder
  docs are not trusted for recurrence math
- if a recurring item has no usable `users/{uid}.timezone`, it must not silently
  fall back to an arbitrary default timezone

## V1 Architecture

The system remains backend-driven.

| Component | Role |
| --- | --- |
| Mobile App (React Native) | Displays and edits alarms/reminders; handles notification permissions and user UI |
| Backend / Firestore | Source of truth for schedules, reminder docs, alarm docs, and user metadata |
| Cloud Scheduler + Cloud Function | Periodically scans due alarms/reminders and triggers delivery |
| Xiaozhi Server | Hydrates device mode sessions, handles reminder/alarm runtime behavior, and server-initiated speech |
| Plushie Firmware / Device | Receives MQTT wake, opens websocket, and executes the reminder/alarm flow |

## V1 Current Workflow

### Alarm workflow

1. Cloud Scheduler invokes `scan-due-alarms-[env]` every minute.
2. Server queries due alarms where:
   - `status == "on"`
   - `nextOccurrenceUTC <= now`
3. For each due alarm target:
   - reject invalid targets
   - reject devices with an active session
   - create `sessionContext` with mode `morning_alarm`
   - create a wake request
4. Server publishes MQTT `ws_start` for the target device.
5. Device opens websocket connection.
6. `ConnectionHandler` hydrates the stored `sessionContext`.
7. Runtime loads morning alarm mode behavior.
8. Alarm fires on-device.
9. Recurring alarms advance to the next occurrence.
10. One-time alarms are completed.

Alarm recurrence timezone resolution order:

1. cached `users/{uid}.timezone` loaded during fetch
2. fresh `users/{uid}` lookup if cache is unavailable
3. fail loudly if the user profile timezone is still missing

### Reminder workflow

1. Reminder is created from app or voice and stored in Firestore.
2. Reminder remains `status = "on"` until it is delivered and finalized, or
   explicitly turned off.
3. Cloud Scheduler invokes the same scheduler entrypoint every minute.
4. Reminder path queries due reminders where:
   - `status == "on"`
   - `nextOccurrenceUTC <= now`
5. If reminder is older than `3 minutes` overdue, skip active delivery.
6. Resolve `deliveryChannel`.
7. Generate reminder message using character persona + reminder meaning.
8. Deliver channels:
   - app only: send push
   - plushie only: create reminder session + publish `ws_start`
   - both: send app first, then plushie
9. Finalize exactly once:
   - one-time reminder: `status = "off"`
   - recurring reminder: compute next `nextOccurrenceUTC` and
     `nextTriggerUTC`
10. Write delivery metadata:
   - `lastDelivered.app.at`
   - `lastDelivered.plushie.at`
   - `lastDelivered.occurrenceUTC`

Reminder recurrence timezone rule:

- recurring reminders advance using `users/{uid}.timezone`
- one-time reminders do not need timezone to turn `off`
- if a recurring reminder has no usable `users/{uid}.timezone`, skip it rather
  than silently advancing in `UTC` or another fallback zone

## Latest Schema

### Alarm

Path:

- `/users/{uid}/alarms/{alarmId}`

```json
{
  "label": "Morning Alarm",
  "schedule": {
    "repeat": "weekly",
    "timeLocal": "07:30",
    "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]
  },
  "nextOccurrenceUTC": "2026-04-26T14:30:00Z",
  "lastProcessedUTC": "2026-04-25T14:30:00Z",
  "status": "on",
  "targets": [
    {
      "deviceId": "deviceId_123",
      "mode": "morning_alarm"
    }
  ],
  "uid": "+16173350204",
  "updatedAt": "<timestamp>"
}
```

Note:

- a legacy `timezone` field may still appear on some alarm docs from older
  writes, but V1 runtime logic uses `users/{uid}.timezone` for recurrence
  instead

### Reminder

Path:

- `/users/{uid}/reminders/{reminderId}`

```json
{
  "label": "Take Lexapro",
  "schedule": {
    "repeat": "daily",
    "timeLocal": "15:30"
  },
  "nextOccurrenceUTC": "2026-04-25T07:30:00Z",
  "nextTriggerUTC": "2026-04-25T07:30:00Z",
  "status": "on",
  "createdAt": "<timestamp>",
  "updatedAt": "<timestamp>",
  "uid": "+11233456xx",
  "targets": [
    {
      "mode": "reminder",
      "deviceId": "deviceId_123"
    }
  ],
  "deliveryChannel": ["app", "plushie"],
  "lastDelivered": {
    "app": {
      "at": "2026-04-25T07:30:12Z"
    },
    "plushie": {
      "at": "2026-04-25T07:30:12Z"
    },
    "occurrenceUTC": "2026-04-25T07:30:00Z"
  },
  "lastAction": null,
  "lastActionAt": null
}
```

Note:

- a legacy `timezone` field may still appear on some reminder docs from older
  writes, but V1 runtime logic uses `users/{uid}.timezone` for recurring
  advancement instead

### Session Context

Path:

- `/sessionContexts/{deviceId}`

```json
{
  "sessionType": "alarm",
  "triggeredAt": "2026-04-25T07:30:12Z",
  "expiresAt": "2026-04-25T07:31:12Z",
  "ttlSeconds": 60,
  "sessionConfig": {
    "mode": "reminder",
    "alarmId": "reminderId_123",
    "reminderId": "reminderId_123",
    "userId": "+11233456xx",
    "context": null,
    "title": "Take Lexapro",
    "label": "Take Lexapro",
    "firstMessage": "Hey, don't forget to take Lexapro later."
  }
}
```

### Notes on Session Context

- reminder sessions use short TTL
- alarm sessions use longer TTL
- `firstMessage` is used for plushie reminder delivery
- reminder hydration fallback order is:
  - `context`
  - `title`
  - `label`

## Operational Notes

### Alarm scheduler rules in V1

- due when `nextOccurrenceUTC <= now`
- no lookahead
- timezone source of truth is `users/{uid}.timezone`
- server-driven wake via MQTT + websocket mode hydration
- recurring alarms advance after firing
- one-time alarms complete after firing

### Reminder scheduler rules in V1

- due when `nextOccurrenceUTC <= now`
- no lookahead
- timezone source of truth is `users/{uid}.timezone`
- skip active delivery if overdue by more than `3 minutes`
- deliver all configured channels
- finalize once

## Testing

### Reminder smoke testing

See:

- [Reminder Smoke Testing TDD](./reminder-smoke-testing-tdd.md)

### Recommended V1 validation

1. App-only reminder
2. Plushie-only reminder
3. App + plushie one-time reminder
4. App + plushie recurring reminder
5. Stale overdue reminder
6. Mock-device QA using the local harness
7. User-timezone smoke:
   - recurring reminder advances using `users/{uid}.timezone` even if the
     reminder doc carries stale timezone metadata
   - recurring alarm advances using `users/{uid}.timezone` even if the alarm
     doc carries stale timezone metadata

## V0 - Oct 2025 (Archived Context)

### V0 summary

V0 established the original server-driven alarm/reminder architecture:

- Firestore as source of truth
- Cloud Scheduler scanning due items
- MQTT `ws_start` used to wake plushies
- websocket runtime hydrating mode sessions

Key V0 assumptions:

- reminders and alarms could still rely on lookahead-style scanning
- reminder delivery behavior was less centralized
- reminder hydration was weaker for app-created reminders
- smoke-testing environment and mock-device workflow were not formally
  documented

V1 supersedes V0 for current implementation behavior.
