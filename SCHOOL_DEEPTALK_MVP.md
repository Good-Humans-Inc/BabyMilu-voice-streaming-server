# School 文化课 — Deep-Talk Manual MVP

A manual MVP for the **School "Take a Lesson" (文化课)** experience: the user sends BabyMilu
to "study" a topic, and it comes home and has a **grounded, proactive deep talk** about it —
the signature payoff in the School PRD.

This doc covers: what was built, how it maps to the PRD deliverable, the regular-vs-deep-talk
change, the eval, the results (with the study-model A/B), and pricing.

---

## TL;DR / Verdict

- ✅ **The experience delta is real.** A studied deep talk decisively beats baseline chat,
  **especially for low-effort ("passive") users** — exactly where it matters.
- ⭐ **Use `gpt-4o-mini-search-preview` for the study step**, *not* the bigger `gpt-4o-search-preview`.
  Surprisingly, the mini search model wins more often **and** is far more factually accurate;
  the big model hallucinates much more. It's also cheaper.
- 💸 **Cost is negligible** — the study runs **once per lesson** (~3¢). Deep-talk turns use
  `gpt-4o-mini` with no per-turn search (fractions of a cent).
- ⚠️ Out-of-lesson follow-ups can still hallucinate (responder is `gpt-4o-mini`, no live search);
  mitigated by an honesty guardrail, not eliminated.

---

## 1. What this delivers vs. the PRD

Maps to the PRD **V0 — Take a Lesson** deliverable ("a manual MVP for the 文化课 experience").

**Built (the experience core):**
- Topic → **小宝学习** (web-search-grounded study) → lesson artifact → injected into the
  system prompt → **proactive opener** → `lesson_ready` signal to the frontend → deep talk.
- Frontend-driven **A/B** (control = no lesson, treatment = lesson) from the test page.
- A repeatable **eval harness** scoring the experience.

**Deferred by design for this validation round** (collapse the plumbing, prove the experience):
- Payment / 150 Lumis, plushie gating.
- Server-side 1h "in class" + 5h active-window timers — collapsed to "study now". (The PRD's
  1-hour window is the latency budget for study; we run it synchronously for the MVP.)
- Moments generation.
- Character-memory **write-through** (lesson persists past the session) — currently per-session only.

---

## 2. Regular conversation vs. deep conversation — what changed

The core design question. The answer is **context injection, not a tool**:

| | Regular chat | Deep talk (after a lesson) |
|---|---|---|
| Knowledge | model training memory only | **+ a web-searched lesson artifact** injected into the system prompt |
| Initiative | reactive (waits for user) | **proactive opener**, leads the topic |
| Mode | always-on companion (brief, leaves space) | **bounded "deep-talk mode" override**: lead more, bring substance every turn |
| Persistence | none | lesson is in-session (write-through deferred) |
| Honesty | — | **guardrail**: don't state guessed specifics; don't blindly agree when corrected |

Why context-injection over a dedicated `recall_lesson` tool: zero per-turn latency, guaranteed
grounding, simpler. A tool is the V1 upgrade only if conversations exhaust the fixed artifact.

---

## 3. How it works (data flow)

```
frontend hello payload  {"lesson": {"topic": "BLACKPINK"}}
   → server: 小宝学习 study (web-search model)  ── once per lesson
   → artifact { key_facts, milu_likes, insider_bits, questions_for_user, opener }
   → inject "# Today's lesson" block into the system prompt (idempotent; survives re-renders)
   → send {"type":"lesson_ready"} to frontend  ("🏫 宝学完回来了")
   → proactive opener (Milu speaks first about the topic)
   → deep-talk turns on gpt-4o-mini, artifact in context (NO per-turn search)
```

Key files:
- [main/xiaozhi-server/core/handle/helloHandle.py](main/xiaozhi-server/core/handle/helloHandle.py)
  — `generate_lesson_artifact` (study + web search + fallback), `build_lesson_injection`,
  `LESSON_STUDY_SYSTEM`, `_run_lesson`, `lesson_ready`.
- [main/xiaozhi-server/core/connection.py](main/xiaozhi-server/core/connection.py)
  — `change_system_prompt` re-appends the lesson block on every render (prompt-cache-safe).
- [main/xiaozhi-server/test/test_page.html](main/xiaozhi-server/test/test_page.html)
  — always-visible lesson box for A/B.

Study model is configurable via env `SCHOOL_STUDY_MODEL` (default `gpt-4o-mini-search-preview`;
empty disables search and falls back to the configured LLM).

---

## 4. The eval

[main/xiaozhi-server/eval/](main/xiaozhi-server/eval/) — reuses the **production** prompt,
study prompt, and lesson-block builder, so it reflects the live server.

- **Arms:** control (no lesson) vs treatment (studied lesson).
- **User archetypes:** `curious` (chatty) and `passive` ("hmm", "idk" — forces Milu to carry it).
- **Reps + variance**, **blind pairwise win-rate** (order randomized, length-bias ignored).
- **Dimensions:** behavioral 1-5 (`volunteers_new_info`, `subjectivity`, `insider_flavor`,
  `asks_user`, `conversation_not_encyclopedia`, `factual_accuracy`) + ceiling-free counts
  (`new_things_volunteered`, `likes_expressed`, `questions_to_user`, `insider_bits`,
  `likely_false_claims`).
- **Topics:** our user base — K-pop / anime / gacha (Genshin, Jujutsu Kaisen, BTS, BLACKPINK, …).
- **Compare tool:** `compare_reports.py` turns two runs into a side-by-side doc.

Run:
```bash
conda activate babymilu-local && cd main/xiaozhi-server
python eval/school_deeptalk_eval.py --limit 4 --reps 3                                  # mini-search (default)
python eval/school_deeptalk_eval.py --limit 4 --reps 3 --study-model gpt-4o-search-preview
python eval/compare_reports.py results_<A>.json results_<B>.json --labels 4o-mini-search 4o-search
```

---

## 5. Results

From [eval/results/comparison_20260615_155213.md](main/xiaozhi-server/eval/results/comparison_20260615_155213.md)
— 4 topics (Genshin / Jujutsu Kaisen / BTS / BLACKPINK) × 3 reps, judge `gpt-4o`, responder `gpt-4o-mini`.

### 5a. The lesson clearly works — most of all for passive users

Treatment vs control (study model = `gpt-4o-mini-search-preview`):

| dim | curious (ctrl → treat) | passive (ctrl → treat) |
|---|---|---|
| volunteers_new_info | 2.92 → 4.33 | **1.67 → 4.33** |
| insider_flavor | 3.00 → 4.25 | **1.42 → 3.75** |
| conversation_not_encyclopedia | 5.0 → 5.0 | **3.83 → 4.75** |
| likes_expressed (count) | 2.17 → 3.33 | **0.42 → 2.25** |
| insider_bits (count) | 1.83 → 2.25 | **0.17 → 1.92** |

A passive user + no lesson = a near-empty Milu (insider 0.17, likes 0.42). The lesson is what
makes it carry a real conversation. **This is the strongest argument for the feature.**

### 5b. Study-model A/B — mini-search wins (the surprising result)

| metric | `gpt-4o-mini-search-preview` | `gpt-4o-search-preview` |
|---|---|---|
| win-rate curious | **11/12** | 7/12 |
| win-rate passive | **12/12** | 11/12 |
| factual_accuracy curious | **4.42** | 3.83 |
| factual_accuracy passive | **4.67** | 2.83 |
| likely_false_claims curious (lower better) | **0.33** | 0.83 |
| likely_false_claims passive (lower better) | **0.25** | 1.50 |
| engagement (new_things / insider_bits) | comparable, slightly lower | slightly higher |

The bigger model produces richer but **more confidently wrong** lessons; on passive runs it
averaged **1.5 likely-false claims per conversation** vs the mini model's 0.25, and factual
accuracy cratered (2.83 vs 4.67). The mini model is the better choice — higher win-rate, far
more accurate, comparable engagement, **and cheaper**.

### 5c. The unavoidable tradeoff

Control scores a perfect `factual_accuracy = 5.0` because it stays vague and never ventures a
specific claim (vacuously safe). Any substantive treatment trades a little accuracy for a lot of
engagement. With mini-search that cost is small (5.0 → ~4.5) and acceptable; with the big model
it's not. The honesty guardrail keeps it bounded; per-turn retrieval would shrink it further (at
a cost — see §6).

### Caveats
- Judge is `gpt-4o`; its own knowledge bounds factual scoring on niche fandom facts.
- 4 topics × 3 reps — directional, not final. Bump reps/topics before any hard call.
- `character_consistency` is not currently a scored dim — eyeball transcripts to confirm the
  proactivity override didn't flatten warmth.

---

## 6. Pricing

Order-of-magnitude (verify current rates at platform.openai.com/pricing):

| step | frequency | model | est. cost |
|---|---|---|---|
| 小宝学习 study | **once per lesson** | `gpt-4o-mini-search-preview` | **~$0.03** (tokens + ~$0.025–0.03 web-search surcharge) |
| 小宝学习 study (big alt) | once per lesson | `gpt-4o-search-preview` | ~$0.05 |
| deep-talk turn | per turn | `gpt-4o-mini` (no search) | fraction of a cent |

Against a **150-Lumis** lesson, the study cost is a rounding error, and the bigger model's +2¢
isn't worth it (it's *less* accurate here). The only thing that would scale cost meaningfully is
adding **per-turn web search to the responder** (cost × number of turns) — deliberately not done;
the honesty guardrail is the cheaper mitigation for now.

---

## 7. How to run / test

**Live (test page A/B):**
```bash
conda activate babymilu-local && cd main/xiaozhi-server
export CHAT_DB_PATH="$PWD/data/conversations.db"
python app.py
# open http://127.0.0.1:8003/test/test_page.html
# control  = leave the lesson box empty
# treatment = {"topic": "BLACKPINK"}  → wait for "🏫 宝学完回来了" → chat
```

**Gotchas** (see also [memory: local dev gotchas]):
- Python is **not** hot-reloaded — restart `app.py` after editing `.py`.
- A stale process can hold `:8000` and serve old code — `lsof -i tcp:8000`, `kill -9 <pid>`, restart.
- Pick the study model with `export SCHOOL_STUDY_MODEL=gpt-4o-search-preview` before launching.

---

## 8. Open items / next steps

- **Character-memory write-through** so a lesson persists past the session (PRD `school_lesson` event).
- **Moments** generation during the 5h window.
- **Real async** 1h study job + server-side timers (currently collapsed to synchronous).
- **Per-turn retrieval** for out-of-artifact follow-ups (the remaining hallucination source) — weigh against per-turn cost.
- Tighten the eval: more reps/topics, add `character_consistency` back as a guardrail dim.
