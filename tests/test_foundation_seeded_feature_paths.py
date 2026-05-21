"""Regression: foundation must not commit files into a feature's owned region.

The structural backstop (audit-prompts.md task #72 / F-3) checks at
foundation_gate, BEFORE features dispatch, whether the architect's branch
contains any paths declared as feature-owned in CHARTER. Linkboard
2026-05-21 session 140216-db3185: the architect committed BookmarksPage.tsx
and TagsPage.tsx as "pragmatic stubs" — both feature-owned — and the
union-guard caught it ~25 min later during integration. This check catches
it before features start.
"""

from __future__ import annotations

from otto.v5_runner import _compute_foundation_seeded_findings


def test_no_findings_when_committed_paths_empty() -> None:
    findings = _compute_foundation_seeded_findings(
        committed_paths=[],
        feature_owners=[("v5-feature-a", ["frontend/src/pages/BookmarksPage.tsx"])],
        architect_task_id="v5-arch",
    )
    assert findings == []


def test_no_findings_when_no_feature_owners() -> None:
    findings = _compute_foundation_seeded_findings(
        committed_paths=["backend/app/main.py"],
        feature_owners=[],
        architect_task_id="v5-arch",
    )
    assert findings == []


def test_no_findings_when_committed_paths_are_foundation_owned() -> None:
    """Architect committing foundation aggregator/loader is fine."""
    findings = _compute_foundation_seeded_findings(
        committed_paths=[
            "backend/app/routers/__init__.py",
            "frontend/src/store/index.ts",
            "start.sh",
        ],
        feature_owners=[
            ("v5-feature-a", ["frontend/src/pages/BookmarksPage.tsx"]),
            ("v5-feature-b", ["frontend/src/pages/TagsPage.tsx"]),
        ],
        architect_task_id="v5-arch",
    )
    assert findings == []


def test_exact_path_match_flags_seeded_feature_file() -> None:
    """Linkboard reproduction: architect committed feature-owned stubs."""
    findings = _compute_foundation_seeded_findings(
        committed_paths=[
            "frontend/src/pages/BookmarksPage.tsx",  # feature-owned!
            "frontend/src/pages/TagsPage.tsx",  # feature-owned!
            "backend/app/main.py",  # foundation, fine
        ],
        feature_owners=[
            ("v5-feature-a", ["frontend/src/pages/BookmarksPage.tsx"]),
            ("v5-feature-b", ["frontend/src/pages/TagsPage.tsx"]),
        ],
        architect_task_id="v5-arch",
    )
    assert len(findings) == 2
    by_feature = {f["feature_task_id"]: f for f in findings}
    assert by_feature["v5-feature-a"]["seeded_paths"] == [
        "frontend/src/pages/BookmarksPage.tsx"
    ]
    assert by_feature["v5-feature-b"]["seeded_paths"] == [
        "frontend/src/pages/TagsPage.tsx"
    ]
    for f in findings:
        assert f["kind"] == "foundation_seeded_feature_path"
        assert f["architect_task_id"] == "v5-arch"
        assert "aggregator" in f["guidance"]


def test_directory_glob_overlap_flags_seeded_feature_file() -> None:
    """Feature owns a directory; architect commits a file inside it."""
    findings = _compute_foundation_seeded_findings(
        committed_paths=["backend/routers/tags/router.py"],
        feature_owners=[
            ("v5-feature-tags", ["backend/routers/tags/"]),
        ],
        architect_task_id="v5-arch",
    )
    assert len(findings) == 1
    assert findings[0]["seeded_paths"] == ["backend/routers/tags/router.py"]


def test_unrelated_feature_owned_paths_do_not_flag() -> None:
    """Architect commits A, feature B's owned paths are elsewhere — no flag."""
    findings = _compute_foundation_seeded_findings(
        committed_paths=["frontend/src/pages/HomePage.tsx"],
        feature_owners=[
            ("v5-feature-tags", ["frontend/src/pages/TagsPage.tsx"]),
        ],
        architect_task_id="v5-arch",
    )
    assert findings == []
