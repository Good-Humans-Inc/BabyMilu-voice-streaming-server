#!/usr/bin/env python3
"""School 文化课 deep-talk eval — control (no lesson) vs treatment (lesson injected).

For each topic × user-archetype, run `reps` simulated multi-turn conversations per arm:
  - control:   no lesson; the user opens with "let's talk about X".
  - treatment: Milu "studied" X (小宝学习) → lesson injected → opens proactively.

Two scoring tracks:
  1. A strong LLM judge scores each conversation on behavioral dims (1-5) + objective
     counts (ceiling-free). We report mean±std and treatment−control deltas.
  2. A blind pairwise judge sees control vs treatment (order randomized, labels hidden)
     and picks the better "conversation with an enthusiastic friend" → win-rate.

User archetypes stress proactivity differently:
  - curious: chatty, asks back (easy mode).
  - passive: low-effort ("hmm", "idk") — forces Milu to carry the conversation (hard mode).

Faithfulness: reuses the production agent base prompt, the production 小宝学习 study
system prompt, and the production lesson-block builder (core.handle.helloHandle).
Models/creds come from data/.config.yaml (OpenAILLM block); no secrets in code.

Usage:
    conda activate babymilu-local && cd main/xiaozhi-server
    python eval/school_deeptalk_eval.py --limit 2 --reps 2     # quick smoke
    python eval/school_deeptalk_eval.py                        # full (slow)
    python eval/school_deeptalk_eval.py --archetypes curious   # one archetype only
"""
import argparse
import json
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from jinja2 import Template
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]  # main/xiaozhi-server
sys.path.insert(0, str(ROOT))

try:
    from core.handle.helloHandle import (
        LESSON_STUDY_SYSTEM,
        build_lesson_injection,
        _parse_lesson_json,
    )
except Exception as e:  # pragma: no cover
    print(
        "ERROR: could not import production lesson logic from core.handle.helloHandle.\n"
        "Run inside the babymilu-local conda env, from main/xiaozhi-server.\n"
        f"Import error: {e}"
    )
    sys.exit(1)

_get_emoji = None
for _mod in ("core.utils.util", "core.utils.text_utils"):
    try:
        _m = __import__(_mod, fromlist=["get_allowed_emoji_list_string"])
        _get_emoji = getattr(_m, "get_allowed_emoji_list_string")
        break
    except Exception:
        pass
if _get_emoji is None:
    _get_emoji = lambda: "😊😄😢😠😮🤔😅😍😆🥰😌😳"


# ----------------------------------------------------------------------------
# Config + prompt assembly
# ----------------------------------------------------------------------------
def load_cfg():
    cfg = yaml.safe_load((ROOT / "data" / ".config.yaml").read_text())
    llm = (cfg.get("LLM") or {}).get("OpenAILLM")
    if not llm or not llm.get("api_key"):
        print("ERROR: data/.config.yaml has no LLM.OpenAILLM.api_key.")
        sys.exit(1)
    return {
        "api_key": llm["api_key"],
        "base_url": llm.get("base_url") or llm.get("url") or "https://api.openai.com/v1",
        "char_model": llm.get("model_name", "gpt-4o-mini"),
        "temperature": float(llm.get("temperature", 0.7) or 0.7),
        "persona": cfg.get("prompt") or "You are BabyMilu, a small warm plushie companion.",
    }


def build_base_system_prompt(persona):
    """Render agent-base-prompt.txt like production; missing template vars render
    empty. Both arms share this base, so the only difference is the lesson block."""
    tmpl = (ROOT / "agent-base-prompt.txt").read_text()
    return Template(tmpl).render(base_prompt=persona, user="friend", emojiList=_get_emoji())


# ----------------------------------------------------------------------------
# LLM helpers
# ----------------------------------------------------------------------------
def chat(client, model, messages, temperature=0.7, max_tokens=400):
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
            )
            return (r.choices[0].message.content or "").strip()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def render_transcript(transcript):
    return "\n".join(f"{'Milu' if s == 'milu' else 'User'}: {t}" for s, t in transcript)


def character_messages(system_prompt, transcript):
    msgs = [{"role": "system", "content": system_prompt}]
    for speaker, text in transcript:
        msgs.append({"role": "assistant" if speaker == "milu" else "user", "content": text})
    return msgs


# FIXED user scripts — the same user turns are replayed in BOTH arms, so the only
# variable is the lesson (control vs treatment). Deterministic + reproducible, and it
# fixes the "the two arms were asked different questions" confound. The `curious` script
# deliberately includes a "what's the latest?" probe (K-pop / games / anime move fast),
# and leaves room for Milu to volunteer beyond what was asked. The `passive` script gives
# almost nothing, so any substance has to come from Milu (the real proactivity test).
USER_SCRIPTS = {
    "curious": [
        "Ooh, can we talk about {topic}? What got you into it?",
        "Nice! What's your favorite part of it?",
        "Is there anything new — any recent update or news with {topic} lately?",
        "Oh interesting, tell me more about that!",
        "Haha, what else are you into about it?",
    ],
    "passive": [
        "i guess we can talk about {topic}",
        "hmm",
        "oh ok",
        "not really",
        "sure",
    ],
}


def build_user_script(topic, archetype, n_turns):
    base = USER_SCRIPTS[archetype]
    return [(base[i] if i < len(base) else base[-1]).format(topic=topic) for i in range(n_turns)]


def study(client, model, topic, fallback="gpt-4o-mini"):
    """Mirror generate_lesson_artifact: one-shot 小宝学习 study call.

    `model` may be a web-search model (e.g. gpt-4o-mini-search-preview); those reject
    sampling params, so we omit temperature. Falls back to a plain model on error.
    """
    msgs = [
        {"role": "system", "content": LESSON_STUDY_SYSTEM},
        {"role": "user", "content": f"Topic to study: {topic}\nReturn the JSON study notes now."},
    ]
    try:
        kwargs = {} if "search" in model else {"temperature": 0.7, "max_tokens": 1500}
        resp = client.chat.completions.create(model=model, messages=msgs, **kwargs)
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"    (study via {model} failed: {e}; falling back to {fallback})")
        resp = client.chat.completions.create(model=fallback, messages=msgs, temperature=0.7, max_tokens=1500)
        raw = (resp.choices[0].message.content or "").strip()
    artifact = _parse_lesson_json(raw) or {}
    artifact.setdefault("topic", topic)
    return artifact


JUDGE_SYSTEM = (
    "You evaluate a plush-toy companion (named Milu) talking with its owner about a topic. "
    "Judge ONLY Milu's turns. The bar is high: we want a real conversation with an "
    "enthusiastic friend who's into the topic — NOT the user querying an encyclopedia.\n\n"
    "Score these as integers 1-5 (1 = encyclopedia/reactive, 3 = some personality, 5 = vivid "
    "opinionated friend). Do not be generous; generic assistant-style answers are a 2.\n"
    "- volunteers_new_info: Milu proactively brings up NEW topic-specific things unasked. "
    "1 = only answers what's asked. 5 = keeps introducing fresh angles itself.\n"
    "- subjectivity: Milu voices genuine PERSONAL preferences — specific favorites, things it "
    "loves, what excites it. 1 = neutral/objective only. 5 = vivid personal taste.\n"
    "- insider_flavor: insider/fan/expert flavor — niche details, in-jokes, recent 'news', deep "
    "cuts. 1 = textbook basics anyone knows. 5 = rich insider detail or jokes.\n"
    "- asks_user: Milu asks the user genuine questions to draw them out. 1 = never. "
    "5 = naturally curious about the user's take.\n"
    "- conversation_not_encyclopedia: gestalt — mutual, Milu-driven chat vs user-asks/Milu-answers. "
    "1 = pure Q&A. 5 = real two-way conversation.\n"
    "- brings_latest_updates: Does Milu proactively surface RECENT/latest developments (new "
    "releases, events, news) and insights the user did NOT ask about? 1 = only evergreen basics / "
    "only answers what's asked. 5 = volunteers specific recent updates and fresh angles.\n"
    "- factual_accuracy: Are Milu's factual claims accurate and not fabricated? 1 = several "
    "likely-false/made-up specifics (names, dates, titles). 5 = claims look accurate, or Milu "
    "honestly avoids guessing. Judge by your own knowledge; if you're unsure, don't penalize.\n\n"
    "Also COUNT instances in Milu's turns (non-negative integers):\n"
    "- new_things_volunteered: distinct topic facts/angles Milu raised unprompted\n"
    "- likes_expressed: times Milu expressed liking/favoriting something specific\n"
    "- questions_to_user: questions Milu asked the user\n"
    "- insider_bits: insider/niche/in-joke/news items beyond textbook basics\n"
    "- likely_false_claims: specific claims by Milu that are probably false/fabricated (lower is better)\n\n"
    "Return STRICT JSON only with ALL keys: "
    '{"volunteers_new_info":N,"subjectivity":N,"insider_flavor":N,"asks_user":N,'
    '"conversation_not_encyclopedia":N,"brings_latest_updates":N,"factual_accuracy":N,'
    '"new_things_volunteered":N,"likes_expressed":N,"questions_to_user":N,"insider_bits":N,'
    '"likely_false_claims":N,"reason":"1-2 sentences"}'
)

PAIRWISE_SYSTEM = (
    "Two plush-toy companions (A and B) each chatted with their owner about the same topic. "
    "Pick the one that feels more like a real conversation with an enthusiastic friend genuinely "
    "into the topic — leads with its own takes, volunteers new and insider details, has "
    "personality and curiosity — rather than the user querying an encyclopedia. "
    "Judge quality, IGNORE length. "
    'Return STRICT JSON: {"winner":"A" or "B","reason":"one sentence"}'
)


def judge_scores(client, model, topic, transcript):
    raw = chat(
        client, model,
        [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"Topic: {topic}\n\nConversation:\n{render_transcript(transcript)}\n\nScore now as JSON."},
        ],
        temperature=0.0, max_tokens=600,
    )
    return _parse_lesson_json(raw) or {}


def judge_pairwise(client, model, topic, t_control, t_treatment):
    """Blind A/B with randomized position. Returns 'control'|'treatment'|None."""
    treat_is_a = random.random() < 0.5
    a, b = (t_treatment, t_control) if treat_is_a else (t_control, t_treatment)
    content = (
        f"Topic: {topic}\n\n=== Companion A ===\n{render_transcript(a)}\n\n"
        f"=== Companion B ===\n{render_transcript(b)}\n\nWhich is better? JSON."
    )
    res = _parse_lesson_json(
        chat(client, model, [
            {"role": "system", "content": PAIRWISE_SYSTEM},
            {"role": "user", "content": content},
        ], temperature=0.0, max_tokens=200)
    ) or {}
    w = str(res.get("winner") or "").strip().upper()
    if w not in ("A", "B"):
        return None
    if w == "A":
        return "treatment" if treat_is_a else "control"
    return "control" if treat_is_a else "treatment"


# ----------------------------------------------------------------------------
# One conversation; always yields n_turns Milu turns
# ----------------------------------------------------------------------------
SCORE_DIMS = ["volunteers_new_info", "subjectivity", "insider_flavor", "asks_user", "conversation_not_encyclopedia", "brings_latest_updates", "factual_accuracy"]
# likely_false_claims: lower is better, so a NEGATIVE Δ is good there.
COUNT_DIMS = ["new_things_volunteered", "likes_expressed", "questions_to_user", "insider_bits", "likely_false_claims"]
DIMS = SCORE_DIMS + COUNT_DIMS


def run_dialogue(client, cfg, base_prompt, artifact, user_script):
    """artifact=None -> control; artifact=dict -> treatment (lesson injected).

    Replays the SAME fixed user_script in both arms (no proactive opener, matches
    production) so the only difference between arms is the lesson block.
    """
    system_prompt = base_prompt
    if artifact is not None:
        block, _opener = build_lesson_injection(artifact)
        if block:
            system_prompt = f"{base_prompt}\n\n{block}"
    transcript = []
    for uturn in user_script:
        transcript.append(("user", uturn))
        transcript.append(("milu", chat(client, cfg["char_model"], character_messages(system_prompt, transcript), cfg["temperature"])))
    return transcript


# ----------------------------------------------------------------------------
def mean_std(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    if not xs:
        return None, None
    m = round(statistics.mean(xs), 2)
    s = round(statistics.pstdev(xs), 2) if len(xs) > 1 else 0.0
    return m, s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topics", default=str(Path(__file__).parent / "topics.yaml"))
    ap.add_argument("--judge-model", default="gpt-4o")
    ap.add_argument("--study-model", default="gpt-4o-mini-search-preview",
                    help="model for 小宝学习 (web-search model grounds it; falls back to char model on error)")
    ap.add_argument("--turns", type=int, default=4, help="Milu turns per conversation")
    ap.add_argument("--reps", type=int, default=3, help="repetitions per (topic, archetype, arm)")
    ap.add_argument("--archetypes", nargs="+", default=["curious", "passive"], choices=list(USER_SCRIPTS))
    ap.add_argument("--limit", type=int, default=0, help="only first N topics (0 = all)")
    ap.add_argument("--out", default=str(Path(__file__).parent / "results"))
    args = ap.parse_args()

    cfg = load_cfg()
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    base_prompt = build_base_system_prompt(cfg["persona"])

    topics = yaml.safe_load(Path(args.topics).read_text())
    if args.limit:
        topics = topics[: args.limit]

    print(
        f"char/user: {cfg['char_model']} | study: {args.study_model} | judge: {args.judge_model} | "
        f"{len(topics)} topics × {len(args.archetypes)} archetypes × 2 arms × {args.reps} reps × {args.turns} turns"
    )

    # collectors
    score_runs = {a: {arm: {d: [] for d in DIMS} for arm in ("control", "treatment")} for a in args.archetypes}
    overall = {arm: {d: [] for d in DIMS} for arm in ("control", "treatment")}
    wins = {a: {"control": 0, "treatment": 0, "tie": 0} for a in args.archetypes}
    full = []

    # Human-readable transcript log for manual inspection (every utterance printed
    # to console AND saved), so you can eyeball Milu's lines against a human.
    tlog = ["# School deep-talk — transcripts (manual check)", ""]

    for i, t in enumerate(topics, 1):
        topic, category = t["topic"], t.get("category", "")
        print(f"\n{'='*70}\n[{i}/{len(topics)}] {topic} ({category})\n{'='*70}")
        artifact = study(client, args.study_model, topic, fallback=cfg["char_model"])  # one lesson per topic
        # Show the studied lesson JSON (this is what gets injected — see SCHOOL_DEEPTALK_MVP.md).
        art_str = json.dumps(artifact, ensure_ascii=False, indent=2)
        print(f"\n--- studied lesson JSON ({args.study_model}) ---\n{art_str}\n")
        tlog += [f"## {i}. {topic} ({category})", "", "**Studied lesson JSON:**",
                 "```json", art_str, "```", ""]
        topic_dump = {"topic": topic, "category": category, "artifact": artifact, "convos": []}
        for arch in args.archetypes:
            user_script = build_user_script(topic, arch, args.turns)  # SAME turns for both arms
            ctrl_runs, treat_runs = [], []
            for arm, runs_list in (("control", ctrl_runs), ("treatment", treat_runs)):
                for rep in range(args.reps):
                    art = artifact if arm == "treatment" else None
                    tr = run_dialogue(client, cfg, base_prompt, art, user_script)
                    sc = judge_scores(client, args.judge_model, topic, tr)
                    runs_list.append({"transcript": tr, "scores": sc})
                    for d in DIMS:
                        score_runs[arch][arm][d].append(sc.get(d))
                        overall[arm][d].append(sc.get(d))
                    topic_dump["convos"].append(
                        {"archetype": arch, "arm": arm, "rep": rep,
                         "scores": sc, "transcript": [{"speaker": s, "text": x} for s, x in tr]}
                    )
                    # print + log every line of this conversation
                    hdr = f"[{topic}] {arch} / {arm} / rep{rep}"
                    print(f"\n----- {hdr} -----")
                    for s, x in tr:
                        who = "🧸 Milu" if s == "milu" else "👤 User"
                        print(f"{who}: {x}")
                    print(f"  ↳ scores: " + ", ".join(f"{d}={sc.get(d)}" for d in DIMS))
                    tlog.append(f"### {hdr}")
                    tlog += [f"- **{'Milu' if s=='milu' else 'User'}:** {x}" for s, x in tr]
                    tlog.append(f"_scores: " + ", ".join(f"{d}={sc.get(d)}" for d in DIMS) + "_")
                    tlog.append("")
            # blind pairwise per rep
            for rep in range(args.reps):
                w = judge_pairwise(client, args.judge_model, topic, ctrl_runs[rep]["transcript"], treat_runs[rep]["transcript"])
                wins[arch][w if w else "tie"] += 1
            cm, _ = mean_std(score_runs[arch]["control"]["conversation_not_encyclopedia"])
            tm, _ = mean_std(score_runs[arch]["treatment"]["conversation_not_encyclopedia"])
            print(f"  {arch}: conv-score control={cm} treatment={tm} | "
                  f"treatment wins {wins[arch]['treatment']}/{args.reps}")
        full.append(topic_dump)

    # ---- report ----
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"results_{stamp}.json").write_text(
        json.dumps({"judge_model": args.judge_model, "study_model": args.study_model,
                    "char_model": cfg["char_model"], "reps": args.reps, "turns": args.turns,
                    "archetypes": args.archetypes, "wins": wins, "results": full},
                   ensure_ascii=False, indent=2)
    )
    (out_dir / f"transcripts_{stamp}.md").write_text("\n".join(tlog))

    n = len(topics) * args.reps
    lines = [
        f"# School deep-talk eval — {stamp}",
        "",
        f"char/user `{cfg['char_model']}` · judge `{args.judge_model}` · "
        f"{len(topics)} topics × {args.reps} reps × {args.turns} turns",
        "",
        "## Blind pairwise win-rate (treatment vs control)",
        "",
        "| archetype | treatment wins | control wins | ties | n |",
        "|---|---|---|---|---|",
    ]
    for a in args.archetypes:
        lines.append(f"| {a} | {wins[a]['treatment']} | {wins[a]['control']} | {wins[a]['tie']} | {n} |")

    def score_block(title, dims, src):
        out = ["", f"## {title} (mean ± std, Δ = treatment − control)", ""]
        cols = " | ".join(args.archetypes)
        out += [f"| dim | arm | {cols} |", "|" + "---|" * (len(args.archetypes) + 2)]
        for d in dims:
            for arm in ("control", "treatment"):
                cells = []
                for a in args.archetypes:
                    m, s = mean_std(src[a][arm][d])
                    cells.append(f"{m}±{s}" if m is not None else "-")
                out.append(f"| {d} | {arm} | " + " | ".join(cells) + " |")
            deltas = []
            for a in args.archetypes:
                cm, _ = mean_std(src[a]["control"][d])
                tm, _ = mean_std(src[a]["treatment"][d])
                deltas.append(f"{tm - cm:+.2f}" if isinstance(cm, (int, float)) and isinstance(tm, (int, float)) else "-")
            out.append(f"| {d} | **Δ** | " + " | ".join(f"**{x}**" for x in deltas) + " |")
        return out

    lines += score_block("Behavioral scores (1-5)", SCORE_DIMS, score_runs)
    lines += score_block("Counts (ceiling-free)", COUNT_DIMS, score_runs)

    report = "\n".join(lines)
    (out_dir / f"report_{stamp}.md").write_text(report)
    print("\n" + report)
    print(f"\nSaved: report_{stamp}.md · results_{stamp}.json · transcripts_{stamp}.md  (in {out_dir})")


if __name__ == "__main__":
    main()
