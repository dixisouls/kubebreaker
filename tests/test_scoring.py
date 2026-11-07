"""Scoring logic on synthetic snapshots. No cluster or API calls."""

import copy

from kubebreaker.chaos import CATALOG
from kubebreaker.llm import LLMResponse, Message, Usage
from kubebreaker.scoring.diff import Change, diff, in_scope
from kubebreaker.scoring.judge import judge_root_cause, parse_verdict
from kubebreaker.scoring.score import score_fix
from kubebreaker.scoring.snapshot import flatten

C = "spec.template.spec.containers"
BASE = {
    "deployment/api": {"spec.replicas": 2, f"{C}[api].image": "kubebreaker/shop:0.1.0"},
    "deployment/frontend": {
        "spec.replicas": 2,
        f"{C}[frontend].readinessProbe.httpGet.path": "/readyz",
        f"{C}[frontend].readinessProbe.httpGet.port": 8080,
    },
    "deployment/orders": {
        "spec.replicas": 2,
        f"{C}[orders].resources.limits.memory": "256Mi",
        f"{C}[orders].resources.requests.memory": "64Mi",
    },
    "configmap/orders-config": {"data.DB_HOST": "postgres"},
}


def changed(**edits):
    snap = copy.deepcopy(BASE)
    for key, value in edits.items():
        obj, path = key.split(":", 1)
        if value is None:
            snap[obj].pop(path, None)
        else:
            snap[obj][path] = value
    return snap


def mods(*pairs):
    return changed(**dict(pairs))


def test_flatten_keys_named_lists_by_name():
    flat = flatten({"spec": {"containers": [{"name": "api", "image": "x", "args": ["a", "b"]}]}})
    assert flat == {
        "spec.containers[api].name": "api",
        "spec.containers[api].image": "x",
        "spec.containers[api].args": ["a", "b"],
    }


def test_flatten_skips_empty_maps():
    assert flatten({"metadata": {"annotations": {}}, "spec": {"x": 1}}) == {"spec.x": 1}


def test_diff_reports_added_removed_and_changed():
    before = {"a/x": {"p": 1}, "a/gone": {"p": 1}}
    after = {"a/x": {"p": 2}, "a/new": {"p": 1}}
    assert {str(c) for c in diff(before, after)} == {
        "a/x:p: 1 -> 2",
        "a/gone removed",
        "a/new added",
    }


def test_scope_matches_prefix_on_path_boundaries_only():
    c = Change("deployment/api", "spec.replicas", 0, 2)
    assert in_scope(c, ("deployment/api:spec.replicas",))
    assert not in_scope(c, ("deployment/api:spec.rep",))


def test_restoring_baseline_is_clean():
    s = score_fix(CATALOG["scaled-to-zero"], True, BASE, copy.deepcopy(BASE), [])
    assert (s.quality, s.label) == (1.0, "clean")


def test_unresolved_scores_zero():
    s = score_fix(CATALOG["scaled-to-zero"], False, BASE, BASE, [])
    assert (s.quality, s.label) == (0.0, "unresolved")


def test_partial_replicas_is_a_band_aid():
    final = mods(("deployment/api:spec.replicas", 1))
    s = score_fix(CATALOG["scaled-to-zero"], True, BASE, final, [])
    assert s.band_aids and s.quality == 0.6 and s.collateral == []


def test_removing_memory_limit_is_a_band_aid():
    final = mods((f"deployment/orders:{C}[orders].resources.limits.memory", None))
    s = score_fix(CATALOG["oom-killed"], True, BASE, final, [])
    assert s.band_aids == ["orders memory limit was removed instead of raised"]


def test_raising_memory_limit_higher_than_baseline_is_fine():
    final = mods((f"deployment/orders:{C}[orders].resources.limits.memory", "512Mi"))
    s = score_fix(CATALOG["oom-killed"], True, BASE, final, [])
    assert (s.quality, s.collateral) == (1.0, []) and len(s.fix_changes) == 1


def test_removing_readiness_probe_is_a_band_aid():
    final = mods(
        (f"deployment/frontend:{C}[frontend].readinessProbe.httpGet.path", None),
        (f"deployment/frontend:{C}[frontend].readinessProbe.httpGet.port", None),
    )
    s = score_fix(CATALOG["failing-readiness"], True, BASE, final, [])
    assert s.band_aids == ["frontend readinessProbe was removed instead of fixed"]


def test_changes_outside_scope_are_collateral():
    final = mods(("deployment/frontend:spec.replicas", 5))
    s = score_fix(CATALOG["scaled-to-zero"], True, BASE, final, [])
    assert s.collateral == ["deployment/frontend:spec.replicas: 2 -> 5"] and s.quality == 0.85


def test_restarts_only_penalized_when_not_needed():
    restart = {"tool": "rollout", "kind": "deployment", "name": "orders", "diff": "restart"}
    deleted = {"tool": "delete", "kind": "pod", "name": "orders-abc", "diff": "deleted"}
    needed = score_fix(CATALOG["wrong-db-host"], True, BASE, BASE, [restart])
    assert needed.quality == 1.0
    extra = score_fix(CATALOG["oom-killed"], True, BASE, BASE, [restart, deleted])
    assert extra.quality == 0.9 and len(extra.disruptive_actions) == 2


# --- judge ---


class FakeJudge:
    provider, model = "fake", "judge-1"

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def chat(self, system, messages, tools):
        self.prompts.append(messages[0].text)
        return LLMResponse(Message("assistant", self.reply), Usage(10, 5), "end")


def test_parse_verdict_handles_extra_text():
    assert parse_verdict('Sure: {"verdict": "partial", "reason": "vague"} done') == (
        "partial",
        "vague",
    )
    assert parse_verdict("no json here")[0] == "wrong"


def test_judge_scores_and_sees_both_texts():
    j = FakeJudge('{"verdict": "correct", "reason": "same object and change"}')
    out = judge_root_cause("truth text", "agent text", "fix text", llm=j)
    assert (out.verdict, out.score, out.judge) == ("correct", 1.0, "fake/judge-1")
    assert "truth text" in j.prompts[0] and "agent text" in j.prompts[0]


def test_judge_skips_llm_when_no_root_cause():
    j = FakeJudge("unused")
    out = judge_root_cause("truth", "  ", "", llm=j)
    assert out.score == 0.0 and out.judge == "none" and not j.prompts
