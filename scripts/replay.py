"""Print one session's timeline: `uv run python scripts/replay.py runs/<date>/<file>.jsonl`."""

import sys
from pathlib import Path

import orjson

INTERESTING = {
    "KeyDown": lambda e: "",
    "KeyUp": lambda e: f"held {e['held_ms']}ms",
    "TranscriptPartial": lambda e: f"…{e['text']}",
    "TranscriptFinal": lambda e: f"\"{e['raw']}\" ({e['stt_ms']}ms, {e['provider']})",
    "Normalized": lambda e: f"\"{e['text']}\" {e['facts']}",
    "IntentClassified": lambda e: f"{e['intent']} p={e['confidence']:.2f} ({e['jev_ms']}ms {e['model']})",
    "GoalUpdated": lambda e: f"[{e['goal_version']}] {e['reason']}: {e['goal']}",
    "StartResolved": lambda e: f"{e['mode']} {e['site'] or ''} {e['url'] or ''}".strip(),
    "PageObserved": lambda e: f"{e['n_elements']} elements, {e['observe_ms']}ms — {e['url'][:70]}",
    "ActionDecided": lambda e: f"{e['operation']} {e['label']} p={e['probability']:.2f} ({e['jev_ms']}ms)",
    "ActionExecuted": lambda e: f"{e['kind']} {e['label']}" + (f" = {e['text']}" if e.get("text") else ""),
    "FieldValuesCached": lambda e: f"{e['count']} values, {e['nulls']} null ({e['llm_ms']}ms)",
    "RiskScored": lambda e: f"{e['risky']}/{e['count']} risky ({e['jev_ms']}ms)",
    "GateOpened": lambda e: f"{e['kind']}: {e['prompt']}",
    "GateResolved": lambda e: f"{e['kind']} -> {e['resolution']}",
    "StateChanged": lambda e: f"{e['from_state']} -> {e['to_state']} ({e['trigger']})",
    "RunStatusChanged": lambda e: f"{e['status']} {e['detail']}".strip(),
    "TabSwitched": lambda e: e["reason"],
    "Notice": lambda e: e["text"],
    "Error": lambda e: f"{e['where']}: {e['message']}",
}


def replay(path):
    for line in Path(path).read_bytes().splitlines():
        if not line.strip():
            continue
        event = orjson.loads(line)
        render = INTERESTING.get(event["type"])
        if render is None:
            continue
        print(f"{event['t_ms']:>7}ms  {event['type']:<20} {render(event)}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    replay(sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
