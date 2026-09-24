"""One JSONL file per session: runs/YYYY-MM-DD/HHMMSS_<session>.jsonl (Plan §12)."""

from datetime import datetime
from pathlib import Path

import orjson

from .config import ROOT
from .events import Event, redact


class JsonlLog:
    def __init__(self, session_id, root=None):
        now = datetime.now()
        directory = Path(root or ROOT / "runs") / now.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{now.strftime('%H%M%S')}_{session_id}.jsonl"
        self.handle = self.path.open("ab")

    def __call__(self, event: Event):
        self.write(event)

    def write(self, event: Event):
        payload = redact(event.model_dump())
        self.handle.write(orjson.dumps(payload, option=orjson.OPT_APPEND_NEWLINE))
        self.handle.flush()

    def close(self):
        if not self.handle.closed:
            self.handle.close()
