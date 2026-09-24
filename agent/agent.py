"""The complete agent loop. Typed choices, observable state, bounded execution.

Vendored from jev-ultrafast. Local changes (see UPSTREAM.md): async, no URL at
construction, goal updates applied between ticks, NAVIGATE, gates for risky
clicks and missing field values, tab following, and an event callback.
"""

import time

from core.config import settings
from core.events import (
    ActionDecided,
    ActionExecuted,
    Error,
    GateOpened,
    PageObserved,
    RunStatusChanged,
    TabSwitched,
)
from core.field_cache import MISS, is_blocked_label

from .browser import BlockedField, StalePage
from .model import choose, field_context, field_text

# A page with this many distinct clickable elements is "result-like" enough to be
# worth asking whether the goal actually says which one to pick (Plan §7.11.6).
RESULT_LIKE_CLICKABLES = 8
CHOICE_GATE_THRESHOLD = 0.6


class GateRequired(Exception):
    """The loop stopped in front of an action that needs the user (Plan §7.7, §7.6)."""

    def __init__(self, kind, prompt, action=None, decision=None):
        super().__init__(prompt)
        self.kind = kind
        self.prompt = prompt
        self.action = action
        self.decision = decision


class Agent:
    def __init__(
        self,
        goal,
        browser,
        *,
        bus=None,
        field_cache=None,
        risk=None,
        sites=None,
        facts=None,
        max_steps=None,
    ):
        goal = goal.strip() if isinstance(goal, str) else "\n".join(goal).strip()
        if not goal:
            raise ValueError("Supply a task")
        config = settings()
        self.browser = browser
        self.bus = bus
        self.field_cache = field_cache
        self.risk = risk
        self.sites = sites or {}
        self.facts = facts or {}
        self.max_steps = max_steps or config.budgets.max_steps
        self.max_model_calls = config.budgets.max_model_calls
        self.pending_text = None
        self.pending_gate = None
        self.goal_version = 1
        self.next_goal = None
        self.cancelled = False
        self.needs_choice = False
        self.choice_asked_for = None
        self.state = dict(
            goal=goal,
            goal_version=1,
            page=None,
            decision=None,
            history=[],
            status="ready",
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
        )

    # ------------------------------------------------------------------ state

    def emit(self, event):
        if self.bus:
            return self.bus.publish(event)
        return event

    def snapshot(self):
        page = self.state["page"] or {}
        return {
            **self.state,
            "elements": len(page.get("actions", [])),
            "url": page.get("url", ""),
            "title": page.get("title", ""),
        }

    def set_goal(self, text, reason="correction"):
        """Queued, never applied mid-action (Plan §7.8.2)."""
        self.next_goal = (text.strip(), reason)
        return self.goal_version + 1

    def apply_goal(self):
        if not self.next_goal:
            return None
        text, reason = self.next_goal
        self.next_goal = None
        self.goal_version += 1
        self.state["goal"] = text
        self.state["goal_version"] = self.goal_version
        if self.field_cache:
            self.field_cache.invalidate(self.goal_version)
        return text, reason, self.goal_version

    async def start(self):
        await self.observe()
        return self.state["page"]

    async def observe(self, screenshot=False):
        started = time.perf_counter()
        page = await self.browser.observe(screenshot=screenshot)
        self.state["page"] = page
        if self.field_cache:
            self.field_cache.prune(page)
            self.field_cache.prefetch(page, self.state["goal"], self.goal_version, self.facts)
        if self.risk:
            self.risk.score(page, self.state["goal"])
        self.emit(
            PageObserved(
                url=page["url"],
                title=page.get("title", ""),
                fingerprint=page["fingerprint"],
                n_elements=len(page.get("actions", [])),
                observe_ms=round((time.perf_counter() - started) * 1000),
            )
        )
        return page

    # ---------------------------------------------------------------- predict

    def clickables(self, page):
        return len({a["node"] for a in page.get("actions", []) if a.get("kind") == "click"})

    def choice_gate_question(self, page):
        """Ask on result-like pages, once per goal version -- an answered goal says enough."""
        if self.choice_asked_for == self.goal_version:
            return {}
        if self.clickables(page) < RESULT_LIKE_CLICKABLES:
            return {}
        from core.fused import choice_gate_question

        return choice_gate_question(self.state["goal"])

    async def predict(self, extra_questions=None):
        state = self.state
        if state["started_at"] is None:
            state["started_at"] = time.perf_counter()
        if not await self.browser.fresh(state["page"]):
            await self.observe()
        state["decision"] = None
        if state["status"] in {"done", "blocked", "cancelled"}:
            raise ValueError("This run has stopped. Start a fresh task.")
        if len(state["decisions"]) >= self.max_model_calls:
            raise ValueError("Reached the run's model-call budget")
        questions = {**self.choice_gate_question(state["page"]), **(extra_questions or {})}
        decision = await choose(
            state["page"],
            state["goal"],
            state["history"],
            extra_questions=questions or None,
            sites=self.sites,
        )
        state["decision"] = decision
        state["decisions"].append(
            {
                **{k: v for k, v in decision.items() if k not in ("request", "raw_answers", "action")},
                "fingerprint": state["page"]["fingerprint"],
                "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
            }
        )
        state["status"] = "predicted"
        answer = (decision.get("extra") or {}).get("needs_user_choice")
        if answer is not None:
            self.choice_asked_for = self.goal_version
            self.needs_choice = answer["probability"] >= CHOICE_GATE_THRESHOLD
        action = decision.get("action") or {}
        self.emit(
            ActionDecided(
                operation=decision["operation"],
                target=decision["target"],
                label=action.get("label", decision["operation"]),
                probability=decision["probabilities"].get(decision["choice"], 0.0),
                confidence=decision["confidence"],
                jev_ms=decision["latency_ms"],
                model=decision.get("model", ""),
            )
        )
        return decision

    # -------------------------------------------------------------------- act

    async def gate_for(self, action, decision):
        """Everything that must be true before this action may execute."""
        kind = action.get("kind")
        if kind == "click" and self.needs_choice:
            self.needs_choice = False
            count = self.clickables(self.state["page"])
            return GateRequired(
                "choice",
                f"{count} options here — which one? (the cheapest, the first one, the 7 AM one)",
                action,
                decision,
            )
        if kind == "click" and self.risk:
            risky, why = await self.risk.is_risky(action, self.state["page"], self.state["goal"])
            if risky:
                return GateRequired(
                    "confirm",
                    f"{action.get('label', 'this action')} — say yes or no.",
                    action,
                    decision,
                )
            del why
        if kind == "fill":
            if is_blocked_label(action.get("label")):
                return GateRequired(
                    "manual",
                    f"Please type {action.get('label', 'this field')} yourself.",
                    action,
                    decision,
                )
            if self.field_cache:
                cached = self.field_cache.get(self.goal_version, action["node"])
                if cached is None:
                    return GateRequired(
                        "info",
                        f"What should I put in {action.get('label', 'this field')}?",
                        action,
                        decision,
                    )
                del cached
        return None

    async def text_for(self, action):
        """Cache first (Plan §4.2.6); a miss falls back to upstream's single call."""
        if self.field_cache:
            cached = self.field_cache.get(self.goal_version, action["node"])
            if cached is not MISS and cached is not None:
                return cached, {"model": "cache", "latency_ms": 0, "usage": {}}
        if not await self.browser.fresh(self.state["page"]):
            raise StalePage("Page changed before text generation. Choose again.")
        context = field_context(self.state["goal"], action, self.state["page"], self.state["history"])
        if self.pending_text and self.pending_text[0] == context:
            _, text, helper = self.pending_text
            return text, helper
        text, helper = await field_text(context)
        self.pending_text = (context, text, helper)
        self.state["text_calls"].append({**helper, "field": action["label"], "value": text})
        return text, helper

    async def act(self, fingerprint=None):
        state = self.state
        decision, page = state["decision"], state["page"]
        if not decision or (fingerprint is not None and fingerprint != page["fingerprint"]):
            raise ValueError("Observe and choose before acting")
        # Consume once, before any mutation or model call. A retry cannot double-click.
        state["decision"] = None
        selected = decision["choice"]
        if selected in {"DONE", "BLOCKED"}:
            if not await self.browser.fresh(page):
                state["status"] = "ready"
                raise StalePage("Page changed since the decision. Choose again.")
            self.set_status("done" if selected == "DONE" else "blocked")
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            return self.snapshot()
        action = decision.get("action")
        if action is None:
            action = next(a for a in page["actions"] if a["id"] == selected)
        if len(state["history"]) >= self.max_steps:
            self.set_status("blocked", f"Stopped at the {self.max_steps}-action budget")
            return self.snapshot()
        gate = await self.gate_for(action, decision)
        if gate is not None:
            self.open_gate(gate)
            raise gate
        return await self.execute(action, decision)

    def open_gate(self, gate):
        self.pending_gate = gate
        self.state["status"] = "waiting_confirm" if gate.kind == "confirm" else "waiting_info"
        self.emit(GateOpened(kind=gate.kind, prompt=gate.prompt))

    async def execute(self, action, decision):
        """Run one already-gated action and record it. Never called twice for one decision."""
        state = self.state
        page = state["page"]
        text, helper = None, None
        if action.get("kind") == "navigate":
            return await self.execute_navigate(action, decision)
        if action["kind"] == "fill":
            text, helper = await self.text_for(action)
            if text is None:
                # The goal does not contain this value: ask, never invent (Plan §7.6).
                gate = GateRequired(
                    "info", f"What should I put in {action.get('label', 'this field')}?", action, decision
                )
                self.open_gate(gate)
                raise gate
        try:
            # Browser.act checks freshness immediately before input, including after text generation.
            await self.browser.act(action, page, text=text)
        except BlockedField as blocked:
            self.open_gate(GateRequired("manual", f"Please type {blocked} yourself.", action, decision))
            raise self.pending_gate from None
        self.pending_text = None
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        # Record execution before observing. A stale post-action observation must not erase the action.
        state["history"].append(
            {
                "step": len(state["history"]) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "choice": decision["choice"],
                "probability": decision["probabilities"].get(decision["choice"], 0.0),
                "confidence": decision["confidence"],
                "latency_ms": decision["latency_ms"],
                "text": text,
                "text_helper": helper["model"] if helper else None,
                "text_latency_ms": helper["latency_ms"] if helper else 0,
                "operation": decision["operation"],
                "target": decision["target"],
                "page_changed": None,
                "url": page["url"],
                "usage": decision["usage"],
                "goal_version": self.goal_version,
                "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                "elapsed_ms": state["elapsed_ms"],
            }
        )
        self.emit(
            ActionExecuted(
                label=action["label"],
                kind=action["kind"],
                text=text,
                exec_ms=state["elapsed_ms"],
            )
        )
        await self.after_action(page)
        return self.snapshot()

    async def execute_navigate(self, action, decision):
        """NAVIGATE: the URL comes from sites.yaml or the search template, never from a model."""
        state = self.state
        url = action.get("url")
        if not url:
            raise ValueError("NAVIGATE target carried no code-owned URL")
        await self.browser.navigate(url)
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        state["history"].append(
            {
                "step": len(state["history"]) + 1,
                "action": action["label"],
                "kind": "navigate",
                "choice": decision["choice"],
                "probability": decision["probabilities"].get(decision["choice"], 0.0),
                "confidence": decision["confidence"],
                "latency_ms": decision["latency_ms"],
                "text": None,
                "operation": "NAVIGATE",
                "target": decision["target"],
                "page_changed": True,
                "url": url,
                "usage": decision["usage"],
                "goal_version": self.goal_version,
                "elapsed_ms": state["elapsed_ms"],
            }
        )
        self.emit(
            ActionExecuted(label=action["label"], kind="navigate", page_changed=True, exec_ms=state["elapsed_ms"])
        )
        await self.observe()
        state["history"][-1]["url"] = state["page"]["url"]
        state["status"] = "ready"
        return self.snapshot()

    async def after_action(self, page):
        """Follow a tab the click opened, then observe and judge whether we are stuck."""
        state = self.state
        if settings().navigation.follow_new_tabs:
            try:
                followed = await self.browser.follow_new_tab()
            except Exception:
                followed = None
            if followed:
                self.emit(TabSwitched(from_target=followed[0], to_target=followed[1], reason="opened-by-page"))
        await self.observe()
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        state["history"][-1].update(
            page_changed=state["page"]["fingerprint"] != page["fingerprint"],
            url=state["page"]["url"],
            elapsed_ms=state["elapsed_ms"],
        )
        repeated = state["history"][-3:]
        stuck = len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
        self.set_status("blocked" if stuck else "ready", "no page change in three actions" if stuck else "")

    def set_status(self, status, detail=""):
        if self.state["status"] != status:
            self.state["status"] = status
            self.emit(RunStatusChanged(status=status, detail=detail))
        return status

    # ------------------------------------------------------------------ gates

    async def resolve_gate(self, approved, answer=None):
        """Release or cancel the gated action. A gate is consumed exactly once."""
        gate, self.pending_gate = self.pending_gate, None
        if gate is None:
            return None
        if not approved:
            self.set_status("ready")
            return None
        if gate.kind == "manual":
            # The user types it; the agent only continues afterwards.
            self.set_status("ready")
            return None
        if gate.kind == "info" and answer is not None:
            self.set_status("ready")
            return None
        if not await self.browser.fresh(self.state["page"], gate.action):
            self.set_status("ready")
            return None
        self.state["status"] = "running"
        return await self.execute(gate.action, gate.decision)

    # ------------------------------------------------------------------- loop

    async def tick(self, extra_questions=None):
        if self.cancelled:
            self.set_status("cancelled")
            return self.snapshot()
        applied = self.apply_goal()
        if applied:
            self.state["history"].append(
                {"action": f"goal updated ({applied[1]})", "kind": "goal", "text": applied[0], "page_changed": None}
            )
        try:
            await self.predict(extra_questions=extra_questions)
            return await self.act(self.state["page"]["fingerprint"])
        except StalePage:
            self.state["decision"] = None
            self.state["status"] = "ready"
            await self.observe()
            return self.snapshot()
        except GateRequired:
            return self.snapshot()
        except (ValueError, RuntimeError) as error:
            self.emit(Error(where="tick", message=str(error), recoverable=False))
            self.set_status("blocked", str(error))
            return self.snapshot()

    async def run(self):
        """A generator that yields after every tick — the hook for mid-run goal changes."""
        while self.state["status"] not in {"done", "blocked", "cancelled"}:
            if self.pending_gate is not None:
                return
            yield await self.tick()

    def cancel(self):
        """Stop after the action in flight completes (Plan §11.6)."""
        self.cancelled = True
