# School 文化课 deep-talk eval

Measures whether a **studied deep talk** (treatment) beats **baseline chat** (control)
for the same topic — does it feel like a conversation with an enthusiastic friend, vs the
user querying an encyclopedia?

For each **topic × user-archetype**, it runs `--reps` conversations per arm and scores them
two ways:

1. **Behavioral judge (1-5 + counts)** on: `volunteers_new_info`, `subjectivity`,
   `insider_flavor`, `asks_user`, `conversation_not_encyclopedia`, plus ceiling-free counts
   (`new_things_volunteered`, `likes_expressed`, `questions_to_user`, `insider_bits`).
   Reported as mean ± std with treatment − control deltas.
2. **Blind pairwise win-rate** — judge sees both arms (order randomized, labels hidden) and
   picks the better conversation. Robust, intuitive ("treatment wins 8/10").

**User archetypes** stress proactivity:
- `curious` — chatty, asks back (easy mode).
- `passive` — low-effort ("hmm", "idk"); forces Milu to carry the talk (hard mode — the real
  test of proactivity).

Arms:
- **control**: no lesson; user opens with "let's talk about X".
- **treatment**: Milu studied X (小宝学习 → lesson injected) and opens proactively.

Faithfulness: reuses the production agent base prompt, the production study system prompt
(`LESSON_STUDY_SYSTEM`), and the production lesson-block builder — so changes there flow
straight into the eval. Models/keys come from `data/.config.yaml` (`OpenAILLM`); nothing hardcoded.

## Run

```bash
conda activate babymilu-local
cd main/xiaozhi-server
python eval/school_deeptalk_eval.py --limit 2 --reps 2     # quick smoke
python eval/school_deeptalk_eval.py                        # full (10 topics, both archetypes, reps=3 — slow)
python eval/school_deeptalk_eval.py --archetypes passive   # hard mode only
```

Options: `--reps N`, `--archetypes curious passive`, `--turns N`, `--judge-model gpt-4o`,
`--limit N`, `--topics path`. Cost scales with `topics × archetypes × 2 × reps`, so smoke
with `--limit`/`--reps 2` first.

## Comparing two runs (e.g. study-model A/B)

Each run writes a `results_<ts>.json`. Feed two of them to the comparison generator to get a
single side-by-side doc (win-rate + treatment-by-model with shared control baseline):

```bash
python eval/school_deeptalk_eval.py --reps 3 --study-model gpt-4o-search-preview
python eval/school_deeptalk_eval.py --reps 3 --study-model gpt-4o-mini-search-preview
python eval/compare_reports.py results_<tsA>.json results_<tsB>.json \
    --labels 4o-search 4o-mini-search
# → eval/results/comparison_<ts>.md
```
Run both arms on the **same topic set** for a fair comparison (the tool warns if they differ).

## Output (`eval/results/`)

- `report_<ts>.md` — win-rate table + behavioral/count tables (per archetype, mean±std, Δ).
- `results_<ts>.json` — full transcripts, artifacts, raw scores (for eyeballing + tuning).

## Reading it

- **Win-rate** is the headline. High treatment win-rate, *especially for `passive`*, = the
  lesson makes Milu carry a real conversation. If treatment only wins for `curious`, it's
  riding the user's energy, not its own.
- Positive Δ on `volunteers_new_info` / `insider_flavor` / `subjectivity` = the lesson adds
  the conversational substance we want. Flat Δ → tune `LESSON_STUDY_SYSTEM` / the lesson block
  in `core/handle/helloHandle.py`.
- `character_consistency` is not scored here; keep an eye on transcripts to ensure the
  proactivity override didn't break warmth/persona.
- The topic set is **K-pop and anime** (our user base). These move fast, so watch
  `factual_accuracy` and `likely_false_claims` (lower is better) — that's what web-search
  grounding in the study step should improve. Compare established titles (One Piece, BTS) vs
  newer/faster-moving ones (NewJeans, Frieren, Oshi no Ko) to see where search matters most.
- A/B the study model to see if the bigger search model is worth it:
  `--study-model gpt-4o-mini-search-preview` vs `--study-model gpt-4o-search-preview`.

Edit the topic set in `eval/topics.yaml` (`{topic, category}` per line).
