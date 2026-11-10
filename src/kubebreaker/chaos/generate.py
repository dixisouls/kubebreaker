"""Ask a model for new faults, aimed at what agents get wrong.

The model sees the app's manifests (Secrets left out), the catalog, the spec format, and, if a
sweep folder is given, which faults agents solved least often. It replies with fault specs as
YAML. Each is checked by generated.validate and saved to faults/generated/ unverified; only
`kubebreaker selftest <id>` on the cluster can mark one verified. Nothing the model writes is
ever executed: specs are data, applied through the allowlisted operations in generated.py.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from kubebreaker.chaos import generated
from kubebreaker.chaos.base import CATALOG
from kubebreaker.llm import LLMClient, Message, Usage, make_client

SYSTEM = """\
You design chaos-engineering faults for a benchmark that tests AI on-call engineers on a small
Kubernetes shop app. A good fault is a realistic misconfiguration a teammate could plausibly
ship, whose symptom shows up away from its cause, and which an engineer can find and fix with
read access to resources, logs, events and Prometheus metrics. Avoid faults that are trivial
(the alert names the cause) or unfair (no evidence anywhere in the cluster).

Reply with only a YAML list of fault specs, no prose and no code fences.
"""

FORMAT = """\
Spec format (every field required unless marked optional):

- id: gen-<short-kebab-name>
  title: one line, what is misconfigured
  tier: easy | medium | hard
  alert: "<AlertName>: <what the user-facing symptom looks like>"
    # AlertName is one of: HighErrorRate, HighLatency, KubePodCrashLooping,
    # KubeDeploymentRolloutStuck. Describe the symptom only, never the cause or an
    # injected value. KubePodCrashLooping and KubeDeploymentRolloutStuck alerts name the
    # deployment as shop/<name>.
  target: <deployment>   # only for KubePodCrashLooping and KubeDeploymentRolloutStuck
  ground_truth: the real cause, naming the object and the misconfiguration
  expected_fix: what a correct fix changes
  inject: [operations]
  reference_fix: [operations that undo the fault]

Operations:
  {op: patch, kind: deployment|service|configmap, name: <existing object>,
   patch: <strategic merge patch, written as a YAML map, not a string>}
  {op: create, kind: networkpolicy, name: <new name>, spec: <NetworkPolicy spec>}  # inject only
  {op: delete, kind: networkpolicy, name: <one this fault created>}  # reference_fix only
  {op: restart, kind: deployment, name: <deployment>}  # e.g. after a ConfigMap change

Rules (specs that break them are rejected automatically):
- Only the shop app's Deployments, Services and ConfigMaps shown below. Never Secrets, never
  the loadgen deployment, never another namespace.
- Deployment patches may touch only: spec.replicas, pod template labels, and per container
  (by existing name) args, env, resources, readiness/liveness/startup probes (httpGet or
  tcpSocket only), ports, and image (only another tag of kubebreaker/shop).
- Never container commands, exec probes, volumes or security settings.
- Container args are plain flags: no spaces or shell characters.
- Service patches: spec.selector and spec.ports. ConfigMap patches: data.
- Don't repeat a catalog fault (same object and field).
- A container list in a strategic merge patch merges by container name.

Lessons from specs that failed their cluster test:
- Clients keep connections open. A Service port or NetworkPolicy change only breaks new
  connections, so the old ones keep working and the fault shows up only now and then. After
  such a change, restart the deployment that makes the calls (e.g. the api).
- Pick the alert for what users actually see. When the api can't reach a dependency at all,
  requests fail fast: that's HighErrorRate, not HighLatency. HighLatency fits faults that slow
  a dependency down without breaking it.
"""


@dataclass
class Generation:
    saved: list[Path] = field(default_factory=list)
    rejected: list[tuple[str, list[str]]] = field(default_factory=list)
    usage: Usage | None = None
    model: str = ""
    reply: str = ""


def app_manifests() -> str:
    docs = []
    for f in sorted(generated.APP_DIR.glob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text()):
            if d and d.get("kind") not in ("Secret", "Kustomization"):
                docs.append(yaml.safe_dump(d, sort_keys=False, width=100))
    return "---\n".join(docs)


def catalog_summary() -> str:
    lines = [
        f"- {f.id} ({f.tier}): {f.title}. Changes: {', '.join(f.fix_scope)}"
        for f in CATALOG.values()
    ]
    return "\n".join(lines)


def weaknesses(results_dir: Path | None) -> str:
    """Solve rate per fault in a sweep folder, worst first."""
    if not results_dir:
        return ""
    stats: dict[str, list[int]] = {}
    for f in Path(results_dir).glob("**/result.json"):
        r = json.loads(f.read_text())
        solved = r.get("resolved") and r.get("root_cause_verdict") == "correct"
        s = stats.setdefault(r["fault_id"], [0, 0])
        s[0] += bool(solved)
        s[1] += 1
    if not stats:
        return ""
    rows = sorted(stats.items(), key=lambda kv: kv[1][0] / kv[1][1])
    lines = [f"- {fid}: solved {s}/{n}" for fid, (s, n) in rows]
    return (
        "How often agents solved each catalog fault (worst first). Aim new faults at the "
        "patterns behind the weak ones, without copying them:\n" + "\n".join(lines)
    )


def build_prompt(count: int, results_dir: Path | None = None) -> str:
    parts = [
        f"Propose {count} new faults.",
        FORMAT,
        "The app (namespace shop): a frontend calls an api, which calls orders (backed by "
        "Postgres) and caches in Redis. A load generator sends steady traffic to the "
        "frontend. Baseline manifests:\n" + app_manifests(),
        "Existing catalog (don't repeat these):\n" + catalog_summary(),
    ]
    weak = weaknesses(results_dir)
    if weak:
        parts.append(weak)
    return "\n\n".join(parts)


def parse_specs(text: str) -> list:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"reply isn't valid YAML: {exc}") from None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("reply isn't a list of specs")
    return data


def normalize(spec):
    """Parse a patch or NetworkPolicy spec the model wrote as a YAML/JSON string into a map.
    Validation still runs on the result."""
    if not isinstance(spec, dict):
        return spec
    spec = dict(spec)
    for key in ("inject", "reference_fix"):
        ops = spec.get(key)
        if not isinstance(ops, list):
            continue
        fixed = []
        for op in ops:
            if isinstance(op, dict):
                op = dict(op)
                for field_ in ("patch", "spec"):
                    if isinstance(op.get(field_), str):
                        try:
                            parsed = yaml.safe_load(op[field_])
                        except yaml.YAMLError:
                            continue
                        if isinstance(parsed, dict):
                            op[field_] = parsed
            fixed.append(op)
        spec[key] = fixed
    return spec


def generator_client() -> LLMClient:
    model = (
        os.environ.get("GENERATOR_MODEL")
        or os.environ.get("JUDGE_MODEL")
        or os.environ.get("GOOGLE_MODEL")
    )
    provider = os.environ.get("GENERATOR_PROVIDER") or "google"
    return make_client(provider, model, effort=os.environ.get("GENERATOR_EFFORT") or "medium")


def generate(
    count: int,
    results_dir: Path | None = None,
    llm: LLMClient | None = None,
    directory: Path = generated.GENERATED_DIR,
) -> Generation:
    llm = llm or generator_client()
    resp = llm.chat(SYSTEM, [Message("user", build_prompt(count, results_dir))], [])
    out = Generation(
        usage=resp.usage, model=f"{llm.provider}/{llm.model}", reply=resp.message.text or ""
    )
    existing = {p.stem for p in directory.glob("gen-*.yaml")}
    for spec in parse_specs(out.reply):
        spec = normalize(spec)
        sid = spec.get("id", "?") if isinstance(spec, dict) else "?"
        errors = generated.validate(spec)
        if not errors and sid in existing:
            errors = ["id: a generated fault with this id already exists"]
        if errors:
            out.rejected.append((sid, errors))
            continue
        spec = {**spec, "verified": False, "generated_by": out.model}
        out.saved.append(generated.save(spec, directory))
        existing.add(sid)
    return out
