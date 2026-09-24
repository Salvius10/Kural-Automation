"""Every observable thing the system does, as a typed event (Plan §9).

Events are the only channel between components and the UI/logs. Each carries
`t_ms`: milliseconds since the latest key release, so the status page can draw
the per-stage timing bar without joining anything.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class Event(BaseModel):
    type: str = ""
    session_id: str = ""
    seq: int = 0
    t_wall: float = 0.0
    t_ms: int = 0

    def model_post_init(self, _context):
        if not self.type:
            self.type = type(self).__name__


class KeyDown(Event):
    pass


class KeyUp(Event):
    held_ms: int = 0


class TranscriptPartial(Event):
    text: str


class TranscriptFinal(Event):
    raw: str
    stt_ms: int = 0
    provider: str = ""


class Normalized(Event):
    text: str
    facts: dict[str, Any] = Field(default_factory=dict)
    norm_ms: int = 0


class IntentClassified(Event):
    intent: str
    confidence: float = 0.0
    probabilities: dict[str, float] = Field(default_factory=dict)
    jev_ms: int = 0
    model: str = ""


class GoalUpdated(Event):
    goal: str
    goal_version: int
    reason: str


class PageObserved(Event):
    url: str
    title: str = ""
    fingerprint: str = ""
    n_elements: int = 0
    observe_ms: int = 0


class StartResolved(Event):
    mode: Literal["named", "here", "site", "search"]
    site: str | None = None
    url: str | None = None


class TabSwitched(Event):
    from_target: str | None = None
    to_target: str | None = None
    reason: str = ""


class ActionDecided(Event):
    operation: str
    target: str | None = None
    label: str = ""
    probability: float = 0.0
    confidence: float = 0.0
    jev_ms: int = 0
    model: str = ""


class FieldValuesCached(Event):
    count: int = 0
    nulls: int = 0
    llm_ms: int = 0


class RiskScored(Event):
    count: int = 0
    risky: int = 0
    jev_ms: int = 0


class GateOpened(Event):
    kind: Literal["confirm", "info", "manual", "choice"]
    prompt: str


class GateResolved(Event):
    kind: str
    resolution: str


class ActionExecuted(Event):
    label: str
    kind: str
    text: str | None = None
    page_changed: bool | None = None
    exec_ms: int = 0


class RunStatusChanged(Event):
    status: str
    detail: str = ""


class StateChanged(Event):
    """Session state-machine transition (Plan §8)."""

    from_state: str
    to_state: str
    trigger: str


class Notice(Event):
    """Operator-facing message rendered from a template, never model-written."""

    text: str
    level: Literal["info", "warn"] = "info"


class Error(Event):
    where: str
    message: str
    recoverable: bool = True


SENSITIVE_KEYS = ("password", "otp", "cvv", "cvc", "pin", "card", "aadhaar", "pan", "token", "key")


def redact(value):
    """Defence in depth for the JSONL log: never persist anything key-shaped."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if any(s in str(k).lower() for s in SENSITIVE_KEYS) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
