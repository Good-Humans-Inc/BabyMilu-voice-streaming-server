# Next Starter Latency Smoke

## Status

This flow is not yet a first-class `tools/smoke/` scenario.

For now, the validated regression path remains the legacy e2e harness in `memory-worker`, while the shared smoke framework covers scheduled reminder and alarm scenarios.

## Why This Exists

The `next_starter` latency-hack path crosses repos:

- server repo owns websocket playback and consume behavior
- `memory-worker` owns pending-session processing and starter generation

That makes it a real regression risk, but also means it is not yet a pure server-only scenario.

## Current Regression Entry Point

Run:

```bash
/Users/xueyinghuang/Desktop/babymilu agents/memory-worker/.venv/bin/python \
  /Users/xueyinghuang/Desktop/babymilu agents/memory-worker/scripts/run_next_starter_e2e.py \
  --ws-url <WS_URL> \
  --device-id <TEST_DEVICE_ID> \
  --ssh-host <SSH_HOST>
```

Reference doc:

- `/Users/xueyinghuang/Desktop/babymilu agents/memory-worker/docs/next_starter_e2e_harness.md`

Use a local-only env file or shell exports for the real values. Do not commit VM URLs, test device ids, or SSH host aliases into the repo.

## What It Verifies

1. first conversation creates session and turns
2. local `memory-worker` processes the pending session
3. `character_memory_model.next_starter` becomes `ready`
4. second conversation plays the starter on `listen start`
5. starter becomes `consumed`
6. server logs show load + playback lines

## Convincing Evidence

Do not call this flow passing unless you have at least:

- session id and character id
- generated `next_starter` payload
- consumed payload after second session
- server log proof of load + playback

## Migration Target

Long-term, this should become a proper shared smoke scenario such as:

- `memory.write_then_recall`
- `interaction.next_starter`

inside `tools/smoke/harness/scenarios/`.
