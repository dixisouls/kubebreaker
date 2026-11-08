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


CPU_UNITS = {"": 1000, "m": 1}


def cpu_millicores(quantity: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(m?)", str(quantity))
    return float(m.group(1)) * CPU_UNITS[m.group(2)] if m else None


def cpu_limit_check(snap: dict, container: str, minimum_millicores: float = 50) -> list[str]:
    """A CPU limit is fine (or none at all); one this low is still starving the container."""
    fields = snap.get(f"deployment/{container}", {})
    limit = fields.get(f"{CONTAINERS}[{container}].resources.limits.cpu")
    if limit is not None and (cpu_millicores(limit) or 0) < minimum_millicores:
        return [f"{container} CPU limit {limit} is still below {minimum_millicores:.0f}m"]
    return []


def redis_memory_check(snap: dict, minimum: str = "16mb") -> list[str]:
    """Redis needs real headroom and an eviction policy, or cache writes fail again."""
    args = snap.get("deployment/redis", {}).get(f"{CONTAINERS}[redis].args") or []
    opts = dict(zip(args[::2], args[1::2], strict=False))
    findings = []
    maxmem = opts.get("--maxmemory")
    if maxmem is not None:
        units = {"kb": 2**10, "mb": 2**20, "gb": 2**30, "b": 1, "": 1}
        m = re.fullmatch(r"(\d+)([kmg]?b?)", maxmem.lower())
        size = int(m.group(1)) * units[m.group(2)] if m else 0
        if size and size < memory_bytes(minimum.upper().replace("MB", "Mi")):
            findings.append(f"redis maxmemory {maxmem} is still too small for the cache")
    if opts.get("--maxmemory-policy", "noeviction") == "noeviction" and maxmem is not None:
        findings.append("redis still refuses writes when full (maxmemory-policy noeviction)")
    return findings
