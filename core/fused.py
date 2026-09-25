"""One Jev request per utterance: intent + start + the first decision (Plan §7.5).

Routing and deciding in two calls would cost two round trips. Jev bills input
tokens only and output is free, so every head rides along and the unused ones
are discarded.
"""

from agent.questions import (
    INTENT,
    INTENT_CRITERIA,
    NEEDS_USER_CHOICE,
    START_HERE,
    START_SITE,
    TASK_DONE,
)

INTENTS = tuple(INTENT_CRITERIA)


def intent_question(goal_ctx):
    """`goal_ctx` = {current_goal, latest_utterance, pending_question}."""
    return {
        "type": "choice",
        "criteria": dict(INTENT_CRITERIA),
        "instructions": {**goal_ctx, "rules": INTENT},
    }


def start_questions(sites, goal):
    """`start_here` and `start_site` ride in the same request as the first decision."""
    questions = {
        "start_here": {
            "type": "noul",
            "criteria": {"statement": START_HERE},
            "instructions": {"goal": goal, "rules": START_HERE},
        }
    }
    if sites:
        criteria = {site_id: site.get("about", site_id) for site_id, site in sites.items()}
        criteria["none"] = "No listed site fits this goal; search the web instead."
        questions["start_site"] = {
            "type": "choice",
            "criteria": criteria,
            "instructions": {"goal": goal, "rules": START_SITE},
        }
    return questions


def choice_gate_question(goal):
    return {
        "needs_user_choice": {
            "type": "noul",
            "criteria": {"statement": NEEDS_USER_CHOICE},
            "instructions": {"goal": goal, "rules": NEEDS_USER_CHOICE},
        }
    }


def task_done_question(goal):
    return {
        "task_done": {
            "type": "noul",
            "criteria": {"statement": TASK_DONE},
            "instructions": {"goal": goal, "rules": TASK_DONE},
        }
    }


def utterance_questions(goal_ctx, sites=None, include_start=True, include_choice_gate=False):
    """Every head the session manager may need from one utterance."""
    questions = {"intent": intent_question(goal_ctx)}
    goal = goal_ctx.get("latest_utterance") or goal_ctx.get("current_goal") or ""
    questions.update(task_done_question(goal))
    if include_start:
        questions.update(start_questions(sites or {}, goal))
    if include_choice_gate:
        questions.update(choice_gate_question(goal))
    return questions


def read_intent(extra, minimum_confidence):
    """(intent, confidence, probabilities). A low-confidence intent is never acted on."""
    answer = (extra or {}).get("intent")
    if not answer:
        return None, 0.0, {}
    confidence = float(answer.get("confidence", 0.0))
    if confidence < minimum_confidence:
        return None, confidence, answer.get("probabilities", {})
    return answer["choice"], confidence, answer.get("probabilities", {})
