from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_field_tests as rig  # noqa: E402


def _scenario_dir(root: Path, name: str, *, kind: str = "web") -> Path:
    path = root / name
    path.mkdir(parents=True)
    path.joinpath("intent.md").write_text("# Intent\n\nBuild something.\n", encoding="utf-8")
    path.joinpath("expected_shape.md").write_text(
        "# Expected Shape\n\nInline for a tiny product.\n",
        encoding="utf-8",
    )
    path.joinpath("success_criteria.md").write_text(
        "# Success Criteria\n\n"
        f"- kind: {kind}\n"
        "- budget_seconds: 777\n"
        "- max_parallel: 2\n"
        "- tier: auto\n"
        "- boot_smoke: true\n"
        "- smoke_path: /health\n"
        "- smoke_port_var: PORT\n",
        encoding="utf-8",
    )
    return path


def test_parse_success_metadata() -> None:
    meta = rig.parse_success_metadata(
        "- kind: cli\n"
        "- budget-seconds: 900\n"
        "- boot_smoke: false\n"
        "not metadata\n"
    )

    assert meta["kind"] == "cli"
    assert meta["budget_seconds"] == "900"
    assert meta["boot_smoke"] == "false"


def test_discover_scenarios_ignores_runs_dir(tmp_path: Path) -> None:
    _scenario_dir(tmp_path, "01-one")
    (tmp_path / "runs").mkdir()
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")

    scenarios = rig.discover_scenarios(tmp_path)

    assert [s.name for s in scenarios] == ["01-one"]
    assert scenarios[0].budget_seconds == 777
    assert scenarios[0].tier == "auto"
    assert scenarios[0].smoke_path == "/health"


def test_load_scenario_rejects_unknown_tier(tmp_path: Path) -> None:
    path = _scenario_dir(tmp_path, "01-one")
    success = path.joinpath("success_criteria.md")
    success.write_text(
        success.read_text(encoding="utf-8").replace("- tier: auto\n", "- tier: diagonal\n"),
        encoding="utf-8",
    )

    try:
        rig.load_scenario(path)
    except ValueError as exc:
        assert "invalid tier" in str(exc)
    else:
        raise AssertionError("invalid tier should fail scenario loading")


def test_collect_metrics_from_task_graph_and_summaries(tmp_path: Path) -> None:
    graph_dir = tmp_path / "otto_logs" / "cross-sessions"
    graph_dir.mkdir(parents=True)
    graph_dir.joinpath("task_graph.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": {
                    "root": {
                        "decomposition": "emit",
                        "verdict": "partial",
                        "cost_usd": 0.5,
                        "child_task_ids": ["api", "ui"],
                    },
                    "api": {
                        "parent_task_id": "root",
                        "decomposition": "inline",
                        "verdict": "pass",
                        "cost_usd": 0.25,
                        "child_task_ids": [],
                    },
                    "ui": {
                        "parent_task_id": "root",
                        "decomposition": "inline",
                        "verdict": "unverified",
                        "cost_usd": 0.3,
                        "child_task_ids": [],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    session = tmp_path / "otto_logs" / "sessions" / "s1"
    session.mkdir(parents=True)
    session.joinpath("summary.json").write_text(
        json.dumps(
            {
                "task_id": "ui",
                "verdict": "unverified",
                "duration_s": 12.5,
                "cost_usd": 0.3,
                "verify_result": {"summary": "missing browser proof"},
            }
        ),
        encoding="utf-8",
    )

    metrics = rig.collect_metrics(tmp_path)

    assert metrics["tree_nodes"] == 3
    assert metrics["tree_depth"] == 2
    assert metrics["shape"] == "root emit -> 2 children"
    assert metrics["agent_seconds"] == 12.5
    assert metrics["cost_usd"] == 1.05
    assert "final verdict partial" in metrics["bugs"]
    assert "ui: unverified" in metrics["bugs"]


def test_render_report_includes_dry_run_matrix(tmp_path: Path) -> None:
    scenario = rig.load_scenario(_scenario_dir(tmp_path, "01-one", kind="cli"))
    result = rig.run_scenario(
        scenario,
        run_root=tmp_path / "runs" / "dry",
        ports=rig.PortAllocation(index=0, start=19000, end=19099),
        boot_smoke_enabled=True,
        boot_timeout_s=1,
        dry_run=True,
    )

    report = rig.render_report(
        results=[result],
        scenarios=[scenario],
        run_id="dry",
        run_root=tmp_path / "runs" / "dry",
        dry_run=True,
    )

    assert "No live Otto runs were executed" in report
    assert "`01-one`" in report
    assert "otto run <intent>" in report
    assert "| Scenario | Tier | Expected |" in report
    assert "| `01-one` | `auto` | Inline for a tiny product." in report
