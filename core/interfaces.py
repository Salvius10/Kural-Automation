"""Vendor-swappable seams (Plan §7.1). Business logic depends only on these."""

from typing import Any, AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class SpeechToText(Protocol):
    async def start(self) -> None: ...          # open the socket and keep it warm
    async def begin_utterance(self) -> None: ...  # key down
    async def send_audio(self, pcm: bytes) -> None: ...
    async def end_utterance(self) -> str: ...     # key up -> ForceEndpoint -> final text
    def partials(self) -> AsyncIterator[str]: ...
    async def close(self) -> None: ...


@runtime_checkable
class Decider(Protocol):
    async def decide(
        self,
        page: dict,
        goal_ctx: dict,
        history: list,
        extra_questions: dict | None = None,
    ) -> dict: ...


@runtime_checkable
class TextWriter(Protocol):
    async def field_values(
        self,
        goal: str,
        fields: list[dict],
        page_text: str,
        facts: dict[str, Any],
    ) -> dict[str, str | None]: ...

    async def answer_question(self, question: str, page_text: str) -> str: ...

    async def search_query(self, goal: str) -> str: ...


@runtime_checkable
class BrowserDriver(Protocol):
    async def observe(self) -> dict: ...
    async def fresh(self, page: dict) -> bool: ...
    async def act(self, action: dict, page: dict, text: str | None = None) -> Any: ...
    async def navigate(self, url: str) -> None: ...
