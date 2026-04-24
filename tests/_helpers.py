"""Shared test helpers for otto's pytest suite.

These are plain functions (not pytest fixtures) so call sites stay
explicit: `init_repo(tmp_path, ...)` reads more naturally than injecting
a `init_repo` factory fixture into every test signature.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def init_repo(
    tmp_path: Path,
    *,
    subdir: str | None = "repo",
    commit_file: str = "f.txt",
    commit_content: str = "x",
    commit_msg: str = "i",
    initial_commit: bool = True,
) -> Path:
    """Create a tiny git repo on `main` with one commit. Returns the repo path.

    Defaults match the most-common pattern across tests/. Override for
    variants:
    - `subdir=None` to init in `tmp_path` directly (instead of `tmp_path/"repo"`)
    - `initial_commit=False` for tests that need an empty repo
    - `commit_content`/`commit_msg` for tests that assert on the exact content
    """
    repo = tmp_path / subdir if subdir else tmp_path
    if not repo.exists():
        repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=repo,
        check=True,
    )
    if initial_commit:
        (repo / commit_file).write_text(commit_content)
        subprocess.run(
            ["git", "add", commit_file],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", commit_msg],
            cwd=repo,
            check=True,
        )
    return repo


def write_test_pow_report(
    output_dir: Path,
    story_results: list[dict[str, Any]],
    outcome: str,
    duration: float,
    cost: float,
    passed: int,
    total: int,
    *,
    diagnosis: str = "",
    round_history: list[dict[str, Any]] | None = None,
    evidence_dir: Path | None = None,
    certifier_cost: float | None = None,
    coverage_observed: list[str] | None = None,
    coverage_gaps: list[str] | None = None,
    coverage_emitted: bool | None = None,
) -> dict[str, Any]:
    """Build and write a proof-of-work report with the current report pipeline."""
    from otto.certifier import _build_pow_report_data, _write_pow_report

    options = type("Opts", (), {"provider": "", "model": None, "effort": None})()
    report = _build_pow_report_data(
        project_dir=output_dir,
        report_dir=output_dir,
        log_dir=output_dir,
        run_id="ad-hoc-report",
        session_id="",
        pipeline_mode="agentic_v3",
        certifier_mode="standard",
        outcome=outcome,
        story_results=story_results,
        diagnosis=diagnosis,
        certify_rounds=[
            {
                "round": item.get("round", index + 1),
                "stories": [],
                "verdict": (
                    item.get("verdict") == "passed"
                    if isinstance(item.get("verdict"), str)
                    else item.get("verdict")
                ),
                "diagnosis": item.get("diagnosis", ""),
                "tested": item.get("stories_tested", item.get("stories_count", 0)),
            }
            for index, item in enumerate(round_history or [])
        ],
        duration_s=duration,
        certifier_cost_usd=float(certifier_cost if certifier_cost is not None else cost),
        total_cost_usd=float(cost),
        intent="",
        options=options,
        evidence_dir=evidence_dir,
        stories_tested=total,
        stories_passed=passed,
        coverage_observed=coverage_observed,
        coverage_gaps=coverage_gaps,
        coverage_emitted=coverage_emitted,
        round_timings=None,
    )
    _write_pow_report(output_dir, report)
    return report
