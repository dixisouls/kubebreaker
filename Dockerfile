# The kubebreaker harness: CLI, agent, chaos injector and scorer, plus kubectl for resets.
# kind itself stays on the host; this container joins kind's Docker network.
#   make harness-image
#   make harness ARGS="run wrong-db-host"
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# Matches the cluster's Kubernetes version; checksums from dl.k8s.io
ARG KUBECTL_VERSION=v1.37.0
ARG KUBECTL_SHA256_amd64=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
ARG KUBECTL_SHA256_arm64=922df28df248cc00a9e025f947704f1d1482de64ece54cfe57e61f19eaf1eef3
ARG TARGETARCH

# Inside the image only: install curl, clear apt's package lists, fetch and verify kubectl
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/kubectl \
       "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && eval "sha=\$KUBECTL_SHA256_${TARGETARCH}" \
    && echo "${sha}  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONWARNINGS=ignore::SyntaxWarning
WORKDIR /kubebreaker

# Dependencies first, so code changes don't reinstall them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Everything the harness reads at run time: code, reset scripts, app manifests, agent RBAC
COPY src ./src
COPY scripts ./scripts
COPY app/k8s ./app/k8s
COPY cluster ./cluster
COPY experiments ./experiments
RUN uv sync --frozen --no-dev && mkdir -p runs && chmod 777 runs

# The kind kubeconfig for the cluster's internal address is mounted here
ENV KUBECONFIG=/kube/config
ENTRYPOINT ["kubebreaker"]
CMD ["--help"]
