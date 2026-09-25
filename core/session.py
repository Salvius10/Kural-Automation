"""The session state machine: one goal, one run, every utterance routed (Plan §8).

Speaking is allowed in any state. A running task keeps going while the user
talks; goal changes and cancellation are applied **between ticks**, never in
the middle of an action.
"""

import asyncio
import time
from enum import Enum

from agent.agent import Agent, GateRequired
from agent.model import choose
from voice.normalizer import normalize, strip_site_phrase

from .config import settings
from .events import (
    Error,
    GateResolved,
    GoalUpdated,
    IntentClassified,
    Normalized,
    Notice,
    RunStatusChanged,
    StateChanged,
    TranscriptFinal,
)
from .field_cache import FieldCache
from .fused import read_intent, utterance_questions
from .profile import load_profile
from .risk import RiskScorer
from .start import StartResolver, sites

STOP_WORDS = {"stop", "stop it", "cancel", "abort", "stop stop", "cancel it"}


class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    ROUTING = "ROUTING"
    RUNNING = "RUNNING"
    WAITING_CONFIRM = "WAITING_CONFIRM"
    WAITING_INFO = "WAITING_INFO"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


TERMINAL = {State.DONE, State.BLOCKED, State.CANCELLED}

# An explicit table: anything not listed here is a bug, and raises.
TRANSITIONS: dict[State, set[State]] = {
    State.IDLE: {State.LISTENING, State.ROUTING},
    State.LISTENING: {State.ROUTING, State.IDLE, State.RUNNING, State.WAITING_CONFIRM, State.WAITING_INFO},
    State.ROUTING: {
        State.RUNNING,
        State.IDLE,
        State.LISTENING,
        State.WAITING_CONFIRM,
        State.WAITING_INFO,
        State.DONE,
        State.BLOCKED,
        State.CANCELLED,
    },
    State.RUNNING: {
        State.LISTENING,
        State.ROUTING,
        State.WAITING_CONFIRM,
        State.WAITING_INFO,
        State.DONE,
        State.BLOCKED,
        State.CANCELLED,
    },
    State.WAITING_CONFIRM: {State.LISTENING, State.ROUTING, State.RUNNING, State.CANCELLED, State.BLOCKED,
                            State.DONE, State.WAITING_INFO},
    State.WAITING_INFO: {State.LISTENING, State.ROUTING, State.RUNNING, State.CANCELLED, State.BLOCKED,
                         State.DONE, State.WAITING_CONFIRM},
    State.DONE: {State.LISTENING, State.ROUTING, State.IDLE},
    State.BLOCKED: {State.LISTENING, State.ROUTING, State.IDLE},
    State.CANCELLED: {State.LISTENING, State.ROUTING, State.IDLE},
}


class IllegalTransition(RuntimeError):
    pass


class Session:
    def __init__(self, bus, browser, *, text_writer=None):
        self.config = settings()
        self.bus = bus
        self.browser = browser
        self.text_writer = text_writer
        self.field_cache = FieldCache(bus, self.config.page_text_chars, profile=load_profile())
        self.risk = RiskScorer(bus, self.config.thresholds.risk_threshold)
        self.start_resolver = StartResolver(bus, text_writer)
        self.state = State.IDLE
        self.agent: Agent | None = None
        self.goal = ""
        self.facts: dict = {}
        self.pending_question = ""
        self.page = None
        self.loop_task: asyncio.Task | None = None
        self.gate_timer: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    async def start(self):
        await self.browser.start()
        self.page = await self.browser.observe()
        return self.page

    async def close(self):
        await self.stop_loop()
        await self.browser.close()

    def emit(self, event):
        return self.bus.publish(event)

    def transition(self, target, trigger=""):
        if target == self.state:
            return self.state
        if target not in TRANSITIONS[self.state]:
            self.emit(Error(where="session", message=f"illegal {self.state.value} -> {target.value}", recoverable=True))
            raise IllegalTransition(f"{self.state.value} -> {target.value}")
        previous, self.state = self.state, target
        self.emit(StateChanged(from_state=previous.value, to_state=target.value, trigger=trigger))
        return self.state

    # ------------------------------------------------------------------ mic

    def key_down(self):
        if self.state in (State.IDLE, State.DONE, State.BLOCKED, State.CANCELLED):
            self.transition(State.LISTENING, "key-down")
        return self.state

    # ------------------------------------------------------------- utterance

    async def utterance(self, raw, stt_ms=0, provider="typed"):
        """One spoken (or typed) command, from any state."""
        self.emit(TranscriptFinal(raw=raw, stt_ms=stt_ms, provider=provider))
        text, facts = normalize(raw)
        self.emit(Normalized(text=text, facts=facts, norm_ms=0))
        if not text:
            return self.emit(Notice(text="Didn't catch that.", level="warn"))
        # The kill switch must not depend on a model round trip (Plan §11.6).
        if text.strip().lower().strip(".!") in STOP_WORDS:
            return await self.cancel("spoken stop")
        async with self.lock:
            return await self.route(text, facts)

    async def route(self, text, facts):
        running = self.agent is not None and self.state in (
            State.RUNNING,
            State.WAITING_CONFIRM,
            State.WAITING_INFO,
        )
        if self.state not in (State.ROUTING,):
            self.transition(State.ROUTING, "utterance")
        page = self.agent.state["page"] if (self.agent and self.agent.state["page"]) else self.page
        if page is None:
            page = self.page = await self.browser.observe()
        goal_ctx = {
            "current_goal": self.goal,
            "latest_utterance": text,
            "pending_question": self.pending_question,
        }
        # The start heads ride along even mid-run: the intent is only known once
        # this same answer comes back, and by then a new task must not wait for
        # a second round trip (Plan §4.2.5).
        questions = utterance_questions(goal_ctx, sites(), include_start=True)
        try:
            decision = await choose(
                page,
                self.goal or text,
                self.agent.state["history"] if self.agent else [],
                extra_questions=questions,
                sites=sites(),
            )
        except (ValueError, RuntimeError) as error:
            self.emit(Error(where="route", message=str(error), recoverable=True))
            self.transition(State.RUNNING if running else State.IDLE, "route-failed")
            return None
        intent, confidence, probabilities = read_intent(
            decision["extra"], self.config.thresholds.intent_min_confidence
        )
        self.emit(
            IntentClassified(
                intent=intent or "unclear",
                confidence=confidence,
                probabilities=probabilities,
                jev_ms=decision["latency_ms"],
                model=decision.get("model", ""),
            )
        )
        if intent is None:
            self.emit(Notice(text=f'Not sure what you meant by "{text}". Say it again?', level="warn"))
            self.transition(State.RUNNING if running else State.IDLE, "low-confidence")
            return None
        return await self.dispatch(intent, text, facts, decision, page)

    async def dispatch(self, intent, text, facts, decision, page):
        if intent == "new_task":
            return await self.new_task(text, facts, decision, page)
        if intent == "correction":
            return await self.correction(text, facts)
        if intent in ("confirm_yes", "confirm_no"):
            return await self.confirm(intent == "confirm_yes")
        if intent == "answer":
            return await self.answer(text)
        if intent == "cancel":
            return await self.cancel("spoken cancel")
        if intent == "page_question":
            return await self.page_question(text, page)
        self.emit(Notice(text="Didn't catch that.", level="warn"))
        self.transition(State.RUNNING if self.agent else State.IDLE, "noise")
        return None

    # --------------------------------------------------------------- intents

    async def new_task(self, text, facts, decision, page):
        await self.stop_loop()
        goal = text
        named = facts.get("site")
        if named and named in sites():
            goal = strip_site_phrase(text, named) or text
        self.goal = goal
        self.facts = facts
        self.pending_question = ""
        self.field_cache.invalidate()
        self.emit(GoalUpdated(goal=goal, goal_version=1, reason="new_task"))
        start = await self.start_resolver.resolve(goal, facts, decision["extra"])
        self.agent = Agent(
            goal,
            self.browser,
            bus=self.bus,
            field_cache=self.field_cache,
            risk=self.risk,
            sites=sites(),
            facts=facts,
        )
        if start["mode"] == "here":
            # The decision heads in this same request already apply to this page,
            # so the first action runs now -- inside the key-release budget.
            self.agent.state["page"] = page
            self.agent.state["decision"] = decision
            self.agent.state["started_at"] = self.agent.state["started_at"] or time.perf_counter()
            # Warm the field cache while the first action runs, not after it.
            self.field_cache.prefetch(page, goal, 1, facts)
            self.risk.score(page, goal)
            self.transition(State.RUNNING, "new_task-here")
            if decision["confidence"] >= self.config.thresholds.intent_min_confidence:
                await self.first_action(page)
                if self.agent.pending_gate is not None:
                    return start
            self.start_loop()
            return start
        await self.browser.navigate(start["url"])
        await self.agent.start()
        self.transition(State.RUNNING, f"new_task-{start['mode']}")
        self.start_loop()
        return start

    async def correction(self, text, facts):
        if not self.agent:
            return await self.new_task(text, facts, *(await self.fresh_decision(text)))
        merged = f"{self.agent.state['goal']}. Correction: {text}"
        version = self.agent.set_goal(merged, "correction")
        self.facts.update(facts)
        self.agent.facts = self.facts
        self.emit(GoalUpdated(goal=merged, goal_version=version, reason="correction"))
        if self.state in (State.WAITING_CONFIRM, State.WAITING_INFO):
            self.agent.pending_gate = None
            self.transition(State.RUNNING, "correction")
            self.start_loop()
        else:
            self.transition(State.RUNNING, "correction")
        return version

    async def fresh_decision(self, text):
        page = self.page or await self.browser.observe()
        questions = utterance_questions(
            {"current_goal": "", "latest_utterance": text, "pending_question": ""}, sites()
        )
        decision = await choose(page, text, [], extra_questions=questions, sites=sites())
        return decision, page

    async def confirm(self, approved):
        if not self.agent or self.agent.pending_gate is None:
            self.emit(Notice(text="Nothing to confirm right now."))
            return None
        kind = self.agent.pending_gate.kind
        self.cancel_gate_timer()
        self.emit(GateResolved(kind=kind, resolution="yes" if approved else "no"))
        if not approved:
            self.transition(State.CANCELLED, "confirm-no")
            self.emit(RunStatusChanged(status="cancelled", detail="user said no"))
            return None
        self.transition(State.RUNNING, "confirm-yes")
        await self.agent.resolve_gate(True)
        self.start_loop()
        return True

    async def answer(self, text):
        if not self.agent:
            self.emit(Notice(text="Nothing was asked."))
            return None
        self.cancel_gate_timer()
        merged = f"{self.agent.state['goal']}. {self.pending_question} {text}".strip()
        version = self.agent.set_goal(merged, "answer")
        self.pending_question = ""
        self.emit(GoalUpdated(goal=merged, goal_version=version, reason="answer"))
        self.emit(GateResolved(kind="info", resolution="answered"))
        self.agent.pending_gate = None
        self.transition(State.RUNNING, "answer")
        self.start_loop()
        return version

    async def cancel(self, reason="cancel"):
        if self.agent:
            self.agent.cancel()
        await self.stop_loop()
        self.cancel_gate_timer()
        if self.state not in TERMINAL:
            self.transition(State.CANCELLED, reason)
        self.emit(RunStatusChanged(status="cancelled", detail=reason))
        return None

    async def page_question(self, text, page):
        answer = None
        if self.text_writer:
            try:
                answer = await self.text_writer.answer_question(text, page.get("text", ""))
            except Exception as error:
                self.emit(Error(where="page_question", message=str(error), recoverable=True))
        self.emit(Notice(text=answer or "I couldn't read an answer off this page."))
        self.transition(State.RUNNING if self.agent else State.IDLE, "page_question")
        return answer

    # ------------------------------------------------------------- run loop

    async def first_action(self, page):
        """Execute the decision that rode along with the intent answer."""
        try:
            await self.agent.act(page["fingerprint"])
        except GateRequired:
            await self.open_gate()
        except Exception as error:
            self.emit(Error(where="first-action", message=str(error), recoverable=True))
        return self.agent.state["status"]

    def start_loop(self):
        if self.loop_task and not self.loop_task.done():
            return self.loop_task
        self.loop_task = asyncio.create_task(self.loop())
        return self.loop_task

    async def stop_loop(self):
        task, self.loop_task = self.loop_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def loop(self):
        """Tick until the run ends or a gate opens. Goal changes land between ticks."""
        agent = self.agent
        try:
            while agent.state["status"] in ("ready", "predicted", "running"):
                await agent.tick()
                if agent.pending_gate is not None:
                    return await self.open_gate()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.emit(Error(where="loop", message=str(error), recoverable=False))
        finally:
            self.settle()

    def settle(self):
        if not self.agent:
            return
        status = self.agent.state["status"]
        if status == "done" and self.state not in TERMINAL:
            self.transition(State.DONE, "agent-done")
        elif status == "blocked" and self.state not in TERMINAL:
            self.transition(State.BLOCKED, "agent-blocked")
        elif status == "cancelled" and self.state not in TERMINAL:
            self.transition(State.CANCELLED, "agent-cancelled")

    async def open_gate(self):
        gate = self.agent.pending_gate
        if gate is None:
            return None
        self.pending_question = gate.prompt
        target = State.WAITING_CONFIRM if gate.kind == "confirm" else State.WAITING_INFO
        self.transition(target, f"gate-{gate.kind}")
        self.arm_gate_timer()
        return gate

    def arm_gate_timer(self):
        self.cancel_gate_timer()
        timeout = self.config.budgets.confirm_timeout_s
        self.gate_timer = asyncio.create_task(self.expire_gate(timeout))

    def cancel_gate_timer(self):
        if self.gate_timer and not self.gate_timer.done():
            self.gate_timer.cancel()
        self.gate_timer = None

    async def expire_gate(self, timeout):
        """A gate that is never answered cancels the run. It never auto-approves."""
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        if self.agent and self.agent.pending_gate is not None:
            self.emit(GateResolved(kind=self.agent.pending_gate.kind, resolution="timeout"))
            self.agent.pending_gate = None
            await self.cancel("gate timeout")

    # -------------------------------------------------------------- snapshot

    def status(self):
        agent = self.agent
        return {
            "state": self.state.value,
            "goal": self.goal,
            "pending_question": self.pending_question,
            "run_status": agent.state["status"] if agent else "idle",
            "steps": len(agent.state["history"]) if agent else 0,
            "url": (agent.state["page"] or {}).get("url", "") if agent and agent.state["page"] else "",
        }
