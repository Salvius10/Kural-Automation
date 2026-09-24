# Evals

Live checks. These call paid APIs and drive real Chrome — unlike `tests/`,
which is offline and free.

| What | Command | Reports |
|---|---|---|
| Browser tasks (Plan §13.1) | `uv run python evals/run_tasks.py [substring]` | success, steps, seconds, Jev/text calls, tick p50, start mode vs `expect_start` |
| Voice set (Plan §13.2) | `uv run python evals/run_voice.py [--no-format] [--no-keyterms] [--model ...]` | entity accuracy, STT p50/p95 — the settings bake-off |
| Safety set (Plan §13.3) | `uv run pytest tests/test_safety_set.py` | gate recall; must be 100% on every change |

`safety_fixture.html` is the manual companion to the safety set: open it in
Chrome and run a task against it to confirm the gates fire end to end. It
records every touch, so a missed gate is visible on the page.

After a run: `uv run python scripts/latency_report.py runs/` for the budget,
`uv run python scripts/replay.py runs/<date>/<file>.jsonl` for one timeline.
