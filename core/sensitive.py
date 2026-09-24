"""What the agent must never type (Plan §7.9, §11.2). One list, used everywhere.

The executor, the field cache and the in-page guard all read from here, so a
field can never be blocked in one layer and typed by another.
"""

import re

# Matched against a label, and against an input's name/id/placeholder/aria-label.
BLOCKED_WORDS = (
    "password",
    "passcode",
    "otp",
    "one time code",
    "one-time code",
    "pin",
    "upi pin",
    "cvv",
    "cvc",
    "security code",
    "card number",
    "cardnumber",
    "card no",
    "credit card",
    "debit card",
    "aadhaar",
    "aadhar",
    "pan number",
    "pan card",
    "ssn",
)

# HTML autocomplete tokens that name a secret outright.
BLOCKED_AUTOCOMPLETE = (
    "current-password",
    "new-password",
    "one-time-code",
    "cc-number",
    "cc-csc",
    "cc-exp",
)

# Whole words only: "Pincode" and "Shipping" must stay fillable while "PIN" does not.
BLOCKED_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(word) for word in BLOCKED_WORDS) + r")(?![a-z0-9])",
    re.I,
)


def blocked_label(label):
    """True when this label names something only the user may type."""
    return bool(BLOCKED_PATTERN.search(str(label or "")))


def js_pattern():
    """The same rule, for the in-page guard. Kept here so the two cannot drift."""
    return BLOCKED_PATTERN.pattern
