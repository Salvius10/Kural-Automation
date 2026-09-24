"""Offline fixtures. No test in this directory may touch a paid API or Chrome."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TYPESAFE_API_KEY", "test-key")
os.environ.setdefault("INCEPTION_API_KEY", "test-key")


def action(node, kind, label, **extra):
    return {
        "id": f"e{node}",
        "node": node,
        "kind": kind,
        "role": extra.pop("role", "button" if kind == "click" else "textbox"),
        "label": label,
        "value": extra.pop("value", ""),
        **extra,
    }


def page(actions=None, url="https://example.test/", text="example page", fingerprint="fp1"):
    actions = actions or []
    return {
        "url": url,
        "title": "Example",
        "text": text,
        "actions": [*actions, {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}],
        "marker": ["m", url, fingerprint],
        "page_key": ["k", url],
        "guards": {str(a["node"]): ["g", a["node"]] for a in actions},
        "scroll": {"y": 0, "height": 800},
        "fingerprint": fingerprint,
    }


class FakeBrowser:
    """Implements the BrowserDriver seam; records everything it was asked to do."""

    def __init__(self, pages=None):
        self.pages = list(pages or [page()])
        self.index = 0
        self.acted = []
        self.navigated = []
        self.stale = False
        self.blocked_labels = ()
        self.started = False

    async def start(self):
        self.started = True
        return self

    async def observe(self, screenshot=False):
        return self.pages[min(self.index, len(self.pages) - 1)]

    async def fresh(self, page_state, action=None):
        return not self.stale

    async def act(self, action, page_state, text=None):
        from agent.browser import BlockedField, blocked_label

        if action["kind"] == "fill" and (blocked_label(action.get("label")) or action["label"] in self.blocked_labels):
            raise BlockedField(action["label"])
        self.acted.append((action["label"], action["kind"], text))
        self.index = min(self.index + 1, len(self.pages) - 1)
        return {"executed": action["id"]}

    async def navigate(self, url):
        self.navigated.append(url)
        self.index = min(self.index + 1, len(self.pages) - 1)
        return url

    async def follow_new_tab(self):
        return None

    async def return_to_opener(self):
        return None

    async def close(self):
        return None


class FakeTextWriter:
    def __init__(self, values=None, answer="an answer", query="search words"):
        self.values = values or {}
        self.answer = answer
        self.query = query
        self.calls = []

    async def field_values(self, goal, fields, page_text, facts):
        self.calls.append(("field_values", goal))
        return dict(self.values)

    async def answer_question(self, question, page_text):
        self.calls.append(("answer_question", question))
        return self.answer

    async def search_query(self, goal):
        self.calls.append(("search_query", goal))
        return self.query


def choice(pick, ids, confidence=0.9):
    """A well-formed Jev choice answer for `ids`, argmax on `pick`."""
    ids = list(ids)
    rest = (1 - 0.8) / max(len(ids) - 1, 1)
    probabilities = {i: (0.8 if i == pick else rest) for i in ids}
    if len(ids) == 1:
        probabilities = {ids[0]: 1.0}
    return {"choice": pick, "probabilities": probabilities, "confidence": confidence}


def noul(probability, confidence=0.9):
    return {"probability": probability, "confidence": confidence}


async def drain(session, timeout=2.0):
    """Let the run loop finish before asserting.

    Cancelling it (stop_loop) can kill the task before it has run at all, which
    silently turns a loop test into a no-op.
    """
    import asyncio

    task = session.loop_task
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    await session.stop_loop()
    return session.state


class NetworkUsed(RuntimeError):
    """Raised if a test reaches for a paid API. Tests are offline (AGENTS.md)."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Stub both providers.

    Callers that treat a model failure as "no answer" -- the risk scorer, the
    field cache -- keep working, which is exactly the offline behaviour worth
    testing. Anything that would act on a model answer fails loudly instead.
    """
    import agent.model
    import core.mercury

    async def refuse(*args, **kwargs):
        raise NetworkUsed("a test tried to call a paid API")

    monkeypatch.setattr(agent.model, "post_json", refuse)
    monkeypatch.setattr(core.mercury, "complete_json", refuse)


@pytest.fixture
def bus():
    from core.bus import Bus

    return Bus(session_id="test")


@pytest.fixture
def events(bus):
    captured = []
    bus.add_sink(captured.append)
    return captured
