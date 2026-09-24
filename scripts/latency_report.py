"""p50/p95 per stage across sessions: `uv run python scripts/latency_report.py runs/`.

Every optimisation in Plan §4.2 has to show up here. If one does not, remove it.
"""

import statistics
import sys
from pathlib import Path

import orjson

# Each stage names the event that carries its measurement.
STAGES = [
    ("stt", "TranscriptFinal", "stt_ms"),
    ("normalize", "Normalized", "norm_ms"),
    ("jev_intent", "IntentClassified", "jev_ms"),
    ("jev_decide", "ActionDecided", "jev_ms"),
    ("observe", "PageObserved", "observe_ms"),
    ("field_cache", "FieldValuesCached", "llm_ms"),
    ("risk_scoring", "RiskScored", "jev_ms"),
]

# The number that matters (Plan §4.1): key release to the first action of that turn.
FIRST_ACTION = ("key_release_to_first_action", "ActionExecuted", "t_ms")


def percentile(values, fraction):
    if not values:
        return 0
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def collect(root):
    files = sorted(Path(root).rglob("*.jsonl")) if Path(root).is_dir() else [Path(root)]
    samples = {name: [] for name, _, _ in STAGES}
    samples[FIRST_ACTION[0]] = []
    turns = 0
    for path in files:
        seen_release = False
        for line in path.read_bytes().splitlines():
            if not line.strip():
                continue
            event = orjson.loads(line)
            if event["type"] == "KeyUp":
                seen_release, turns = True, turns + 1
                continue
            for name, kind, field in STAGES:
                if event["type"] == kind and event.get(field):
                    samples[name].append(event[field])
            if event["type"] == FIRST_ACTION[1] and seen_release:
                samples[FIRST_ACTION[0]].append(event["t_ms"])
                seen_release = False
    return samples, len(files), turns


def report(root):
    samples, files, turns = collect(root)
    print(f"{files} session file(s), {turns} spoken turn(s)\n")
    print(f"{'stage':<32}{'n':>6}{'p50':>9}{'p95':>9}{'max':>9}")
    print("-" * 65)
    for name in [name for name, _, _ in STAGES] + [FIRST_ACTION[0]]:
        values = samples[name]
        if not values:
            continue
        print(
            f"{name:<32}{len(values):>6}"
            f"{percentile(values, 0.5):>8}ms"
            f"{percentile(values, 0.95):>8}ms"
            f"{max(values):>8}ms"
        )
    budget = samples[FIRST_ACTION[0]]
    if budget:
        p50, p95 = percentile(budget, 0.5), percentile(budget, 0.95)
        print(
            f"\nBudget (Plan §4.1): p50 {p50}ms / 1000ms {'PASS' if p50 <= 1000 else 'FAIL'}, "
            f"p95 {p95}ms / 2000ms {'PASS' if p95 <= 2000 else 'FAIL'}"
        )
        print(f"mean {round(statistics.fmean(budget))}ms over {len(budget)} turns")


def main():
    report(sys.argv[1] if len(sys.argv) > 1 else "runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
