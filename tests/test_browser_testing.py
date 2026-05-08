from __future__ import annotations

from otto.browser_testing import (
    agent_browser_argv,
    classify_browser_command,
    declared_browser_evidence_missing,
    validate_agent_browser_command,
)


def test_browser_command_policy_requires_agent_browser_session() -> None:
    assert classify_browser_command(["agent-browser", "open", "http://x"]) == "agent-browser"
    assert "without a unique" in (
        validate_agent_browser_command(["agent-browser", "open", "http://x"]) or ""
    )
    assert validate_agent_browser_command(
        ["agent-browser", "--session", "journey-1", "open", "http://x"]
    ) is None


def test_agent_browser_argv_adds_explicit_session() -> None:
    assert agent_browser_argv("Journey 1", "open", "http://x") == [
        "agent-browser",
        "--session",
        "Journey-1",
        "open",
        "http://x",
    ]


def test_declared_browser_evidence_missing_policy() -> None:
    assert declared_browser_evidence_missing(
        returncode=0,
        evidence_globs=["evidence/*.png"],
        artifact_count=0,
    )
    assert not declared_browser_evidence_missing(
        returncode=0,
        evidence_globs=["evidence/*.png"],
        artifact_count=1,
    )
    assert not declared_browser_evidence_missing(
        returncode=1,
        evidence_globs=["evidence/*.png"],
        artifact_count=0,
    )
