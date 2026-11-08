"""The HTML report builds from result files and escapes what it reads."""

import json

from kubebreaker import report


def result(**kw):
    base = {
        "fault_id": "wrong-db-host",
        "tier": "medium",
        "model": "gpt-6.1-terra",
        "resolved": True,
        "declared": True,
        "root_cause_verdict": "correct",
        "fix_quality": 1.0,
        "fix_label": "clean",
        "steps": 12,
        "max_steps": 20,
        "agent_cost_usd": 0.02,
        "judge_cost_usd": 0.002,
        "time_to_resolve_seconds": 30,
        "stop_reason": "declared",
    }
    return {**base, **kw}


def write(root, label, fault, n, **kw):
    d = root / "sweeps" / "exp" / label / fault / f"r{n}"
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps(result(fault_id=fault, **kw)))


def test_report_builds_with_all_sections(tmp_path):
    write(tmp_path, "terra-10", "wrong-db-host", 1, max_steps=10)
    write(tmp_path, "terra-20", "wrong-db-host", 1, resolved=False, root_cause_verdict="wrong")
    write(tmp_path, "sonnet", "netpol-block", 1, model="claude-sonnet-5-5", tier="hard")
    page = report.build(tmp_path)
    for section in (
        "Configurations",
        "Solve rate vs cost",
        "Solve rate by tier",
        "Step budget",
        "Fault by configuration",
        "All 3 episodes",
    ):
        assert section in page
    assert page.count("<svg") == 3


def test_step_curve_only_when_budgets_vary(tmp_path):
    write(tmp_path, "terra", "wrong-db-host", 1)
    assert "Step budget" not in report.build(tmp_path)


def test_labels_from_disk_are_escaped(tmp_path):
    write(tmp_path, "x", "wrong-db-host", 1, model="<script>alert(1)</script>")
    page = report.build(tmp_path)
    assert "<script>alert(1)</script>" not in page
