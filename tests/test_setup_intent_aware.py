from __future__ import annotations

from click.testing import CliRunner

import otto.cli_setup as cli_setup
from otto.cli import main


GENERIC_CLAUDE_MD = """This repo is very small — read these files first before doing anything:
- intent.md — describes what the project is for and its goals
- otto.yaml — project configuration (likely defines the agent/workflow contract)
There is no source tree, build system, or test suite yet. Treat this as an early-stage
project where conventions are still being established.
"""


def test_setup_without_intent_md_keeps_generic_claude_md(tmp_bare_git_repo, monkeypatch) -> None:
    async def fake_run_setup_query(prompt, project_dir, config=None):
        assert "kanban-style multi-user task tracker" not in prompt
        return GENERIC_CLAUDE_MD

    monkeypatch.chdir(tmp_bare_git_repo)
    monkeypatch.setattr(cli_setup, "_run_setup_query", fake_run_setup_query)

    result = CliRunner().invoke(main, ["setup"], input="\n", catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_bare_git_repo / "CLAUDE.md").read_text() == GENERIC_CLAUDE_MD


def test_setup_with_nontrivial_intent_md_generates_product_specific_claude_md(
    tmp_bare_git_repo,
    monkeypatch,
) -> None:
    product_description = (
        "A kanban-style multi-user task tracker with swimlanes, live cursor presence, "
        "and drag-and-drop task ordering."
    )
    (tmp_bare_git_repo / "intent.md").write_text(product_description)

    async def fake_run_setup_query(prompt, project_dir, config=None):
        assert product_description in prompt
        return (
            "# CLAUDE\n"
            "Build the kanban-style multi-user task tracker around collaborative boards.\n"
            "Preserve live cursor presence while implementing drag-and-drop task ordering.\n"
        )

    monkeypatch.chdir(tmp_bare_git_repo)
    monkeypatch.setattr(cli_setup, "_run_setup_query", fake_run_setup_query)

    result = CliRunner().invoke(main, ["setup"], input="\n", catch_exceptions=False)

    assert result.exit_code == 0
    content = (tmp_bare_git_repo / "CLAUDE.md").read_text()
    assert "kanban-style multi-user task tracker" in content
    assert "live cursor presence" in content


def test_setup_with_trivial_intent_md_keeps_generic_claude_md(tmp_bare_git_repo, monkeypatch) -> None:
    (tmp_bare_git_repo / "intent.md").write_text("todo board")

    async def fake_run_setup_query(prompt, project_dir, config=None):
        assert "todo board" not in prompt
        return GENERIC_CLAUDE_MD

    monkeypatch.chdir(tmp_bare_git_repo)
    monkeypatch.setattr(cli_setup, "_run_setup_query", fake_run_setup_query)

    result = CliRunner().invoke(main, ["setup"], input="\n", catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_bare_git_repo / "CLAUDE.md").read_text() == GENERIC_CLAUDE_MD
