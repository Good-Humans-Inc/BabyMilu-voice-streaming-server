# Daily Tasks VM Testing Guide

## Goal

Verify daily plushie task detection on `bm-staging-vm` end to end:

- the voice server loads the `Task` provider
- mocked conversations trigger LLM-based task detection
- `process-user-action` updates Firestore-backed task state
- the test leaves no long-lived smoke-test user data behind

## Scope

This guide is for the testing VM only.

- VM: `bm-staging-vm`
- GCE zone: `us-central1-a`
- Deploy root: `/srv/staging/current`
- Voice server code mount: `/srv/staging/current/main/xiaozhi-server`
- Runtime config mount: `/srv/staging/current/data/.config.yaml`

## Required Config

The runtime override must enable the task provider:

```yaml
selected_module:
  VAD: SileroVAD
  ASR: OpenaiASR
  LLM: OpenAILLM
  TTS: ElevenLabsTTS
  Memory: mem_local_short
  Task: llm_task
  Intent: function_call

Task:
  llm_task:
    type: llm_task
```

## Code Changes Needed

Patch the testing VM with these changes:

- Enable `selected_module.Task` in `/srv/staging/current/data/.config.yaml`
- Update `core/connection.py`
  - avoid duplicate task completion processing when the task provider already runs in the memory-save path
  - treat `Memory.mem_local_short.llm` as optional
- Update `core/providers/task/llm_task/llm_task.py`
  - allow the provider to accept either a task string or task list cleanly
- Add `tests/task_smoke_test.py`
  - creates a synthetic user
  - assigns daily tasks through the real backend
  - runs negative and positive mocked conversations
  - prints before/after Firestore-backed state
  - cleans up the synthetic user

## Restart

```bash
gcloud compute ssh bm-staging-vm --zone=us-central1-a --command='cd /srv/staging/current && sudo docker compose restart server'
```

## Verify Effective Config

```bash
gcloud compute ssh bm-staging-vm --zone=us-central1-a --command='cd /srv/staging/current && sudo docker compose exec -T server python - <<\"PY\"
from config.config_loader import load_config
cfg = load_config()
print(cfg.get(\"selected_module\"))
print(cfg.get(\"Task\"))
PY'
```

Expected:

- `selected_module["Task"] == "llm_task"`
- `Task["llm_task"]["type"] == "llm_task"`

## Run Smoke Test

```bash
gcloud compute ssh bm-staging-vm --zone=us-central1-a --command='cd /srv/staging/current && sudo docker compose exec -T server bash -lc "cd /opt/xiaozhi-esp32-server && PYTHONPATH=/opt/xiaozhi-esp32-server python tests/task_smoke_test.py"'
```

## What The Smoke Test Does

1. Creates a synthetic test user with a phone-like UID.
2. Calls `get-tasks-for-user` with `extra: true` so the backend auto-assigns daily tasks.
3. Picks the active daily plushie task.
4. Runs a negative control conversation.
5. Runs a positive greeting conversation.
6. Confirms Firestore-backed fields changed as expected.
7. Deletes the synthetic user data.

## Expected Results

### Negative Control

Expected:

- `negativeMatches` is `[]`
- `userTasks/{taskId}.status` stays `action`
- `userTasks/{taskId}.progress` stays `0`
- `dailySummaries/{today}.tasksCompleted` does not change

### Positive Greeting

Expected:

- `positiveMatches` contains the daily plushie greet task
- `userTasks/{taskId}.status` changes from `action` to `completed`
- `userTasks/{taskId}.progress` changes from `0` to `1`
- `userTasks/{taskId}.lastCompletedAt` is set
- `dailySummaries/{today}.tasksCompleted` increments by `1`
- `taskLogs` gets a new `completed` log entry for that task

## Validated Result From This Run

The tested daily plushie task was:

- `taskId`: `task_e128dd93417344f6`
- `action`: `greet`

Observed before and after:

- Before positive test:
  - `status: action`
  - `progress: 0`
  - `tasksCompleted: 1`
- After positive test:
  - `status: completed`
  - `progress: 1`
  - `lastCompletedAt` populated
  - `tasksCompleted: 2`
  - new `taskLogs` record with `action: completed`

The synthetic smoke-test user was cleaned up after verification.

## Troubleshooting

If the smoke test fails:

- Check merged config inside the container and confirm `selected_module.Task` exists.
- Check that `get-tasks-for-user` returns an active plushie daily task.
- Check that the LLM config is valid and reachable.
- Check container logs:

```bash
gcloud compute ssh bm-staging-vm --zone=us-central1-a --command='cd /srv/staging/current && sudo docker compose logs --tail=200 server'
```

## Rollback

If you need to roll back quickly on the testing VM:

1. Restore the latest `.bak.codex.*` backups for:
   - `/srv/staging/current/main/xiaozhi-server/core/connection.py`
   - `/srv/staging/current/main/xiaozhi-server/core/providers/task/llm_task/llm_task.py`
   - `/srv/staging/current/data/.config.yaml`
2. Restart the server container:

```bash
gcloud compute ssh bm-staging-vm --zone=us-central1-a --command='cd /srv/staging/current && sudo docker compose restart server'
```
