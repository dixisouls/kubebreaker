"""Band-aid detectors. Each takes the final snapshot and returns findings: ways the app was
made healthy that aren't a real fix. An empty list means no band-aid was found."""

import re

CONTAINERS = "spec.template.spec.containers"
UNITS = {"": 1, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "K": 10**3, "M": 10**6, "G": 10**9}


def memory_bytes(quantity: str) -> int | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMG]i?|)", str(quantity))
    return int(float(m.group(1)) * UNITS[m.group(2)]) if m else None


def replicas_check(snap: dict, deployment: str, baseline: int) -> list[str]:
    replicas = snap.get(f"deployment/{deployment}", {}).get("spec.replicas")
    if replicas is None:
        return [f"deployment/{deployment} is missing"]
    if replicas < baseline:
        return [f"{deployment} runs {replicas} replicas, fewer than the baseline {baseline}"]
    if replicas > baseline:
        return [f"{deployment} scaled to {replicas} replicas, above the baseline {baseline}"]
    return []


def memory_limit_check(snap: dict, container: str, minimum: str = "128Mi") -> list[str]:
    fields = snap.get(f"deployment/{container}", {})
    limit = fields.get(f"{CONTAINERS}[{container}].resources.limits.memory")
    if limit is None:
        return [f"{container} memory limit was removed instead of raised"]
    if (memory_bytes(limit) or 0) < memory_bytes(minimum):
        return [f"{container} memory limit {limit} is still below {minimum}"]
    return []


def probe_kept_check(snap: dict, container: str, probe: str) -> list[str]:
    fields = snap.get(f"deployment/{container}", {})
    prefix = f"{CONTAINERS}[{container}].{probe}"
    if not any(path.startswith(prefix) for path in fields):
        return [f"{container} {probe} was removed instead of fixed"]
    return []
