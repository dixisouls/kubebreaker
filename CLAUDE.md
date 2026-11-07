# kubebreaker

Chaos benchmark for LLM SRE agents on a local kind cluster. Design: `docs/design.md`.

## Git workflow

- Never commit directly to `main` and never push to `main`. All work happens on branches and lands through pull requests.
- Branch names: `feat/<topic>`, `fix/<topic>`, `chore/<topic>`, `docs/<topic>`.
- Keep commits small and focused.

## Conventions

- Python 3.12, managed with `uv`. Lint and format with `ruff`, test with `pytest`.
- The harness talks to LLMs only through the `LLMClient` interface. Never import a provider SDK outside its adapter.
- API keys come from the environment (`.env`, see `.env.example`). Never hardcode or log them.
- Every Kubernetes call must go through the `kind-kubebreaker` context. Never read or modify `~/.kube/config` contexts for other clusters.
- Each fault lives in its own module with `inject`, `verify`, a ground-truth label and an expected fix category.

## Safety

- Never run destructive commands against any cluster other than kind.
- Don't add a raw shell or exec tool for the agent.
