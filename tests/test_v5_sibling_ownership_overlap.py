"""Unit tests for the Phase 3 sibling-ownership overlap helpers.

Pure-helper testing only: `_validate_owned_path` and
`_compute_sibling_owned_path_overlap_findings`. Integration of the helpers
into `_foundation_isolation_feedback` + the dispatch loop is covered in
`test_v5_decomposed_child_lands_in_main.py`.

Plan: plan-phase-3-sibling-ownership.md
"""
from __future__ import annotations

import pytest

from otto.v5_runner import (
    _compute_sibling_owned_path_overlap_findings,
    _validate_owned_path,
)


# ---- _validate_owned_path -------------------------------------------------


@pytest.mark.parametrize(
    "good_path",
    [
        "frontend/src/App.tsx",
        "backend/app/routers/bookmarks.py",
        "backend/tests/test_bookmarks.py",
        "frontend/src/pages/Bookmarks.tsx",
        "docs/architecture.md",
    ],
)
def test_validate_accepts_normal_relative_paths(good_path: str) -> None:
    assert _validate_owned_path(good_path) is None


@pytest.mark.parametrize(
    ("bad_path", "expected_kind"),
    [
        ("", "invalid_owned_path"),
        ("   ", "invalid_owned_path"),
        ("/etc/passwd", "invalid_owned_path"),  # POSIX absolute
        ("/", "invalid_owned_path"),
        ("//server/share/foo", "invalid_owned_path"),  # UNC via double-slash
        ("\\\\server\\share\\foo", "invalid_owned_path"),  # UNC via backslash
        ("C:\\Windows\\App.tsx", "invalid_owned_path"),  # Windows drive
        ("D:/src/App.tsx", "invalid_owned_path"),  # Windows drive forward-slash
        ("features/../bookmarks.py", "invalid_owned_path"),  # parent traversal
        ("./bookmarks.py", "invalid_owned_path"),  # dot segment after strip
        ("./../etc", "invalid_owned_path"),
    ],
)
def test_validate_rejects_pathological_paths(
    bad_path: str, expected_kind: str
) -> None:
    finding = _validate_owned_path(bad_path)
    assert finding is not None
    assert finding["kind"] == expected_kind
    assert "reason" in finding


@pytest.mark.parametrize(
    "glob_path",
    [
        "frontend/src/**/*.tsx",
        "backend/app/*.py",
        "backend/tests/test_?.py",
        "docs/[abc]*.md",
        "frontend/src/components/**",
    ],
)
def test_validate_rejects_globs(glob_path: str) -> None:
    finding = _validate_owned_path(glob_path)
    assert finding is not None
    assert finding["kind"] == "unsupported_owned_path_glob"
    assert "globs" in finding["reason"] or "wildcards" in finding["reason"]


# ---- _compute_sibling_owned_path_overlap_findings -------------------------


def test_overlap_empty_inputs() -> None:
    assert _compute_sibling_owned_path_overlap_findings(feature_owners=[]) == []


def test_overlap_disjoint_features_no_findings() -> None:
    feature_owners = [
        ("v5-bookmarks", ["backend/app/routers/bookmarks.py"]),
        ("v5-tasks", ["backend/app/routers/tasks.py"]),
        ("v5-notes", ["backend/app/routers/notes.py"]),
    ]
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners
    )
    assert findings == []


def test_overlap_exact_path_two_features() -> None:
    feature_owners = [
        ("v5-bookmarks", ["frontend/src/App.tsx"]),
        ("v5-tasks", ["frontend/src/App.tsx"]),
    ]
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners
    )
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "feature_owned_paths_overlap"
    assert {f["task_id"], f["other_task_id"]} == {"v5-bookmarks", "v5-tasks"}
    assert f["overlaps"] == [
        {"path": "frontend/src/App.tsx", "other_path": "frontend/src/App.tsx"}
    ]


def test_overlap_prefix_containment() -> None:
    # bookmarks claims a directory; tasks claims a file inside it
    feature_owners = [
        ("v5-bookmarks", ["frontend/src/components"]),
        ("v5-tasks", ["frontend/src/components/Card.tsx"]),
    ]
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners
    )
    assert len(findings) == 1
    assert findings[0]["kind"] == "feature_owned_paths_overlap"


def test_overlap_case_insensitive_collision() -> None:
    # macOS / case-insensitive filesystems treat these as the same file
    feature_owners = [
        ("v5-bookmarks", ["FRONTEND/src/App.tsx"]),
        ("v5-tasks", ["frontend/src/App.tsx"]),
    ]
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners
    )
    assert len(findings) == 1
    assert findings[0]["kind"] == "feature_owned_paths_overlap"


def test_overlap_n_equals_3_claimants() -> None:
    # All three features claim the same path: we get pairwise findings
    feature_owners = [
        ("v5-bookmarks", ["frontend/src/App.tsx"]),
        ("v5-tasks", ["frontend/src/App.tsx"]),
        ("v5-notes", ["frontend/src/App.tsx"]),
    ]
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners
    )
    # Pairs: (bookmarks, tasks), (bookmarks, notes), (tasks, notes)
    assert len(findings) == 3
    pair_keys = {
        (f["task_id"], f["other_task_id"]) for f in findings
    }
    assert pair_keys == {
        ("v5-bookmarks", "v5-tasks"),
        ("v5-bookmarks", "v5-notes"),
        ("v5-tasks", "v5-notes"),
    }


def test_overlap_same_feature_duplicate_paths_not_counted() -> None:
    # A feature listing the same path twice should not flag itself
    feature_owners = [
        ("v5-bookmarks", ["frontend/src/App.tsx", "frontend/src/App.tsx"]),
        ("v5-tasks", ["backend/app/routers/tasks.py"]),
    ]
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners
    )
    assert findings == []


def test_overlap_empty_owned_paths_no_findings() -> None:
    # Feature with no paths owns nothing, can't conflict
    feature_owners = [
        ("v5-bookmarks", []),
        ("v5-tasks", ["backend/app/routers/tasks.py"]),
    ]
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners
    )
    assert findings == []


def test_overlap_suppression_filters_listed_paths() -> None:
    # After graceful-degrade, the parent task has the overlapping path in
    # `decomposition_overlap_unresolved`. The check must not re-emit it.
    feature_owners = [
        ("v5-bookmarks", ["frontend/src/App.tsx"]),
        ("v5-tasks", ["frontend/src/App.tsx"]),
    ]
    # Path passed in suppressed_paths is case-folded normalized form
    from otto.v5_runner import _casefold_path

    suppressed = {_casefold_path("frontend/src/App.tsx")}
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners,
        suppressed_paths=suppressed,
    )
    assert findings == [], (
        "suppressed paths must not re-trigger findings after exhaustion"
    )


def test_overlap_partial_suppression_still_reports_other_overlap() -> None:
    # One path suppressed, another still hits — only the un-suppressed
    # one should appear.
    feature_owners = [
        ("v5-bookmarks", ["frontend/src/App.tsx", "frontend/src/theme.ts"]),
        ("v5-tasks", ["frontend/src/App.tsx", "frontend/src/theme.ts"]),
    ]
    from otto.v5_runner import _casefold_path

    suppressed = {_casefold_path("frontend/src/App.tsx")}
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners,
        suppressed_paths=suppressed,
    )
    assert len(findings) == 1
    overlap_paths = {o["path"] for o in findings[0]["overlaps"]}
    assert overlap_paths == {"frontend/src/theme.ts"}


def test_overlap_normalizes_trailing_slash() -> None:
    feature_owners = [
        ("v5-bookmarks", ["frontend/src/components/"]),
        ("v5-tasks", ["frontend/src/components"]),
    ]
    findings = _compute_sibling_owned_path_overlap_findings(
        feature_owners=feature_owners
    )
    assert len(findings) == 1
