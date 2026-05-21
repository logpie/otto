"""Phase 2 tests (plan-checkpoint-resume-v2.md): `otto v5 plan-resume`
— read-only resume simulation via a canonical planner shared with the
runner + status.

Codex Plan Gate APPROVED at R5; this is Phase 2.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from otto.v5_resume_plan import compute_resume_plan, plan_to_json


REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=str(path), check=True
    )


def _seed_graph(project_dir: Path, tasks: dict) -> None:
    cross = project_dir / "otto_logs" / "cross-sessions"
    cross.mkdir(parents=True, exist_ok=True)
    graph = {"schema_version": 1, "tasks": tasks}
    (cross / "task_graph.json").write_text(
        json.dumps(graph, indent=2), encoding="utf-8"
    )


def _make_spec(project_dir: Path) -> str:
    sdir = project_dir / "otto_logs" / "sessions" / "2026-05-20-000000-aaaaaa"
    spec_dir = sdir / "spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.json").write_text('{"schema_version": 4}', encoding="utf-8")
    return sdir.name


# --- planner ---------------------------------------------------------


def test_plan_resume_resumable_with_blocked_children(tmp_path: Path):
    """Mimics iTracker Opus broken state: foundation+pass / 3 blocked / 1 pass."""
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {
            "id": "root",
            "intent": "iTracker",
            "verdict": "partial",
            "child_task_ids": ["v5-found", "v5-pass1", "v5-b1", "v5-b2", "v5-b3"],
        },
        "v5-found": {"id": "v5-found", "verdict": "pass", "task_role": "foundation",
                     "parent_task_id": "root", "intent": "foundation"},
        "v5-pass1": {"id": "v5-pass1", "verdict": "pass",
                     "parent_task_id": "root", "intent": "auth"},
        "v5-b1": {"id": "v5-b1", "verdict": "merge_blocked",
                  "merge_blocked_origin": "verification",
                  "merge_blocked_reason": "child verify failed",
                  "parent_task_id": "root", "intent": "issues"},
        "v5-b2": {"id": "v5-b2", "verdict": "merge_blocked",
                  "merge_blocked_origin": "verification",
                  "parent_task_id": "root", "intent": "cycles"},
        "v5-b3": {"id": "v5-b3", "verdict": "merge_blocked",
                  "merge_blocked_origin": "verification",
                  "parent_task_id": "root", "intent": "search"},
    })
    spec_session = _make_spec(tmp_path)

    plan = compute_resume_plan(project_dir=tmp_path, model="opus")
    assert plan.status == "RESUMABLE"
    assert plan.phase_to_enter == "integration"
    assert plan.root_verdict == "partial"
    assert plan.latest_spec_checkpoint == spec_session

    actions = {c.task_id: c.action for c in plan.children}
    assert actions["v5-found"] == "skip_pass"
    assert actions["v5-pass1"] == "skip_pass"
    assert actions["v5-b1"] == "stays_merge_blocked"
    assert actions["v5-b2"] == "stays_merge_blocked"
    assert actions["v5-b3"] == "stays_merge_blocked"

    # Suggests retry-children for the 3 blocked
    suggestion_text = "\n".join(plan.suggested_next)
    assert "retry-children" in suggestion_text
    for tid in ["v5-b1", "v5-b2", "v5-b3"]:
        assert tid in suggestion_text


def test_plan_resume_not_resumable_on_pass_root(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {"id": "root", "verdict": "pass", "child_task_ids": ["v5-x"],
                 "intent": "done"},
        "v5-x": {"id": "v5-x", "verdict": "pass", "parent_task_id": "root"},
    })
    _make_spec(tmp_path)
    plan = compute_resume_plan(project_dir=tmp_path)
    assert plan.status == "NOT_RESUMABLE"
    assert "terminal done" in (plan.not_resumable_reason or "")


def test_plan_resume_fresh_only_when_no_graph(tmp_path: Path):
    _init_git_repo(tmp_path)
    plan = compute_resume_plan(project_dir=tmp_path)
    assert plan.status == "FRESH_ONLY"


def test_plan_resume_not_resumable_no_spec_checkpoint(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {"id": "root", "verdict": "partial",
                 "child_task_ids": ["v5-x"], "intent": "foo"},
        "v5-x": {"id": "v5-x", "verdict": "partial", "parent_task_id": "root"},
    })
    # No spec.json created.
    plan = compute_resume_plan(project_dir=tmp_path)
    assert plan.status == "NOT_RESUMABLE"
    assert "spec.json" in (plan.not_resumable_reason or "")


def test_plan_resume_intent_drift_refuses(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {"id": "root", "verdict": "partial",
                 "child_task_ids": ["v5-x"], "intent": "old intent"},
        "v5-x": {"id": "v5-x", "verdict": "pass", "parent_task_id": "root"},
    })
    _make_spec(tmp_path)
    plan = compute_resume_plan(
        project_dir=tmp_path, intent_for_match="completely different intent"
    )
    assert plan.status == "NOT_RESUMABLE"
    assert "intent" in (plan.not_resumable_reason or "")


def test_plan_resume_recognizes_retry_pending(tmp_path: Path):
    """A task that's been through retry-children: verdict=None + retry_count>0
    → predicted action is rebuild_via_retry."""
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {"id": "root", "verdict": "partial",
                 "child_task_ids": ["v5-retry"], "intent": "foo"},
        "v5-retry": {"id": "v5-retry", "verdict": None, "retry_count": 1,
                     "retry_reason": "cli_retry_children",
                     "parent_task_id": "root", "intent": "retry me"},
    })
    _make_spec(tmp_path)
    plan = compute_resume_plan(project_dir=tmp_path, model="opus")
    assert plan.status == "RESUMABLE"
    assert plan.children[0].action == "rebuild_via_retry"
    assert plan.children[0].retry_count == 1
    # Cost estimate now non-zero for the rebuild
    assert plan.estimated_cost_usd_range[1] > 5.0  # p50 > base integration


def test_plan_resume_cost_estimates_scale_with_rebuild_count(tmp_path: Path):
    """3 rebuild children should cost roughly 3x what 1 rebuild costs."""
    _init_git_repo(tmp_path)
    children = {
        f"v5-r{i}": {
            "id": f"v5-r{i}",
            "verdict": None,
            "retry_count": 1,
            "parent_task_id": "root",
            "intent": f"retry {i}",
        }
        for i in range(3)
    }
    children["root"] = {
        "id": "root",
        "verdict": "partial",
        "intent": "foo",
        "child_task_ids": list(children.keys()),
    }
    _seed_graph(tmp_path, children)
    _make_spec(tmp_path)
    plan = compute_resume_plan(project_dir=tmp_path, model="opus")
    # Cost p50 should be > 50 (3 opus rebuilds × $35 each + integration)
    assert plan.estimated_cost_usd_range[1] > 50.0


def test_plan_resume_opt_out_respects_config(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {"id": "root", "verdict": "partial",
                 "child_task_ids": ["v5-x"], "intent": "foo"},
        "v5-x": {"id": "v5-x", "verdict": "pass", "parent_task_id": "root"},
    })
    _make_spec(tmp_path)
    plan = compute_resume_plan(
        project_dir=tmp_path, config={"v5_resume_from_checkpoint": False},
    )
    assert plan.status == "NOT_RESUMABLE"
    assert "opt-out" in (plan.not_resumable_reason or "")


# --- JSON schema -----------------------------------------------------


def test_plan_to_json_schema_versioned(tmp_path: Path):
    """The JSON output must include `schema_version` so MC/scripts can
    detect breaking changes."""
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {"id": "root", "verdict": "partial",
                 "child_task_ids": ["v5-x"], "intent": "foo"},
        "v5-x": {"id": "v5-x", "verdict": "merge_blocked", "parent_task_id": "root"},
    })
    _make_spec(tmp_path)
    plan = compute_resume_plan(project_dir=tmp_path)
    payload = json.loads(plan_to_json(plan))
    assert payload["schema_version"] == 1
    assert payload["status"] == "RESUMABLE"
    assert "children" in payload
    assert "estimated_cost_usd_range" in payload
    assert {"low", "p50", "high"} <= set(payload["estimated_cost_usd_range"].keys())


# --- CLI smoke tests -------------------------------------------------


def _otto(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(VENV_PY), "-m", "otto.cli", "recover", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def test_cli_plan_resume_renders_human_text(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {"id": "root", "verdict": "partial",
                 "child_task_ids": ["v5-x"], "intent": "foo"},
        "v5-x": {"id": "v5-x", "verdict": "merge_blocked",
                 "parent_task_id": "root", "intent": "issues"},
    })
    _make_spec(tmp_path)
    r = _otto("plan-resume", "--model", "opus", cwd=tmp_path)
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    out = r.stdout
    assert "RESUMABLE" in out
    assert "stays_merge_blocked" in out
    assert "retry-children" in out
    assert "Cost estimate" in out


def test_cli_plan_resume_json_output(tmp_path: Path):
    _init_git_repo(tmp_path)
    _seed_graph(tmp_path, {
        "root": {"id": "root", "verdict": "partial",
                 "child_task_ids": ["v5-x"], "intent": "foo"},
        "v5-x": {"id": "v5-x", "verdict": "pass", "parent_task_id": "root"},
    })
    _make_spec(tmp_path)
    r = _otto("plan-resume", "--json", cwd=tmp_path)
    assert r.returncode == 0
    # First line of stdout might have Rich console noise; find the JSON.
    text = r.stdout
    json_start = text.find("{")
    assert json_start >= 0
    payload = json.loads(text[json_start:])
    assert payload["schema_version"] == 1
    assert payload["status"] == "RESUMABLE"
