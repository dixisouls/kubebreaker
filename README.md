# kubebreaker

**An AI on-call engineer you can grade.**

kubebreaker breaks a local Kubernetes cluster in a known way, pages an LLM agent with an alert, and lets it diagnose and fix the incident using only what a human SRE would have: resources, events, logs and metrics. Every episode is scored on whether the app recovered, whether the agent named the right root cause, whether the fix was real or a band-aid, and whether it broke anything else.

It runs on a laptop. A full 72-episode comparison of two models costs about $2.

## What an episode looks like

Claude Sonnet 5.5 on `redis-oom`, where the Redis cache refuses writes and the error surfaces two services away, at the frontend. Condensed from `trace.jsonl`:

```text
ALERT  HighErrorRate: the shop frontend is returning 5xx errors for most requests.

1. get_resources pods          "See pod state in shop"       every pod Running, redis included
2. get_resources endpointslices "Check service endpoints"     all services have ready endpoints
3. get_logs frontend           "See errors"                  ERROR frontend api returned 503 for GET /api/orders
4. get_logs api                "See errors"                  ERROR api redis set failed (host=redis): OutOfMemoryError:
                                                              command not allowed when used memory > 'maxmemory'.
5. describe deployment redis   "Check redis config"          args: --maxmemory 1mb --maxmemory-policy noeviction
6. apply_patch deployment redis "Fix maxmemory"               dry-run diff shown, then applied
7. query_metrics               "Verify errors gone"          frontend: 200s only, no 5xx
8. declare_resolved
   root cause: redis had --maxmemory 1mb with noeviction, so it rejected writes; the api
               returned 503s and the frontend 5xx.
   fix:        set --maxmemory 64mb and --maxmemory-policy allkeys-lru.

Scored: resolved · root cause correct (judge) · fix clean (no band-aids, no collateral)
```

## How it works

1. **Reset** the app namespace and CoreDNS to a verified baseline (~20s).
2. **Inject** one fault from the catalog and wait until its symptom has held for 45 seconds, like an alerting rule's `for:` clause.
3. **Alert** the agent. It works through nine structured tools (no shell), each call stating its purpose, within a step budget.
4. **Verify** the app against its baseline health checks once the agent declares the incident resolved or runs out of steps.
5. **Score**: diff the cluster against a snapshot taken before the fault, check for band-aids, and have a judge model grade the root-cause write-up.

## Fault catalog

| Tier | Fault | What breaks |
| --- | --- | --- |
| Easy | `bad-command` | api container starts a module that doesn't exist |
| Easy | `bad-image-tag` | frontend image tag doesn't exist |
| Easy | `scaled-to-zero` | api scaled to 0 replicas |
| Medium | `wrong-db-host` | orders ConfigMap points at a database host that doesn't exist |
| Medium | `oom-killed` | orders memory limit far too low |
| Medium | `broken-selector` | orders Service selector matches no pods |
| Medium | `failing-readiness` | frontend readiness probe checks the wrong path |
| Medium | `missing-secret-ref` | orders references a Secret that doesn't exist |
| Hard | `netpol-block` | a NetworkPolicy lets only the frontend reach orders, cutting off the api |
| Hard | `dns-namespace-filter` | CoreDNS stops answering for the app's namespace |
| Hard | `redis-oom` | Redis rejects cache writes; errors surface at the frontend |
| Hard | `cpu-starvation` | Redis CPU-starved; latency rises 7-15x with no errors anywhere |

In the hard tier the symptom always shows up somewhere other than the cause. Every fault ships with a reference fix, and `kubebreaker selftest` proves each one can be injected, detected and repaired.

## What gets measured

| Metric | How |
| --- | --- |
| Solved | The app recovered **and** the judge graded the root cause correct |
| Resolved | The fault's health checks pass after the agent stops |
| Root cause | A judge model compares the agent's write-up with the ground truth: correct, partial or wrong |
| Fix quality | Starts at 1.0; band-aids (removed probe, removed limit, too few replicas) cost 0.4, collateral changes 0.15, unneeded restarts or pod deletions 0.05 |
| Collateral damage | Every change outside the fault's fix scope, from a before/after snapshot diff |
| Steps, time, cost | Tool calls, wall clock from alert to recovery, and dollars per episode (agent and judge) |

## Models

- **Agents:** OpenAI (Responses API) and Anthropic (Messages API) behind one provider-neutral interface. Switching is a setting.
- **Judge:** Google Gemini 3.8 Flash, a third provider, so neither agent ever grades its own work.

## Quickstart

Requirements: Docker with at least 8GB for its VM, [`kind`](https://kind.sigs.k8s.io/), `kubectl`, [`uv`](https://docs.astral.sh/uv/), and API keys for the models you use. Don't enable the Kubernetes built into Docker Desktop or OrbStack; kubebreaker uses only its own kind cluster.

```sh
make cluster-up                 # kind + Calico, Prometheus, kube-state-metrics, the shop app (~80s)
cp .env.example .env            # add API keys, pick models
uv sync

uv run kubebreaker faults                   # the catalog
uv run kubebreaker selftest                 # every fault: inject, reference fix, verify (no API calls)
uv run kubebreaker run redis-oom            # one episode with the default agent
uv run kubebreaker run redis-oom --provider anthropic

uv run kubebreaker sweep experiments/core.yaml --dry-run      # plan, cost and time estimate
uv run kubebreaker sweep experiments/core.yaml --max-cost 5   # run it; resumable, asks first
uv run kubebreaker report runs/sweeps/core                    # HTML report with charts
```

Or run the harness in a container instead of installing Python dependencies (kind and Docker stay on the host):

```sh
make harness ARGS="run redis-oom"
```

Each episode writes `trace.jsonl` (every model turn and tool call), `result.json` (scores, tokens, cost, every change the agent made) and `snapshots.json` (cluster state before and after, for re-scoring). `make help` lists everything else.

## Safety

The agent never gets a shell. Its tools run as a dedicated ServiceAccount with RBAC limited to the app namespace, the CoreDNS ConfigMap and Prometheus. It can't touch RBAC, namespaces or nodes. Every change is dry-run on the server first and logged as a diff. Secret values are always redacted, and the harness refuses any kube context that isn't the local kind cluster.

## Project layout

```text
app/            the target shop app (FastAPI services, Postgres, Redis, load generator)
cluster/        kind config, monitoring, the agent's RBAC
src/kubebreaker/
  chaos/        fault catalog, health checks, band-aid detectors, runner
  agent/        tools, loop, identity, trace
  llm/          OpenAI, Anthropic and Google adapters behind one interface
  scoring/      snapshots, collateral diff, fix quality, root-cause judge
  sweep.py      experiment runner with cost estimates and a spend cap
  report.py     HTML results report
experiments/    sweep definitions
docs/design.md  design notes and the reasoning behind them
```

## Status

| Phase | |
| --- | --- |
| Cluster, target app, monitoring, reliable reset | done |
| Chaos injector: 12 faults across three tiers | done |
| Agent harness, provider adapters, safety layer, traces | done |
| Scoring: collateral diff, fix quality, root-cause judge | done |
| Sweeps, cost tracking, results report, harness image | done |
| Stretch: adversarial fault generation, multi-fault incidents, postmortem memory, Alertmanager | not started |

## Related work

[AIOpsLab](https://github.com/microsoft/AIOpsLab) (Microsoft) and [ITBench](https://github.com/IBM/ITBench) (IBM) are larger benchmarks in this space. kubebreaker is laptop-sized and cheap to run, and scores fix quality and collateral damage, not just whether the alert went away.

## License

MIT. See [LICENSE](LICENSE).
