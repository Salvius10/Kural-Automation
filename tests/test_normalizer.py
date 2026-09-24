"""The normalizer is pure and on the critical path: test it hard (Plan §7.4)."""

from datetime import date

import pytest

from voice.normalizer import normalize, resolve_date, strip_site_phrase, words_to_number

FRIDAY = date(2026, 9, 25)


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("chen eye", "Chennai"),
        ("chennai", "Chennai"),
        ("bangalore", "Bengaluru"),
        ("bangaluru", "Bengaluru"),
        ("bengaluru", "Bengaluru"),
        ("bombay", "Mumbai"),
        ("madras", "Chennai"),
    ],
)
def test_city_aliases_resolve_to_one_spelling(spoken, expected):
    text, facts = normalize(f"flights to {spoken}", today=FRIDAY)
    assert expected in text
    assert expected in facts["cities"]


def test_unknown_words_are_never_dropped():
    text, _ = normalize("order two kg of nannari sarbath", today=FRIDAY)
    assert "nannari sarbath" in text


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("today", "2026-09-25"),
        ("tomorrow", "2026-09-26"),
        ("day after tomorrow", "2026-09-27"),
        ("saturday", "2026-09-26"),
        ("next friday", "2026-10-02"),
        ("2nd October", "2026-10-02"),
        ("october 2", "2026-10-02"),
    ],
)
def test_dates_resolve_to_absolute_iso(phrase, expected):
    text, facts = normalize(f"book a flight on {phrase}", today=FRIDAY)
    assert expected in text
    assert facts["date"] == expected


def test_same_weekday_means_a_week_later():
    assert resolve_date("friday", FRIDAY).isoformat() == "2026-10-02"


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("four thousand two hundred", 4200),
        ("two lakh", 200000),
        ("fifteen thousand", 15000),
        ("one crore", 10000000),
        ("twenty five", 25),
    ],
)
def test_spoken_numbers(spoken, expected):
    assert words_to_number(spoken) == expected


def test_currency_is_normalized_after_number_words():
    text, facts = normalize("earphones under four thousand two hundred rupees", today=FRIDAY)
    assert "₹4200" in text
    assert facts["amount"] == 4200


def test_rupee_symbol_and_rs_prefix():
    for spoken in ("pay rs 4,850 now", "pay ₹4850 now", "pay 4850 rupees now"):
        text, facts = normalize(spoken, today=FRIDAY)
        assert facts["amount"] == 4850, spoken
        assert "₹4850" in text


def test_from_and_to_slots():
    _, facts = normalize("find flights from chennai to mumbai tomorrow", today=FRIDAY)
    assert facts["from"] == "Chennai"
    assert facts["to"] == "Mumbai"
    assert facts["date"] == "2026-09-26"


def test_site_is_recognised_and_strippable():
    text, facts = normalize("open ixigo and book a flight", today=FRIDAY)
    assert facts["site"] == "ixigo"
    assert strip_site_phrase(text, "ixigo") == "book a flight"


def test_site_phrase_is_left_alone_when_not_leading():
    assert strip_site_phrase("book a flight using ixigo", "ixigo") == "book a flight using ixigo"


def test_empty_input_is_safe():
    assert normalize("", today=FRIDAY) == ("", {})
    assert normalize("   ", today=FRIDAY) == ("", {})


def test_is_fast_enough_for_the_budget():
    import time

    start = time.perf_counter()
    for _ in range(100):
        normalize("find flights from chen eye to bangalore next friday under 5000 rupees", today=FRIDAY)
    assert (time.perf_counter() - start) / 100 * 1000 < 5  # Plan §4.1: normalizer <= 5 ms
