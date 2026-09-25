"""Your details, for filling forms (Plan §7.12).

Local only. These values reach Mercury -- and only Mercury -- so it can map
"Passenger name" to a name. They are never sent to Jev, which sees the page and
the goal; they are never put in an event, so they never reach the JSONL log or
the status page; and they are never typed into a blocked field.
"""

import functools

import yaml

from .config import ROOT, settings

# Only these keys are ever read out of the file. A stray key in profile.yaml
# cannot widen what gets sent anywhere.
FIELDS = ("name", "gender", "date_of_birth", "email", "phone")
ADDRESS_FIELDS = ("line1", "city", "state", "pincode")


def flatten(raw):
    """profile.yaml -> a flat dict of non-empty strings, nothing else."""
    if not isinstance(raw, dict):
        return {}
    facts = {}
    for key in FIELDS:
        value = raw.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            facts[key] = str(value).strip()
    address = raw.get("address")
    if isinstance(address, dict):
        for key in ADDRESS_FIELDS:
            value = address.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                facts[f"address_{key}"] = str(value).strip()
    return facts


@functools.lru_cache(maxsize=1)
def load_profile():
    """Empty when the file is missing -- a missing value becomes a spoken question."""
    path = ROOT / settings().navigation.profile_file
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return flatten(raw)


def reload_profile():
    load_profile.cache_clear()
    return load_profile()
