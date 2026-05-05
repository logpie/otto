"""Tests for the A6.5 out-of-scope intent guard (research §9.5b).

Otto cannot meaningfully verify systems-level products. The guard runs
BEFORE LLM cost; intents containing systems-level keywords raise
SpecValidationError. The user can override by including the literal
`override-scope` token in their intent — the proof packet later marks
the verdict as suggestive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from otto.spec_compile import (
    OUT_OF_SCOPE_KEYWORDS,
    OUT_OF_SCOPE_OVERRIDE_TOKEN,
    SpecValidationError,
    compile_spec,
    detect_out_of_scope_intent,
)


# ---------------------------------------------------------------------------
# detect_out_of_scope_intent — pure-function unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("a tiny webapp", None),
        ("a doc editor with comments", None),
        ("a CLI tool that lints Python", None),
        # browser-based UI is benign (browser alone isn't enough)
        ("a browser-based bookmark manager", None),
        # but explicit browser engine triggers
        ("a custom browser engine for testing", "browser engine"),
        ("a JavaScript runtime for the cloud", "javascript runtime"),
        ("a language compiler for our DSL", "language compiler"),
        ("a database engine with btree storage", "database engine"),
        ("an operating system kernel for IoT", "operating system kernel"),
        ("a Linux kernel module", "linux kernel"),
        ("a hypervisor for nested virtualisation", "hypervisor"),
        ("embedded firmware for the gadget", "embedded firmware"),
        ("a device driver for our card", "device driver"),
        ("a memory allocator with arenas", "memory allocator"),
        ("a garbage collector for our runtime", "garbage collector"),
    ],
)
def test_detect_out_of_scope_intent(intent: str, expected: str | None) -> None:
    assert detect_out_of_scope_intent(intent) == expected


def test_override_token_bypasses_guard() -> None:
    """User-explicit override-scope token disables the keyword guard."""
    intent = "a custom browser engine — override-scope, please"
    assert detect_out_of_scope_intent(intent) is None


def test_override_token_is_lowercase_match() -> None:
    """The override token check is case-insensitive."""
    intent = "OVERRIDE-SCOPE: custom javascript runtime"
    assert detect_out_of_scope_intent(intent) is None


def test_empty_intent_is_in_scope() -> None:
    """Empty intent (brownfield with no intent) doesn't trigger the guard."""
    assert detect_out_of_scope_intent("") is None


def test_keyword_list_has_no_dupes() -> None:
    """OUT_OF_SCOPE_KEYWORDS list is deliberate; no duplicate phrases."""
    assert len(set(OUT_OF_SCOPE_KEYWORDS)) == len(OUT_OF_SCOPE_KEYWORDS)


# ---------------------------------------------------------------------------
# compile_spec integration — guard fires before LLM cost
# ---------------------------------------------------------------------------


def test_compile_spec_rejects_out_of_scope_intent_before_llm(
    tmp_path: Path, monkeypatch
) -> None:
    """Out-of-scope intent raises SpecValidationError before any agent call.

    We monkeypatch run_agent_with_timeout to a function that raises if
    invoked — proving the guard short-circuits earlier.
    """
    project = tmp_path / "proj"
    project.mkdir()
    run_dir = tmp_path / "session" / "spec"

    def explode(*_a: object, **_kw: object) -> None:
        raise AssertionError("agent must NOT be called for out-of-scope intent")

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", explode)
    monkeypatch.setattr(
        "otto.agent.make_agent_options",
        lambda *_a, **_kw: object(),
    )

    with pytest.raises(SpecValidationError) as excinfo:
        asyncio.run(
            compile_spec(
                "build me a custom browser engine",
                project,
                run_dir,
                {},
                project_kind="webapp",
            )
        )
    msg = str(excinfo.value)
    assert "browser engine" in msg
    # The override token must be discoverable in the error
    assert OUT_OF_SCOPE_OVERRIDE_TOKEN in msg
    assert "§9.5b" in msg


def test_compile_spec_guard_runs_in_brownfield_mode_too(
    tmp_path: Path, monkeypatch
) -> None:
    """Brownfield path also triggers the guard — it's an intent check,
    not a project-state check."""
    project = tmp_path / "proj"
    project.mkdir()
    run_dir = tmp_path / "session" / "spec"

    def explode(*_a: object, **_kw: object) -> None:
        raise AssertionError("agent must NOT be called")

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", explode)
    monkeypatch.setattr(
        "otto.agent.make_agent_options",
        lambda *_a, **_kw: object(),
    )

    with pytest.raises(SpecValidationError):
        asyncio.run(
            compile_spec(
                "audit our linux kernel module's memory safety",
                project,
                run_dir,
                {},
                project_kind="library",
                brownfield=True,
            )
        )


def test_compile_spec_with_override_token_proceeds_to_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """With override-scope present, the guard does NOT fire and the
    agent is invoked. We don't run the agent for real — we just verify
    the guard has been bypassed by detecting that the agent stub is hit.
    """
    project = tmp_path / "proj"
    project.mkdir()
    run_dir = tmp_path / "session" / "spec"

    agent_called: dict[str, bool] = {"hit": False}

    async def stub(*_a: object, **_kw: object):
        agent_called["hit"] = True
        # Raise a sentinel so we don't have to mock the rest of the pipeline
        raise RuntimeError("guard bypassed; sentinel reached agent")

    monkeypatch.setattr("otto.agent.run_agent_with_timeout", stub)
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

    with pytest.raises(RuntimeError, match="guard bypassed"):
        asyncio.run(
            compile_spec(
                "build a custom browser engine — override-scope: I know",
                project,
                run_dir,
                {},
                project_kind="webapp",
            )
        )
    assert agent_called["hit"] is True
