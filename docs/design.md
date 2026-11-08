# Design

## Components

1. **Cluster:** kind, 1 control plane + 1 worker, Calico CNI (kind's default CNI ignores NetworkPolicy).
2. **Target app:** 4–5 small services written for this project, so each fault is fully under our control. Images are built locally and loaded with `kind load docker-image`.
3. **Observability:** Prometheus in-cluster (plain chart, short retention), plus logs and events through the Kubernetes API.
4. **Chaos injector:** a catalog of faults. Each fault defines `inject()`, a ground-truth label, `verify()` and the expected fix category.
5. **Agent harness:** a hand-written tool-calling loop over a provider-agnostic `LLMClient`, with step and token caps and a full trace logger.
6. **Scorer and dashboard:** health checks, collateral diff, root-cause judge, results page.

## Target app

Namespace `shop`, manifests in `app/k8s`, one Python image (`kubebreaker/shop`) for all services:

```
loadgen ──► frontend ──► api ──► orders ──► postgres
                          │
                          └──► redis (cache, no fallback)
```

| Service | Replicas | Notes |
| --- | --- | --- |
| frontend | 2 | HTML page from `api`; 502 when `api` fails |
| api | 2 | Caches the order list in Redis for 5s; fails if Redis fails (cascading failure) |
| orders | 2 | Postgres via `orders-config` ConfigMap (host) and `postgres-credentials` Secret |
| postgres | 1 | emptyDir storage, seeded by `postgres-init` |
| redis | 1 | No persistence |
| loadgen | 1 | ~2 req/s against the frontend, 10% writes; logs ok/failed counts every 30s |

Every HTTP service exposes `http_requests_total`, `http_request_duration_seconds` and `dependency_request_duration_seconds` (labelled by dependency and outcome) on `/metrics`.

Timeouts are kept short and predictable (frontend → api 3s, api → orders 3s, Redis 2s with one retry), so a fault shows up within seconds and with the same timing on every run.

`make app-reset` force-deletes the pods and the namespace, reapplies the manifests and runs `scripts/verify-baseline.sh`: all Deployments available, every Service has ready endpoints, the frontend renders orders. It takes about 15–20s.

## Fault catalog

Code: `src/kubebreaker/chaos/faults.py`. Each fault defines `inject`, `symptom` (true once the break is visible), an `alert` for the agent that never names the cause, a `ground_truth`, an `expected_fix`, and a `reference_fix` that `kubebreaker selftest` uses to prove the fault can be fixed.

A fault counts as fixed when the whole app matches its baseline: every Deployment has its replicas and a finished rollout, every Service has ready endpoints, and the frontend renders orders. Some faults add their own checks on top (for example, `wrong-db-host` also requires `DB_HOST=postgres`).

| Fault | Tier | Injection | Alert the agent gets |
| --- | --- | --- | --- |
| bad-command | easy | api command loads `shop.gateway:app` | KubePodCrashLooping on api |
| bad-image-tag | easy | frontend image `kubebreaker/shop:0.2.0` | KubeDeploymentRolloutStuck on frontend |
| scaled-to-zero | easy | api replicas 0 | HighErrorRate on frontend |
| wrong-db-host | medium | `orders-config` DB_HOST `postgres-primary`, orders restarted | HighErrorRate on frontend |
| oom-killed | medium | orders memory limit 24Mi | KubePodCrashLooping on orders |
| broken-selector | medium | orders Service selector `app=order` | HighErrorRate on frontend |
| failing-readiness | medium | frontend readiness path `/ready` | KubeDeploymentRolloutStuck on frontend |
| missing-secret-ref | medium | orders env references Secret `orders-db-credentials` | KubeDeploymentRolloutStuck on orders |

**Hard tier** (`src/kubebreaker/chaos/faults_hard.py`): the symptom appears somewhere other than the cause.

| Fault | Injection | What the agent sees first |
| --- | --- | --- |
| netpol-block | NetworkPolicy `orders-allow-frontend` admits only app=frontend to orders | Frontend 5xx; api logs orders `ConnectTimeout`; every pod healthy |
| dns-namespace-filter | CoreDNS `kubernetes` plugin gets `namespaces kube-system`; CoreDNS and orders restarted | orders can't resolve `postgres` although its ConfigMap is right |
| redis-oom | redis args `--maxmemory 1mb --maxmemory-policy noeviction` | Frontend 5xx; api logs Redis `OutOfMemoryError`; Redis pod healthy |
| cpu-starvation | redis CPU limit and request 1m | Frontend p95 ~20ms → 140-340ms, no errors; Redis heavily throttled |

`cpu-starvation` is the only fault where the app looks healthy by the usual checks, so its verification also requires frontend p95 latency (30s window) back under 60ms.

The reset restores CoreDNS too (`scripts/restore-coredns.sh`): the first run saves the Corefile as `kubebreaker/coredns-baseline`, a namespace the agent can't read, and every reset puts it back and restarts CoreDNS if it changed.

Faults that change a pod template cause a **partial outage**: the rolling update keeps the old pods serving while the new ones fail, as it would in production. The health check still counts them as broken, because the rollout never finishes.

`missing-secret-ref` replaces the original "delete the Secret" idea. A deleted Secret holds a password the agent has no way to recover, while a wrong reference can be found and fixed from what the cluster shows.

## Runtime layout

- `kind` and `kubectl` run on the host. kind is never run inside a container.
- The harness runs in a Docker container attached to the `kind` network, using `kind get kubeconfig --internal`. Only that kubeconfig is mounted, so the agent can't reach any other cluster.
- Prometheus is reached through its in-cluster Service name, not a localhost port-forward.
- During development the harness can also run on the host via `uv`.

## LLM abstraction

The harness depends only on neutral types:

```python
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict

@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage   # input, output, cached tokens
    stop: str      # "tool_use" | "end" | "max_tokens"

class LLMClient(Protocol):
    def chat(self, system: str, messages: list[Msg], tools: list[ToolSpec]) -> LLMResponse: ...
```

Each tool is defined once as a JSON schema. The adapters translate tool schemas, tool results, system prompts, argument encoding and usage fields for each provider. Provider and model are recorded in every trace. The root-cause judge should use a different provider from the agent.

## Agent harness

Code: `src/kubebreaker/llm/` (provider adapters) and `src/kubebreaker/agent/` (tools, loop, identity, trace).

- **OpenAI** uses the Responses API, because newer models only support function tools there. Calls are stateless (`store=False`), and encrypted reasoning items are passed back each turn.
- **Anthropic** uses the Messages API (beta namespace, for server-side fallback), with prompt caching on the system prompt, the tools and the latest turn. Thinking stays at the model default (adaptive) and depth is set with `output_config.effort` (default `medium`, override with `LLM_REASONING_EFFORT`). `max_tokens` defaults to 16,000 because thinking counts toward it. Assistant turns are replayed exactly as returned, thinking blocks included, and the history is append-only, so Claude's preserved-thinking checks pass.
- **Refusals** (`stop_reason: "refusal"`) end the episode with `stop_reason: refusal`; partial content is discarded. Anthropic server-side fallback (`fallbacks: "default"`) is on unless `LLM_FALLBACKS=off`; turns served by a fallback model are counted in `fallback_turns` and listed in `served_by`, so those runs can be excluded from per-model results.
- Each assistant turn keeps its provider-native output (`Message.raw`) so it can be sent back unchanged. If the history is replayed to a different provider, the adapter rebuilds it from the neutral fields instead.
- The loop stops on `declare_resolved`, the step cap (tool calls), the token cap, an LLM error, or 3 turns in a row without tool calls. Budget notices are sent as separate mid-conversation system messages (OpenAI: developer messages), never inside tool results, at two points: halfway, and when 5 or fewer calls remain. Claude Sonnet 5.5 can read harness text inside tool results on every step as a prompt injection.
- The agent's Kubernetes client authenticates as the `kubebreaker/sre-agent` ServiceAccount with a short-lived token (`cluster/agent/rbac.yaml`), never as the kubeconfig admin.

## Agent tools

`get_resources`, `describe`, `get_logs`, `get_events`, `query_metrics`, `apply_patch` (strategic merge), `rollout` (status, restart, undo), `delete` (pods and NetworkPolicies only), `declare_resolved(root_cause, fix_summary)`.

Allowed namespaces are `shop` and `kube-system`; RBAC is the final guard. Secret values are always redacted. Output is trimmed to 6,000 characters (head and tail kept).

Every tool call carries a one-sentence `purpose` (what it checks or changes, and why), so traces read like an investigation log. An earlier version asked for the agent's "current hypothesis" on every call; Claude Sonnet 5.5's `reasoning_extraction` safeguard refused those requests outright, so the field asks for a purpose instead.

## Safety

- Pinned to the `kind-kubebreaker` context; refuses to start otherwise
- ServiceAccount RBAC: the app namespace, plus `get`/`patch` on the `coredns` ConfigMap only (`resourceNames: [coredns]`)
- Operations are allowlisted; namespace deletion and similar actions are blocked
- Every mutation is dry-run on the server first, and the diff is logged

## Reset

Reset works at the namespace level (delete and reapply the app manifests, restore CoreDNS from a saved copy), not by recreating the cluster. The reset is verified against the baseline before every injection.

## Scoring

Code: `src/kubebreaker/scoring/`. Every episode writes these to `result.json`; `kubebreaker results` prints them as a table.

**Snapshots.** Before injecting, the episode snapshots the state we own: Deployments, Services, ConfigMaps, Secrets (values hashed) and NetworkPolicies in `shop`, plus the CoreDNS ConfigMap. Only spec, labels and data are kept, flattened to paths like `deployment/orders:spec.template.spec.containers[orders].resources.limits.memory`. Lists of named items are keyed by name. Cluster-assigned fields, status, and the `restartedAt` annotation are left out, so an untouched cluster snapshots identically every time (there's a test for this).

**Collateral damage.** The final snapshot is diffed against the baseline. Each fault declares a `fix_scope` (the paths where the fix belongs); any difference outside it is collateral. There's a cluster test that each fault's injection lands inside its scope, and that its reference fix scores 1.0.

**Fix quality.** A resolved incident starts at 1.0:

| Finding | Penalty | Examples |
| --- | --- | --- |
| Band-aid | -0.4 each | readiness probe removed, memory limit removed or still too low, replicas below baseline |
| Collateral change | -0.15 each | changing a Deployment the fault didn't touch |
| Unneeded disruption | -0.05 each | deleting pods, restarting a Deployment the fix doesn't require |

Labels: clean (>= 0.9), acceptable (>= 0.6), band-aid (below). Unresolved scores 0. Band-aid detectors live in `chaos/quality.py` and are attached per fault (`quality_checks`).

**Root-cause accuracy.** A judge model (`JUDGE_PROVIDER`, `JUDGE_MODEL`) compares the agent's `root_cause` with the fault's `ground_truth` and returns correct (1.0), partial (0.5) or wrong (0). No root cause scores 0 without calling the judge. When the judge's provider also ran the agent, `judge_same_provider` is set so results can be filtered or caveated.

## Scoring notes

- The collateral diff compares only fields we own (spec, labels, data) and ignores `status`, `resourceVersion`, timestamps and controller-managed annotations.
- Token cost grows with every step, because the full history is sent on each call. Use prompt caching, trim tool output, and log real usage rather than trusting estimates.
- Recording LLM responses for replay during development means we only pay for real benchmark runs.

## Experiments

- Model size: small vs large vs plan-large/execute-small
- Provider: OpenAI vs Anthropic at similar price points
- Signals: logs only vs logs + metrics
- Memory: no runbook vs retrieved past postmortems
- Topology: single agent vs triage + specialist
- Step budget: 5 / 10 / 20

Headline chart: resolve rate against cost per configuration.
