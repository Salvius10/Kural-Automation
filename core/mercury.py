"""Mercury on Inception's OpenAI-compatible API (Plan §7.6a).

Direct to Inception: one less hop than upstream's OpenRouter route. JSON mode is
not a strict schema, so every response is validated in code and discarded on
failure -- nothing is typed from an unvalidated model reply.
"""

import json
import os
import time

import httpx

from agent.questions import FIELD_VALUES, PAGE_ANSWER, SEARCH_QUERY, TEXT_VALUE

from .http import inception_client

MAX_VALUE_CHARS = 2000


class MercuryError(RuntimeError):
    """The text helper produced nothing usable; the caller must not invent a value."""


def _settings():
    key = os.environ.get("INCEPTION_API_KEY")
    if not key:
        raise MercuryError("TYPE_TEXT needs INCEPTION_API_KEY; no text is hardcoded or guessed by the executor.")
    return (
        key,
        os.environ.get("INCEPTION_BASE_URL", "https://api.inceptionlabs.ai/v1").rstrip("/"),
        os.environ.get("MERCURY_MODEL", "mercury-2.5"),
        os.environ.get("MERCURY_REASONING_EFFORT", "instant"),
    )


async def complete_json(system, payload, max_tokens=512, timeout=5.0):
    """One chat completion in JSON mode. Returns the parsed object, or raises."""
    key, base, model, effort = _settings()
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "reasoning_effort": effort,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload)},
        ],
    }
    client = inception_client()
    started = time.perf_counter()
    result = None
    for attempt in range(2):
        try:
            response = await client.post(
                base + "/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=timeout,
            )
        except httpx.HTTPError:
            raise MercuryError("Text model connection failed; nothing typed.") from None
        if response.status_code in {429, 500, 502, 503, 529} and attempt == 0:
            continue
        if response.is_error:
            raise MercuryError(f"Text model returned HTTP {response.status_code}; nothing typed.")
        result = response.json()
        break
    if result is None:
        raise MercuryError("Text model unavailable; nothing typed.")
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise MercuryError("Text model returned no valid JSON; nothing typed.") from None
    meta = {
        "model": result.get("model", model),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
    return output, meta


def _clean(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_VALUE_CHARS:
        raise ValueError("unusable value")
    return value


async def single_field_value(context):
    """Upstream's one-field call, kept for cache misses. Returns (text | None, meta).

    `None` is a real answer, not a failure: the model is saying the goal does not
    contain this value, which opens the missing-info gate. Only malformed output
    raises -- and then nothing is typed either way.
    """
    output, meta = await complete_json(TEXT_VALUE, context, max_tokens=1024)
    try:
        if set(output) != {"text"}:
            raise ValueError()
        value = _clean(output["text"])
    except (KeyError, TypeError, ValueError):
        raise MercuryError("Text helper returned no valid field value; nothing typed.") from None
    return value, meta


async def batch_field_values(goal, fields, page_text, facts):
    """All visible editable fields in one call (Plan §4.2.6).

    Returns `{index: value | None}` containing only indexes that were asked for.
    `None` means the goal lacks that information -- the caller opens a
    missing-info gate rather than inventing anything.
    """
    indexes = [str(field["index"]) for field in fields]
    payload = {
        "goal": goal,
        "facts": facts or {},
        "fields": [
            {
                "index": str(field["index"]),
                "label": field.get("label", ""),
                "role": field.get("role", ""),
                "current_value": field.get("value", ""),
            }
            for field in fields
        ],
        "page": page_text,
    }
    output, meta = await complete_json(FIELD_VALUES, payload, max_tokens=512)
    values = output.get("values") if isinstance(output, dict) else None
    if not isinstance(values, dict):
        raise MercuryError("Text model returned no values object; cache left empty.")
    cleaned = {}
    for index in indexes:
        if index not in values:
            continue
        try:
            cleaned[index] = _clean(values[index])
        except ValueError:
            continue
    return cleaned, meta


async def search_query(goal):
    """The model writes search words only -- never a URL (Plan §11.10)."""
    output, meta = await complete_json(SEARCH_QUERY, {"goal": goal}, max_tokens=128)
    query = output.get("query") if isinstance(output, dict) else None
    if not isinstance(query, str) or not query.strip() or len(query) > 120 or "://" in query:
        raise MercuryError("Text model returned no usable search words.")
    return query.strip(), meta


async def answer_question(question, page_text):
    output, meta = await complete_json(
        PAGE_ANSWER, {"question": question, "page": page_text}, max_tokens=256
    )
    answer = output.get("answer") if isinstance(output, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        raise MercuryError("Text model returned no answer.")
    return answer.strip()[:600], meta


class Mercury:
    """The `TextWriter` seam (Plan §7.1)."""

    async def field_values(self, goal, fields, page_text, facts):
        values, _meta = await batch_field_values(goal, fields, page_text, facts)
        return values

    async def answer_question(self, question, page_text):
        answer, _meta = await answer_question(question, page_text)
        return answer

    async def search_query(self, goal):
        query, _meta = await search_query(goal)
        return query
