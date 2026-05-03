"""Tests for `otto run` — Step 9 of the intent-to-product plan.

These tests stub the LLM compile call. They verify:
  * `otto run --help` exposes the new subcommand
  * Argument parsing routes to `compile_spec` with the right project_kind
  * The session dir is allocated under `otto_logs/sessions/<id>/spec/`
  * The exit-message hint about deferred build/audit/render is printed
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from otto.cli import main
from otto.spec_compile import (
    BrowserJourney,
    Slice,
    Spec,
    StructureDecisions,
)


def _init_project(path: Path) -> None:
    """Initialise a minimal otto-shaped project tree."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@otto.local"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Otto Tester"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / "README.md").write_text("test project\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _fixture_spec(intent: str = "a tiny webapp") -> Spec:
    return Spec(
        intent=intent,
        project_kind="webapp",
        structure=StructureDecisions(payload={
            "routes": [{"path": "/", "component": "Home", "key_text": "Hello"}],
            "components": [{"name": "Home", "key_text": "Hello"}],
        }),
        slices=[
            Slice(
                id="shell",
                title="App shell",
                tasks=["scaffold the SPA"],
                deps=[],
                owned_paths=["src/**"],
                checks=[
                    BrowserJourney(
                        command=("pytest", "tests/browser/test_shell.py"),
                        evidence_globs=("evidence/*.png",),
                    ),
                ],
            ),
        ],
    )


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    runner = CliRunner()
    saved_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(main, args, catch_exceptions=False, env=env or {})
    finally:
        os.chdir(saved_cwd)
    return result.exit_code, result.output


def test_run_subcommand_appears_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "  run " in result.output or "\n  run\n" in result.output


def test_run_with_intent_dispatches_to_compile_spec(tmp_path: Path, monkeypatch) -> None:
    _init_project(tmp_path)

    captured: dict[str, object] = {}
    fake_spec = _fixture_spec("hello fixture")

    async def fake_compile_spec(intent, *, project_dir, run_dir, config, project_kind, **kwargs):
        from otto.spec_compile import persist_spec

        captured["intent"] = intent
        captured["project_dir"] = project_dir
        captured["run_dir"] = run_dir
        captured["project_kind"] = project_kind
        # Persist the canonical spec.json the way the real `compile_spec`
        # does, so the CLI can report a real path.
        run_dir.mkdir(parents=True, exist_ok=True)
        persist_spec(fake_spec, run_dir / "spec.json", allow_initial=True)
        return fake_spec

    monkeypatch.setattr("otto.cli_run.compile_spec", fake_compile_spec)

    env = {"OTTO_RUN_ID": "2026-05-03-120000-abc123"}
    code, out = _run(
        ["run", "build me a tiny webapp"],
        cwd=tmp_path,
        env=env,
    )

    assert code == 0, out
    assert captured["intent"] == "build me a tiny webapp"
    assert captured["project_kind"] == "webapp"
    expected_run_dir = tmp_path / "otto_logs" / "sessions" / env["OTTO_RUN_ID"] / "spec"
    assert Path(captured["run_dir"]).resolve() == expected_run_dir.resolve()

    spec_path = expected_run_dir / "spec.json"
    assert spec_path.exists()
    persisted = json.loads(spec_path.read_text(encoding="utf-8"))
    assert persisted["intent"] == "hello fixture"
    assert persisted["project_kind"] == "webapp"

    # The Phase-A stub message is printed
    assert "Build, audit, and render are not yet implemented" in out
    # Rich may line-wrap long paths in the console output; check the
    # filename is mentioned and that the run-id segment is present so we
    # know the message points at the right session.
    assert "spec.json" in out
    assert env["OTTO_RUN_ID"] in out


def test_run_respects_project_kind_flag(tmp_path: Path, monkeypatch) -> None:
    _init_project(tmp_path)

    captured: dict[str, object] = {}
    fake_spec = _fixture_spec()

    async def fake_compile_spec(intent, *, project_dir, run_dir, config, project_kind, **kwargs):
        from otto.spec_compile import persist_spec

        captured["project_kind"] = project_kind
        run_dir.mkdir(parents=True, exist_ok=True)
        persist_spec(fake_spec, run_dir / "spec.json", allow_initial=True)
        return fake_spec

    monkeypatch.setattr("otto.cli_run.compile_spec", fake_compile_spec)

    env = {"OTTO_RUN_ID": "2026-05-03-120100-def456"}
    code, _out = _run(
        ["run", "--project-kind", "cli", "make a small linter"],
        cwd=tmp_path,
        env=env,
    )

    assert code == 0
    assert captured["project_kind"] == "cli"


def test_run_rejects_unknown_project_kind(tmp_path: Path) -> None:
    _init_project(tmp_path)
    code, out = _run(["run", "--project-kind", "alien", "x"], cwd=tmp_path)
    assert code != 0
    assert "alien" in out


def test_run_falls_back_to_intent_md(tmp_path: Path, monkeypatch) -> None:
    _init_project(tmp_path)
    (tmp_path / "intent.md").write_text("intent loaded from file\n", encoding="utf-8")

    fake_spec = _fixture_spec("intent loaded from file")
    captured: dict[str, object] = {}

    async def fake_compile_spec(intent, *, project_dir, run_dir, config, project_kind, **kwargs):
        from otto.spec_compile import persist_spec

        captured["intent"] = intent
        run_dir.mkdir(parents=True, exist_ok=True)
        persist_spec(fake_spec, run_dir / "spec.json", allow_initial=True)
        return fake_spec

    monkeypatch.setattr("otto.cli_run.compile_spec", fake_compile_spec)

    env = {"OTTO_RUN_ID": "2026-05-03-120200-aaa111"}
    code, out = _run(["run"], cwd=tmp_path, env=env)
    assert code == 0, out
    assert captured["intent"] == "intent loaded from file"
