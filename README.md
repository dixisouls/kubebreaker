# kubebreaker

**An AI on-call engineer you can grade.**

kubebreaker breaks a local Kubernetes cluster in a known way, lets an LLM agent diagnose and fix it using only the signals a human SRE would have (resources, events, logs, metrics), and scores every run. The agent can run on OpenAI or Anthropic models.

> Status: early development. Nothing here runs yet.

## How an episode works

1. Reset the app namespace to a verified baseline
2. Inject one fault from the catalog
3. Wait for symptoms, then fire an alert at the agent
4. The agent investigates and acts through a small set of structured tools until it declares a fix or hits its step cap
5. Run the fault's health check, diff the cluster against the baseline, judge the root-cause write-up
6. Log the full trace and score

## What gets measured

| Metric | Measured by |
| --- | --- |
| Resolve rate | The fault's verify check passes after the agent finishes |
| Time to resolve | Wall clock from alert to passing health check |
| Steps | Number of tool calls |
| Cost | Input, output and cached tokens per episode |
| Collateral damage | Changes to the cluster beyond the intended fix |
| Root cause accuracy | A judge model compares the write-up to the ground-truth label |
| Fix quality | A real fix versus a band-aid (e.g. raising a memory limit versus deleting pods until one survives) |

## Fault tiers

- **Easy:** the symptom points straight at the cause (bad image tag, scaled to zero, bad container command)
- **Medium:** the cause sits one hop away (wrong ConfigMap value, OOMKilled, broken Service selector, failing readiness probe, missing Secret)
- **Hard:** the symptom shows up in a different service from the cause (NetworkPolicy block, CoreDNS breakage, CPU starvation, cascading Redis failure)

## Stack

- [kind](https://kind.sigs.k8s.io/) cluster with Calico, so NetworkPolicy is enforced
- A small purpose-built microservice app (frontend, API, orders, Redis, Postgres)
- Prometheus inside the cluster
- Python harness, chaos injector and scorer, packaged as a Docker image
- Provider-agnostic LLM client with OpenAI and Anthropic adapters

## Requirements

- Docker (Docker Desktop, OrbStack or Colima) with at least 8GB of memory for its VM
- `kind` and `kubectl`
- [`uv`](https://docs.astral.sh/uv/) for local development
- An OpenAI or Anthropic API key

Don't enable the built-in Kubernetes in Docker Desktop or OrbStack. kubebreaker uses only its own kind cluster.

## Quickstart

```sh
make cluster-up     # kind cluster, Calico, Prometheus, kube-state-metrics and the shop app
make status         # nodes and pods
make shop           # shop frontend at http://localhost:8080
make prometheus     # Prometheus UI at http://localhost:9090
make app-reset      # put the shop app back to its healthy baseline (~20s)
make cluster-down   # delete everything
```

Break things and check recovery:

```sh
uv sync                                  # install the kubebreaker CLI
uv run kubebreaker faults                # list the fault catalog
uv run kubebreaker inject wrong-db-host  # reset, inject, wait for the symptom, print the alert
uv run kubebreaker verify wrong-db-host  # is the app back to baseline?
uv run kubebreaker selftest              # every fault: inject, reference fix, verify
```

Run the agent against a fault (needs an API key in `.env`, see `.env.example`):

```sh
cp .env.example .env                     # then set LLM_PROVIDER, LLM_MODEL and the key
uv run kubebreaker run wrong-db-host     # reset, inject, alert the agent, verify, save the trace
```

Each run writes `runs/<time>-<fault>/trace.jsonl` (every LLM turn and tool call, with the agent's hypothesis) and `result.json` (resolved, steps, tokens, time, root cause, every change the agent made).

Run `make help` for all targets. Tested with kind v0.33.0 (Kubernetes v1.37.0) and Calico v3.33.0.

## Safety

The agent never gets a shell. It works through structured tools, under a ServiceAccount whose RBAC is limited to the app namespace (plus the CoreDNS ConfigMap), pinned to the `kind-kubebreaker` context. Destructive operations are blocked, and every change is dry-run on the server and logged as a diff before it's applied.

## Roadmap

| Phase | Milestone |
| --- | --- |
| 1 | kind + Calico cluster, target app, Prometheus, reliable reset to baseline |
| 2 | Chaos injector with easy and medium faults |
| 3 | Agent harness, LLM adapters, safety layer, trace logging |
| 4 | Scorer, collateral diffing, root-cause judge, hard faults |
| 5 | Experiment sweeps, results dashboard, example traces |

See [docs/design.md](docs/design.md) for the full design.

## Related work

[AIOpsLab](https://github.com/microsoft/AIOpsLab) (Microsoft) and [ITBench](https://github.com/IBM/ITBench) (IBM) are larger benchmarks in this space. kubebreaker aims to be laptop-sized and cheap to run, and to score fix quality and collateral damage, not just whether the alert went away.

## License

MIT. See [LICENSE](LICENSE).
