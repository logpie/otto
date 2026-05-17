"""A6.7 integration test — full brownfield-compile Python plumbing.

Builds a realistic small CLI project in tmp_path and runs
`compile_spec(brownfield=True)` end-to-end. The agent is stubbed (no
LLM cost) but every other piece is real:

  build_project_preamble  →  render_prompt('compile-spec-brownfield.md')
                          →  agent stub captures prompt
                          →  spec_from_dict + validate_spec
                          →  _reconcile_brownfield (additive case only)
                          →  persist_spec (writes spec.json + spec.md)

Coverage:
    1. Empty-base case: no base_spec → fresh spec persisted.
    2. Additive case: base_spec with prior audit verdicts → reconciled
       spec preserves them.
    3. Prompt content: README, manifest, source filenames, brownfield
       markers, scope-hint guidance all present.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from otto.spec_compile import (
    Feature,
    Group,
    Spec,
    compile_spec,
    spec_to_dict,
)


# ---------------------------------------------------------------------------
# Fixture: realistic small CLI project
# ---------------------------------------------------------------------------


def _build_fixture_project(root: Path) -> None:
    """Compose a deterministic small CLI project layout under `root`.

    Mirrors what `sample-cli` (a stand-in for any small Python CLI)
    would look like in the real world: README, pyproject, src layout,
    multiple subcommand modules, and a tests/ folder.
    """
    (root / "README.md").write_text(
        "# Sample CLI\n"
        "\n"
        "A small command-line linter and formatter.\n"
        "\n"
        "## Usage\n"
        "\n"
        "    sample-cli lint path/to/file.py\n"
        "    sample-cli format path/to/file.py\n"
        "    sample-cli version\n"
    )
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "sample-cli"\n'
        'version = "0.1.0"\n'
        "\n"
        "[project.scripts]\n"
        'sample-cli = "sample_cli.__main__:main"\n'
    )
    src = root / "src" / "sample_cli"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "__main__.py").write_text(
        "import sys\n"
        "from sample_cli import lint, fmt, version\n"
        "\n"
        "def main():\n"
        "    cmd = sys.argv[1] if len(sys.argv) > 1 else 'version'\n"
        "    if cmd == 'lint':\n"
        "        return lint.run(sys.argv[2:])\n"
        "    if cmd == 'format':\n"
        "        return fmt.run(sys.argv[2:])\n"
        "    return version.run()\n"
    )
    (src / "lint.py").write_text("def run(args): return 0\n")
    (src / "fmt.py").write_text("def run(args): return 0\n")
    (src / "version.py").write_text("def run(): print('0.1.0'); return 0\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_lint.py").write_text("def test_smoke(): pass\n")
    (tests / "test_fmt.py").write_text("def test_smoke(): pass\n")
    # Pre-init git so the preamble exercises the git-tracked path
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "f@otto"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "F"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "commit.gpgsign", "false"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)


def _agent_emits_three_features() -> dict[str, object]:
    """Spec the stub agent returns — one Feature per CLI subcommand."""
    spec = Spec(
        intent="document this CLI tool",
        project_kind="cli",
        groups=[
            Group(
                id="cli-commands",
                name="CLI commands",
                owned_paths=["src/sample_cli/**"],
            ),
        ],
        features=[
            Feature(
                id="lint-cmd",
                name="Lint subcommand",
                description="Lints a Python file.",
                evidence_kinds=["CLIProbe", "RepoTestCheck"],
                group_id="cli-commands",
            ),
            Feature(
                id="format-cmd",
                name="Format subcommand",
                description="Formats a Python file.",
                evidence_kinds=["CLIProbe", "RepoTestCheck"],
                group_id="cli-commands",
            ),
            Feature(
                id="version-cmd",
                name="Version subcommand",
                description="Prints the package version.",
                evidence_kinds=["CLIProbe"],
                group_id="cli-commands",
            ),
        ],
    )
    return spec_to_dict(spec)


def _stub_compile_internals(monkeypatch, captured: dict[str, object],
                              spec_dict: dict[str, object]) -> None:
    body = json.dumps(spec_dict)
    text = f"<spec_json>{body}</spec_json>\nSPEC_PATH: anything\n"

    async def _stub(prompt, *_args: object, **_kwargs: object):
        captured["prompt"] = prompt
        return text, 0.0, "stub-session", {}

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", _stub)
    monkeypatch.setattr(
        "otto.agent.make_agent_options",
        lambda *_a, **_kw: object(),
    )
    monkeypatch.setattr("otto.config.get_spec_timeout", lambda _c: 30)
    monkeypatch.setattr(
        "otto.observability.save_rendered_prompt",
        lambda *_a, **_kw: {"sha256": "x", "path": "x"},
    )
    monkeypatch.setattr(
        "otto.observability.update_input_provenance",
        lambda *_a, **_kw: None,
    )


# ---------------------------------------------------------------------------
# Empty-base case
# ---------------------------------------------------------------------------


def test_brownfield_compile_against_real_fixture_no_base(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _build_fixture_project(project)
    run_dir = tmp_path / "session" / "spec"

    captured: dict[str, object] = {}
    _stub_compile_internals(monkeypatch, captured, _agent_emits_three_features())

    asyncio.run(
        compile_spec(
            "document this CLI tool",
            project,
            run_dir,
            {},
            project_kind="cli",
            brownfield=True,
        )
    )

    # ---- Rendered prompt content ----
    rendered = captured["prompt"]
    assert isinstance(rendered, str)
    # Brownfield-mode signals
    assert "brownfield mode" in rendered
    assert "scope hint" in rendered.lower()
    # README content interpolated via project_preamble
    assert "Sample CLI" in rendered
    assert "sample-cli lint" in rendered
    # Manifest content interpolated
    assert 'name = "sample-cli"' in rendered
    # Source filenames from the file tree (verifies preamble enumerated them)
    assert "src/sample_cli/__main__.py" in rendered
    assert "src/sample_cli/lint.py" in rendered
    assert "tests/test_lint.py" in rendered

    # ---- Persisted spec.json ----
    spec_path = run_dir / "spec.json"
    assert spec_path.exists()
    persisted = json.loads(spec_path.read_text())
    assert persisted["project_kind"] == "cli"
    feature_ids = sorted(f["id"] for f in persisted["features"])
    assert feature_ids == ["format-cmd", "lint-cmd", "version-cmd"]
    # Group preserved
    assert persisted["groups"][0]["id"] == "cli-commands"
    assert persisted["groups"][0]["owned_paths"] == ["src/sample_cli/**"]


# ---------------------------------------------------------------------------
# Additive case: base_spec with prior audit verdicts
# ---------------------------------------------------------------------------


def test_brownfield_compile_additive_preserves_verdicts(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _build_fixture_project(project)
    run_dir = tmp_path / "session" / "spec"

    base = Spec(
        intent="document this CLI tool",
        project_kind="cli",
        groups=[
            Group(
                id="cli-commands",
                name="CLI commands",
                owned_paths=["src/sample_cli/**"],
            ),
        ],
        features=[
            Feature(
                id="lint-cmd",
                name="Lint (old)",
                description="Old description.",
                evidence_kinds=["CLIProbe"],
                group_id="cli-commands",
                verdict="passed",
                evidence_completeness="full",
                coverage_confidence="high",
            ),
        ],
    )
    base.intent_hash = "deadbeef"

    captured: dict[str, object] = {}
    _stub_compile_internals(monkeypatch, captured, _agent_emits_three_features())

    spec = asyncio.run(
        compile_spec(
            "document this CLI tool",
            project,
            run_dir,
            {},
            project_kind="cli",
            brownfield=True,
            base_spec=base,
        )
    )

    feat_by_id = {f.id: f for f in spec.features}
    # New ids appended
    assert set(feat_by_id) == {"lint-cmd", "format-cmd", "version-cmd"}
    # Audit state on the existing id (lint-cmd) preserved from base
    assert feat_by_id["lint-cmd"].verdict == "passed"
    assert feat_by_id["lint-cmd"].evidence_completeness == "full"
    # Author fields on lint-cmd come from agent (renamed)
    assert feat_by_id["lint-cmd"].name == "Lint subcommand"
    # New features have no verdict yet (audit hasn't run)
    assert feat_by_id["format-cmd"].verdict is None
    # intent + intent_hash from base (additive does NOT change intent)
    assert spec.intent == "document this CLI tool"
    assert spec.intent_hash == "deadbeef"
