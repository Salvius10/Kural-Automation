"""The guards that must never weaken (Plan §11): validation, blocked fields, risk."""

import pytest
from conftest import action, choice, page

from agent.browser import blocked_label
from agent.model import action_space, validate_choice, validate_noul
from core.field_cache import editable_fields, is_blocked_label
from core.risk import keyword_risky


@pytest.mark.parametrize(
    "answer,ids",
    [
        ({"choice": "2", "probabilities": {"1": 1.0}, "confidence": 0.9}, {"1": "a"}),          # choice not offered
        ({"choice": "1", "probabilities": {"1": 0.3}, "confidence": 0.9}, {"1": "a"}),          # not summing to 1
        ({"choice": "1", "probabilities": {"1": 0.4, "2": 0.6}, "confidence": 0.9},
         {"1": "a", "2": "b"}),                                                                 # not the argmax
        ({"choice": "1", "probabilities": {"1": 1.0}, "confidence": 5}, {"1": "a"}),            # bad confidence
        ({"choice": "1", "probabilities": {"1": "high"}, "confidence": 0.9}, {"1": "a"}),       # not a number
        ({"choice": "1", "probabilities": {"1": 0.5, "2": 0.5}, "confidence": 0.9}, {"1": "a"}),  # unknown id
        ({}, {"1": "a"}),
        (None, {"1": "a"}),
    ],
)
def test_malformed_choices_are_rejected(answer, ids):
    with pytest.raises(ValueError):
        validate_choice(answer or {}, ids)


def test_valid_choice_passes():
    answer = choice("1", ["1", "2"])
    assert validate_choice(answer, {"1": "a", "2": "b"})["choice"] == "1"


def test_the_live_choice_shape_parses():
    """Captured from jev-1.13.0; the extra `type` key must not upset validation."""
    live = {"type": "choice", "choice": "TYPE_TEXT", "confidence": 0.92,
            "probabilities": {"TYPE_TEXT": 0.93, "BLOCKED": 0.05, "CLICK": 0.02, "DONE": 0.0,
                              "WAIT": 0.0, "NAVIGATE": 0.0}}
    ids = {k: "" for k in live["probabilities"]}
    assert validate_choice(live, ids)["choice"] == "TYPE_TEXT"


@pytest.mark.parametrize("answer", [{"probability": 1.5}, {"probability": "yes"}, {}, None, {"probabilities": {}}])
def test_malformed_booleans_are_rejected(answer):
    with pytest.raises(ValueError):
        validate_noul(answer)


def test_the_live_boolean_shape_parses():
    """Verified against jev-1.13.0: a bare probability under `noul`, no confidence."""
    answer = validate_noul({"type": "noul", "noul": 0.9})
    assert answer["probability"] == pytest.approx(0.9)
    assert answer["confidence"] == pytest.approx(0.8)  # |p - 0.5| * 2 stands in


def test_alternative_boolean_shapes_still_parse():
    assert validate_noul({"probability": 0.7})["probability"] == pytest.approx(0.7)
    assert validate_noul({"probabilities": {"yes": 0.3, "no": 0.7}})["probability"] == pytest.approx(0.3)


@pytest.mark.parametrize(
    "label",
    ["Password", "Enter OTP", "CVV", "Card number", "UPI PIN", "Aadhaar number", "Security code"],
)
def test_blocked_fields_are_recognised(label):
    assert blocked_label(label)
    assert is_blocked_label(label)


@pytest.mark.parametrize("label", ["Email", "Passenger name", "Pincode", "Search", "Departure"])
def test_ordinary_fields_are_not_blocked(label):
    assert not blocked_label(label)


def test_blocked_fields_never_reach_the_field_cache():
    fields = editable_fields(page([action(1, "fill", "Email"), action(2, "fill", "Card number")]))
    assert [f["label"] for f in fields] == ["Email"]


@pytest.mark.parametrize(
    "label",
    ["Pay now", "Book", "Confirm booking", "Place order", "Delete", "Proceed to pay", "Sign out"],
)
def test_risky_labels_hit_the_keyword_floor(label):
    assert keyword_risky(label)


@pytest.mark.parametrize("label", ["Facebook", "Bookmarks", "Search flights", "Filter", "Next page"])
def test_safe_labels_do_not_hit_the_floor(label):
    assert not keyword_risky(label)


def test_action_space_indexes_only_observed_elements():
    elements, targets, controls = action_space(
        page([action(7, "click", "Search"), action(9, "fill", "From")])["actions"]
    )
    assert [e["label"] for e in elements] == ["Search", "From"]
    assert set(targets) == {"CLICK", "TYPE_TEXT"}
    assert targets["CLICK"]["1"]["node"] == 7
    assert "WAIT" in controls


def test_navigate_targets_are_code_owned_urls():
    sites = {"ixigo": {"url": "https://www.ixigo.com", "about": "flights"}}
    _, targets, _ = action_space(page([action(1, "click", "Search")])["actions"], sites=sites)
    assert targets["NAVIGATE"]["ixigo"]["url"] == "https://www.ixigo.com"
    assert targets["NAVIGATE"]["search"]["url"] is None


def test_only_a_tab_we_created_is_ever_closed(monkeypatch):
    """Regression: `open_in='new_tab'` with no URL attached to the user's tab and
    then closed it, because ownership was inferred from config instead of tracked."""
    import agent.browser as browser_module

    created, closed, switched = [], [], []

    def fake_cdp(method, **params):
        if method == "Target.createTarget":
            created.append(params)
            return {"targetId": "ours"}
        if method == "Target.closeTarget":
            closed.append(params["targetId"])
        return {}

    monkeypatch.setattr(browser_module, "ensure_daemon", lambda *a, **k: None)
    monkeypatch.setattr(browser_module, "cdp", fake_cdp)
    monkeypatch.setattr(browser_module, "switch_tab", lambda t: switched.append(t) or "sid")
    monkeypatch.setattr(browser_module, "current_tab", lambda: {"targetId": "users", "url": "https://x.test/"})
    monkeypatch.setattr(
        browser_module, "list_tabs", lambda **k: [{"targetId": "users", "url": "https://x.test/", "title": "x"}]
    )
    monkeypatch.setattr(browser_module, "activate_tab", lambda t: t)

    # Attaching to the user's tab: never closed, whatever the config says.
    attached = browser_module.Browser(open_in="current_tab")
    assert attached.target == "users" and attached.owned is False
    attached.close()
    assert closed == []

    # new_tab with no URL must still create a tab, and that one is ours to close.
    own = browser_module.Browser(open_in="new_tab")
    assert created and own.target == "ours" and own.owned is True
    own.close()
    assert closed == ["ours"]


def test_following_a_page_opened_tab_does_not_make_it_ours(monkeypatch):
    import agent.browser as browser_module

    closed = []

    def fake_cdp(method, **params):
        if method == "Target.createTarget":
            return {"targetId": "ours"}
        if method == "Target.closeTarget":
            closed.append(params["targetId"])
        return {}

    monkeypatch.setattr(browser_module, "ensure_daemon", lambda *a, **k: None)
    monkeypatch.setattr(browser_module, "cdp", fake_cdp)
    monkeypatch.setattr(browser_module, "switch_tab", lambda t: "sid")
    monkeypatch.setattr(browser_module, "current_tab", lambda: {"targetId": "users", "url": "https://x.test/"})
    monkeypatch.setattr(
        browser_module, "list_tabs", lambda **k: [{"targetId": "users", "url": "https://x.test/", "title": "x"}]
    )
    monkeypatch.setattr(browser_module, "activate_tab", lambda t: t)

    made = browser_module.Browser(open_in="new_tab")
    assert made.owned is True
    made.switch("popup")          # a tab the page opened
    assert made.owned is False
    made.close()
    assert closed == []           # the popup is not ours to close


def test_a_dead_remembered_tab_is_not_used(monkeypatch):
    """current_tab() names a tab the daemon remembers; it may already be closed."""
    import agent.browser as browser_module

    monkeypatch.setattr(browser_module, "ensure_daemon", lambda *a, **k: None)
    monkeypatch.setattr(browser_module, "cdp", lambda m, **p: {"targetId": "ours"})
    monkeypatch.setattr(browser_module, "switch_tab", lambda t: "sid")
    monkeypatch.setattr(browser_module, "activate_tab", lambda t: t)
    # The daemon still points at a tab that is no longer in the tab list.
    monkeypatch.setattr(browser_module, "current_tab", lambda: {"targetId": "closed", "url": "https://x.test/"})
    monkeypatch.setattr(
        browser_module, "list_tabs", lambda **k: [{"targetId": "alive", "url": "https://y.test/", "title": "y"}]
    )

    made = browser_module.Browser(open_in="current_tab")
    assert made.target == "alive"       # not the remembered, dead one


def test_a_dead_session_is_recovered_once(monkeypatch):
    """A cached session id is not valid forever; `call` re-attaches and retries."""
    import agent.browser as browser_module

    calls = []

    def fake_cdp(method, session_id=None, **params):
        calls.append((method, session_id))
        if method == "Page.navigate" and session_id == "stale":
            raise RuntimeError("{'code': -32001, 'message': 'Session with given id not found.'}")
        return {"targetId": "ours"}

    monkeypatch.setattr(browser_module, "ensure_daemon", lambda *a, **k: None)
    monkeypatch.setattr(browser_module, "cdp", fake_cdp)
    monkeypatch.setattr(browser_module, "switch_tab", lambda t: "fresh")
    monkeypatch.setattr(browser_module, "activate_tab", lambda t: t)
    monkeypatch.setattr(browser_module, "current_tab", lambda: {"targetId": "alive", "url": "https://x.test/"})
    monkeypatch.setattr(
        browser_module, "list_tabs", lambda **k: [{"targetId": "alive", "url": "https://x.test/", "title": "x"}]
    )

    made = browser_module.Browser(open_in="current_tab")
    assert made.owned is False                   # started on the user's tab
    made.session = "stale"
    made.call("Page.navigate", url="https://x.test/")
    assert made.session == "fresh"
    assert made.reattached == 1
    assert ("Page.navigate", "fresh") in calls   # retried on the new session
    # Recovery takes a fresh tab rather than navigating a tab the user is reading.
    assert made.owned is True
    assert ("Target.createTarget", None) in [(m, s) for m, s in calls]


def test_a_dead_session_mid_action_never_replays_the_action(monkeypatch):
    """Upstream's rule: never retry a browser mutation."""
    import agent.browser as browser_module

    executed = []

    def fake_operation(request):
        executed.append(request["action"]["id"])
        raise RuntimeError("Session with given id not found")

    monkeypatch.setattr(browser_module, "ensure_daemon", lambda *a, **k: None)
    monkeypatch.setattr(browser_module, "cdp", lambda m, **p: {"targetId": "ours"})
    monkeypatch.setattr(browser_module, "switch_tab", lambda t: "fresh")
    monkeypatch.setattr(browser_module, "activate_tab", lambda t: t)
    monkeypatch.setattr(browser_module, "current_tab", lambda: {"targetId": "alive", "url": "https://x.test/"})
    monkeypatch.setattr(
        browser_module, "list_tabs", lambda **k: [{"targetId": "alive", "url": "https://x.test/", "title": "x"}]
    )
    monkeypatch.setattr(browser_module, "browser_operation", fake_operation)

    made = browser_module.Browser(open_in="current_tab")
    made.fresh = lambda page, action=None: True
    clicked = action(1, "click", "Search")
    with pytest.raises(browser_module.StalePage):
        made.act(clicked, page([clicked]))
    assert executed == ["e1"]      # attempted exactly once, never replayed
