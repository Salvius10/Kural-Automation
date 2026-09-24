"""Rules only, no network, target < 5 ms (Plan §7.4).

Fixes what speech-to-text reliably gets wrong for Indian-accented English --
city names, dates, rupee amounts -- and hands the extracted values on as
`facts` so the text model has less to infer. It never drops a word it does not
understand; it only rewrites what it is confident about.
"""

import functools
import re
from datetime import date, datetime, timedelta

import dateparser
import yaml
from rapidfuzz import fuzz, process

from core.config import ROOT, settings

FUZZY_THRESHOLD = 90

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
SCALES = {"hundred": 100, "thousand": 1000, "lakh": 100000, "lakhs": 100000, "crore": 10000000, "crores": 10000000}
NUMBER_WORDS = set(UNITS) | set(SCALES) | {"and"}

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
MONTH_PATTERN = "|".join(MONTHS) + "|" + "|".join(m[:3] for m in MONTHS)

DATE_PATTERNS = [
    re.compile(r"\b(day after tomorrow|tomorrow|today|tonight)\b", re.I),
    re.compile(r"\b((?:next|this|coming)\s+(?:" + "|".join(WEEKDAYS) + r"))\b", re.I),
    re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b", re.I),
    re.compile(r"\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:" + MONTH_PATTERN + r")(?:\s+\d{4})?)\b", re.I),
    re.compile(r"\b((?:" + MONTH_PATTERN + r")\s+\d{1,2}(?:st|nd|rd|th)?(?:\s+\d{4})?)\b", re.I),
    re.compile(r"\bon the (\d{1,2}(?:st|nd|rd|th))\b", re.I),
]

CURRENCY = re.compile(
    r"(?:(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:rupees|rs\.?|inr))",
    re.I,
)
NUMBER_PHRASE = re.compile(r"\b((?:" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")(?:[\s-]+(?:"
                          + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r"))*)\b", re.I)


@functools.lru_cache(maxsize=1)
def load_gazetteer():
    path = ROOT / "data" / "gazetteer.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data or {}


@functools.lru_cache(maxsize=1)
def load_sites():
    path = ROOT / settings().navigation.sites_file
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data or {}


@functools.lru_cache(maxsize=1)
def alias_index():
    """alias -> (canonical, category). Built once; matched with rapidfuzz."""
    index = {}
    gazetteer = load_gazetteer()
    for category in ("cities", "airports", "people"):
        for canonical, aliases in (gazetteer.get(category) or {}).items():
            index[canonical.lower()] = (canonical, category)
            for alias in aliases or []:
                index[str(alias).lower()] = (canonical, category)
    for site_id, site in (load_sites().get("sites") or {}).items():
        for alias in site.get("aliases", []) + [site_id]:
            index[str(alias).lower()] = (site_id, "sites")
    return index


def keyterms(limit=50):
    """The most useful names to bias AssemblyAI with (Plan §7.3)."""
    gazetteer = load_gazetteer()
    terms = list((gazetteer.get("cities") or {}).keys())
    terms += list((load_sites().get("sites") or {}).keys())
    terms += list((gazetteer.get("people") or {}).keys())
    return terms[:limit]


def words_to_number(phrase):
    total, current = 0, 0
    seen = False
    for word in re.split(r"[\s-]+", phrase.lower()):
        if word == "and" or not word:
            continue
        if word in UNITS:
            current += UNITS[word]
            seen = True
        elif word in SCALES:
            scale = SCALES[word]
            if scale >= 1000:
                total += max(current, 1) * scale
                current = 0
            else:
                current = max(current, 1) * scale
            seen = True
        else:
            return None
    return total + current if seen else None


def resolve_date(phrase, today=None):
    today = today or date.today()
    lowered = phrase.lower().strip()
    if lowered in ("today", "tonight"):
        return today
    if lowered == "tomorrow":
        return today + timedelta(days=1)
    if lowered == "day after tomorrow":
        return today + timedelta(days=2)
    # Weekdays are resolved here, not by dateparser: "next friday" is ambiguous
    # enough that it needs one rule we can state and the user can correct by
    # voice -- the next occurrence strictly in the future (DECISIONS.md).
    weekday = re.fullmatch(r"(?:(next|this|coming)\s+)?(" + "|".join(WEEKDAYS) + r")", lowered)
    if weekday:
        ahead = (WEEKDAYS.index(weekday.group(2)) - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)
    parsed = dateparser.parse(
        phrase,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": datetime(today.year, today.month, today.day),
            "TIMEZONE": settings().timezone,
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    return parsed.date() if parsed else None


def fix_names(text):
    """Fuzzy-match tokens and bigrams against the gazetteer; only high-confidence hits rewrite."""
    index = alias_index()
    choices = list(index)
    tokens = text.split()
    out, facts, i = [], {}, 0
    while i < len(tokens):
        for size in (3, 2, 1):
            if i + size > len(tokens):
                continue
            chunk = " ".join(tokens[i : i + size])
            bare = re.sub(r"[^\w\s]", "", chunk).lower()
            if not bare or bare in NUMBER_WORDS:
                continue
            match = process.extractOne(bare, choices, scorer=fuzz.ratio, score_cutoff=FUZZY_THRESHOLD)
            if not match:
                continue
            canonical, category = index[match[0]]
            if category == "sites":
                facts.setdefault("site", canonical)
                out.append(chunk)
            else:
                facts.setdefault(category, []).append(canonical)
                trailing = re.sub(r"[\w\s]", "", chunk[-1]) if chunk else ""
                out.append(canonical + trailing)
            i += size
            break
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out), facts


def normalize(text, today=None):
    """Returns (clean text, facts). Pure: same input, same output."""
    if not text or not text.strip():
        return "", {}
    today = today or date.today()
    text = re.sub(r"\s+", " ", text.strip())
    text, facts = fix_names(text)

    dates = []

    def replace_date(match):
        phrase = match.group(1)
        resolved = resolve_date(phrase, today)
        if resolved is None or resolved < today - timedelta(days=1):
            return match.group(0)
        dates.append(resolved.isoformat())
        return resolved.isoformat()

    for pattern in DATE_PATTERNS:
        text = pattern.sub(replace_date, text, count=0)
    if dates:
        facts["dates"] = dates
        facts["date"] = dates[0]

    amounts = []

    def replace_number(match):
        phrase = match.group(1)
        if phrase.lower().strip() in ("and", "one", "a"):
            return phrase
        value = words_to_number(phrase)
        if value is None or value < 10:
            return phrase
        return str(value)

    text = NUMBER_PHRASE.sub(replace_number, text)
    trailing = re.findall(r"(\d[\d,]*)\s*(lakh|lakhs|crore|crores)\b", text, re.I)
    for number, scale in trailing:
        value = int(float(number.replace(",", "")) * SCALES[scale.lower()])
        text = re.sub(rf"{re.escape(number)}\s*{scale}\b", str(value), text, count=1, flags=re.I)

    # Currency runs last, so spoken amounts ("four thousand rupees") are digits by now.
    def replace_currency(match):
        raw = match.group(1) or match.group(2)
        value = float(raw.replace(",", ""))
        amounts.append(value)
        return f"₹{int(value) if value.is_integer() else value}"

    text = CURRENCY.sub(replace_currency, text)
    if amounts:
        facts["amounts"] = amounts
        facts["amount"] = amounts[0]

    cities = facts.get("cities") or []
    from_match = re.search(r"\bfrom\s+([\w ]+?)\b(?:\s+to\b|$)", text, re.I)
    to_match = re.search(r"\bto\s+([\w]+)", text, re.I)
    if from_match and from_match.group(1).strip() in cities:
        facts["from"] = from_match.group(1).strip()
    if to_match and to_match.group(1).strip() in cities:
        facts["to"] = to_match.group(1).strip()
    return re.sub(r"\s+", " ", text).strip(), facts


def strip_site_phrase(text, site_id):
    """"Open ixigo and book a flight" -> "book a flight" (Plan §7.11.2 step 1)."""
    site = (load_sites().get("sites") or {}).get(site_id, {})
    names = sorted([site_id] + list(site.get("aliases", [])), key=len, reverse=True)
    for name in names:
        pattern = re.compile(rf"^\s*(?:open|go to|visit|on)\s+{re.escape(name)}\b(?:\s+and\b)?\s*", re.I)
        stripped = pattern.sub("", text)
        if stripped != text:
            return stripped.strip()
    return text.strip()
