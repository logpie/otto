from __future__ import annotations

from click.testing import CliRunner

from otto.cli import main
from otto.pipeline import BuildResult


BOOTSTRAP_INTENT = "Bootstrap and maintain the product described in intent.md"


def test_build_without_arg_uses_nontrivial_intent_md(tmp_git_repo, monkeypatch) -> None:
    (tmp_git_repo / "intent.md").write_text(
        "A kanban-style multi-user task tracker with swimlanes, live cursor presence, "
        "and drag-and-drop task ordering."
    )
    captured: dict[str, object] = {}

    async def fake_build(intent, project_dir, config, **kwargs):
        captured["intent"] = intent
        captured["project_dir"] = project_dir
        captured["record_intent"] = kwargs.get("record_intent")
        return BuildResult(
            passed=True,
            build_id="run-intent-md",
            total_cost=0.0,
            tasks_passed=1,
            tasks_failed=0,
        )

    monkeypatch.chdir(tmp_git_repo)
    monkeypatch.setattr("otto.pipeline.build_agentic_v3", fake_build)

    result = CliRunner().invoke(main, ["build"], catch_exceptions=False)

    assert result.exit_code == 0
    assert captured["intent"] == BOOTSTRAP_INTENT
    assert captured["record_intent"] is True
    assert "Using project intent from intent.md" in result.output


def test_build_explicit_intent_still_wins_over_intent_md(tmp_git_repo, monkeypatch) -> None:
    (tmp_git_repo / "intent.md").write_text(
        "A kanban-style multi-user task tracker with swimlanes, live cursor presence, "
        "and drag-and-drop task ordering."
    )
    captured: dict[str, str] = {}

    async def fake_build(intent, project_dir, config, **kwargs):
        captured["intent"] = intent
        return BuildResult(
            passed=True,
            build_id="run-explicit-intent",
            total_cost=0.0,
            tasks_passed=1,
            tasks_failed=0,
        )

    monkeypatch.chdir(tmp_git_repo)
    monkeypatch.setattr("otto.pipeline.build_agentic_v3", fake_build)

    result = CliRunner().invoke(
        main,
        ["build", "ship the MVP shell first"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert captured["intent"] == "ship the MVP shell first"
    assert "Using project intent from intent.md" not in result.output


def test_build_without_arg_and_without_intent_md_shows_improved_error(
    tmp_git_repo,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_git_repo)

    result = CliRunner().invoke(main, ["build"])

    assert result.exit_code == 2
    assert (
        "Intent cannot be empty. Either pass a description as the first argument, "
        "or write a description of the product to ./intent.md"
    ) in " ".join(result.output.split())
