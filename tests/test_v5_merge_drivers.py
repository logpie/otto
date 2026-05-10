"""Regression: structured merge drivers resolve common file conflicts.

Source bug: chat-platform decomp had 3 merge_blocked verdicts on trivially
unionable files (package.json deps + scripts, pytest.ini sections,
package-lock.json regen drift). The integration Lead then re-implemented
those features from scratch — ~50% of the run cost was rework.

These tests cover the structured drivers directly + the end-to-end path
through merge_branch_into.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from otto.v5_branching import child_branch_name, merge_branch_into
from otto.v5_merge_drivers import (
    find_driver,
    is_discard_signal,
    merge_gitignore,
    merge_package_json,
    merge_pytest_ini,
    merge_requirements_txt,
    merge_tsconfig_json,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    )


# ---------------------------------------------------------------------------
# Unit: each driver in isolation
# ---------------------------------------------------------------------------

def test_package_json_unions_deps() -> None:
    ours = json.dumps({"name": "x", "dependencies": {"react": "^18"}})
    theirs = json.dumps({"name": "x", "dependencies": {"zustand": "^5"}})
    merged_text = merge_package_json(ours, theirs, None)
    assert merged_text is not None
    merged = json.loads(merged_text)
    assert merged["dependencies"] == {"react": "^18", "zustand": "^5"}


def test_package_json_ours_wins_on_version_disagreement() -> None:
    ours = json.dumps({"dependencies": {"react": "^18"}})
    theirs = json.dumps({"dependencies": {"react": "^19"}})
    merged = json.loads(merge_package_json(ours, theirs, None))
    assert merged["dependencies"]["react"] == "^18"


def test_package_json_unions_scripts() -> None:
    ours = json.dumps({"scripts": {"test": "pytest"}})
    theirs = json.dumps({"scripts": {"build": "vite"}})
    merged = json.loads(merge_package_json(ours, theirs, None))
    assert merged["scripts"] == {"build": "vite", "test": "pytest"}


def test_requirements_txt_unions() -> None:
    ours = "fastapi==0.110\nuvicorn==0.27\n"
    theirs = "fastapi==0.111\nclick==8.1\n"
    merged = merge_requirements_txt(ours, theirs, None)
    assert "fastapi==0.110" in merged  # ours wins
    assert "uvicorn==0.27" in merged
    assert "click==8.1" in merged  # theirs added
    assert "fastapi==0.111" not in merged  # version conflict, ours wins


def test_gitignore_dedupe_union() -> None:
    ours = "__pycache__/\nnode_modules/\n"
    theirs = "node_modules/\nbuild/\n.vite/\n"
    merged = merge_gitignore(ours, theirs, None)
    lines = [ln for ln in merged.splitlines() if ln.strip()]
    assert "__pycache__/" in lines
    assert "node_modules/" in lines
    assert "build/" in lines
    assert ".vite/" in lines
    # node_modules appears once, not twice
    assert lines.count("node_modules/") == 1


def test_pytest_ini_unions_sections() -> None:
    ours = "[pytest]\naddopts = --browser=chromium\n"
    theirs = "[pytest]\npythonpath = .\ntestpaths = tests\n"
    merged = merge_pytest_ini(ours, theirs, None)
    assert "addopts" in merged
    assert "pythonpath" in merged
    assert "testpaths" in merged


def test_tsconfig_deep_merges() -> None:
    ours = json.dumps({"compilerOptions": {"strict": True}, "include": ["src"]})
    theirs = json.dumps({"compilerOptions": {"jsx": "react"}, "include": ["lib"]})
    merged = json.loads(merge_tsconfig_json(ours, theirs, None))
    assert merged["compilerOptions"]["strict"] is True
    assert merged["compilerOptions"]["jsx"] == "react"
    assert set(merged["include"]) == {"src", "lib"}


def test_find_driver_lookup() -> None:
    assert find_driver("package.json") is merge_package_json
    assert find_driver("web/package.json") is merge_package_json
    assert find_driver(".gitignore") is merge_gitignore
    assert find_driver("a/b/c/.gitignore") is merge_gitignore
    assert find_driver("src/App.tsx") is None


def test_lockfile_discard_signal() -> None:
    driver = find_driver("package-lock.json")
    assert driver is not None
    result = driver("anything", "anything", None)
    assert is_discard_signal(result)


# ---------------------------------------------------------------------------
# End-to-end: merge_branch_into resolves structured conflicts and lands work
# ---------------------------------------------------------------------------

def test_merge_resolves_package_json_conflict_end_to_end(tmp_path: Path) -> None:
    """Two siblings each add a dep — merge should land both without manual intervention."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / "package.json").write_text(json.dumps({"name": "app", "dependencies": {}}))
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "init")

    integration = "i2p/integ/parent"
    _git(repo, "branch", integration, "main")

    # Child A adds zustand on its build branch.
    child_a = "v5-feat-a"
    _git(repo, "checkout", "-q", "-b", child_branch_name(child_a), "main")
    (repo / "package.json").write_text(json.dumps({"name": "app", "dependencies": {"zustand": "^5"}}))
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "feat A: add zustand")

    # Integration branch (with a sibling already merged) has recharts.
    _git(repo, "checkout", "-q", integration)
    (repo / "package.json").write_text(json.dumps({"name": "app", "dependencies": {"recharts": "^2"}}))
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "sibling B: add recharts")

    ok, detail = merge_branch_into(
        project_dir=repo,
        source_branch=child_branch_name(child_a),
        target_branch=integration,
    )
    assert ok, f"merge should succeed via structured driver: {detail}"
    assert "structured" in detail

    # Both deps should be present.
    final = json.loads((repo / "package.json").read_text())
    assert "zustand" in final["dependencies"]
    assert "recharts" in final["dependencies"]


def test_merge_discards_lockfile_conflict(tmp_path: Path) -> None:
    """package-lock.json drift gets resolved by deleting it (regen-on-next-build)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / "package.json").write_text('{"name":"app"}')
    (repo / "package-lock.json").write_text('{"lockfileVersion":1,"name":"v1"}')
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "init")

    integration = "i2p/integ/parent"
    _git(repo, "branch", integration, "main")

    child = "v5-feat"
    _git(repo, "checkout", "-q", "-b", child_branch_name(child), "main")
    (repo / "package-lock.json").write_text('{"lockfileVersion":1,"name":"v2"}')
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "child lockfile drift")

    _git(repo, "checkout", "-q", integration)
    (repo / "package-lock.json").write_text('{"lockfileVersion":1,"name":"v3"}')
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "integration lockfile drift")

    ok, detail = merge_branch_into(
        project_dir=repo,
        source_branch=child_branch_name(child),
        target_branch=integration,
    )
    assert ok, detail
    # Lockfile should be gone (regen on next install).
    assert not (repo / "package-lock.json").exists()


def test_real_source_conflict_still_blocks(tmp_path: Path) -> None:
    """Layer 3 doesn't blanket auto-resolve everything — real source still blocks."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / "src.py").write_text("a = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "init")

    integration = "i2p/integ/parent"
    _git(repo, "branch", integration, "main")

    child = "v5-feat"
    _git(repo, "checkout", "-q", "-b", child_branch_name(child), "main")
    (repo / "src.py").write_text("a = 2\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "child")

    _git(repo, "checkout", "-q", integration)
    (repo / "src.py").write_text("a = 3\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "integ")

    ok, detail = merge_branch_into(
        project_dir=repo,
        source_branch=child_branch_name(child),
        target_branch=integration,
    )
    assert not ok
    assert "src.py" in detail
