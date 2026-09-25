"""Pre-computed field values, so TYPE_TEXT never waits on the text model (Plan §7.6).

One background Mercury call per (fingerprint, goal_version) fills every visible
editable field at once. The critical path then reads from memory.

Three outcomes when the executor asks for a value:
  HIT   -> type it
  NULL  -> the goal does not contain this information: missing-info gate
  MISS  -> not ready or never asked: fall back to the synchronous single call
"""

import asyncio

from .events import FieldValuesCached
from .mercury import MercuryError, batch_field_values
from .sensitive import blocked_label as is_blocked_label

MISS = object()
NULL = None

__all__ = ["FieldCache", "MISS", "NULL", "editable_fields", "is_blocked_label"]


def editable_fields(page):
    """Visible editable fields, one entry per node, blocked fields excluded (Plan §7.9)."""
    fields, seen = [], set()
    for action in page.get("actions", []):
        if action.get("kind") != "fill" or action["node"] in seen:
            continue
        if is_blocked_label(action.get("label")):
            continue
        seen.add(action["node"])
        fields.append(
            {
                "index": action["node"],
                "node": action["node"],
                "label": action.get("label", ""),
                "role": action.get("role", ""),
                "value": action.get("value", ""),
            }
        )
    return fields


class FieldCache:
    def __init__(self, bus=None, page_text_chars=6000, profile=None):
        self.bus = bus
        self.page_text_chars = page_text_chars
        # Merged into the Mercury payload only. Never emitted, never logged.
        self.profile = profile or {}
        self.values: dict[tuple[int, int], object] = {}
        self.inflight: dict[tuple[int, str], asyncio.Task] = {}

    def key(self, goal_version, node):
        return (int(goal_version), int(node))

    def get(self, goal_version, node):
        return self.values.get(self.key(goal_version, node), MISS)

    def invalidate(self, goal_version=None):
        """A goal change invalidates everything computed for older goal versions."""
        if goal_version is None:
            self.values.clear()
        else:
            self.values = {k: v for k, v in self.values.items() if k[0] >= int(goal_version)}
        for task in self.inflight.values():
            task.cancel()
        self.inflight.clear()

    def prune(self, page):
        """Drop entries whose node is no longer on the page."""
        live = {action["node"] for action in page.get("actions", []) if "node" in action}
        self.values = {k: v for k, v in self.values.items() if k[1] in live}

    def prefetch(self, page, goal, goal_version, facts=None):
        """Fire-and-forget. Never awaited by the executor; failures only cause a cache miss."""
        fields = editable_fields(page)
        if not fields:
            return None
        key = (int(goal_version), page.get("fingerprint", ""))
        if key in self.inflight:
            return self.inflight[key]
        self.supersede(key)
        task = asyncio.create_task(self._fill(key, page, fields, goal, goal_version, facts or {}))
        self.inflight[key] = task
        return task

    def supersede(self, key):
        """Cancel a prefetch still running for a page or goal we have left behind."""
        for older, task in list(self.inflight.items()):
            if older != key and not task.done():
                task.cancel()
                self.inflight.pop(older, None)

    async def _fill(self, key, page, fields, goal, goal_version, facts):
        try:
            values, meta = await batch_field_values(
                goal,
                fields,
                page.get("text", "")[: self.page_text_chars],
                {**self.profile, **facts},
            )
        except asyncio.CancelledError:
            self.inflight.pop(key, None)
            raise
        except (MercuryError, Exception):
            self.inflight.pop(key, None)
            return
        nulls = 0
        for field in fields:
            index = str(field["index"])
            if index not in values:
                continue
            value = values[index]
            self.values[self.key(goal_version, field["node"])] = value
            nulls += value is None
        self.inflight.pop(key, None)
        if self.bus:
            self.bus.publish(
                FieldValuesCached(count=len(values), nulls=nulls, llm_ms=meta.get("latency_ms", 0))
            )
