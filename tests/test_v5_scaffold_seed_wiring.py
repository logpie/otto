"""Regression: the P0 scaffold-seed PIPELINE WIRING (v5_runner).

Covers the thin glue around the pure planner (tested in
test_v5_scaffold_profiles.py): the hard-commit invariant (R3#3 — seed is
committed on root_branch with a clean tree before decomposition, so child
worktrees branching from the parent integration branch actually receive it),
hydrate-first on re-entry (R4#1 — no 2nd seed commit), the observable
skip/invalid outcomes, and that the committed contract surfaces into the
decomposition runtime context (R2#1) for BOTH the root lead and
architect-contract re-entry.

Real temp git repo (integration, fast) — mocked git would only prove the
mock. The branch-mismatch / clean-tree assertions are the regression-critical
part the Codex Plan Gate added.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from otto.scaffold_profiles import (
    PROFILE_WEBAPP_REACT_VITE_FASTAPI_PY312,
    read_existing_contract,
)
from otto.v5_runner import _scaffold_seed_runtime_context, _seed_scaffold_profile

PID = PROFILE_WEBAPP_REACT_VITE_FASTAPI_PY312


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _fresh_repo(tmp_path: Path) -> tuple[Path, str]:
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    (proj / "intent.md").write_text("Build a React + FastAPI bookmark webapp.")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    branch = _git(proj, "rev-parse", "--abbrev-ref", "HEAD")
    return proj, branch


def _webapp_spec() -> SimpleNamespace:
    return SimpleNamespace(
        project_kind="webapp",
        behavior_journeys=[{"verification_level": "ui", "id": "j1"}],
    )


def _events_sink() -> tuple[list[dict], object]:
    events: list[dict] = []
    return events, (lambda e: events.append(e))


def test_seed_commits_on_root_branch_with_clean_tree(tmp_path) -> None:
    proj, branch = _fresh_repo(tmp_path)
    events, sink = _events_sink()

    action, reason = _seed_scaffold_profile(
        project_dir=proj,
        spec=_webapp_spec(),
        intent="Build a React + FastAPI bookmark webapp.",
        config={},
        root_branch=branch,
        on_event=sink,
    )
    assert action == "seed", reason
    # files written + committed (clean tree, R3#3)
    assert (proj / "start.sh").is_file()
    assert (proj / "backend/.python-version").is_file()
    assert _git(proj, "status", "--porcelain") == ""
    contract = read_existing_contract(proj)
    assert contract is not None and contract["profile_id"] == PID
    committed = [e for e in events if e.get("event") == "scaffold_seed_committed"]
    assert committed and committed[0]["profile_id"] == PID
    assert committed[0]["head_sha"]
    # the seed is a real commit on the root branch
    assert "seed scaffold profile" in _git(proj, "log", "-1", "--pretty=%s")


def test_reentry_hydrates_without_a_second_commit(tmp_path) -> None:
    proj, branch = _fresh_repo(tmp_path)
    _seed_scaffold_profile(
        project_dir=proj, spec=_webapp_spec(), intent="x webapp",
        config={}, root_branch=branch, on_event=None,
    )
    head_after_seed = _git(proj, "rev-parse", "HEAD")
    events, sink = _events_sink()

    action, _ = _seed_scaffold_profile(
        project_dir=proj, spec=_webapp_spec(), intent="x webapp",
        config={}, root_branch=branch, on_event=sink,
    )
    assert action == "hydrate"
    assert _git(proj, "rev-parse", "HEAD") == head_after_seed  # NO 2nd commit
    assert any(e.get("event") == "scaffold_seed_hydrated" for e in events)


def test_branch_mismatch_is_invalid_not_a_wrong_branch_commit(tmp_path) -> None:
    proj, _ = _fresh_repo(tmp_path)
    events, sink = _events_sink()
    action, reason = _seed_scaffold_profile(
        project_dir=proj, spec=_webapp_spec(), intent="x webapp",
        config={}, root_branch="some-other-branch", on_event=sink,
    )
    assert action == "invalid"
    assert "branch_mismatch" in reason
    assert not (proj / "start.sh").exists()
    assert any(
        e.get("event") == "scaffold_seed_state_invalid" for e in events
    )


def test_non_webapp_skips_observably(tmp_path) -> None:
    proj, branch = _fresh_repo(tmp_path)
    events, sink = _events_sink()
    action, reason = _seed_scaffold_profile(
        project_dir=proj,
        spec=SimpleNamespace(project_kind="cli", behavior_journeys=[]),
        intent="A CLI tool.",
        config={},
        root_branch=branch,
        on_event=sink,
    )
    assert action == "skip"
    assert reason and not (proj / "start.sh").exists()
    assert any(e.get("event") == "scaffold_seed_skipped" for e in events)


def test_runtime_context_surfaces_committed_contract(tmp_path) -> None:
    proj, branch = _fresh_repo(tmp_path)
    assert _scaffold_seed_runtime_context(proj) is None  # not seeded yet

    _seed_scaffold_profile(
        project_dir=proj, spec=_webapp_spec(), intent="x webapp",
        config={}, root_branch=branch, on_event=None,
    )
    ctx = _scaffold_seed_runtime_context(proj)
    assert ctx is not None
    assert ctx["profile_id"] == PID
    assert "AUTHORITATIVE" in ctx["note"]
    assert "start.sh" in (ctx["seeded_paths"] or [])
