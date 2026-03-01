# PRD: Memory V1 — Multi-Modal Memory for BabyMilu

**Author:** Engineering  
**Sprint:** 1 week  
**Status:** Draft  
**Date:** 2026-02-28

---

## 0. Why Now / Problem Statement

`mem_local_short` stores a single ~900-char JSON blob per role, regenerated from scratch every conversation. It has no concept of the user as a person across sessions, no way to incorporate photo/audio/reminder/drawing data, and the 900-char cap means important memories get evicted after a few conversations. Milu should know a child the way a real friend does — what they looked like in their last photo, what story they told yesterday, what reminder they set.

---

## 1. Architecture Overview

```
┌───────────────┐   WS connect    ┌─────────────────────┐
│  Device/App   │ ──────────────▸ │  ConnectionHandler   │
└───────────────┘                 │                      │
                                  │  1. Load memory once │◂── Firestore read
                                  │     (session start)  │    users/{uid}/memory
                                  │                      │
                                  │  2. Inject into LLM  │
                                  │     as <memory> tag  │
                                  │                      │
                                  │  3. Conversation...  │
                                  │                      │
                                  │  4. WS close         │
                                  └──────────┬───────────┘
                                             │
                              ┌──────────────▼──────────────┐
                              │    Memory Worker (async)     │
                              │  • Extract facts from convo  │
                              │  • Store raw event           │
                              │  • Re-synthesize profile     │
                              └──────────────┬───────────────┘
                                             │
                              ┌──────────────▼──────────────┐
                              │  Daily Weaver (Cloud Sched)  │
                              │  • Cross-modal synthesis     │
                              │  • Profile consolidation     │
                              │  • Contradiction resolution  │
                              └─────────────────────────────┘
```

---

## 2. Firestore Schema

### Identifiers
- **userId** = `owner_phone` (e.g. `+14155551234`) — already the canonical user key in our Firestore `users/` collection.
- **characterId** = Firestore `characters/{characterId}` — the child persona.

Memory is **per-user** (the parent/owner), not per-device or per-character.

### 2.1 Raw Events (append-only log)

```
users/{userId}/memoryEvents/{eventId}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Auto-generated Firestore doc ID |
| `type` | string | `"conversation"` · `"photo"` · `"audio"` · `"reminder"` · `"drawing"` |
| `createdAt` | timestamp | When the event occurred |
| `deviceId` | string | Which device generated this |
| `characterId` | string | Which character was active |
| `sessionId` | string | WS session ID (null for app-originated events) |
| `rawText` | string | Full conversation transcript / reminder text / drawing prompt |
| `summary` | string | LLM-generated 1-2 sentence summary of this event |
| `extractedFacts` | string[] | Key facts pulled out (e.g. `"Likes dinosaurs"`, `"Has a pet hamster named Biscuit"`) |
| `mediaUrl` | string? | GCS URL for photo/audio blob (if applicable) |
| `mediaDescription` | string? | VLM/ASR-generated description of the media |
| `emotionTone` | string? | Detected emotional tone: `"happy"`, `"sad"`, `"excited"`, `"neutral"` |
| `metadata` | map | Flexible bag: `{duration_sec, turn_count, photo_objects: [...], ...}` |

**Indexes needed:**
- `(userId, createdAt DESC)` — for loading recent events
- `(userId, type, createdAt DESC)` — for filtering by modality

### 2.2 Synthesized Memory Profile (single document, LLM-maintained)

```
users/{userId}/memory/profile
```

This is the **one document** that gets loaded at session start and injected into the LLM. It's a human-readable narrative, not a JSON blob of compressed symbols.

| Field | Type | Description |
|-------|------|-------------|
| `identity` | map | `{name, age, pronouns, nickname, personality_traits: [...]}` |
| `world` | map | `{family_members: [...], pets: [...], school, friends: [...], home_city}` |
| `interests` | string[] | `["dinosaurs", "space", "baking cookies with mom"]` |
| `recentHighlights` | string | 3-5 sentence narrative of what's happened in the last ~7 days. Written in natural language. |
| `emotionalState` | string | Current read on the child's emotional state + what to be sensitive about |
| `activeThreads` | string[] | Open conversational threads to follow up on (e.g. `"Asked about getting a puppy — parents haven't decided yet"`) |
| `reminders` | string[] | Active reminders the child has set |
| `photoMemories` | string[] | Recent photo descriptions that are worth referencing (e.g. `"2/27: Drew a rainbow cat with cosmic draw"`) |
| `longTermFacts` | string[] | Durable facts that rarely change: `"Birthday is March 15"`, `"Allergic to peanuts"` |
| `lastUpdated` | timestamp | When this profile was last synthesized |
| `version` | int | Monotonically increasing; guards against race conditions |

**Why a narrative profile instead of embeddings/vectors?**
- Our user base is small → no need for vector search yet. A well-written ~2000-token profile is far more useful to the LLM than top-k vector hits.
- Human-readable = debuggable. We can read a child's memory profile and immediately tell if it's good.
- Fits naturally into the existing `<memory>` injection in `connection.py`.
- We can always add vector search later as a retrieval layer on top of raw events.

### 2.3 Schema Summary Diagram

```
Firestore
├── users/{phone}                    ← existing (displayName, birthday, city, etc.)
│   ├── memoryEvents/{eventId}       ← NEW: append-only raw events
│   └── memory/
│       └── profile                  ← NEW: synthesized memory profile
├── devices/{mac}                    ← existing  
├── characters/{charId}              ← existing
└── sessionContexts/{deviceId}       ← existing
```

---

## 3. Memory Provider Implementation

### 3.1 New Provider: `mem_firestore_v1`

Replaces `mem_local_short` in config. Implements `MemoryProviderBase`.

```python
class MemoryProvider(MemoryProviderBase):
    """
    V1 Firestore-backed memory.
    - query_memory(): reads users/{uid}/memory/profile once per session
    - save_memory(): kicks off async post-conversation worker
    """
```

**`query_memory(query)` → str**
1. Read `users/{userId}/memory/profile` from Firestore (single doc read, ~50-100ms)
2. Format the profile fields into a clean `<memory>` block:
   ```
   # What I know about {name}
   {identity summary}
   
   ## Their world
   {family, pets, friends, school}
   
   ## What they love
   {interests}
   
   ## Recent highlights  
   {recentHighlights narrative}
   
   ## Open threads
   {activeThreads}
   
   ## Things to remember
   {longTermFacts}
   ```
3. Cache in-memory for the session duration (avoid re-reads).
4. Return the formatted string.

**`save_memory(msgs)` → None**
1. Build the full conversation transcript from `msgs`.
2. Fire-and-forget to the **Memory Worker** (see §4).
3. Return immediately (non-blocking).

### 3.2 Session Lifecycle Integration

In `ConnectionHandler`:

```
handle_connection():
    ...existing auth/setup...
    
    # MEMORY: Load profile once at session start
    # (already happens in chat() via query_memory — no change needed)
    
    ...conversation loop...

_save_and_close():
    ...existing logic...
    
    # MEMORY: Trigger async worker (replaces current memory.save_memory call)
    # Worker receives: dialogue transcript, device_id, session_id, character_id
```

No per-turn retrieval. No latency impact during conversation.

---

## 4. Memory Worker (Post-Conversation)

### 4.1 Trigger
Called from `_save_and_close()` in a background thread (same pattern as existing `save_memory_task`).

### 4.2 Pipeline

```
Step 1: BUILD TRANSCRIPT
  └─ From dialogue messages, build a clean "User said X / Milu said Y" transcript

Step 2: EXTRACT (LLM call #1)
  └─ Prompt: "Given this conversation, extract:
      - A 1-2 sentence summary
      - Key facts about the user (new or updated)
      - Emotional tone
      - Any open threads / follow-ups"
  └─ Output: structured JSON

Step 3: STORE RAW EVENT
  └─ Write to users/{uid}/memoryEvents/{auto-id}
      type: "conversation"
      rawText: full transcript
      summary: from Step 2
      extractedFacts: from Step 2
      emotionTone: from Step 2
      sessionId, deviceId, characterId, createdAt

Step 4: RE-SYNTHESIZE PROFILE (LLM call #2)
  └─ Fetch current profile doc
  └─ Fetch last ~20 raw events (all types) for context
  └─ Prompt: "Here is the existing memory profile for {name}.
      Here are the recent events since last update.
      Please produce an updated memory profile.
      Rules:
      - Preserve all long-term facts unless explicitly contradicted
      - Update recentHighlights to cover the last ~7 days
      - Add any new facts from conversations
      - Incorporate photo descriptions and reminders
      - Keep total profile under 2000 tokens
      - Write in natural, warm language"
  └─ Write updated profile doc with version bump

Step 5: CLEANUP (optional)
  └─ Archive events older than 90 days to a cold subcollection
```

### 4.3 Error Handling
- If LLM extraction fails → still store the raw event (transcript), skip profile re-synthesis
- If Firestore write fails → retry with exponential backoff (3 attempts)
- If profile re-synthesis produces garbage → keep old profile (compare version, don't overwrite)

---

## 5. Multi-Modal Event Ingestion

Each modality writes to the **same** `memoryEvents` collection. The Memory Worker or a lightweight Cloud Function processes them.

### 5.1 Conversation Text (from WS session)
- **When:** `_save_and_close()` fires
- **What:** Full transcript + LLM-extracted summary/facts
- **How:** Memory Worker (§4)

### 5.2 Magic Camera Photos (from Vision API)
- **When:** `VisionHandler.handle_post()` returns a response
- **What:** Photo description (already generated by VLM) + GCS URL for the image
- **How:** Add 3 lines to `vision_handler.py` after the VLM response:
  ```python
  # After getting VLM result, store as memory event
  owner_phone = get_owner_phone_for_device(device_id)
  if owner_phone:
      store_memory_event(owner_phone, type="photo",
          summary=result, media_description=result,
          media_url=image_gcs_url, device_id=device_id)
  ```

### 5.3 Audio (raw WAV from ASR)
- **When:** ASR processes speech
- **What:** We do NOT store raw audio as memory events. The conversation transcript covers this. Audio is an *input modality*, not a *memory modality*.
- **Exception:** If we ever add "voice journal" or "audio message" features, those would be stored.

### 5.4 Reminders (from Alarms Service)
- **When:** A reminder is created or fires
- **What:** Reminder label/description + scheduled time
- **How:** The alarm creation path already writes to Firestore. Add a parallel write to `memoryEvents`:
  ```python
  store_memory_event(owner_phone, type="reminder",
      summary=f"Set reminder: {label} at {time}",
      metadata={"alarm_id": alarm_id, "scheduled_for": next_occurrence})
  ```

### 5.5 Cosmic Draw (from App)
- **When:** App completes a cosmic draw and gets a result
- **What:** The draw prompt + result text
- **How:** App writes to Firestore directly (or via Cloud Function). Schema:
  ```python
  store_memory_event(owner_phone, type="drawing",
      summary=f"Drew: {prompt}. Result: {result_text}",
      media_url=drawing_image_url)
  ```

### 5.6 Helper Function

```python
def store_memory_event(
    user_id: str,
    type: str,
    summary: str,
    raw_text: str = None,
    extracted_facts: list = None,
    media_url: str = None,
    media_description: str = None,
    emotion_tone: str = None,
    device_id: str = None,
    character_id: str = None,
    session_id: str = None,
    metadata: dict = None,
) -> str:
    """Write a single memory event to Firestore. Returns the event doc ID."""
    client = _build_client()
    doc_ref = client.collection("users").document(user_id) \
                    .collection("memoryEvents").document()
    doc_ref.set({
        "type": type,
        "createdAt": firestore.SERVER_TIMESTAMP,
        "summary": summary,
        "rawText": raw_text,
        "extractedFacts": extracted_facts or [],
        "mediaUrl": media_url,
        "mediaDescription": media_description,
        "emotionTone": emotion_tone,
        "deviceId": device_id,
        "characterId": character_id,
        "sessionId": session_id,
        "metadata": metadata or {},
    })
    return doc_ref.id
```

---

## 6. Daily Weaver (Background Consolidation)

### 6.1 Purpose
The post-conversation worker updates the profile after each conversation. But cross-modal patterns (e.g., "took a photo of their hamster + talked about hamster in 3 separate conversations") emerge over time. The Daily Weaver catches these.

### 6.2 Implementation
- **Trigger:** Cloud Scheduler → Cloud Function, runs once daily at 3 AM PST
- **For each active user** (had events in last 7 days):
  1. Fetch all events from last 7 days
  2. Fetch current profile
  3. LLM call: "Synthesize an updated profile from these events + existing profile. Focus on cross-modal connections, emerging patterns, and evolving emotional state."
  4. Write updated profile

### 6.3 V1 Scope
**Week 1: Skip the Daily Weaver.** The post-conversation worker (§4) already re-synthesizes the profile after each conversation. The Daily Weaver is a polish step for Week 2.

**Week 1 alternative:** If a conversation happens and the profile hasn't been updated in >24h, the post-conversation worker also pulls in non-conversation events (photos, reminders, drawings) during its re-synthesis step. This gives us 80% of the weaver benefit with zero new infrastructure.

---

## 7. Implementation Plan (1 Week)

| Day | Deliverable | Details |
|-----|-------------|---------|
| **Day 1** | Firestore schema + `store_memory_event()` | Create the `memoryEvents` subcollection, write the helper function, add Firestore indexes |
| **Day 2** | `mem_firestore_v1` provider: `query_memory()` | Read profile doc, format into memory string, cache for session. Wire into config. |
| **Day 3** | Memory Worker: conversation extraction pipeline | LLM-based fact extraction from conversation transcript. Store raw event. |
| **Day 4** | Memory Worker: profile re-synthesis | Fetch recent events + current profile → LLM re-synthesis → write updated profile |
| **Day 5** | Multi-modal ingestion hooks | Add `store_memory_event()` calls to: `vision_handler.py` (photos), alarm creation (reminders). Add app API endpoint for cosmic draw events. |
| **Day 6** | Integration testing + prompt tuning | End-to-end test: conversation → memory event → profile update → next conversation loads updated profile. Tune extraction + synthesis prompts. |
| **Day 7** | Buffer / polish | Edge cases, error handling, profile formatting improvements, seed initial profiles for existing users |

---

## 8. Migration from `mem_local_short`

1. **Config change:** Switch `selected_module.Memory` from `mem_local_short` to `mem_firestore_v1`
2. **Seed profiles:** For existing users, run a one-time script that:
   - Reads their existing `mem_local_short` JSON blob
   - Converts it to the new profile format via LLM
   - Writes to `users/{uid}/memory/profile`
3. **Fallback:** If `memory/profile` doc doesn't exist yet, `query_memory()` returns empty string (same as new user)

---

## 9. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Narrative profile over vector embeddings** | Small user base, LLM reads text better than reassembled vector chunks, debuggable, fits existing `<memory>` injection |
| **Per-user memory, not per-character** | A family's memories belong to the family. The character is just the voice/persona. |
| **Append-only raw events + synthesized profile** | Raw events = audit trail + training data. Profile = what the LLM actually sees. Two concerns, cleanly separated. |
| **No vector DB in V1** | Premature optimization. When we have 10k+ events per user, we add a retrieval layer. Not now. |
| **Profile re-synthesis after every conversation** | Costs ~1 extra LLM call per session. We said credits aren't a concern. Keeps profile fresh. |
| **Skip Daily Weaver in Week 1** | Post-conversation worker gives us 80% of the value. Add the weaver in Week 2 when we've validated the profile format. |

---

## 10. Prompt Templates (Draft)

### 10.1 Fact Extraction Prompt

```
You are a memory assistant for BabyMilu, a children's AI companion.

Given this conversation between a child and Milu, extract:
1. **summary**: A 1-2 sentence summary of what happened
2. **facts**: A list of key facts about the child (things worth remembering long-term)
3. **emotion**: The overall emotional tone (happy/sad/excited/anxious/neutral/mixed)
4. **threads**: Any open conversational threads to follow up on next time

Conversation:
{transcript}

Respond in JSON:
{
  "summary": "...",
  "facts": ["...", "..."],
  "emotion": "...",
  "threads": ["...", "..."]
}
```

### 10.2 Profile Synthesis Prompt

```
You are maintaining a memory profile for BabyMilu, a children's AI companion.

Here is the CURRENT memory profile:
{current_profile}

Here are RECENT EVENTS since the last update:
{recent_events}

Please produce an UPDATED memory profile. Rules:
- Keep all long-term facts unless explicitly contradicted by new events
- Update "recentHighlights" to reflect the most interesting things from the last ~7 days
- Add any new interests, facts, or people mentioned
- Incorporate photo descriptions and reminders naturally
- If a fact has changed (e.g. "favorite color was blue, now says green"), update it
- Write in warm, natural language — this will be read by an AI talking to a child
- Keep total length under 2000 tokens
- Return as JSON matching this schema:
{profile_schema}
```

---

## 11. Success Metrics

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Session load latency | < 200ms | Timer around `query_memory()` |
| Profile freshness | Updated within 60s of session end | `lastUpdated` timestamp vs session end |
| Memory accuracy | Milu correctly references past events in conversation | Manual QA: 10 test conversations |
| Multi-modal coverage | Photos + reminders appear in profile within 1 conversation cycle | Check profile after photo upload + conversation |

---

## 12. Future (Week 2+)

- **Daily Weaver** as a Cloud Function for cross-modal pattern detection
- **Vector retrieval layer** on `memoryEvents` for when raw events exceed what fits in context
- **Memory sharing** across characters (same family)
- **User-facing memory UI** in the app ("Here's what Milu remembers about you")
- **Forgetting**: User can ask Milu to forget specific things → soft-delete events, re-synthesize profile
