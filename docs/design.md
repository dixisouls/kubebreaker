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

## Agent tools

`get_resources`, `describe`, `get_logs`, `get_events`, `query_metrics`, `apply_patch`, `rollout` (restart and undo), `declare_resolved(root_cause, fix_summary)`.

The agent writes its current hypothesis before every action.

## Safety

- Pinned to the `kind-kubebreaker` context; refuses to start otherwise
- ServiceAccount RBAC: the app namespace, plus `get`/`patch` on the `coredns` ConfigMap only (`resourceNames: [coredns]`)
- Operations are allowlisted; namespace deletion and similar actions are blocked
- Every mutation is dry-run on the server first, and the diff is logged

## Reset

Reset works at the namespace level (delete and reapply the app manifests, restore CoreDNS from a saved copy), not by recreating the cluster. The reset is verified against the baseline before every injection.

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
