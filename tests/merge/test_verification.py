from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from otto.merge import verification
from otto.merge.git_ops import GitResult


def test_merge_verification_plan_detects_overlap_and_requires_targeted_checks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_git(project_dir: Path, *args: str) -> GitResult:
        del project_dir
        ref = args[-1]
        if ref.endswith("feature/a"):
            return GitResult(0, "app/models.py\napp/a.py\n", "")
        if ref.endswith("feature/b"):
            return GitResult(0, "app/models.py\napp/b.py\n", "")
        return GitResult(1, "", "unknown ref")

    monkeypatch.setattr(verification.git_ops, "run_git", fake_run_git)

    plan = verification.build_merge_verification_plan(
        project_dir=tmp_path,
        target="main",
        branches=["feature/a", "feature/b"],
        queue_lookup={"feature/a": "task-a", "feature/b": "task-b"},
        target_head_before="base",
        target_head_after="head",
        changed_files=["app/models.py", "app/a.py", "app/b.py"],
        stories=[
            {"story_id": "a-story", "source_branch": "feature/a"},
            {"story_id": "b-story", "source_branch": "feature/b"},
        ],
        outcomes=[],
        full_verify=False,
    )

    assert plan.risk_level == "risky_overlap"
    assert plan.verification_level == "targeted"
    assert plan.allow_skip is False
    assert plan.overlapping_files == ["app/models.py"]
    assert [story.action for story in plan.stories] == ["CHECK", "CHECK"]
    rendered = verification.format_merge_verification_plan(plan)
    assert "Risk level: `risky_overlap`" in rendered
    assert "`app/models.py`" in rendered


def test_merge_verification_plan_allows_skips_for_clean_disjoint_merge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_git(project_dir: Path, *args: str) -> GitResult:
        del project_dir
        ref = args[-1]
        if ref.endswith("feature/a"):
            return GitResult(0, "app/a.py\n", "")
        if ref.endswith("feature/b"):
            return GitResult(0, "app/b.py\n", "")
        return GitResult(1, "", "unknown ref")

    monkeypatch.setattr(verification.git_ops, "run_git", fake_run_git)

    plan = verification.build_merge_verification_plan(
        project_dir=tmp_path,
        target="main",
        branches=["feature/a", "feature/b"],
        queue_lookup={},
        target_head_before="base",
        target_head_after="head",
        changed_files=["app/a.py", "app/b.py"],
        stories=[
            {"story_id": "a-main", "source_branch": "feature/a"},
            {"story_id": "a-secondary", "source_branch": "feature/a"},
            {"story_id": "b-main", "source_branch": "feature/b"},
        ],
        outcomes=[],
        full_verify=False,
    )

    assert plan.risk_level == "clean_disjoint"
    assert plan.verification_level == "selective"
    assert plan.allow_skip is True
    assert [story.action for story in plan.stories] == [
        "CHECK",
        "SKIP_ALLOWED",
        "CHECK",
    ]


def test_merge_verification_plan_for_conflict_resolution_forces_full_checks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        verification.git_ops,
        "run_git",
        lambda *args, **kwargs: GitResult(0, "app/view.py\n", ""),
    )

    plan = verification.build_merge_verification_plan(
        project_dir=tmp_path,
        target="main",
        branches=["feature/a"],
        queue_lookup={},
        target_head_before="base",
        target_head_after="head",
        changed_files=["app/view.py"],
        stories=[{"story_id": "ui-flow", "source_branch": "feature/a"}],
        outcomes=[SimpleNamespace(status="conflict_resolved")],
        full_verify=False,
    )

    assert plan.risk_level == "conflict_resolved"
    assert plan.verification_level == "full"
    assert plan.allow_skip is False
    assert plan.stories[0].action == "CHECK"
