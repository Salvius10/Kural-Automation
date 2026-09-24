"""Every transition in the table, and the ones that must never happen (Plan §8)."""

import pytest
from conftest import FakeBrowser

from core.session import TERMINAL, TRANSITIONS, IllegalTransition, Session, State


@pytest.fixture
def session(bus):
    return Session(bus, FakeBrowser())


def test_every_state_has_a_transition_row():
    assert set(TRANSITIONS) == set(State)


@pytest.mark.parametrize(
    "source,target",
    [(source, target) for source, targets in TRANSITIONS.items() for target in targets],
)
def test_legal_transitions_are_allowed(session, source, target):
    session.state = source
    assert session.transition(target, "test") == target


@pytest.mark.parametrize(
    "source,target",
    [
        (source, target)
        for source in State
        for target in State
        if target not in TRANSITIONS[source] and target != source
    ],
)
def test_illegal_transitions_raise(session, source, target):
    session.state = source
    with pytest.raises(IllegalTransition):
        session.transition(target, "test")


def test_illegal_transition_is_logged(session, events):
    session.state = State.IDLE
    with pytest.raises(IllegalTransition):
        session.transition(State.DONE, "test")
    assert any(e.type == "Error" and "illegal" in e.message for e in events)


def test_a_transition_to_the_same_state_is_a_no_op(session, events):
    session.state = State.RUNNING
    assert session.transition(State.RUNNING, "test") == State.RUNNING
    assert not [e for e in events if e.type == "StateChanged"]


def test_transitions_emit_state_changed(session, events):
    session.state = State.IDLE
    session.transition(State.LISTENING, "key-down")
    changed = [e for e in events if e.type == "StateChanged"]
    assert (changed[0].from_state, changed[0].to_state, changed[0].trigger) == ("IDLE", "LISTENING", "key-down")


def test_a_new_task_can_start_from_any_terminal_state(session):
    for terminal in TERMINAL:
        session.state = terminal
        assert session.transition(State.ROUTING, "utterance") == State.ROUTING


def test_key_down_from_terminal_states_listens_again(session):
    for state in (State.IDLE, State.DONE, State.BLOCKED, State.CANCELLED):
        session.state = state
        assert session.key_down() == State.LISTENING


def test_key_down_while_running_does_not_pause_the_run(session):
    session.state = State.RUNNING
    assert session.key_down() == State.RUNNING
