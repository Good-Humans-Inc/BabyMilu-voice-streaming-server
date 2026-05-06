# Magic Camera Photo Tool Call TDD

## Purpose

This document defines the V1 behavior, conversation contract, and evaluation
plan for the `inspect_recent_magic_camera_photo` tool call.

The goals are:

- reliably trigger the tool when a user wants BabyMilu to check a recent Magic
  Camera photo
- recover gracefully when no recent photo exists or the tool fails
- produce grounded, concrete, encouraging replies that keep the conversation
  going
- provide a repeatable eval plan so we can validate the feature before manual
  plushie testing

## Scope

This TDD covers:

- tool-call routing for recent Magic Camera photo requests
- Firestore lookup of the user’s latest recent photo metadata
- image inspection and structured analysis payload generation
- assistant response policy for `found`, `no_match`, and `error`
- unit tests and smoke/eval coverage for the end-to-end behavior

This TDD does not cover:

- older memory pipelines beyond selecting the latest recent photo
- image generation or editing
- app UI flows for taking or uploading the photo
- long-term ranking of multiple historical photos

## V1 User Promise

When a user asks BabyMilu to check a recently taken Magic Camera photo,
BabyMilu should try to inspect the latest recent photo instead of asking the
user to describe it from scratch.

If the photo is available, BabyMilu should respond with grounded observations.
If it is not available yet, or the inspection fails, BabyMilu should guide the
user toward the next step in a warm, concrete way.

## Current Runtime Contract

### Tool Name

- `inspect_recent_magic_camera_photo`

### Current implementation files

- Tool implementation:
  [inspect_recent_magic_camera_photo.py](/Users/yan/Desktop/BabyMilu/.worktrees/magic-camera-photo-lookup/main/xiaozhi-server/plugins_func/functions/inspect_recent_magic_camera_photo.py)
- Prompt wiring:
  [agent-base-prompt.txt](/Users/yan/Desktop/BabyMilu/.worktrees/magic-camera-photo-lookup/main/xiaozhi-server/agent-base-prompt.txt)
- Focused tests:
  [test_inspect_recent_magic_camera_photo.py](/Users/yan/Desktop/BabyMilu/.worktrees/magic-camera-photo-lookup/main/xiaozhi-server/tests/test_inspect_recent_magic_camera_photo.py)
- Shared smoke scenario:
  [interaction.py](/Users/yan/Desktop/BabyMilu/.worktrees/magic-camera-photo-lookup/tools/smoke/harness/scenarios/interaction.py)

### Data lookup model

For staging and current dev testing, the tool resolves:

- `device_id -> ownerPhone`
- `uid = ownerPhone`
- Firestore path: `users/{uid}/magicPhotos/{photoId}`

The Firestore document stores metadata plus image URLs. The image itself is not
stored inline in Firestore.

### Recent-photo selection rules

V1 selects the newest photo that is:

- within the recency window
- not deleted
- backed by a usable image URL

Current URL preference order:

1. `photoUrl`
2. `processedPhotoUrl`
3. `cardUrl`

Current recency window:

- `24 hours`

## Conversation Contract

The conversation behavior should be evaluated in three buckets:

1. Tool routing
2. Failure recovery
3. Response quality after success

### 1. Tool Routing

BabyMilu should call `inspect_recent_magic_camera_photo` when the user is
clearly asking about a recent Magic Camera photo, including direct and indirect
follow-ups.

Examples that should usually trigger the tool:

- “Can you check the photo I just sent you?”
- “What do you see in my Magic Camera picture?”
- “React to the photo I just took.”
- “I just took another one.”
- “I just sent it, can you check?”
- “Can you look at the Magic Camera photo I just took?”

Examples that should not trigger the tool by default:

- generic art chat without a recent-photo request
- imagination or roleplay prompts with no claim that a photo was taken
- discussion of an old memory or photo unless the user asks to check the recent
  Magic Camera photo

### 2. Failure Recovery

If the tool does not find a recent photo, BabyMilu should:

- say clearly that it could not find a recent Magic Camera photo
- ask the user to take another photo in Magic Camera
- explicitly say it can wait patiently
- encourage the user to come back once the new photo is taken

Target `no_match` tone:

- “I couldn’t find a recent Magic Camera photo yet. Take another one in the app
  and come back to me. I’ll patiently wait to see your masterpiece.”

If the tool errors, BabyMilu should:

- say that something glitched while checking the photo
- ask the user to retry or take another photo
- stay confident and helpful
- avoid falling back to “I can only imagine it” when the tool path was intended

Target `error` tone:

- “Something glitched while I was checking it. Try taking or sending another
  Magic Camera photo and I’ll look again.”

If the user says something like “I just took it” or “I sent another one,”
BabyMilu should confirm that it is ready to check again and say it will
patiently wait to see their masterpiece.

### 3. Response Quality After Success

If the tool returns `found`, BabyMilu should:

- mention `1-2` concrete, grounded visible details from the analysis
- add a warm, encouraging reaction
- ask one specific follow-up question that keeps the conversation moving

Target `found` reply pattern:

1. one or two grounded observations
2. one encouraging reaction
3. one specific follow-up question

Examples of good grounded response behavior:

- mention visible desk setup details instead of generic praise
- reference visible colors, objects, or text if the tool returned them
- ask something concrete like what the user was making, building, or trying to
  show

Examples of weak response behavior to avoid:

- “I can imagine it looks great”
- generic praise with no visible details
- long speculative interpretation not supported by the image
- no follow-up question

## Tool Result Contract

### `found`

Expected behavior:

- tool returns structured image-analysis payload
- assistant uses the payload to produce a grounded conversational response
- assistant should not pretend it failed to see the image

Expected minimum reply qualities:

- at least one grounded visible detail
- at least one encouraging phrase
- at least one forward-moving follow-up

### `no_match`

Expected behavior:

- tool reports that no recent qualifying photo exists in the recency window
- assistant asks the user to take a new Magic Camera photo
- assistant says it can patiently wait

Expected minimum reply qualities:

- clear explanation that no recent photo was found
- clear next action for the user
- supportive waiting language

### `error`

Expected behavior:

- tool reports that an inspection attempt failed
- assistant frames it as a glitch or temporary issue
- assistant asks for a retry or new photo

Expected minimum reply qualities:

- clear statement that the inspection hit a problem
- user guidance for the next step
- no fallback to “describe it to me” unless repeated failures make that the last
  resort

## Test Strategy

### Unit Coverage

Focused unit coverage lives in:

- [test_inspect_recent_magic_camera_photo.py](/Users/yan/Desktop/BabyMilu/.worktrees/magic-camera-photo-lookup/main/xiaozhi-server/tests/test_inspect_recent_magic_camera_photo.py)

Current unit-test areas include:

- recent photo selection
- deleted / missing-URL filtering
- URL preference order
- OpenAI client config fallback
- recovery from malformed JSON escape sequences
- `no_match` responses
- `found` responses

Additional unit coverage to add if needed:

- explicit `error` payload shaping when the image-analysis call fails
- routing helper coverage if prompt-to-tool heuristics move into code

### Shared Smoke Coverage

Shared smoke coverage should validate:

- the live runtime advertises `inspect_recent_magic_camera_photo`
- the LLM emits the tool call for a direct photo-check request
- the tool executes and returns one of `found`, `no_match`, or `error`
- the assistant does not fall back to “I can only imagine it” when the tool was
  expected

The smoke should be the gate before manual plushie testing.

## Eval Plan

### Eval Group 1: Tool Routing

Pass criteria:

- the tool is called for direct recent-photo requests
- the tool is called for short follow-ups after the user says they just took or
  sent another photo
- unrelated chat does not trigger the tool

Suggested routing eval prompts:

- “Can you check the photo I just sent you?”
- “What do you see in my recent Magic Camera photo?”
- “I just took another one.”
- “I just sent it. Can you check?”
- “React to the Magic Camera photo I just took.”
- negative control: “Do you like paintings?”

### Eval Group 2: Failure Recovery

Pass criteria for `no_match`:

- assistant clearly says no recent photo was found
- assistant asks the user to take another Magic Camera photo
- assistant says it can wait patiently

Pass criteria for `error`:

- assistant clearly says something glitched or failed
- assistant asks the user to retry or send/take another photo
- assistant avoids generic “I can’t see photos” fallback language

Suggested failure-recovery eval prompts:

- `no_match`: “Can you look at the photo I just took?” with no qualifying photo
  in the last `24 hours`
- `error`: force a tool failure, then ask “Can you check the photo I just sent?”
- retry follow-up: “Okay, I just took another one.”

### Eval Group 3: Response Quality After Success

Pass criteria:

- response includes at least one grounded visible detail
- response includes encouragement
- response includes one specific follow-up question
- response avoids unsupported speculation

Suggested success eval prompts:

- “What do you see in the photo I just took?”
- “Can you react to my Magic Camera picture?”
- “I just sent you a new one. Tell me what stands out.”

## Sample Trigger Library

These sample triggers should be used in smoke runs, prompt reviews, and manual
QA.

### Success / `found`

- “Can you check the Magic Camera photo I just took?”
- “What do you see in the photo I sent you?”
- “I just sent another one. Can you react to it?”
- “Tell me what stands out in my recent Magic Camera picture.”

### Missing Photo / `no_match`

- “Can you look at my photo?” when no Magic Camera photo exists in the last
  `24 hours`
- “I want you to check the picture I just took” when no recent Firestore photo
  exists yet
- follow-up target response should invite the user to take another photo and
  come back

### Tool Failure / `error`

- “Can you check the photo I just sent?” during a forced tool error
- “I just took another one, try again” after a prior inspection failure
- follow-up target response should acknowledge the glitch and guide the user
  into retrying

## Reviewer Notes

The main product risk is not only whether the tool runs, but whether BabyMilu
handles the awkward moments well:

- the first request should usually trigger the tool
- a missing photo should turn into clear, patient guidance
- a tool failure should sound temporary and recoverable
- a successful inspection should sound concrete and alive, not generic

This feature should be considered incomplete if it only works in happy-path
manual testing but lacks a repeatable smoke/eval path for `found`, `no_match`,
and `error`.
