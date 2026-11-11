# kubebreaker

**An AI on-call engineer you can grade.**

kubebreaker breaks a local Kubernetes cluster in a known way, pages an LLM agent with an alert, and lets it diagnose and fix the incident using only what a human SRE would have: resources, events, logs and metrics. Every episode is scored on whether the app recovered, whether the agent named the right root cause, whether the fix was real or a band-aid, and whether it broke anything else.

It runs on a laptop. The [results](#results) below come from 468 incidents across 16 configurations.

**Write-up:** [Fixed, Never Declared: Evaluating LLM On-Call Agents on Kubernetes](https://divya-panchal.hashnode.dev/fixed-never-declared-evaluating-llm-on-call-agents-on-kubernetes), the full study with figures, a per-fault breakdown and failure analysis.

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
| Fix quality | Starts at 1.0; band-aids (removed probe, removed limit, too few replicas) subtract 0.4, collateral changes 0.15, unneeded restarts or pod deletions 0.05 |
| Collateral damage | Every change outside the fault's fix scope, from a before/after snapshot diff |
| Steps, time, tokens | Tool calls, wall clock from alert to recovery, and model tokens per episode |

## Results

The full v2 study, [`experiments/full-v2.yaml`](experiments/full-v2.yaml), ran 468 incidents across 16 configurations, each three times. The agents are GPT-6.1 Terra and Claude Sonnet 5.5 (medium effort). Gemini 3.8 Flash, a third provider, judged every root cause. An incident counts as **solved** when the app recovers *and* the judge grades the stated root cause correct. The best value in each row is bold.

The [write-up](https://divya-panchal.hashnode.dev/fixed-never-declared-evaluating-llm-on-call-agents-on-kubernetes) covers these results in more depth, including token usage and how many steps agents spent after their final fix.

| Category | Measure | Notes | GPT-6.1 Terra | Claude Sonnet 5.5 |
| --- | --- | --- | ---: | ---: |
| **Main result** | Solved | 12 faults, 20 steps, 36 runs | 69.4% | **97.2%** |
| | App recovered | Health checks pass | **100.0%** | 97.2% |
| | Clean fix | Recoveries with no band-aid | **100.0%** | **100.0%** |
| **By tier** | Easy | 3 faults, 9 runs | **100.0%** | **100.0%** |
| | Medium | 5 faults, 15 runs | 80.0% | **100.0%** |
| | Hard | 4 faults, 12 runs | 33.3% | **91.7%** |
| **Step budget** | 5 steps | 36 runs | 11.1% | **25.0%** |
| | 10 steps | 36 runs | 50.0% | **69.4%** |
| | 20 steps | 36 runs | 69.4% | **97.2%** |
| | 30 steps | 36 runs | 91.7% | **97.2%** |
| **Harder conditions** | Paged by Alertmanager | Real Prometheus alert, 36 runs | 61.1% | **83.3%** |
| | Postmortem memory | Notes from earlier incidents, 36 runs | 66.7% | **94.4%** |
| | Two faults at once | 4 pairs, 30 steps, 12 runs | **75.0%** | 58.3% |
| | Model-written faults | 2 faults by Gemini, 6 runs | **100.0%** | **100.0%** |
| **Efficiency** | Steps to solve | Median, lower is better | 16 | **11** |
| | Time to resolve | Median seconds, page to recovery | 21s | **11s** |
| | Tokens per incident | Median, input plus output | **39.3k** | 53.0k |
| | Tokens per solve | All tokens over solved runs | 62.8k | 62.8k |

### Where failures come from

Across all 16 configurations, 146 incidents went unsolved:

| Outcome | Runs | What happened |
| --- | ---: | --- |
| Fixed, never declared | 103 | The app recovered, but the agent kept re-checking metrics and ran out of steps |
| Not recovered | 37 | The step budget ran out with the app still failing (36 at 5 or 10 steps) |
| Declared too early | 6 | The agent closed the incident while the app was still failing |
| Fixed, wrong cause | 0 | No run that restored the app named a wrong root cause |

### Key findings

- **Knowing when to stop is the bottleneck.** Both agents restore service almost every time. Most failures come from not being sure the fix worked before the budget runs out.
- **The hard tier separates the models.** Sonnet solved 11 of 12 hard faults, Terra 4. Terra's misses cluster on `netpol-block`, `dns-namespace-filter` and `wrong-db-host`, where the symptom is far from the cause.
- **Terra is limited by steps, not tokens.** Both use about 63k tokens per solve. Terra takes more, smaller steps, so 30 steps lift it to 91.7%.
- **Two faults expose opposite failure modes.** Sonnet often declares once it has fixed one fault (5 of 12 runs graded partial). Terra fixes both but ran out of steps in 3 of 12.
- **Realism makes it harder; memory doesn't help yet.** Real Alertmanager pages lowered both agents' scores. Postmortem notes moved solve rates only within run-to-run noise.

### Every configuration

| Configuration | Model | Setting | Runs | Solved | App recovered | Declared | Median steps (solved) | Median tokens |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `terra-5` | GPT-6.1 Terra | 5 steps | 36 | 4 (11.1%) | 21 | 4 | 5 | 10.3k |
| `terra-10` | GPT-6.1 Terra | 10 steps | 36 | 18 (50.0%) | 32 | 18 | 10 | 22.2k |
| `terra` | GPT-6.1 Terra | 20 steps | 36 | 25 (69.4%) | 36 | 25 | 16 | 39.3k |
| `terra-30` | GPT-6.1 Terra | 30 steps | 36 | 33 (91.7%) | 36 | 33 | 29 | 63.4k |
| `terra-alertmanager` | GPT-6.1 Terra | Alertmanager paging | 36 | 22 (61.1%) | 36 | 22 | 16.5 | 41.6k |
| `terra-memory` | GPT-6.1 Terra | Postmortem memory | 36 | 24 (66.7%) | 36 | 24 | 17 | 46.3k |
| `terra-multi` | GPT-6.1 Terra | Two faults, 30 steps | 12 | 9 (75.0%) | 12 | 9 | 30 | 74.1k |
| `terra-generated` | GPT-6.1 Terra | Model-written faults | 6 | 6 (100.0%) | 6 | 6 | 17.5 | 35.8k |
| `sonnet-5` | Claude Sonnet 5.5 | 5 steps | 36 | 9 (25.0%) | 23 | 9 | 5 | 16.9k |
| `sonnet-10` | Claude Sonnet 5.5 | 10 steps | 36 | 25 (69.4%) | 32 | 25 | 9 | 44.0k |
| `sonnet` | Claude Sonnet 5.5 | 20 steps | 36 | 35 (97.2%) | 35 | 35 | 11 | 53.0k |
| `sonnet-30` | Claude Sonnet 5.5 | 30 steps | 36 | 35 (97.2%) | 36 | 35 | 10 | 48.6k |
| `sonnet-alertmanager` | Claude Sonnet 5.5 | Alertmanager paging | 36 | 30 (83.3%) | 35 | 31 | 10 | 53.0k |
| `sonnet-memory` | Claude Sonnet 5.5 | Postmortem memory | 36 | 34 (94.4%) | 36 | 34 | 8.5 | 46.7k |
| `sonnet-multi` | Claude Sonnet 5.5 | Two faults, 30 steps | 12 | 7 (58.3%) | 7 | 12 | 23 | 61.9k |
| `sonnet-generated` | Claude Sonnet 5.5 | Model-written faults | 6 | 6 (100.0%) | 6 | 6 | 9 | 40.9k |

### Every fault

Solved out of 3 runs. T is GPT-6.1 Terra, S is Claude Sonnet 5.5. Columns are step budgets, then Alertmanager paging (AM) and postmortem memory (Mem) at 20 steps.

| Tier | Fault | T 5 | S 5 | T 10 | S 10 | T 20 | S 20 | T 30 | S 30 | T AM | S AM | T Mem | S Mem |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Easy | `bad-command` | 0/3 | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
|  | `bad-image-tag` | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
|  | `scaled-to-zero` | 0/3 | 2/3 | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 2/3 | 1/3 | 2/3 | 3/3 |
| Medium | `wrong-db-host` | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 | 3/3 | 3/3 | 0/3 | 3/3 | 0/3 | 2/3 |
|  | `oom-killed` | 1/3 | 0/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
|  | `broken-selector` | 0/3 | 2/3 | 1/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 2/3 | 3/3 | 3/3 | 3/3 |
|  | `failing-readiness` | 1/3 | 0/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
|  | `missing-secret-ref` | 0/3 | 0/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| Hard | `netpol-block` | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 | 3/3 | 3/3 | 3/3 | 1/3 | 3/3 | 1/3 | 3/3 |
|  | `dns-namespace-filter` | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 | 2/3 | 1/3 | 2/3 | 0/3 | 0/3 | 0/3 | 2/3 |
|  | `redis-oom` | 0/3 | 0/3 | 0/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 2/3 | 3/3 | 2/3 | 3/3 |
|  | `cpu-starvation` | 0/3 | 0/3 | 0/3 | 1/3 | 1/3 | 3/3 | 2/3 | 3/3 | 0/3 | 2/3 | 1/3 | 3/3 |

| Kind | Faults | GPT-6.1 Terra | Claude Sonnet 5.5 |
| --- | --- | ---: | ---: |
| Two faults, 30 steps | `bad-image-tag+redis-oom` | 2/3 | 3/3 |
|  | `missing-secret-ref+cpu-starvation` | 3/3 | 3/3 |
|  | `oom-killed+netpol-block` | 2/3 | 0/3 |
|  | `scaled-to-zero+wrong-db-host` | 2/3 | 1/3 |
| Model-written, 20 steps | `gen-api-cpu-request-typo` | 3/3 | 3/3 |
|  | `gen-api-service-targetport-mismatch` | 3/3 | 3/3 |

**Limitations:**
- Three runs per cell, so a difference of one or two solves is noise.
- A single judge model graded every root cause.
- The two model-written faults turned out easy.
- Earlier v1 sweeps (`core`, `step-budget`) ran without the 45-second alert hold, so they aren't comparable with these numbers.

`kubebreaker report runs/sweeps/full-v2` builds an HTML report from the raw results, with a link to every episode's trace.

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

uv run kubebreaker sweep experiments/full-v2.yaml --dry-run   # the plan and a time estimate
uv run kubebreaker sweep experiments/full-v2.yaml             # run it; resumable
uv run kubebreaker report runs/sweeps/full-v2                 # HTML report with charts
```

Or run the harness in a container instead of installing Python dependencies (kind and Docker stay on the host):

```sh
make harness ARGS="run redis-oom"
```

Each episode writes `trace.jsonl` (every model turn and tool call), `result.json` (scores, tokens, every change the agent made) and `snapshots.json` (cluster state before and after, for re-scoring). `make help` lists everything else.

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
  sweep.py      resumable experiment runner
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
| Sweeps, token tracking, results report, harness image | done |
| Stretch: model-generated faults, multi-fault incidents, postmortem memory, Alertmanager paging | done |
| Full v2 study: 468 episodes, 16 configurations | done |

## Related work

[AIOpsLab](https://github.com/microsoft/AIOpsLab) (Microsoft) and [ITBench](https://github.com/IBM/ITBench) (IBM) are larger benchmarks in this space. kubebreaker is laptop-sized, and scores fix quality and collateral damage, not just whether the alert went away.

## License

MIT. See [LICENSE](LICENSE).
