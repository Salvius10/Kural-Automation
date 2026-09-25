"""Fused-answer handling, gates, corrections and cancellation (Plan §7.5, §7.6, §7.7)."""

import pytest
from conftest import FakeBrowser, FakeTextWriter, action, choice, drain, noul, page

import agent.agent as agent_module
import core.session as session_module
from core.field_cache import MISS
from core.session import Session, State

SEARCH_PAGE = page([action(1, "fill", "From"), action(2, "click", "Search")], fingerprint="fp1")
RESULT_PAGE = page([action(3, "click", "Book now"), action(4, "click", "Details")], fingerprint="fp2")


def decision(operation, target=None, targets=None, extra=None, confidence=0.9, actions=None):
    """A validated `choose()` result, as the model layer would have returned it."""
    targets = targets or {}
    resolved = None
    if target is not None and actions:
        resolved = next(a for a in actions if str(a["node"]) == str(target) or a["id"] == target)
    return {
        "choice": resolved["id"] if resolved else operation,
        "operation": operation,
        "target": target,
        "action": resolved,
        "confidence": confidence,
        "probabilities": {(resolved["id"] if resolved else operation): 0.9},
        "operation_probabilities": {operation: 0.9},
        "target_probabilities": targets,
        "target_confidence": confidence,
        "extra": extra or {},
        "raw_answers": {},
        "model": "jev-test",
        "usage": {},
        "latency_ms": 42,
        "request": {},
    }


@pytest.fixture
def scripted(monkeypatch):
    """Queue of decisions consumed by both the router and the agent loop."""
    queue = []

    async def fake_choose(page_state, goal, history, extra_questions=None, sites=None):
        if not queue:
            return decision("DONE")
        return queue.pop(0)

    monkeypatch.setattr(session_module, "choose", fake_choose)
    monkeypatch.setattr(agent_module, "choose", fake_choose)
    return queue


@pytest.fixture
def session(bus, scripted):
    browser = FakeBrowser([SEARCH_PAGE, RESULT_PAGE])
    made = Session(bus, browser, text_writer=FakeTextWriter())
    made.page = SEARCH_PAGE
    return made


def intent(name, extra=None):
    answers = {"intent": choice(name, ["new_task", "correction", "answer", "cancel", "noise"])}
    answers.update(extra or {})
    return answers


async def test_new_task_on_the_current_page_acts_without_navigating(session, scripted, events):
    scripted.append(
        decision(
            "CLICK",
            "2",
            extra=intent("new_task", {"start_here": noul(0.95)}),
            actions=SEARCH_PAGE["actions"],
        )
    )
    await session.utterance("search for flights to mumbai")
    await drain(session)
    assert session.browser.navigated == []
    assert ("Search", "click", None) in session.browser.acted
    assert [e.mode for e in events if e.type == "StartResolved"] == ["here"]


async def test_new_task_navigates_to_a_named_site(session, scripted):
    scripted.append(
        decision("CLICK", "2", extra=intent("new_task", {"start_here": noul(0.1)}), actions=SEARCH_PAGE["actions"])
    )
    await session.utterance("open ixigo and book a flight to mumbai")
    await drain(session)
    assert session.browser.navigated == ["https://www.ixigo.com"]
    assert session.goal == "book a flight to Mumbai"  # the normalizer canonicalises the city


async def test_unknown_start_falls_back_to_a_search_url(session, scripted):
    scripted.append(
        decision(
            "CLICK",
            "2",
            extra=intent("new_task", {"start_here": noul(0.05), "start_site": choice("none", ["none", "ixigo"])}),
            actions=SEARCH_PAGE["actions"],
        )
    )
    await session.utterance("find show timings for pvr grand galada")
    await drain(session)
    assert session.browser.navigated == ["https://www.google.com/search?q=search+words"]


async def test_low_confidence_intent_never_acts(session, scripted, events):
    scripted.append(
        decision(
            "CLICK",
            "2",
            extra={"intent": choice("new_task", ["new_task", "noise"], confidence=0.2)},
            actions=SEARCH_PAGE["actions"],
        )
    )
    await session.utterance("mmhm something something")
    assert session.browser.acted == []
    assert session.state is State.IDLE
    assert any("Not sure what you meant" in e.text for e in events if e.type == "Notice")


async def test_a_spoken_stop_cancels_without_a_model_call(session, scripted):
    session.state = State.RUNNING
    await session.utterance("stop")
    assert scripted == []  # nothing consumed: no round trip on the kill switch
    assert session.state is State.CANCELLED


async def test_correction_updates_the_goal_between_ticks(session, scripted, events):
    scripted.append(
        decision("CLICK", "2", extra=intent("new_task", {"start_here": noul(0.95)}), actions=SEARCH_PAGE["actions"])
    )
    await session.utterance("book a flight on friday")
    await drain(session)
    scripted.append(decision("CLICK", "2", extra=intent("correction"), actions=SEARCH_PAGE["actions"]))
    session.state = State.RUNNING
    await session.utterance("no make it saturday")
    await drain(session)
    updates = [e for e in events if e.type == "GoalUpdated"]
    assert updates[-1].reason == "correction"
    assert "Correction:" in updates[-1].goal
    assert session.agent.next_goal is not None or session.agent.goal_version == 2


async def test_correction_invalidates_the_field_cache(session, scripted):
    scripted.append(
        decision("CLICK", "2", extra=intent("new_task", {"start_here": noul(0.95)}), actions=SEARCH_PAGE["actions"])
    )
    await session.utterance("book a flight on friday")
    await drain(session)
    session.field_cache.values[(1, 1)] = "Friday"
    session.agent.set_goal("book a flight on saturday", "correction")
    session.agent.apply_goal()
    assert session.field_cache.get(1, 1) is MISS


async def test_a_risky_click_opens_a_confirm_gate(session, scripted, events, monkeypatch):
    session.browser.index = 1
    session.page = RESULT_PAGE
    scripted.append(
        decision("CLICK", "3", extra=intent("new_task", {"start_here": noul(0.95)}), actions=RESULT_PAGE["actions"])
    )
    await session.utterance("book the cheapest flight")
    await drain(session)
    assert session.state is State.WAITING_CONFIRM
    assert session.browser.acted == []
    gate = [e for e in events if e.type == "GateOpened"][0]
    assert gate.kind == "confirm" and "Book now" in gate.prompt


async def test_saying_no_to_a_gate_cancels_and_never_acts(session, scripted):
    session.browser.index = 1
    session.page = RESULT_PAGE
    scripted.append(
        decision("CLICK", "3", extra=intent("new_task", {"start_here": noul(0.95)}), actions=RESULT_PAGE["actions"])
    )
    await session.utterance("book the cheapest flight")
    await drain(session)
    scripted.append(decision("CLICK", "3", extra=intent("confirm_no"), actions=RESULT_PAGE["actions"]))
    await session.utterance("no")
    assert session.browser.acted == []
    assert session.state is State.CANCELLED


async def test_saying_yes_releases_exactly_one_action(session, scripted):
    session.browser.index = 1
    session.page = RESULT_PAGE
    scripted.append(
        decision("CLICK", "3", extra=intent("new_task", {"start_here": noul(0.95)}), actions=RESULT_PAGE["actions"])
    )
    await session.utterance("book the cheapest flight")
    await drain(session)
    scripted.append(decision("CLICK", "3", extra=intent("confirm_yes"), actions=RESULT_PAGE["actions"]))
    await session.utterance("yes")
    await drain(session)
    assert [a for a in session.browser.acted if a[0] == "Book now"] == [("Book now", "click", None)]


async def test_a_gate_timeout_cancels_and_never_approves(session, scripted):
    session.browser.index = 1
    session.page = RESULT_PAGE
    session.config.budgets.confirm_timeout_s = 0.01
    scripted.append(
        decision("CLICK", "3", extra=intent("new_task", {"start_here": noul(0.95)}), actions=RESULT_PAGE["actions"])
    )
    await session.utterance("book the cheapest flight")
    await drain(session)
    import asyncio

    await asyncio.sleep(0.05)
    assert session.browser.acted == []
    assert session.state is State.CANCELLED


@pytest.fixture
def no_value(monkeypatch):
    """The text model reports that the goal does not contain this field's value."""

    async def field_text(context):
        return None, {"model": "test", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(agent_module, "field_text", field_text)


async def test_a_missing_field_value_opens_an_info_gate(session, scripted, events, no_value):
    scripted.append(
        decision("TYPE_TEXT", "1", extra=intent("new_task", {"start_here": noul(0.95)}), actions=SEARCH_PAGE["actions"])
    )
    await session.utterance("find me a flight")
    await drain(session)
    assert session.state is State.WAITING_INFO
    assert session.browser.acted == []
    assert [e.kind for e in events if e.type == "GateOpened"] == ["info"]


async def test_a_cached_null_opens_the_gate_without_a_model_call(session, scripted):
    """The cached NULL path: no synchronous text call is made at all (Plan §7.6)."""
    from agent.agent import Agent, GateRequired

    made = Agent("book a flight", session.browser, field_cache=session.field_cache, risk=session.risk)
    made.state["page"] = SEARCH_PAGE
    session.field_cache.values[(1, 1)] = None
    gate = await made.gate_for(SEARCH_PAGE["actions"][0], decision("TYPE_TEXT", "1"))
    assert isinstance(gate, GateRequired) and gate.kind == "info"


async def test_answering_a_question_appends_it_to_the_goal(session, scripted, events, no_value):
    scripted.append(
        decision("TYPE_TEXT", "1", extra=intent("new_task", {"start_here": noul(0.95)}), actions=SEARCH_PAGE["actions"])
    )
    await session.utterance("find me a flight")
    await drain(session)
    scripted.append(decision("DONE", extra=intent("answer")))
    await session.utterance("from chennai")
    await drain(session)
    goal = [e for e in events if e.type == "GoalUpdated"][-1]
    assert goal.reason == "answer" and "Chennai" in goal.goal


async def test_a_blocked_field_is_refused_by_the_executor(session, scripted, events):
    otp_page = page([action(5, "fill", "Enter OTP")], fingerprint="fp3")
    session.browser.pages = [otp_page]
    session.browser.index = 0
    session.page = otp_page
    scripted.append(
        decision("TYPE_TEXT", "5", extra=intent("new_task", {"start_here": noul(0.95)}), actions=otp_page["actions"])
    )
    await session.utterance("log me in")
    await drain(session)
    assert session.browser.acted == []
    assert [e.kind for e in events if e.type == "GateOpened"] == ["manual"]


async def test_cancel_stops_the_run(session, scripted):
    scripted.append(
        decision("CLICK", "2", extra=intent("new_task", {"start_here": noul(0.95)}), actions=SEARCH_PAGE["actions"])
    )
    await session.utterance("book a flight")
    await drain(session)
    scripted.append(decision("DONE", extra=intent("cancel")))
    await session.utterance("forget it")
    assert session.state is State.CANCELLED
    assert session.agent.cancelled


async def test_a_page_question_is_answered_without_acting(session, scripted, events):
    scripted.append(decision("DONE", extra=intent("page_question")))
    await session.utterance("what is the price shown")
    assert session.browser.acted == []
    assert any(e.text == "an answer" for e in events if e.type == "Notice")


RESULTS_PAGE = page(
    [action(i, "click", f"Flight option {i}") for i in range(10, 22)],
    url="https://example.test/results",
    fingerprint="fp9",
)


async def test_an_ambiguous_choice_pauses_instead_of_picking(session, scripted, events):
    """"Book me a flight" on a results page must ask which one (Plan §7.11.6)."""
    session.browser.pages = [RESULTS_PAGE]
    session.page = RESULTS_PAGE
    scripted.append(
        decision(
            "CLICK",
            "10",
            extra=intent("new_task", {"start_here": noul(0.95)}),
            actions=RESULTS_PAGE["actions"],
        )
    )
    scripted.append(
        decision(
            "CLICK", "11", extra={"needs_user_choice": noul(0.9)}, actions=RESULTS_PAGE["actions"]
        )
    )
    await session.utterance("book me a flight")
    await drain(session)
    assert session.state is State.WAITING_INFO
    gate = [e for e in events if e.type == "GateOpened"][-1]
    assert gate.kind == "choice" and "which one" in gate.prompt


async def test_a_stated_rule_does_not_pause(session, scripted, events):
    """The model says the goal already picks one, so the agent just clicks."""
    session.browser.pages = [RESULTS_PAGE]
    session.page = RESULTS_PAGE
    scripted.append(
        decision(
            "CLICK",
            "10",
            extra=intent("new_task", {"start_here": noul(0.95), "needs_user_choice": noul(0.05)}),
            actions=RESULTS_PAGE["actions"],
        )
    )
    await session.utterance("book the cheapest flight")
    await drain(session)
    assert ("Flight option 10", "click", None) in session.browser.acted
    assert [e for e in events if e.type == "GateOpened" and e.kind == "choice"] == []


def test_the_choice_head_only_rides_on_result_like_pages():
    from agent.agent import Agent

    made = Agent("book a flight", FakeBrowser())
    made.state["page"] = SEARCH_PAGE
    assert made.choice_gate_question(SEARCH_PAGE) == {}          # two controls: not a results page
    assert "needs_user_choice" in made.choice_gate_question(RESULTS_PAGE)
    made.choice_asked_for = made.goal_version
    assert made.choice_gate_question(RESULTS_PAGE) == {}          # asked once per goal version


async def test_a_disputed_done_keeps_working(session, scripted, events):
    """DONE is a claim: when the verifier disagrees, the run continues (Plan §10)."""
    scripted.append(
        decision("DONE", extra=intent("new_task", {"start_here": noul(0.95), "task_done": noul(0.1)}))
    )
    scripted.append(decision("DONE", extra={"task_done": noul(0.95)}))
    await session.utterance("find flights to mumbai")
    await drain(session)
    statuses = [e.status for e in events if e.type == "RunStatusChanged"]
    assert "done-disputed" in statuses
    assert session.state is State.DONE  # the second, confirmed DONE is accepted


async def test_a_confirmed_done_is_accepted_immediately(session, scripted, events):
    scripted.append(
        decision("DONE", extra=intent("new_task", {"start_here": noul(0.95), "task_done": noul(0.9)}))
    )
    await session.utterance("find flights to mumbai")
    await drain(session)
    assert session.state is State.DONE
    assert "done-disputed" not in [e.status for e in events if e.type == "RunStatusChanged"]


async def test_a_disputed_done_cannot_loop_forever(session, scripted):
    """A verifier that never agrees must not trap the run."""
    from agent.agent import MAX_DISPUTED_DONE

    for _ in range(MAX_DISPUTED_DONE + 3):
        scripted.append(decision("DONE", extra={"task_done": noul(0.0)}))
    scripted.insert(0, decision("DONE", extra=intent("new_task", {"start_here": noul(0.95), "task_done": noul(0.0)})))
    await session.utterance("find flights to mumbai")
    await drain(session)
    assert session.state is State.DONE
    assert session.agent.done_disputed <= MAX_DISPUTED_DONE + 1


def test_the_profile_reaches_mercury_but_never_an_event():
    """Plan §11.11: profile values go to Mercury only, and are never logged."""
    from core.field_cache import FieldCache
    from core.profile import flatten

    facts = flatten({"name": "A Name", "email": "a@b.test", "address": {"city": "Chennai", "line1": ""}})
    assert facts == {"name": "A Name", "email": "a@b.test", "address_city": "Chennai"}
    cache = FieldCache(profile=facts)
    assert cache.profile["name"] == "A Name"
    # Nothing the cache emits carries a value -- only counts.
    from core.events import FieldValuesCached

    fields = set(FieldValuesCached.model_fields)
    assert fields.isdisjoint({"values", "profile", "facts"})


def test_the_profile_ignores_stray_keys():
    from core.profile import flatten

    assert flatten({"password": "hunter2", "name": "A"}) == {"name": "A"}
    assert flatten(None) == {}
    assert flatten({"name": "   "}) == {}


async def test_the_agent_returns_to_the_opener_when_a_followed_tab_closes():
    """A popup that closes under the agent must not strand the run."""
    from agent.agent import Agent

    class ClosingTab(FakeBrowser):
        def __init__(self):
            super().__init__([SEARCH_PAGE])
            self.failures = 1
            self.returned = False

        async def observe(self, screenshot=False):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("target closed")
            return await super().observe(screenshot=screenshot)

        async def return_to_opener(self):
            self.returned = True
            return ("tab-2", "tab-1")

    browser = ClosingTab()
    made = Agent("do the thing", browser)
    made.state["page"] = SEARCH_PAGE
    made.state["started_at"] = 0.0
    made.state["history"].append({"kind": "click", "action": "x", "page_changed": None})
    await made.after_action(SEARCH_PAGE)
    assert browser.returned
    assert made.state["page"] is SEARCH_PAGE


async def test_a_dead_tab_with_no_opener_still_raises():
    from agent.agent import Agent

    class DeadTab(FakeBrowser):
        async def observe(self, screenshot=False):
            raise RuntimeError("target closed")

    made = Agent("do the thing", DeadTab())
    made.state["page"] = SEARCH_PAGE
    made.state["started_at"] = 0.0
    made.state["history"].append({"kind": "click", "action": "x", "page_changed": None})
    with pytest.raises(RuntimeError):
        await made.after_action(SEARCH_PAGE)


async def test_a_superseded_risk_score_is_cancelled():
    """Pages settle through several fingerprints; only the last is ever acted on."""
    import asyncio

    from core.risk import RiskScorer

    scorer = RiskScorer(threshold=0.3)
    started = []

    async def never_finishes(*args, **kwargs):
        started.append(1)
        await asyncio.sleep(60)

    scorer._score = never_finishes
    first = scorer.score(page([action(1, "click", "Details")], fingerprint="settling"))
    second = scorer.score(page([action(1, "click", "Details")], fingerprint="settled"))
    await asyncio.sleep(0)
    assert first.cancelled() or first.done()
    assert not second.done()
    assert list(scorer.inflight) == ["settled"]
    second.cancel()


async def test_a_superseded_field_prefetch_is_cancelled():
    import asyncio

    from core.field_cache import FieldCache

    cache = FieldCache()

    async def never_finishes(*args, **kwargs):
        await asyncio.sleep(60)

    cache._fill = never_finishes
    first = cache.prefetch(page([action(1, "fill", "From")], fingerprint="settling"), "goal", 1)
    second = cache.prefetch(page([action(1, "fill", "From")], fingerprint="settled"), "goal", 1)
    await asyncio.sleep(0)
    assert first.cancelled() or first.done()
    assert len(cache.inflight) == 1
    second.cancel()


async def test_a_goal_change_supersedes_an_in_flight_prefetch():
    import asyncio

    from core.field_cache import FieldCache

    cache = FieldCache()

    async def never_finishes(*args, **kwargs):
        await asyncio.sleep(60)

    cache._fill = never_finishes
    same_page = page([action(1, "fill", "From")], fingerprint="same")
    old = cache.prefetch(same_page, "fly friday", 1)
    new = cache.prefetch(same_page, "fly saturday", 2)
    await asyncio.sleep(0)
    assert old.cancelled() or old.done()
    assert list(cache.inflight) == [(2, "same")]
    new.cancel()
