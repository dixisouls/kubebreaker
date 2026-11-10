"""kubebreaker: a chaos benchmark for LLM SRE agents."""

# Bump whenever a change alters what the agent sees or can do, so results from different
# versions are never compared as if they came from the same benchmark.
#   1: original harness (results without a harness_version field)
#   2: get_logs since_seconds, guidance on metric lag when verifying a fix
HARNESS_VERSION = 2
