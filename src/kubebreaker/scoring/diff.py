"""Compare two snapshots and split the differences into the fix and collateral damage."""

from dataclasses import dataclass

from kubebreaker.scoring.snapshot import Snapshot


@dataclass(frozen=True)
class Change:
    obj: str  # e.g. "deployment/orders"
    path: str  # e.g. "spec.replicas"; "" when the whole object was added or removed
    before: object
    after: object

    @property
    def key(self) -> str:
        return f"{self.obj}:{self.path}" if self.path else self.obj

    def __str__(self) -> str:
        if not self.path:
            return f"{self.obj} {'added' if self.before is None else 'removed'}"
        return f"{self.key}: {self.before!r} -> {self.after!r}"


MISSING = None


def diff(before: Snapshot, after: Snapshot) -> list[Change]:
    changes: list[Change] = []
    for obj in sorted(before.keys() | after.keys()):
        if obj not in after:
            changes.append(Change(obj, "", "present", MISSING))
            continue
        if obj not in before:
            changes.append(Change(obj, "", MISSING, "present"))
            continue
        b, a = before[obj], after[obj]
        for path in sorted(b.keys() | a.keys()):
            if b.get(path) != a.get(path):
                changes.append(Change(obj, path, b.get(path), a.get(path)))
    return changes


def in_scope(change: Change, scope: tuple[str, ...]) -> bool:
    """A change is in scope if its key starts with one of the scope prefixes."""
    key = change.key
    return any(key == s or key.startswith(s + ".") or key.startswith(s + "[") for s in scope)


def split(changes: list[Change], scope: tuple[str, ...]) -> tuple[list[Change], list[Change]]:
    """Returns (in-scope changes, collateral changes)."""
    fix = [c for c in changes if in_scope(c, scope)]
    collateral = [c for c in changes if not in_scope(c, scope)]
    return fix, collateral
