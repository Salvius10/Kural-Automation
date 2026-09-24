"""Run the browser tasks with typed goals and report the numbers (Plan §13.1).

    uv run python evals/run_tasks.py [substring]

This calls the real Jev and Mercury APIs and drives the real Chrome. It is the
only place in the repo that is allowed to; `tests/` stays offline.
"""

import asyncio
import statistics
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.browser import AsyncBrowser  # noqa: E402
from core import http  # noqa: E402
from core.bus import Bus  # noqa: E402
from core.config import load_env, settings  # noqa: E402
from core.log import JsonlLog  # noqa: E402
from core.mercury import Mercury  # noqa: E402
from core.session import TERMINAL, Session, State  # noqa: E402

TASKS = Path(__file__).with_name("tasks.yaml")


def load_tasks(pattern=None):
    tasks = yaml.safe_load(TASKS.read_text(encoding="utf-8"))["tasks"]
    return [t for t in tasks if not pattern or pattern in t["id"]]


class Counters:
    """What each run cost, read off the event stream rather than instrumented in."""

    def __init__(self, bus):
        self.jev_calls = 0
        self.text_calls = 0
        self.cache_hits = 0
        self.type_actions = 0
        self.tick_ms = []
        self.start_mode = None
        self.gates = []
        bus.add_sink(self.on_event)

    def on_event(self, event):
        if event.type == "ActionDecided":
            self.jev_calls += 1
            self.tick_ms.append(event.jev_ms)
        elif event.type == "IntentClassified":
            self.jev_calls += 1
        elif event.type == "FieldValuesCached":
            self.text_calls += 1
        elif event.type == "StartResolved":
            self.start_mode = event.mode
        elif event.type == "GateOpened":
            self.gates.append(event.kind)
        elif event.type == "ActionExecuted" and event.kind == "fill":
            self.type_actions += 1


async def run_task(task, log_root=None):
    bus = Bus()
    log = JsonlLog(bus.session_id, root=log_root)
    bus.add_sink(log)
    counters = Counters(bus)
    browser = AsyncBrowser(open_in=settings().navigation.open_in)
    session = Session(bus, browser, text_writer=Mercury())
    started = time.perf_counter()
    try:
        await session.start()
        if task.get("start_page"):
            await browser.navigate(task["start_page"])
            session.page = await browser.observe()
        elif task.get("url"):
            await browser.navigate(task["url"])
            session.page = await browser.observe()
        await asyncio.wait_for(session.utterance(task["goal"]), timeout=task.get("budget_s", 30))
        if session.loop_task:
            await asyncio.wait_for(session.loop_task, timeout=task.get("budget_s", 30))
    except asyncio.TimeoutError:
        pass
    except Exception as error:
        print(f"  ! {error}")
    finally:
        elapsed = round(time.perf_counter() - started, 1)
        state = session.state
        steps = len(session.agent.state["history"]) if session.agent else 0
        await session.close()
        log.close()
    return {
        "id": task["id"],
        "state": state.value,
        "ok": state is State.DONE or (state not in TERMINAL and steps > 0),
        "steps": steps,
        "seconds": elapsed,
        "jev_calls": counters.jev_calls,
        "text_calls": counters.text_calls,
        "start_mode": counters.start_mode,
        "expect_start": task.get("expect_start"),
        "start_ok": task.get("expect_start") in (None, counters.start_mode),
        "gates": counters.gates,
        "tick_p50": round(statistics.median(counters.tick_ms)) if counters.tick_ms else 0,
        "log": str(log.path),
    }


async def main():
    load_env()
    await http.warm()
    pattern = sys.argv[1] if len(sys.argv) > 1 else None
    tasks = load_tasks(pattern)
    print(f"{len(tasks)} task(s)\n")
    results = []
    for task in tasks:
        print(f"* {task['id']}: {task['goal']}")
        result = await run_task(task)
        results.append(result)
        start = f" start={result['start_mode']}"
        expected = "" if result["start_ok"] else f" (expected {result['expect_start']})"
        print(
            f"  {'ok ' if result['ok'] else 'FAIL'} {result['state']} "
            f"{result['steps']} steps {result['seconds']}s "
            f"jev={result['jev_calls']} text={result['text_calls']} tick_p50={result['tick_p50']}ms"
            f"{start}{expected}"
        )
    await http.close_all()
    passed = sum(r["ok"] for r in results)
    starts = [r for r in results if r["expect_start"]]
    print(f"\n{passed}/{len(results)} reached a usable state")
    if starts:
        print(f"{sum(r['start_ok'] for r in starts)}/{len(starts)} start modes as expected")
    ticks = [r["tick_p50"] for r in results if r["tick_p50"]]
    if ticks:
        print(f"tick p50 across tasks: {round(statistics.median(ticks))}ms")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
