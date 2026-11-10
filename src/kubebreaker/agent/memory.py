"""Postmortem memory: the agent's own write-ups of past incidents, shown at the next alert.

After an episode where the agent declared the incident resolved and the harness confirmed the
app recovered, its alert, root cause and fix are appended to a JSONL store. With memory on,
the next episode's system prompt lists the most recent notes. Notes are only ever the agent's
own words: the ground truth and the judge's verdict are never stored, so memory can carry the
agent's mistakes forward as well as its successes.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_NOTES = 12
MAX_CHARS = 6000


@dataclass
class Postmortem:
    alert: str
    root_cause: str
    fix_summary: str
    steps: int


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[Postmortem]:
        if not self.path.exists():
            return []
        notes = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                notes.append(Postmortem(**json.loads(line)))
        return notes

    def add(self, note: Postmortem) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(note)) + "\n")


HEADER = (
    "\nNotes from past incidents on this cluster, written by you after each one (newest "
    "first). The app recovered each time, but a note's root cause may still be wrong. "
    "Use them as leads, and confirm against the cluster before acting.\n"
)


def _entry(i: int, n: Postmortem) -> str:
    alert = " / ".join(line.strip() for line in n.alert.splitlines() if line.strip())
    return f"\n{i}. Alert: {alert}\n   Root cause: {n.root_cause}\n   Fix: {n.fix_summary}\n"


def select(notes: list[Postmortem], limit: int = MAX_NOTES, max_chars: int = MAX_CHARS):
    """The notes to show: newest first, at most `limit`, within `max_chars` in total."""
    chosen, used = [], len(HEADER)
    for n in reversed(notes[-limit:] if limit else []):
        size = len(_entry(len(chosen) + 1, n))
        if used + size > max_chars:
            break
        chosen.append(n)
        used += size
    return chosen


def prompt_block(chosen: list[Postmortem]) -> str:
    """The section appended to the system prompt, for notes picked by `select`."""
    if not chosen:
        return ""
    return HEADER + "".join(_entry(i, n) for i, n in enumerate(chosen, 1))
