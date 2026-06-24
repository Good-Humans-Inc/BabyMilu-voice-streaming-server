# VAD Lease Pool and Concurrency Notes

## Purpose

This document explains how PR #34 fixes the shared VAD provider issue and how
the related concurrency changes are meant to be operated. It is written as a
handoff note for engineers and LLM agents that need to understand the design
without re-reading the entire branch.

The short version:

- Silero VAD is no longer shared as one mutable provider across websocket
  connections.
- The server owns a bounded pool of VAD provider instances.
- A connection checks out a VAD provider only while it is actively processing
  user speech.
- The provider is returned at clear conversation boundaries.
- Blocking audio/provider work runs through bounded executors with explicit
  timeouts.

## Problem Fixed

Before this branch, the websocket server initialized a single VAD provider and
passed that same object into multiple `ConnectionHandler` instances. That was
unsafe for Silero for two reasons:

1. The Silero TorchScript model keeps mutable inference state.
2. The old provider also owned one Opus decoder, which is stream-stateful.

With concurrent devices, that meant one user's audio frames could influence
another user's VAD state. The observed risk was not just load pressure; it was a
correctness issue caused by shared mutable audio state.

Creating a fresh VAD model for every connection would avoid sharing, but it
would also waste memory because many websocket connections are idle most of the
time. This branch uses a lease pool instead: bounded memory, isolated active
inference state, and quick return when a user stops speaking.

## Implementation Overview

### Server-owned VAD pool

`main/xiaozhi-server/core/vad_pool.py` introduces `VadProviderPool`.

The pool:

- preloads `size` VAD provider instances
- blocks only up to a configured lease timeout
- tracks leased provider identities
- resets provider model state before reuse
- ignores unknown/double releases instead of corrupting pool state
- exposes `available` and `leased` counters for tests and diagnostics

`main/xiaozhi-server/core/websocket_server.py` now initializes:

- one `ServerExecutors` instance from config
- one `VadProviderPool` from config

The server passes the pool to each `ConnectionHandler` instead of passing one
shared VAD provider. On config refresh, VAD changes rebuild the pool on the
provider executor rather than on the event loop.

### Lazy per-connection leases

`main/xiaozhi-server/core/connection.py` treats the server-level VAD object as a
pool. Each connection starts with no checked-out provider. `run_vad()` acquires a
provider only when a frame needs VAD:

1. `handleAudioMessage()` submits `conn.run_vad(audio)` to the audio executor.
2. `run_vad()` calls `_acquire_vad_provider()` if `self.vad` is `None`.
3. `_acquire_vad_provider()` checks out one provider from the pool.
4. The provider remains attached to that connection across nearby frames.
5. Release helpers return it to the pool at speech or playback boundaries.

If the pool is empty during realtime audio processing, the acquire path uses the
short active timeout and treats that frame as silence. This avoids wedging the
websocket or letting VAD saturation become unbounded latency.

### Per-connection Opus decoder state

`main/xiaozhi-server/core/providers/vad/silero.py` no longer keeps one decoder
on the VAD provider. Instead, `_get_decoder(conn)` stores the decoder on the
connection as `_vad_opus_decoder`.

That matters because Opus decoding is stream-stateful. Even with a VAD provider
pool, provider instances can move between connections over time, so decoder
state must stay with the connection/audio stream and be removed when the VAD
lease is released.

### Release boundaries

The branch releases VAD leases at the points where a provider is no longer
needed for an active utterance:

- `listen` stop:
  - `core/handle/textHandler/listenMessageHandler.py`
  - releases after the stop frame is flushed through VAD/ASR handling
- ASR voice stop:
  - `core/providers/asr/base.py`
  - releases when the utterance transitions from frame collection to ASR work
- inactive silence:
  - `ConnectionHandler.release_inactive_vad_lease()`
  - releases after `idle_release_ms` when the connection is no longer hearing
    speech
- TTS start:
  - `core/handle/sendAudioHandle.py`
  - releases before server speech playback begins
- connection close:
  - `ConnectionHandler.close()`
  - waits briefly for in-flight VAD calls before returning the provider

This means VAD is held while it is useful for a live utterance, but idle sockets
do not reserve model instances.

### In-flight protection on close

`ConnectionHandler` tracks VAD calls with `_begin_vad_call()`,
`_end_vad_call()`, and `_wait_for_vad_calls_to_finish()`.

On close, the connection waits for audio executor work that is already inside
VAD. If that work does not drain within the bounded wait, the provider is not
returned to the pool. That is intentionally conservative: leaking one provider
under abnormal shutdown is preferable to returning an object while a worker may
still be mutating it.

### Bounded executor model

`main/xiaozhi-server/core/concurrency.py` adds:

- `BoundedThreadPoolExecutor`
- `DropOldestQueue`
- `ExecutorTimeouts`
- `ServerExecutors`

The old pattern allowed blocking provider work to happen directly or through
unbounded queues. Under many clients, that can convert one slow dependency into
process-wide memory pressure and event-loop stalls.

The new model gives each class of blocking work a named executor and timeout:

- `profile`
- `db`
- `provider`
- `tool`
- `audio`
- `persistence`

Audio-side blocking work, including VAD, Opus decoding, WAV conversion, ASR, and
voiceprint work, now uses the shared `audio` executor where possible. Queue
limits are explicit, and rejected work is surfaced instead of silently growing
backlog.

## Commit Progression

The VAD/concurrency work landed incrementally. The important commits are listed
oldest to newest.

### `8dbdd7ec` - Prevent ASR timeout from wedging server

This commit belongs to the same reliability thread:

- made ASR and voiceprint waits use awaitable futures with explicit timeouts
- cancels timed-out futures rather than leaving the connection waiting forever
- added HTTP timeout configuration for OpenAI ASR

This reduced the chance that a slow ASR dependency could pin a conversation
after VAD had already handed off an utterance.

### `d3c68a6f` - Move websocket heavy work off event loop

This laid the concurrency foundation:

- introduced `core.concurrency`
- added bounded executors and queue limits
- routed blocking connection/provider/tool/audio work away from the websocket
  event loop
- added timeout handling around executor calls
- added websocket concurrency contract tests

This commit reduced event-loop stalls but did not by itself fully solve shared
VAD state.

### `97dbe9d2` - Isolate VAD Opus decoder per connection

This removed provider-owned decoder state from Silero VAD:

- deleted the provider-level `self.decoder`
- added per-connection `_vad_opus_decoder`
- added reset cleanup coverage

This fixed one part of the shared-state problem: Opus stream state now follows
the connection rather than the provider object.

### `cfb5514b` - Lease VAD providers per connection

This introduced the pool itself:

- added `core.vad_pool.VadProviderPool`
- changed websocket startup to create a server-level VAD pool
- changed connections to receive the pool rather than one shared provider
- added tests for pool leasing, reset, config parsing, and connection behavior

At this point active connections could lease distinct VAD providers instead of
mutating the same shared Silero model.

### `c657005e` - Make local smoke tests runnable

This made the new tests easier to run locally:

- added test scaffolding for optional native dependencies
- added a default event-loop fixture
- let the VAD/concurrency tests run without a full production dependency stack

This matters because the shared VAD fix is mostly concurrency behavior. Keeping
the focused tests runnable locally makes future regressions much easier to catch.

### `73bb1470` - Expose VAD concurrency settings

This added configuration for the pool and executor relationship:

- `concurrency.vad_pool.size`
- `concurrency.vad_pool.lease_timeout`
- executor sizing comments
- timeout defaults for executor categories

This made VAD concurrency an operational setting instead of a hard-coded code
path.

### `e714b098` - Lease VAD during active utterances

This tightened lease lifetime:

- connections acquire a VAD provider lazily during `run_vad()`
- idle sockets no longer hold VAD providers
- inactive silence can release a lease after `idle_release_ms`
- ASR provider paths release leases after voice-stop transitions
- additional tests cover empty-pool behavior and idle release

This is the commit that changed the model from "per connection" toward "per
active utterance", which is much better for many connected but mostly idle
devices.

### `43fc5a1a` - Tune staging concurrency timeouts

This adjusted staging defaults based on the expected deployment shape:

- increased provider/tool/profile/db/persistence timeout ceilings
- kept audio bounded
- aligned queue/worker values for realistic bursts
- raised VAD pool/audio-worker defaults for staging load
- extended FishAudio HTTP timeout values for slower downstream synthesis calls

This is operational tuning, not a semantic change to VAD leasing.

### `a72f0ad3` - Release VAD lease at listen and TTS boundaries

This added two important release points:

- after client `listen` stop handling finishes
- before server TTS playback starts

These boundaries prevent a connection from holding a VAD provider through a
period where the device is not actively sending user speech.

### `2f3fd7d6` - Pin CPU PyTorch and sherpa onnx

This pinned runtime dependencies used by the server-side audio stack. The goal
is reproducibility for CPU deployments and less drift in VAD/ASR runtime
behavior.

## Runtime Sequence

The normal audio path now looks like this:

1. Device sends an audio frame over websocket.
2. `handleAudioMessage()` submits VAD work to the `audio` executor.
3. `ConnectionHandler.run_vad()` acquires a VAD provider if needed.
4. Silero VAD runs with provider-local model state and connection-local decoder
   state.
5. ASR receives the frame and updates utterance state.
6. If speech stops, ASR copies the utterance, clears the frame buffer, resets
   connection VAD state, and releases the VAD provider.
7. ASR filters empty, non-English, and low-signal fragments before a transcript
   can start an LLM turn. Non-empty fuzzy transcripts can trigger a short repeat
   prompt instead of being treated as user intent.
8. If TTS starts, the send path releases any remaining VAD lease before marking
   the server as speaking.
9. On every top-level user turn, the active character binding is refreshed so
   connected devices can pick up profile/voice changes without reconnecting.
10. If the connection closes, close waits for in-flight VAD work before returning
   the provider.

The key invariant is:

```text
No two active connections should use the same leased Silero provider instance at
the same time.
```

A provider may be reused by another connection later, but only after release and
reset.

## ASR Transcript Gate

`ASRProviderBase` owns the last gate before an ASR transcript becomes an LLM
turn. The gate rejects:

- empty transcripts after punctuation stripping
- single-character fragments
- non-English fragments with no ASCII letters when `reject_non_english_fragments`
  is enabled
- transcripts with non-ASCII letters when the runtime is configured for English
  voice interaction
- configurable low-signal fragments such as `hmm`, `uh`, `you`, or `empty` when
  the captured audio is shorter than `low_signal_fragment_max_audio_seconds`
- configurable ambiguous short fragments such as function words (`the`, `and`,
  `so`) or repair openers (`i said`, `i mean`, `you know`) when the captured
  audio is shorter than
  `ambiguous_short_fragment_max_audio_seconds`

For rejected non-empty fuzzy transcripts, the server may speak
`unclear_asr_prompt` directly through TTS. This response is not inserted into the
dialogue history and does not call the LLM.

Relevant ASR config keys:

```yaml
reject_non_english_fragments: true
reject_low_signal_fragments: true
reject_ambiguous_short_fragments: true
low_signal_fragment_max_audio_seconds: 1.2
ambiguous_short_fragment_max_audio_seconds: 0.7
low_signal_fragments: ["hmm", "uh", "you", "empty"]
ambiguous_short_fragments: ["the", "and", "so", "i said", "i mean", "you know"]
speak_on_unclear_asr: true
unclear_asr_prompt: "I didn't catch that clearly. Can you say it again?"
unclear_asr_prompt_cooldown_seconds: 4.0
```

## Configuration Guide

Current branch defaults in `main/xiaozhi-server/config.yaml`:

```yaml
concurrency:
  vad_pool:
    size: 20
    lease_timeout: 20.0
    active_acquire_timeout: 0.05
    idle_release_ms: 1200
  executors:
    audio:
      max_workers: 28
      max_queue_size: 1000
  timeouts:
    profile: 30.0
    db: 20.0
    provider: 120.0
    tool: 45.0
    audio: 15.0
    persistence: 20.0
    bootstrap_text_wait: 30.0
```

### Effective VAD concurrency

The practical number of simultaneous VAD lanes is:

```text
min(concurrency.vad_pool.size, concurrency.executors.audio.max_workers)
```

`vad_pool.size` limits model instances. `audio.max_workers` limits simultaneous
audio jobs. Increasing only one side may not increase VAD throughput.

### `concurrency.vad_pool.size`

Use this for the number of simultaneous active speakers the process should
support.

Guidance:

- start near expected active speakers, not total connected devices
- keep at or below `audio.max_workers` unless extra preloaded idle models are
  intentional
- increase only after confirming memory headroom

Each slot owns a VAD provider/model instance, so this is a memory and startup
cost.

### `concurrency.vad_pool.lease_timeout`

This is the default blocking wait used by the pool when callers do not provide a
more specific timeout.

Guidance:

- keep it finite
- keep it higher than `active_acquire_timeout`
- treat it as a safety ceiling for non-realtime paths, not the realtime frame
  latency budget

Provider-level overrides are also supported through the selected VAD module
config:

```yaml
VAD:
  SileroVAD:
    pool_size: 20
    lease_timeout: 20.0
```

The pool reads provider-level values first, then falls back to
`concurrency.vad_pool.size` and `concurrency.vad_pool.lease_timeout`.

### `concurrency.vad_pool.active_acquire_timeout`

This is the short wait used by active frame VAD acquisition.

Guidance:

- keep it small for realtime interaction
- `0.05` seconds means an exhausted pool degrades the frame to silence quickly
- increasing this improves the chance of acquiring a provider under load but
  directly adds possible audio latency

### `concurrency.vad_pool.idle_release_ms`

This controls how long a connection may keep a VAD lease after silence begins.

Guidance:

- use a value long enough to span natural speech pauses
- use a value short enough to return providers between turns
- `1200` ms is a reasonable starting point for conversational speech

### `concurrency.executors.audio.max_workers`

This controls parallel audio-side blocking jobs.

Guidance:

- keep it at or above `vad_pool.size` when VAD throughput is the bottleneck
- allow some headroom for Opus decode, WAV conversion, ASR, and voiceprint work
- avoid setting it so high that CPU contention creates worse tail latency

### `concurrency.executors.audio.max_queue_size`

This controls burst buffering for audio executor submissions.

Guidance:

- larger queues absorb bursts but can hide overload and consume memory
- smaller queues expose saturation sooner
- the important improvement is that the queue is bounded at all

### `concurrency.timeouts.audio`

This caps audio executor waits.

Guidance:

- keep it finite so stuck audio work does not pin a connection forever
- make it long enough for expected ASR/voiceprint work
- keep VAD active acquisition much shorter than this value

## Why The Shared VAD Issue No Longer Exists

The old unsafe shape was:

```text
server._vad = one Silero provider
connection A.vad = server._vad
connection B.vad = server._vad
```

The new normal shape is:

```text
server._vad_pool = VadProviderPool(size=N)
connection A.vad = provider leased from pool
connection B.vad = different provider leased from pool
idle connection C.vad = None
```

The fix is not a single guard. It is the combination of:

- pool identity tracking
- lazy acquisition
- bounded acquire waits
- per-connection decoder state
- provider reset before reuse
- explicit release points
- close-time in-flight protection
- tests that assert pool, decoder, and lease behavior

Because each active VAD call uses a leased provider and each provider is removed
from the available queue while leased, two active connections should not share
the same Silero model instance.

## Validation

Focused validation used for PR #34:

```text
python -m pytest tests/test_vad_provider_pool.py tests/test_websocket_concurrency_contracts.py
```

Result:

```text
15 passed
```

The test coverage includes:

- distinct provider leases
- pool blocking/timeout when empty
- provider reset before reuse
- selected VAD config parsing
- server-level pool injection into connections
- lazy acquisition rather than bootstrap acquisition
- empty pool treated as silence
- idle silence release
- short utterance release
- listen/TTS boundary release
- per-connection VAD Opus decoder isolation
- executor timeout/rejection behavior
- bounded early-audio queue behavior

Additional boundary-focused tests cover:

- release on `listen:stop` in `tests/test_listen_next_starter.py`
- release before first TTS playback in `tests/test_send_audio_handle.py`

## Notes For Future Agents

When modifying this area, preserve these invariants:

- do not store a provider-level Opus decoder in Silero VAD
- do not pass one live VAD provider to all connections
- do not acquire a VAD lease during connection bootstrap unless the connection is
  actually processing audio
- always release a pool-backed provider through `release_vad_lease()` or
  `release_inactive_vad_lease()`
- keep realtime VAD acquire waits short
- keep executor queues bounded
- keep close-time in-flight protection conservative

If you need to change pool size or timeouts, update `config.yaml` and rerun the
focused VAD/concurrency tests before relying on staging behavior.
