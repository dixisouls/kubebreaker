"""Append-only JSONL trace of everything that happens in an episode."""

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class Trace:
    def __init__(self, path: Path | None):
        self.path = path
        self.start = time.monotonic()
        self.events: list[dict] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, **data: Any) -> None:
        event = {"t": round(time.monotonic() - self.start, 3), "kind": kind, **data}
        self.events.append(event)
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(event, default=_default) + "\n")


def _default(obj):
    if is_dataclass(obj):
        return asdict(obj)
    return str(obj)
