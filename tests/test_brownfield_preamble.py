"""Tests for `otto.spec_compile.build_project_preamble` (A6.1).

Covers the brownfield-compile project preamble generator: file tree,
README excerpt, and manifest snippet — all capped per
`otto.defaults.BROWNFIELD_PREAMBLE_MAX_*` constants.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from otto.defaults import (
    BROWNFIELD_PREAMBLE_MAX_FILES,
    BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE,
)
from otto.spec_compile import build_project_preamble


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@otto.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Otto Tester"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"],
        check=True,
    )


def _git_add_commit(path: Path, message: str = "snapshot") -> None:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", message], check=True)


# ---------------------------------------------------------------------------
# Empty / minimal projects
# ---------------------------------------------------------------------------


def test_empty_project_emits_honest_message(tmp_path: Path) -> None:
    """An empty git repo (or any project with no tracked files) produces
    a preamble that says so honestly, never crashes."""
    _git_init(tmp_path)
    out = build_project_preamble(tmp_path)
    assert "## File tree" in out
    assert "empty project" in out


def test_non_git_project_uses_glob_fallback(tmp_path: Path) -> None:
    """When the directory is not a git repo, glob enumeration is used."""
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "helper.py").write_text("def f(): pass\n")
    out = build_project_preamble(tmp_path)
    assert "main.py" in out
    assert "lib/helper.py" in out


# ---------------------------------------------------------------------------
# Python project: README + pyproject.toml
# ---------------------------------------------------------------------------


def test_python_project_includes_readme_and_pyproject(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "README.md").write_text(
        "# A Python tool\n\nDoes one thing well.\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "tool"\nversion = "0.1.0"\n'
    )
    (tmp_path / "tool.py").write_text("def main(): pass\n")
    _git_add_commit(tmp_path)

    out = build_project_preamble(tmp_path)
    assert "## File tree" in out
    assert "tool.py" in out
    assert "## README (README.md)" in out
    assert "A Python tool" in out
    assert "Does one thing well" in out
    assert "## Manifest (pyproject.toml)" in out
    assert 'name = "tool"' in out


# ---------------------------------------------------------------------------
# JS project: package.json wins over pyproject.toml only if pyproject absent
# ---------------------------------------------------------------------------


def test_js_project_surfaces_package_json(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "README.md").write_text("# JS app\n")
    (tmp_path / "package.json").write_text(
        '{"name": "app", "version": "0.1.0"}\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("// entry\n")
    _git_add_commit(tmp_path)

    out = build_project_preamble(tmp_path)
    assert "## Manifest (package.json)" in out
    assert '"name": "app"' in out


def test_pyproject_takes_priority_when_both_present(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "py"\n')
    (tmp_path / "package.json").write_text('{"name": "js"}\n')
    _git_add_commit(tmp_path)

    out = build_project_preamble(tmp_path)
    # First in _BROWNFIELD_PREAMBLE_MANIFESTS wins
    assert "## Manifest (pyproject.toml)" in out
    assert "## Manifest (package.json)" not in out


# ---------------------------------------------------------------------------
# Truncation: file count and line count caps
# ---------------------------------------------------------------------------


def test_file_count_truncates_at_cap(tmp_path: Path) -> None:
    _git_init(tmp_path)
    n = BROWNFIELD_PREAMBLE_MAX_FILES + 50
    for i in range(n):
        (tmp_path / f"f{i:04d}.txt").write_text("x\n")
    _git_add_commit(tmp_path)

    out = build_project_preamble(tmp_path)
    # First file shown
    assert "f0000.txt" in out
    # Tail not shown
    assert f"f{n - 1:04d}.txt" not in out
    # Honest truncation marker
    assert "more files" in out
    assert "50 more files" in out


def test_readme_lines_truncate_at_cap(tmp_path: Path) -> None:
    _git_init(tmp_path)
    n = BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE + 20
    body = "\n".join(f"line {i}" for i in range(n))
    (tmp_path / "README.md").write_text(body + "\n")
    _git_add_commit(tmp_path)

    out = build_project_preamble(tmp_path)
    assert "line 0" in out
    assert f"line {n - 1}" not in out
    assert "20 more lines" in out


# ---------------------------------------------------------------------------
# Ignored paths
# ---------------------------------------------------------------------------


def test_ignored_directories_not_in_file_tree(tmp_path: Path) -> None:
    """Common ignore patterns (otto_logs, __pycache__, node_modules, .venv)
    must NOT appear in the file tree section."""
    _git_init(tmp_path)
    # Tracked file we DO want
    (tmp_path / "main.py").write_text("pass\n")
    # NOTE: We add these to the working tree (not git-tracked) so glob mode
    # would otherwise pick them up. They must still be filtered.
    (tmp_path / "otto_logs").mkdir()
    (tmp_path / "otto_logs" / "session-1").mkdir()
    (tmp_path / "otto_logs" / "session-1" / "trace.jsonl").write_text("{}\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "bytecode.pyc").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lodash").mkdir()
    (tmp_path / "node_modules" / "lodash" / "index.js").write_text("//\n")
    _git_add_commit(tmp_path)

    out = build_project_preamble(tmp_path)
    assert "main.py" in out
    assert "otto_logs" not in out
    assert "__pycache__" not in out
    assert "node_modules" not in out


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_preamble_is_deterministic(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "b.py").write_text("b\n")
    (tmp_path / "c.py").write_text("c\n")
    (tmp_path / "README.md").write_text("# proj\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
    _git_add_commit(tmp_path)

    p1 = build_project_preamble(tmp_path)
    p2 = build_project_preamble(tmp_path)
    p3 = build_project_preamble(tmp_path)
    assert p1 == p2 == p3


# ---------------------------------------------------------------------------
# README variants
# ---------------------------------------------------------------------------


def test_readme_rst_variant_recognized(tmp_path: Path) -> None:
    """Project with README.rst (no .md) still surfaces the README section."""
    _git_init(tmp_path)
    (tmp_path / "README.rst").write_text("Project\n=======\n\nA thing.\n")
    _git_add_commit(tmp_path)

    out = build_project_preamble(tmp_path)
    assert "## README (README.rst)" in out
    assert "A thing" in out


def test_no_readme_no_readme_section(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "main.py").write_text("pass\n")
    _git_add_commit(tmp_path)

    out = build_project_preamble(tmp_path)
    assert "## README" not in out
    assert "## File tree" in out


# ---------------------------------------------------------------------------
# A6.2 — Brownfield prompt template renders cleanly
# ---------------------------------------------------------------------------


def test_brownfield_prompt_renders_with_preamble(tmp_path: Path) -> None:
    """`render_prompt('compile-spec-brownfield.md', ...)` substitutes
    {intent}, {project_context}, {project_preamble}, {spec_path}."""
    from otto.prompts import render_prompt

    _git_init(tmp_path)
    (tmp_path / "README.md").write_text("# Sample\n\nA test project.\n")
    (tmp_path / "main.py").write_text("def main(): pass\n")
    _git_add_commit(tmp_path)

    preamble = build_project_preamble(tmp_path)
    rendered = render_prompt(
        "compile-spec-brownfield.md",
        intent="audit the auth flow",
        project_context="project_kind=webapp",
        project_preamble=preamble,
        spec_path="/tmp/spec.json",
    )

    # Brownfield-mode signal in the prompt body
    assert "brownfield mode" in rendered
    # All four placeholders interpolated
    assert "audit the auth flow" in rendered
    assert "project_kind=webapp" in rendered
    assert "main.py" in rendered  # from preamble
    assert "/tmp/spec.json" in rendered
    # Anti-derivation guidance present
    assert "scope hint" in rendered
    assert "do not invent" in rendered.lower() or "never invent" in rendered.lower()


def test_brownfield_prompt_handles_empty_project_preamble() -> None:
    """When the preamble says (empty project), the agent gets the
    bootstrap-case branch in the prompt body."""
    from otto.prompts import render_prompt

    rendered = render_prompt(
        "compile-spec-brownfield.md",
        intent="",
        project_context="project_kind=cli",
        project_preamble="(empty project — no tracked files found)\n",
        spec_path="/tmp/spec.json",
    )
    # Empty-project guidance section present
    assert "Empty-project case" in rendered or "empty-project" in rendered.lower()
    assert "(empty project" in rendered
