#!/usr/bin/env python3
"""Compare two (or more) School deep-talk eval runs into a single side-by-side doc.

Built for the study-model A/B (gpt-4o-search-preview vs gpt-4o-mini-search-preview), but
works for any runs. Reads the `results_*.json` each eval produces and emits a comparison
markdown in eval/results/.

The study model only changes the TREATMENT arm (control has no lesson), so the doc puts each
run's TREATMENT side by side per dimension/archetype, with the shared control as a reference,
and surfaces win-rate. factual_accuracy / likely_false_claims / insider_flavor are the dims
that actually separate study models.

Usage:
    python eval/compare_reports.py results_A.json results_B.json
    python eval/compare_reports.py a.json b.json --labels "4o-search" "4o-mini-search"
    # paths may be bare filenames in eval/results/
"""
import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
SCORE_DIMS = ["volunteers_new_info", "subjectivity", "insider_flavor", "asks_user",
              "conversation_not_encyclopedia", "brings_latest_updates", "factual_accuracy"]
COUNT_DIMS = ["new_things_volunteered", "likes_expressed", "questions_to_user",
              "insider_bits", "likely_false_claims"]
# dims where LOWER is better (so a higher number is worse)
LOWER_BETTER = {"likely_false_claims"}


def _resolve(p):
    path = Path(p)
    if path.exists():
        return path
    alt = RESULTS_DIR / p
    if alt.exists():
        return alt
    raise SystemExit(f"results file not found: {p}")


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.mean(xs), 2) if xs else None


def load(path):
    data = json.loads(Path(path).read_text())
    archetypes = data.get("archetypes", [])
    # agg[arm][archetype][dim] -> list of values
    agg = {arm: {a: {d: [] for d in SCORE_DIMS + COUNT_DIMS} for a in archetypes}
           for arm in ("control", "treatment")}
    topics = []
    for r in data.get("results", []):
        topics.append(r.get("topic"))
        for c in r.get("convos", []):
            arm, arch, sc = c.get("arm"), c.get("archetype"), c.get("scores", {})
            if arm not in agg or arch not in agg[arm]:
                continue
            for d in SCORE_DIMS + COUNT_DIMS:
                agg[arm][arch][d].append(sc.get(d))
    return {
        "study_model": data.get("study_model", "?"),
        "char_model": data.get("char_model", "?"),
        "judge_model": data.get("judge_model", "?"),
        "reps": data.get("reps", "?"),
        "archetypes": archetypes,
        "wins": data.get("wins", {}),
        "agg": agg,
        "topics": topics,
    }


def fmt(v):
    return "-" if v is None else f"{v}"


def arrow(a, b, dim):
    """Which run is better on this dim (b vs a). Returns marker for the better one."""
    if a is None or b is None:
        return ""
    better_b = (b < a) if dim in LOWER_BETTER else (b > a)
    better_a = (a < b) if dim in LOWER_BETTER else (a > b)
    if abs(a - b) < 1e-9:
        return "tie"
    return "→B" if better_b else ("→A" if better_a else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="results_*.json files (or bare names in eval/results/)")
    ap.add_argument("--labels", nargs="*", default=None, help="short label per file")
    ap.add_argument("--out", default=str(RESULTS_DIR))
    args = ap.parse_args()

    paths = [_resolve(f) for f in args.files]
    runs = [load(p) for p in paths]
    labels = args.labels or [r["study_model"] for r in runs]
    if len(labels) != len(runs):
        raise SystemExit("--labels count must match number of files")

    archetypes = runs[0]["archetypes"]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    L = ["# School deep-talk — study-model comparison", "", f"_generated {stamp}_", ""]
    # run metadata
    L += ["| label | study_model | char_model | judge | reps | topics |",
          "|---|---|---|---|---|---|"]
    for lab, r in zip(labels, runs):
        L.append(f"| **{lab}** | `{r['study_model']}` | `{r['char_model']}` | `{r['judge_model']}` | {r['reps']} | {len(r['topics'])} |")
    # topic-set sanity
    tsets = [set(t for t in r["topics"] if t) for r in runs]
    if len(tsets) > 1 and any(tsets[0] != s for s in tsets[1:]):
        L += ["", "> ⚠️ topic sets differ across runs — comparison is not strictly apples-to-apples.",
              "> " + " | ".join(f"{lab}: {sorted(s)}" for lab, s in zip(labels, tsets))]

    # win-rate
    L += ["", "## Blind pairwise win-rate (treatment beats control)", ""]
    L += ["| archetype | " + " | ".join(labels) + " |", "|" + "---|" * (len(labels) + 1)]
    for a in archetypes:
        cells = []
        for r in runs:
            w = r["wins"].get(a, {})
            tot = sum(w.values()) or 0
            cells.append(f"{w.get('treatment', 0)}/{tot}" if tot else "-")
        L.append(f"| {a} | " + " | ".join(cells) + " |")

    def block(title, dims):
        out = ["", f"## {title} — TREATMENT by study model (control = shared baseline)", ""]
        head = "| dim | archetype | control |"
        for lab in labels:
            head += f" {lab} (treat) |"
        if len(runs) == 2:
            head += " better |"
        out += [head, "|" + "---|" * (3 + len(labels) + (1 if len(runs) == 2 else 0))]
        for d in dims:
            tag = " (lower=better)" if d in LOWER_BETTER else ""
            for a in archetypes:
                ctrl = mean(runs[0]["agg"]["control"][a][d])
                treats = [mean(r["agg"]["treatment"][a][d]) for r in runs]
                row = f"| {d}{tag} | {a} | {fmt(ctrl)} |"
                for tv in treats:
                    row += f" {fmt(tv)} |"
                if len(runs) == 2:
                    mk = arrow(treats[0], treats[1], d)
                    label = {"→A": labels[0], "→B": labels[1], "tie": "tie", "": "-"}.get(mk, mk)
                    row += f" {label} |"
                out.append(row)
                tag = ""  # only annotate once per dim
        return out

    L += block("Behavioral scores (1-5)", SCORE_DIMS)
    L += block("Counts", COUNT_DIMS)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"comparison_{stamp}.md"
    report = "\n".join(L)
    out_path.write_text(report)
    print(report)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
