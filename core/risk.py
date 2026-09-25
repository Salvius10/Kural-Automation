"""Is this click irreversible? Keyword floor now, Jev scoring in the background (Plan §7.7).

The floor is never disabled and never overridden by a model: if a label looks
like a commitment, the click is gated even when Jev scores it safe. The model
can only add risk, never remove it.
"""

import asyncio

import yaml

from agent import model as jev
from agent.questions import RISKY

from .config import ROOT
from .events import RiskScored

_KEYWORDS = None


def keywords():
    global _KEYWORDS
    if _KEYWORDS is None:
        path = ROOT / "data" / "gazetteer.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        _KEYWORDS = tuple(str(word).lower() for word in (data or {}).get("risky_labels", []))
    return _KEYWORDS


def keyword_risky(label):
    """Word-boundary-ish match: 'book now' hits, 'Facebook' and 'Bookmarks' do not."""
    text = f" {(label or '').lower().strip()} "
    for word in keywords():
        padded = f" {word} "
        if padded in text or text.strip() == word:
            return True
    return False


class RiskScorer:
    def __init__(self, bus=None, threshold=0.3):
        self.bus = bus
        self.threshold = threshold
        self.scores: dict[int, float] = {}
        self.inflight: dict[str, asyncio.Task] = {}

    def clickable(self, page):
        seen, actions = set(), []
        for action in page.get("actions", []):
            if action.get("kind") != "click" or action["node"] in seen:
                continue
            seen.add(action["node"])
            actions.append(action)
        return actions

    def score(self, page, goal=""):
        """Background Jev pass, one request with one boolean head per clickable element.

        A page that has already been replaced is not worth scoring: pages settle
        through several fingerprints, and only the last one is ever acted on.
        """
        fingerprint = page.get("fingerprint", "")
        if fingerprint in self.inflight:
            return self.inflight[fingerprint]
        self.supersede(fingerprint)
        actions = self.clickable(page)
        if not actions:
            return None
        task = asyncio.create_task(self._score(fingerprint, page, actions, goal))
        self.inflight[fingerprint] = task
        return task

    async def _score(self, fingerprint, page, actions, goal):
        questions = {
            f"risky_{action['node']}": {
                "type": "noul",
                "criteria": {"element": f"[{action['node']}] {action.get('label', '')}"},
                "instructions": {"goal": goal, "rules": RISKY},
            }
            for action in actions
        }
        try:
            result = await jev.ask(questions, state=page)
        except asyncio.CancelledError:
            self.inflight.pop(fingerprint, None)
            raise
        except Exception:
            self.inflight.pop(fingerprint, None)
            return
        risky = 0
        for name, answer in result["answers"].items():
            if not answer:
                continue
            node = int(name.split("_", 1)[1])
            self.scores[node] = answer["probability"]
            risky += answer["probability"] >= self.threshold
        self.inflight.pop(fingerprint, None)
        if self.bus:
            self.bus.publish(
                RiskScored(count=len(result["answers"]), risky=risky, jev_ms=result["latency_ms"])
            )

    def supersede(self, fingerprint):
        """Cancel scoring still running for an older fingerprint."""
        for older, task in list(self.inflight.items()):
            if older != fingerprint and not task.done():
                task.cancel()
                self.inflight.pop(older, None)

    def known(self, action):
        return int(action.get("node", -1)) in self.scores

    async def is_risky(self, action, page=None, goal=""):
        """Gate decision for one click. Floor first, cached score second, one sync check last."""
        if action.get("kind") != "click":
            return False, "not-a-click"
        if keyword_risky(action.get("label")):
            return True, "keyword"
        node = int(action["node"])
        if node in self.scores:
            return self.scores[node] >= self.threshold, "scored"
        if page is None:
            return False, "unscored"
        # Not ready yet: one synchronous boolean rather than acting blind.
        questions = {
            f"risky_{node}": {
                "type": "noul",
                "criteria": {"element": f"[{node}] {action.get('label', '')}"},
                "instructions": {"goal": goal, "rules": RISKY},
            }
        }
        try:
            result = await jev.ask(questions, state=page)
            answer = result["answers"].get(f"risky_{node}")
        except Exception:
            answer = None
        if not answer:
            return False, "unscored"
        self.scores[node] = answer["probability"]
        return answer["probability"] >= self.threshold, "scored-sync"
