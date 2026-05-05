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
    Group,
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
        groups=[
            Group(
                id="shell",
                name="App shell",
                feature_ids=["scaffold the SPA"],
                dependencies=[],
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


# ---------------------------------------------------------------------------
# Phase B.0 — `otto build --i2p` opt-in routing through the new stack
# ---------------------------------------------------------------------------


def test_build_i2p_flag_appears_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["build", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "--i2p" in result.output
    # Click line-wraps long help text, so check for an unbroken token.
    assert "intent-to-product" in result.output


def test_build_i2p_dispatches_to_orchestrate_run(tmp_path: Path, monkeypatch) -> None:
    """`otto build --i2p` calls cli_run.orchestrate_run with the build's
    intent argument and project_kind=webapp default."""
    _init_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_orchestrate_run(*, intent, project_kind, break_lock, no_build,
                              base_url, from_spec, project_dir, **_extra):
        captured["intent"] = intent
        captured["project_kind"] = project_kind
        captured["break_lock"] = break_lock
        captured["no_build"] = no_build
        captured["base_url"] = base_url
        captured["from_spec"] = from_spec
        captured["project_dir"] = project_dir
        captured.update(_extra)
        # Mimic the real orchestrate_run's sys.exit(0) on success.
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)

    code, out = _run(
        ["build", "--i2p", "build me a tiny webapp"],
        cwd=tmp_path,
    )
    assert code == 0, out
    assert captured["intent"] == "build me a tiny webapp"
    assert captured["project_kind"] == "webapp"
    assert captured["no_build"] is False
    assert captured["from_spec"] is None
    assert Path(captured["project_dir"]).resolve() == tmp_path.resolve()


def test_build_i2p_warns_about_ignored_legacy_flags(
    tmp_path: Path, monkeypatch
) -> None:
    """When legacy build flags are passed alongside --i2p, the user gets
    an explicit warning listing the ignored flags."""
    _init_project(tmp_path)

    def fake_orchestrate_run(**_kwargs):
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)

    code, out = _run(
        ["build", "--i2p", "--thorough", "--strict", "intent text"],
        cwd=tmp_path,
    )
    assert code == 0
    # Both flags surfaced as ignored
    assert "i2p mode" in out and "ignored" in out
    assert "--thorough" in out
    assert "--strict" in out


def test_build_without_i2p_hard_errors_after_phase_c3(tmp_path: Path, monkeypatch) -> None:
    """Phase C.3: `otto build` without --i2p (or with `default_pipeline: legacy`
    in otto.yaml) must hard-error — the v3 pipeline was deleted. Users see a
    migration message pointing at the new --i2p default.
    """
    _init_project(tmp_path)
    (tmp_path / "otto.yaml").write_text("default_pipeline: legacy\n")

    sentinel: dict[str, bool] = {"new_called": False}

    def fake_orchestrate_run(**_kwargs):
        sentinel["new_called"] = True
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)

    code, out = _run(
        ["build", "--no-qa", "intent"],
        cwd=tmp_path,
    )
    # The new path is NOT hit (legacy was selected) and the legacy
    # path now exits with status 1 + a migration message.
    assert sentinel["new_called"] is False
    assert code == 1
    assert "Phase C" in out or "removed" in out.lower()


# ---------------------------------------------------------------------------
# Phase B.1 — `otto certify --i2p`
# ---------------------------------------------------------------------------


def test_certify_i2p_flag_appears_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["certify", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "--i2p" in result.output
    # Click line-wraps long help text, so check for an unbroken token.
    assert "intent-to-product" in result.output


def test_certify_i2p_dispatches_to_orchestrate_certify(
    tmp_path: Path, monkeypatch
) -> None:
    """`otto certify --i2p "intent"` calls cli_run.orchestrate_certify."""
    _init_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_orchestrate_certify(*, intent, project_kind, break_lock,
                                  project_dir, **_extra):
        captured["intent"] = intent
        captured["project_kind"] = project_kind
        captured["break_lock"] = break_lock
        captured["project_dir"] = project_dir
        captured.update(_extra)
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_certify", fake_orchestrate_certify)

    code, out = _run(
        ["certify", "--i2p", "audit this CLI"],
        cwd=tmp_path,
    )
    assert code == 0, out
    assert captured["intent"] == "audit this CLI"
    assert captured["project_kind"] == "webapp"
    assert Path(captured["project_dir"]).resolve() == tmp_path.resolve()


def test_certify_i2p_warns_about_ignored_legacy_flags(
    tmp_path: Path, monkeypatch
) -> None:
    _init_project(tmp_path)

    def fake_orchestrate_certify(**_kwargs):
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_certify", fake_orchestrate_certify)

    code, out = _run(
        ["certify", "--i2p", "--thorough", "--strict", "intent text"],
        cwd=tmp_path,
    )
    assert code == 0
    assert "i2p mode" in out and "ignored" in out
    assert "--thorough" in out
    assert "--strict" in out


def test_certify_without_i2p_hard_errors_after_phase_c2(
    tmp_path: Path, monkeypatch
) -> None:
    """Phase C.2 (tick 64): `otto certify` without --i2p (or with
    `default_pipeline: legacy` in otto.yaml) must hard-error — the
    legacy `run_agentic_certifier` dispatcher was deleted. Mirrors
    `test_build_without_i2p_hard_errors_after_phase_c3` for the certify
    subcommand.
    """
    _init_project(tmp_path)
    (tmp_path / "otto.yaml").write_text("default_pipeline: legacy\n")
    (tmp_path / "intent.md").write_text("test intent\n")

    sentinel: dict[str, bool] = {"new_called": False}

    def fake_orchestrate_certify(**_kwargs):
        sentinel["new_called"] = True
        import sys
        sys.exit(0)

    monkeypatch.setattr(
        "otto.cli_run.orchestrate_certify", fake_orchestrate_certify
    )

    code, out = _run(["certify", "explicit intent"], cwd=tmp_path)
    # The new path is NOT hit (legacy was selected) and the legacy
    # path now exits with status 1 + a migration message.
    assert sentinel["new_called"] is False
    assert code == 1
    assert "Phase C" in out or "removed" in out.lower()


# ---------------------------------------------------------------------------
# Phase B.2 — `otto improve bugs --i2p`
# ---------------------------------------------------------------------------


def test_improve_bugs_i2p_flag_appears_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["improve", "bugs", "--help"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "--i2p" in result.output
    # Click line-wraps long help text, so check for an unbroken token.
    assert "intent-to-product" in result.output


def test_improve_bugs_i2p_dispatches_to_orchestrate_improve(
    tmp_path: Path, monkeypatch
) -> None:
    """`otto improve bugs --i2p "focus"` calls cli_run.orchestrate_improve."""
    _init_project(tmp_path)
    (tmp_path / "intent.md").write_text("test intent\n")
    captured: dict[str, object] = {}

    def fake_orchestrate_improve(*, intent, project_kind, break_lock,
                                  project_dir, rounds, focus):
        captured["intent"] = intent
        captured["project_kind"] = project_kind
        captured["break_lock"] = break_lock
        captured["project_dir"] = project_dir
        captured["rounds"] = rounds
        captured["focus"] = focus
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_improve", fake_orchestrate_improve)

    code, out = _run(
        ["improve", "bugs", "--i2p", "--rounds", "3", "error handling"],
        cwd=tmp_path,
    )
    assert code == 0, out
    assert captured["project_kind"] == "webapp"
    assert captured["rounds"] == 3
    assert captured["focus"] == "error handling"
    assert Path(captured["project_dir"]).resolve() == tmp_path.resolve()


def test_improve_bugs_i2p_warns_about_ignored_legacy_flags(
    tmp_path: Path, monkeypatch
) -> None:
    _init_project(tmp_path)
    (tmp_path / "intent.md").write_text("test intent\n")

    def fake_orchestrate_improve(**_kwargs):
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_improve", fake_orchestrate_improve)

    code, out = _run(
        ["improve", "bugs", "--i2p", "--strict", "--thorough", "focus"],
        cwd=tmp_path,
    )
    assert code == 0
    assert "i2p mode" in out and "ignored" in out
    assert "--strict" in out
    assert "--thorough" in out


def test_improve_bugs_legacy_pipeline_now_hard_errors(
    tmp_path: Path, monkeypatch
) -> None:
    """Phase C: pinning ``default_pipeline: legacy`` (or passing
    ``--legacy``) routes ``otto improve bugs`` into the deletion error
    path — no orchestrate_improve call, exit 1.
    """
    _init_project(tmp_path)
    (tmp_path / "intent.md").write_text("test intent\n")
    (tmp_path / "otto.yaml").write_text("default_pipeline: legacy\n")

    sentinel: dict[str, bool] = {"orchestrated": False}

    def fake_orchestrate(**_kwargs):
        sentinel["orchestrated"] = True
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_improve", fake_orchestrate)

    code, _out = _run(
        ["improve", "bugs", "focus"],
        cwd=tmp_path,
    )
    assert code == 1
    assert sentinel["orchestrated"] is False


# ---------------------------------------------------------------------------
# Phase B.2 — `otto improve feature/target --i2p` (tick 51)
# ---------------------------------------------------------------------------


def test_improve_feature_i2p_flag_appears_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["improve", "feature", "--help"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "--i2p" in result.output
    assert "--legacy" in result.output


def test_improve_target_i2p_flag_appears_in_help() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["improve", "target", "--help"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "--i2p" in result.output
    assert "--legacy" in result.output


def test_improve_feature_i2p_dispatches_to_orchestrate_improve(
    tmp_path: Path, monkeypatch
) -> None:
    _init_project(tmp_path)
    (tmp_path / "intent.md").write_text("test intent\n")
    captured: dict[str, object] = {}

    def fake_orchestrate_improve(*, intent, project_kind, break_lock,
                                  project_dir, rounds, focus):
        captured["focus"] = focus
        captured["rounds"] = rounds
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_improve", fake_orchestrate_improve)

    code, _out = _run(
        ["improve", "feature", "--i2p", "search UX"],
        cwd=tmp_path,
    )
    assert code == 0
    assert captured["focus"] == "search UX"


def test_improve_target_i2p_dispatches_to_orchestrate_improve(
    tmp_path: Path, monkeypatch
) -> None:
    _init_project(tmp_path)
    (tmp_path / "intent.md").write_text("test intent\n")
    captured: dict[str, object] = {}

    def fake_orchestrate_improve(*, intent, project_kind, break_lock,
                                  project_dir, rounds, focus):
        # `target`'s goal arg is forwarded as `focus` — the legacy
        # improve loop treated them differently, but for the i2p path
        # they collapse to "scope hint".
        captured["focus"] = focus
        captured["rounds"] = rounds
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_improve", fake_orchestrate_improve)

    code, _out = _run(
        ["improve", "target", "--i2p", "latency < 100ms"],
        cwd=tmp_path,
    )
    assert code == 0
    assert captured["focus"] == "latency < 100ms"


def test_improve_feature_legacy_pipeline_now_hard_errors(
    tmp_path: Path, monkeypatch
) -> None:
    """Phase C: pinning ``default_pipeline: legacy`` routes
    ``otto improve feature`` into the deletion error path."""
    _init_project(tmp_path)
    (tmp_path / "intent.md").write_text("test intent\n")
    (tmp_path / "otto.yaml").write_text("default_pipeline: legacy\n")
    sentinel: dict[str, bool] = {"orchestrated": False}

    def fake_orchestrate(**_kwargs):
        sentinel["orchestrated"] = True
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_improve", fake_orchestrate)

    code, _out = _run(["improve", "feature", "search UX"], cwd=tmp_path)
    assert code == 1
    assert sentinel["orchestrated"] is False


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
        ["run", "--no-build", "build me a tiny webapp"],
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

    # The --no-build skip message is printed
    assert "Build, audit, and render skipped" in out
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
        ["run", "--no-build", "--project-kind", "cli", "make a small linter"],
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
    code, out = _run(["run", "--no-build"], cwd=tmp_path, env=env)
    assert code == 0, out
    assert captured["intent"] == "intent loaded from file"


def test_run_full_pipeline_drives_build_audit_render(tmp_path: Path, monkeypatch) -> None:
    """End-to-end mock: stub compile + build/merge/audit so the pipeline runs."""
    from otto.audit import AuditResult, AuditVerdict
    from otto.build import BuildResult, GroupResult, GroupStatus
    from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus

    _init_project(tmp_path)
    fake_spec = _fixture_spec("full pipe")

    async def fake_compile(intent, *, project_dir, run_dir, config, project_kind, **kwargs):
        from otto.spec_compile import persist_spec
        run_dir.mkdir(parents=True, exist_ok=True)
        persist_spec(fake_spec, run_dir / "spec.json", allow_initial=True)
        return fake_spec

    async def fake_build(spec, *, project_dir, session_dir, build_agent, **kwargs):
        return BuildResult(
            spec_session_dir=session_dir,
            group_results=[
                GroupResult(group_id="shell", status=GroupStatus.PASSING, attempts=1,
                            branch="b", worktree=project_dir),
            ],
            total_cost_usd=0.05,
            total_wall_s=10.0,
        )

    async def fake_merge(spec, build_result, *, project_dir, session_dir, **kwargs):
        return MergeQueueResult(
            landed_ids=["shell"],
            results=[MergeResult(group_id="shell", status=MergeStatus.LANDED,
                                 landed_commit="abc")],
            total_cost_usd=0.02,
            total_wall_s=2.0,
        )

    async def fake_audit(spec, *, project_dir, session_dir, build_result, merge_result,
                        audit_agent, **kwargs):
        return AuditResult(
            verdict=AuditVerdict.PASSED,
            narrative="stub audit",
            cost_usd=0.10,
            wall_s=5.0,
        )

    monkeypatch.setattr("otto.cli_run.compile_spec", fake_compile)
    # A1.6: pipeline phases (build/merge/audit) live in otto.runner now.
    monkeypatch.setattr("otto.runner.run_build", fake_build)
    monkeypatch.setattr("otto.runner.run_merge_queue", fake_merge)
    monkeypatch.setattr("otto.runner.run_audit", fake_audit)

    env = {"OTTO_RUN_ID": "2026-05-03-130000-fff999"}
    code, out = _run(["run", "build it"], cwd=tmp_path, env=env)
    assert code == 0, out
    assert "Compile complete" in out
    assert "Build phase" in out
    assert "Merge phase" in out
    assert "Audit phase" in out
    assert "Render phase" in out
    assert "verdict" in out.lower()
    # Proof packet artifacts written
    session_dir = tmp_path / "otto_logs" / "sessions" / env["OTTO_RUN_ID"]
    assert (session_dir / "proof-packet.html").exists()
    assert (session_dir / "proof-packet.json").exists()


def test_run_full_pipeline_returns_nonzero_on_partial_audit(tmp_path: Path, monkeypatch) -> None:
    from otto.audit import AuditResult, AuditVerdict
    from otto.build import BuildResult, GroupResult, GroupStatus
    from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus

    _init_project(tmp_path)
    fake_spec = _fixture_spec("partial pipe")

    async def fake_compile(intent, *, project_dir, run_dir, config, project_kind, **kwargs):
        from otto.spec_compile import persist_spec
        run_dir.mkdir(parents=True, exist_ok=True)
        persist_spec(fake_spec, run_dir / "spec.json", allow_initial=True)
        return fake_spec

    async def fake_build(spec, *, project_dir, session_dir, **kwargs):
        return BuildResult(
            spec_session_dir=session_dir,
            group_results=[
                GroupResult(group_id="shell", status=GroupStatus.PASSING, attempts=1,
                            branch="b", worktree=project_dir),
            ],
        )

    async def fake_merge(spec, build_result, *, project_dir, session_dir, **kwargs):
        return MergeQueueResult(landed_ids=["shell"],
                                results=[MergeResult(group_id="shell",
                                                     status=MergeStatus.LANDED,
                                                     landed_commit="abc")])

    async def fake_audit(spec, **kwargs):
        return AuditResult(verdict=AuditVerdict.PARTIAL, narrative="partial")

    monkeypatch.setattr("otto.cli_run.compile_spec", fake_compile)
    # A1.6: pipeline phases live in otto.runner now.
    monkeypatch.setattr("otto.runner.run_build", fake_build)
    monkeypatch.setattr("otto.runner.run_merge_queue", fake_merge)
    monkeypatch.setattr("otto.runner.run_audit", fake_audit)

    env = {"OTTO_RUN_ID": "2026-05-03-130100-aaa999"}
    code, _out = _run(["run", "x"], cwd=tmp_path, env=env)
    assert code == 1  # partial → exit 1


def test_run_from_spec_loads_existing_spec(tmp_path: Path, monkeypatch) -> None:
    from otto.audit import AuditResult, AuditVerdict
    from otto.build import BuildResult
    from otto.merge_queue import MergeQueueResult
    from otto.spec_compile import persist_spec

    _init_project(tmp_path)
    fake_spec = _fixture_spec("from-spec")
    spec_dir = tmp_path / "otto_logs" / "sessions" / "manual-session" / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    persist_spec(fake_spec, spec_dir / "spec.json", allow_initial=True)

    captured: dict[str, object] = {}

    async def fake_build(spec, *, project_dir, session_dir, **kwargs):
        captured["build_spec_intent"] = spec.intent
        captured["session_dir"] = session_dir
        return BuildResult(spec_session_dir=session_dir, group_results=[])

    async def fake_merge(spec, build_result, **kwargs):
        return MergeQueueResult()

    async def fake_audit(spec, **kwargs):
        return AuditResult(verdict=AuditVerdict.PASSED, narrative="from-spec passed")

    # A1.6: pipeline phases live in otto.runner now.
    monkeypatch.setattr("otto.runner.run_build", fake_build)
    monkeypatch.setattr("otto.runner.run_merge_queue", fake_merge)
    monkeypatch.setattr("otto.runner.run_audit", fake_audit)

    code, out = _run(
        ["run", "--from-spec", str(spec_dir / "spec.json")],
        cwd=tmp_path,
    )
    assert code == 0, out
    assert captured["build_spec_intent"] == "from-spec"
    assert "manual-session" in str(captured["session_dir"])


# ---------------------------------------------------------------------------
# `otto build --resume` and `otto certify --resume` (i2p resume planner)
# ---------------------------------------------------------------------------


def test_build_resume_flag_propagates(tmp_path: Path, monkeypatch) -> None:
    """`otto build --i2p --resume` calls orchestrate_run with resume=True."""
    _init_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_orchestrate_run(**kwargs):
        captured.update(kwargs)
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)

    code, out = _run(["build", "--i2p", "--resume"], cwd=tmp_path)
    assert code == 0, out
    assert captured.get("resume") is True
    assert captured.get("reset_budget") is False
    assert captured.get("force") is False


def test_build_resume_with_reset_budget_and_force(
    tmp_path: Path, monkeypatch
) -> None:
    _init_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_orchestrate_run(**kwargs):
        captured.update(kwargs)
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)

    code, out = _run(
        ["build", "--i2p", "--resume", "--reset-budget", "--force"],
        cwd=tmp_path,
    )
    assert code == 0, out
    assert captured.get("resume") is True
    assert captured.get("reset_budget") is True
    assert captured.get("force") is True


def test_certify_resume_flag_propagates(tmp_path: Path, monkeypatch) -> None:
    _init_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_orchestrate_certify(**kwargs):
        captured.update(kwargs)
        import sys
        sys.exit(0)

    monkeypatch.setattr(
        "otto.cli_run.orchestrate_certify", fake_orchestrate_certify
    )

    code, out = _run(
        ["certify", "--i2p", "--resume", "--force"], cwd=tmp_path
    )
    assert code == 0, out
    assert captured.get("resume") is True
    assert captured.get("force") is True
    assert captured.get("reset_budget") is False


def test_run_resume_rejects_positional_intent(tmp_path: Path, monkeypatch) -> None:
    """--resume + positional intent → exit 2 ("cannot be combined")."""
    _init_project(tmp_path)
    code, out = _run(["run", "--resume", "intent text"], cwd=tmp_path)
    assert code == 2, out
    assert "--resume cannot be combined" in out


# ---------------------------------------------------------------------------
# A13 — review-gate flag wiring (compile → review-gate → build)
# ---------------------------------------------------------------------------


def test_run_review_gate_flag_threads_into_orchestrate_run(
    tmp_path: Path, monkeypatch
) -> None:
    """`otto run --review-gate` propagates review_gate=True + the timeout
    into orchestrate_run kwargs."""
    _init_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_orchestrate_run(**kwargs):
        captured.update(kwargs)
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)
    code, out = _run(
        ["run", "--review-gate", "--gate-timeout", "60", "intent text"],
        cwd=tmp_path,
    )
    assert code == 0, out
    assert captured.get("review_gate") is True
    assert captured.get("gate_timeout_s") == 60.0


def test_run_default_omits_review_gate(tmp_path: Path, monkeypatch) -> None:
    """Without the flag, review_gate=False is forwarded (preserves the
    existing CI/script default)."""
    _init_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_orchestrate_run(**kwargs):
        captured.update(kwargs)
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)
    code, out = _run(["run", "anything"], cwd=tmp_path)
    assert code == 0, out
    assert captured.get("review_gate") is False


def test_run_review_gate_conflicts_with_auto_approve(
    tmp_path: Path, monkeypatch
) -> None:
    """--review-gate + --auto-approve must hard-error (mutually exclusive)."""
    _init_project(tmp_path)

    def fake_orchestrate_run(**kwargs):
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)
    code, out = _run(
        ["run", "--review-gate", "--auto-approve", "anything"],
        cwd=tmp_path,
    )
    assert code != 0
    assert "mutually exclusive" in out.lower()


def test_build_review_gate_flag_threads_into_orchestrate_run(
    tmp_path: Path, monkeypatch
) -> None:
    """`otto build --i2p --review-gate` also wires through to
    orchestrate_run."""
    _init_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_orchestrate_run(**kwargs):
        captured.update(kwargs)
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_run", fake_orchestrate_run)
    code, out = _run(
        ["build", "--i2p", "--review-gate", "intent"],
        cwd=tmp_path,
    )
    assert code == 0, out
    assert captured.get("review_gate") is True


def test_run_help_lists_review_gate_options() -> None:
    """`otto run --help` exposes both --review-gate and --auto-approve."""
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "--review-gate" in result.output
    assert "--auto-approve" in result.output
