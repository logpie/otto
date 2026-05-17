from __future__ import annotations

import subprocess
from pathlib import Path

from otto.v5_preflight_repair import RepairBudget
from otto.v5_runner import (
    _handle_mechanical_preflight_blocker,
    _repair_budget_from_config,
)


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        text=True,
        capture_output=True,
        check=True,
    )


def _init_repo(project_dir: Path) -> None:
    _git(project_dir, "init")
    _git(project_dir, "config", "user.email", "otto@example.invalid")
    _git(project_dir, "config", "user.name", "Otto Test")
    (project_dir / "CHARTER.md").write_text("# Charter\n", encoding="utf-8")
    _git(project_dir, "add", "CHARTER.md")
    _git(project_dir, "commit", "-m", "initial charter")


def test_dirty_runner_output_is_committed_without_llm_repair(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "CHARTER.md").write_text("# Charter\n\nGenerated.\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    payload = {
        "check": "git_checkout_clean",
        "passed": False,
        "issues": [{
            "kind": "worktree_dirty_at_phase",
            "paths": ["CHARTER.md"],
            "message": " M CHARTER.md",
        }],
    }

    action, classified = _handle_mechanical_preflight_blocker(
        payload=payload,
        project_dir=tmp_path,
        on_event=events.append,
    )

    assert action == "retry"
    assert classified is payload
    assert _git(tmp_path, "status", "--short").stdout == ""
    assert events[-1]["kind"] == "dirty_from_otto_output"


def test_foreign_busy_port_fails_fast_with_process_details(tmp_path: Path) -> None:
    payload = {
        "check": "startup_port_cleanup",
        "passed": False,
        "issues": [{"kind": "clean_deploy_port_busy", "severity": "block"}],
        "cleanup": {
            "still_bound_ports": [5173],
            "ports_without_owned_process": [5173],
            "processes": {
                "5173": [{
                    "pid": 12345,
                    "binary": "/usr/bin/python3",
                    "cmdline": "python3 -m http.server 5173",
                }],
            },
        },
    }

    action, classified = _handle_mechanical_preflight_blocker(
        payload=payload,
        project_dir=tmp_path,
    )

    assert action == "terminal"
    assert classified["repair"]["terminal_state"] == "escalated"
    issue = classified["issues"][0]
    assert issue["kind"] == "clean_deploy_port_busy_foreign_process"
    assert issue["ports"] == [5173]
    assert issue["processes"]["5173"][0]["cmdline"] == "python3 -m http.server 5173"


def test_preflight_repair_budget_defaults_to_400_seconds() -> None:
    assert RepairBudget().wall_clock_s == 400.0
    assert _repair_budget_from_config(
        {},
        prefix="merge",
        default_agent_turns=1,
        default_oracle_invocations=1,
    ).wall_clock_s == 400.0
