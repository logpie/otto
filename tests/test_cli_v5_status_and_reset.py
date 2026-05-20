"""Smoke tests for `otto v5 status` and `otto v5 reset-verdict` — the
read-only diagnostic + targeted-recovery CLI commands added to address
the iTracker Opus broken-state case (2026-05-20).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"


def _otto(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Invoke `otto v5 ...` via the repo's venv."""
    return subprocess.run(
        [str(VENV_PY), "-m", "otto.cli", "v5", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _seed_project(tmp_path: Path) -> Path:
    """Create a minimal project with a v5 task graph mimicking a broken
    Opus-style state: foundation pass, 1 feature pass, 3 features merge_blocked.
    """
    proj = tmp_path / "broken_proj"
    proj.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=str(proj), check=True
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=str(proj), check=True
    )

    # Minimal task graph (the schema otto.queue.task_graph reads).
    otto_logs = proj / "otto_logs" / "cross-sessions"
    otto_logs.mkdir(parents=True)
    graph = {
        "schema_version": 1,
        "tasks": {
            "root": {
                "id": "root",
                "intent": "iTracker-like",
                "verdict": "partial",
                "child_task_ids": [
                    "v5-foundation", "v5-pass1",
                    "v5-block1", "v5-block2", "v5-block3",
                ],
            },
            "v5-foundation": {
                "id": "v5-foundation",
                "parent_task_id": "root",
                "intent": "Foundation",
                "verdict": "pass",
            },
            "v5-pass1": {
                "id": "v5-pass1",
                "parent_task_id": "root",
                "intent": "Auth & Workspace",
                "verdict": "pass",
            },
            "v5-block1": {
                "id": "v5-block1",
                "parent_task_id": "root",
                "intent": "Issues & Labels",
                "verdict": "merge_blocked",
                "merge_blocked_origin": "verification",
                "merge_blocked_reason": "child verify oracle did not pass",
            },
            "v5-block2": {
                "id": "v5-block2",
                "parent_task_id": "root",
                "intent": "Cycles & Notifications",
                "verdict": "merge_blocked",
                "merge_blocked_origin": "verification",
            },
            "v5-block3": {
                "id": "v5-block3",
                "parent_task_id": "root",
                "intent": "Search & Webhooks",
                "verdict": "merge_blocked",
                "merge_blocked_origin": "verification",
            },
        },
    }
    (otto_logs / "task_graph.json").write_text(
        json.dumps(graph, indent=2), encoding="utf-8"
    )

    # Spec checkpoint (otto v5 status checks for one).
    spec_dir = (
        proj / "otto_logs" / "sessions" / "2026-05-20-000000-aaaaaa" / "spec"
    )
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.json").write_text('{"schema_version": 4}', encoding="utf-8")

    return proj


def test_status_renders_resumable_with_blocked_children(tmp_path: Path) -> None:
    proj = _seed_project(tmp_path)
    r = _otto("status", cwd=proj)
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    out = r.stdout
    assert "Root verdict:" in out
    assert "partial" in out
    assert "Children (5):" in out
    assert "v5-block1" in out
    assert "merge_blocked" in out
    assert "RESUMABLE" in out
    # When there ARE merge_blocked children, status surfaces the
    # reset-verdict suggestion (this is the actionable diagnostic).
    assert "reset-verdict" in out
    assert "v5-block1" in out and "v5-block2" in out and "v5-block3" in out


def test_status_verbose_shows_metadata(tmp_path: Path) -> None:
    proj = _seed_project(tmp_path)
    r = _otto("status", "--verbose", cwd=proj)
    assert r.returncode == 0
    # Verbose mode surfaces structured failure reasons per child.
    assert "merge_blocked_origin: verification" in r.stdout
    assert "child verify oracle did not pass" in r.stdout


def test_status_verbose_prefers_top_level_metadata(tmp_path: Path) -> None:
    proj = _seed_project(tmp_path)
    graph_path = proj / "otto_logs" / "cross-sessions" / "task_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["tasks"]["v5-block1"]["metadata"] = {
        "merge_blocked_origin": "legacy-nested"
    }
    graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")

    r = _otto("status", "--verbose", cwd=proj)
    assert r.returncode == 0
    assert "merge_blocked_origin: verification" in r.stdout
    assert "legacy-nested" not in r.stdout


def test_status_no_graph_message(tmp_path: Path) -> None:
    proj = tmp_path / "empty"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=str(proj), check=True
    )
    r = _otto("status", cwd=proj)
    # Either prints "no task graph found" or exits cleanly; tolerate both.
    assert r.returncode == 0
    assert "no v5 task graph" in r.stdout or "no root task" in r.stdout


def test_reset_verdict_dry_run_shows_plan(tmp_path: Path) -> None:
    proj = _seed_project(tmp_path)
    r = _otto(
        "reset-verdict",
        "--task", "v5-block1", "--task", "v5-block2",
        "--dry-run", cwd=proj,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    out = r.stdout
    assert "v5-block1: merge_blocked → unverified" in out
    assert "v5-block2: merge_blocked → unverified" in out
    assert "dry-run" in out
    # Verify graph NOT modified.
    graph_path = proj / "otto_logs" / "cross-sessions" / "task_graph.json"
    graph_after = json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph_after["tasks"]["v5-block1"]["verdict"] == "merge_blocked"


def test_reset_verdict_actually_writes(tmp_path: Path) -> None:
    proj = _seed_project(tmp_path)
    r = _otto(
        "reset-verdict",
        "--task", "v5-block1",
        cwd=proj,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}\nstdout: {r.stdout}"
    graph_path = proj / "otto_logs" / "cross-sessions" / "task_graph.json"
    graph_after = json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph_after["tasks"]["v5-block1"]["verdict"] == "unverified"
    assert graph_after["tasks"]["v5-block1"].get("merge_blocked_reason") is None
    assert graph_after["tasks"]["v5-block1"].get("merge_blocked_origin") is None
    # Other tasks untouched.
    assert graph_after["tasks"]["v5-block2"]["verdict"] == "merge_blocked"
    assert graph_after["tasks"]["v5-pass1"]["verdict"] == "pass"
