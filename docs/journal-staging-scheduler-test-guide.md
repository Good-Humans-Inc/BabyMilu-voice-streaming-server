# Journal Scheduler Staging Test Guide

This guide verifies the production-like journal path in staging:

1. Cloud Scheduler invokes journal Cloud Functions on cadence.
2. The generation job skips users whose local clock is not 6:30 AM.
3. The generation job creates a ready journal at the user's local 6:30 AM.
4. The publish job publishes the ready journal at the user's local 7:00 AM.

Manual function invocation is useful for debugging, but it does not prove the
scheduled production path works.

## Staging Resources

Use staging resources only.

- GCP project: `composed-augury-469200-g6`
- Region: `us-central1`
- Firestore database: staging database for the staging environment
- Supabase: staging Supabase project
- Cloud Functions:
  - `process-journal-sessions-staging`
  - `generate-journals-staging`
  - `publish-journals-staging`
- Cloud Scheduler jobs:
  - `process-journal-sessions-staging-job`
  - `generate-journals-staging-job`
  - `publish-journals-staging-job`

## Required Function Environment

Set these on the staging functions:

```text
GOOGLE_CLOUD_PROJECT=composed-augury-469200-g6
FIRESTORE_DATABASE_ID=<staging Firestore database id>
SUPABASE_URL=<staging Supabase URL>
SUPABASE_SERVICE_ROLE_KEY=<staging Supabase service role key>
OPENAI_API_KEY=<OpenAI API key>

JOURNALS_ENABLED=true
JOURNAL_PROCESSING_ENABLED=true
JOURNAL_GENERATION_ENABLED=true
JOURNAL_PUBLISH_ENABLED=true
JOURNAL_PUSH_ENABLED=false

JOURNAL_PROCESSING_EXECUTE=true
JOURNAL_GENERATION_EXECUTE=true
JOURNAL_PUBLISH_EXECUTE=true
```

Keep `JOURNAL_PUSH_ENABLED=false` for scheduler validation unless the test plan
explicitly includes app push notifications.

## Required Firestore Indexes

The jobs use collection-group queries. Confirm these staging indexes are ready:

```bash
gcloud firestore indexes fields describe status \
  --collection-group=journal_queue \
  --database=<staging Firestore database id> \
  --format="yaml(indexConfig.indexes)"

gcloud firestore indexes fields describe status \
  --collection-group=moments \
  --database=<staging Firestore database id> \
  --format="yaml(indexConfig.indexes)"
```

Each field needs a `COLLECTION_GROUP` ascending index on `status`.

## Scheduler Setup

Create or update Scheduler jobs to call the staging function URLs every minute:

```bash
gcloud scheduler jobs create http process-journal-sessions-staging-job \
  --location=us-central1 \
  --schedule="* * * * *" \
  --uri="https://us-central1-composed-augury-469200-g6.cloudfunctions.net/process-journal-sessions-staging" \
  --http-method=GET

gcloud scheduler jobs create http generate-journals-staging-job \
  --location=us-central1 \
  --schedule="* * * * *" \
  --uri="https://us-central1-composed-augury-469200-g6.cloudfunctions.net/generate-journals-staging" \
  --http-method=GET

gcloud scheduler jobs create http publish-journals-staging-job \
  --location=us-central1 \
  --schedule="* * * * *" \
  --uri="https://us-central1-composed-augury-469200-g6.cloudfunctions.net/publish-journals-staging" \
  --http-method=GET
```

If the jobs already exist, use `gcloud scheduler jobs update http ...` with the
same schedule and URI instead of creating duplicates.

Verify the jobs are enabled and have recent attempts:

```bash
gcloud scheduler jobs describe generate-journals-staging-job \
  --location=us-central1 \
  --format="yaml(state,schedule,scheduleTime,lastAttemptTime,status)"

gcloud scheduler jobs describe publish-journals-staging-job \
  --location=us-central1 \
  --format="yaml(state,schedule,scheduleTime,lastAttemptTime,status)"
```

Expected:

```text
state: ENABLED
schedule: '* * * * *'
status: {}
```

## Test Data Setup

Create a staging test user with:

- A Firestore user document containing an IANA `timezone`.
- A staging Supabase user/session/turns set with at least 3 user turns.
- A Supabase session memory status that normalizes to `done`.
- A Firestore pending queue:

```text
users/{testUserId}/journal_queue/{characterId}_{localDate}
```

The queue must contain:

```json
{
  "characterId": "<characterId>",
  "date": "<local YYYY-MM-DD>",
  "status": "pending",
  "journal_type": "first",
  "sessions": [
    {
      "sessionId": "<staging Supabase session id>",
      "sessionStartTime": "<ISO timestamp>",
      "sessionEndTime": "<ISO timestamp>",
      "classification": {
        "should_journal": true,
        "dedup_clear": true,
        "journal_value_type": "strong",
        "topicSummary": ["scheduler staging test"]
      },
      "sourceMemoryEventIds": []
    }
  ]
}
```

Choose the user timezone so local 6:30 AM occurs soon. For example, if the test
operator is in Chicago, choose a timezone whose 6:30 AM lands at the next
practical `:30` minute in Chicago.

## Expected Observations

Before the user's local 6:30 AM:

- Scheduler job attempts should succeed.
- The queue should remain `pending`.
- Generation logs should show the queue skipped because it is not local 6:30.

At the user's local 6:30 AM:

- The queue changes to `status: generated`.
- The queue gets `entryId` and `memoryEventId`.
- Firestore creates:

```text
users/{testUserId}/moments/{entryId}
```

- The Moment has:

```json
{
  "type": "journal",
  "status": "ready",
  "journalType": "first",
  "memoryEventId": "<Supabase event id>"
}
```

- Staging Supabase `character_memory_events` has a matching
  `eventType: journal_written` event.

At the user's local 7:00 AM:

- The Moment changes from `status: ready` to `status: published`.
- If `JOURNAL_PUSH_ENABLED=false`, no push notification should be sent.

## Useful Read-Only Checks

Recent function logs:

```bash
gcloud functions logs read generate-journals-staging \
  --gen2 \
  --region=us-central1 \
  --limit=30 \
  --format="value(log)"

gcloud functions logs read publish-journals-staging \
  --gen2 \
  --region=us-central1 \
  --limit=30 \
  --format="value(log)"
```

Adapter smoke check from the staging server container:

```bash
docker compose run --rm --no-deps server python - <<'PY'
from services.journals.supabase_client import JournalSupabaseClient

rows = JournalSupabaseClient().get_journal_memory_events(
    "<testUserId>",
    character_id="<characterId>",
    limit=5,
)
print({"count": len(rows), "firstType": rows[0].get("event_type") if rows else None})
PY
```

Expected:

```text
{'count': 1, 'firstType': 'journal_written'}
```

## Pass Criteria

The staging scheduler test passes only if all of these are true:

- Scheduler jobs are enabled and invoke the staging functions on cadence.
- The queue remains pending before local 6:30 AM.
- The queue generates at local 6:30 AM without manual invocation.
- A ready Firestore Moment is created.
- A matching Supabase `journal_written` event is created.
- The Moment publishes at local 7:00 AM without manual invocation.
