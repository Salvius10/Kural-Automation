"""TypeSafe makes choices; Mercury writes field values. Async, one round trip per tick.

Vendored from jev-ultrafast. Local changes: async I/O on shared HTTP/2 clients,
a NAVIGATE operation whose targets are code-owned site ids, fused extra
question heads, and `noul` validation (see UPSTREAM.md).
"""

import asyncio
import math
import os
import time

import httpx

from core.config import settings
from core.http import typesafe_client

from .questions import NAVIGATE_TARGET, NEXT_ACTION, TARGET


async def post_json(url, key, body, client=None):
    client = client or typesafe_client()
    for attempt in range(3):
        try:
            response = await client.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            await asyncio.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def validate_noul(answer):
    """Boolean head -> probability of yes.

    Live shape (verified against jev-1.13.0, 2026-09-26):
    `{"type": "noul", "noul": 0.9}` -- a bare probability under `noul`, with no
    separate confidence. The `probability` / yes-no-`probabilities` forms are
    still accepted so a shape change cannot silently disable every boolean head.

    Anything else is invalid, and the caller must treat an invalid boolean as
    "no answer" -- never as "yes".
    """
    try:
        if isinstance(answer, dict) and type(answer.get("noul")) in (int, float):
            probability = float(answer["noul"])
        elif isinstance(answer, dict) and type(answer.get("probability")) in (int, float):
            probability = float(answer["probability"])
        elif isinstance(answer, dict) and isinstance(answer.get("probabilities"), dict):
            probabilities = {str(k).lower(): v for k, v in answer["probabilities"].items()}
            probability = float(probabilities.get("yes", probabilities.get("true")))
        else:
            raise ValueError()
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid TypeSafe boolean response; treated as no answer.") from None
    confidence = answer.get("confidence")
    return {
        "probability": probability,
        # No confidence is returned for a boolean; distance from 0.5 stands in.
        "confidence": confidence if type(confidence) in (int, float) else abs(probability - 0.5) * 2,
    }


def navigation_targets(sites):
    """Code-owned NAVIGATE targets. The model picks an id here; it never writes a URL."""
    targets = {}
    for site_id, site in (sites or {}).items():
        targets[site_id] = {
            "id": f"navigate:{site_id}",
            "kind": "navigate",
            "site": site_id,
            "url": site["url"],
            "label": f"Go to {site_id}",
            "about": site.get("about", ""),
        }
    if targets:
        targets["search"] = {
            "id": "navigate:search",
            "kind": "navigate",
            "site": "search",
            "url": None,
            "label": "Run a web search",
            "about": "Search the web when no known site fits",
        }
    return targets


def action_space(actions, sites=None):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    navigate = navigation_targets(sites)
    if navigate:
        targets["NAVIGATE"] = navigate
    return elements, targets, controls


def build_questions(goal, targets, controls, extra_questions=None):
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
        "NAVIGATE": "Go to a different website that is needed for the next part of the goal.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        if operation == "NAVIGATE":
            criteria = {site_id: {"site": site_id, "about": a["about"]} for site_id, a in candidates.items()}
        else:
            criteria = {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            }
        rules = [NEXT_ACTION, NAVIGATE_TARGET] if operation == "NAVIGATE" else [NEXT_ACTION, TARGET]
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": criteria,
            "instructions": {"goal": goal, "operation": operation, "rules": rules},
        }
    questions.update(extra_questions or {})
    return operations, questions


def request_body(state, questions, history):
    config = settings()
    return {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {
                "url": state.get("url", ""),
                "title": state.get("title", ""),
                "text": state.get("text", "")[: config.page_text_chars],
            },
            "elements": state.get("elements", []),
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")}
                for h in (history or [])[-config.history_actions:]
            ],
        },
        "questions": questions,
    }


def validate_extra(answers, extra_questions):
    """Validate every fused head against its own spec. An invalid head is dropped, never guessed."""
    validated = {}
    for name, spec in (extra_questions or {}).items():
        answer = answers.get(name)
        try:
            if spec.get("type") == "noul":
                validated[name] = validate_noul(answer)
            else:
                validated[name] = validate_choice(answer or {}, spec["criteria"])
        except ValueError:
            validated[name] = None
    return validated


def endpoint():
    return os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/") + "/v1/systemone"


async def choose(state, goal, history, extra_questions=None, sites=None):
    """One request: the operation, every operation's target head, and any fused heads."""
    elements, targets, controls = action_space(state["actions"], sites=sites)
    operations, questions = build_questions(goal, targets, controls, extra_questions)
    body = request_body({**state, "elements": elements}, questions, history)
    started = time.perf_counter()
    result = await post_json(endpoint(), os.environ["TYPESAFE_API_KEY"], body)
    latency_ms = round((time.perf_counter() - started) * 1000)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        action = targets[operation][target]
        choice = action["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        action = controls.get(operation)
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "action": action,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "extra": validate_extra(result["answers"], extra_questions),
        "raw_answers": result["answers"],
        "model": result.get("model", ""),
        "usage": result.get("usage", {}),
        "latency_ms": latency_ms,
        "request": body,
    }


async def ask(questions, state=None, history=None):
    """A Jev request with no action heads: risk scoring and other background judgements."""
    body = request_body(state or {}, questions, history or [])
    started = time.perf_counter()
    result = await post_json(endpoint(), os.environ["TYPESAFE_API_KEY"], body)
    return {
        "answers": validate_extra(result.get("answers", {}), questions),
        "raw_answers": result.get("answers", {}),
        "model": result.get("model", ""),
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def field_context(goal, action, page, history):
    config = settings()
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][: config.page_text_chars]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


async def field_text(context):
    """Single-field text: the upstream synchronous path, now the cache-miss fallback."""
    from core.mercury import single_field_value

    return await single_field_value(context)
