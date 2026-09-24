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


@pytest.mark.parametrize("answer", [{"probability": 1.5}, {"probability": "yes"}, {}, None, {"probabilities": {}}])
def test_malformed_booleans_are_rejected(answer):
    with pytest.raises(ValueError):
        validate_noul(answer)


def test_boolean_shapes_both_parse():
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
