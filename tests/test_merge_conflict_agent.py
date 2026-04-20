"""Tests for otto.merge.conflict_agent — F12 regression."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from otto.merge.conflict_agent import (
    ConflictContext,
    ConflictResolutionAttempt,
    resolve_one_conflict,
)


class _StubOptions:
    def __init__(self) -> None:
        self.disallowed_tools: list[str] = []
        self.model: str | None = None


def test_conflict_agent_disallows_bash_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """conflict agent MUST disallow 'Bash' to prevent shell escape. F12
    previously also disallowed 'Write' but the P6 rerun showed that
    making the agent use Edit-only made conflict resolution 2-3× slower
    (multiple plan→edit→verify cycles, each with extended-thinking phase).
    Reverted: drift prevention is post-agent (validate_post_agent checks
    no out-of-scope files modified, HEAD unchanged).

    If you re-add 'Write' to disallowed_tools, also re-tune the prompt to
    avoid multi-pass loops, or measure to confirm net benefit."""
    # Minimal git repo so head_sha/changed_files don't crash
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=tmp_path, check=True)

    captured: dict[str, Any] = {}

    def fake_make_options(project_dir: Path, config: dict[str, Any] | None = None, **overrides: Any) -> _StubOptions:
        return _StubOptions()

    async def fake_run_agent(prompt: str, options: _StubOptions, **kwargs: Any) -> tuple[str, float, str]:
        captured["disallowed_tools"] = list(options.disallowed_tools or [])
        return ("ok", 0.0, "session-1")

    def fake_validate(*, project_dir, pre_diff_files, expected_uu_files, pre_untracked_files, pre_head):
        return (True, None)  # pretend the agent did it right

    # Inject stubs via monkeypatch on the imports inside resolve_one_conflict
    import otto.agent as _agent
    import otto.merge.conflict_agent as _ca
    monkeypatch.setattr(_agent, "make_agent_options", fake_make_options)
    monkeypatch.setattr(_agent, "run_agent_with_timeout", fake_run_agent)
    monkeypatch.setattr(_ca, "validate_post_agent", fake_validate)

    ctx = ConflictContext(
        target="main",
        branch_being_merged="feature/x",
        branch_intents={"main": "target", "feature/x": "added X"},
        branch_stories=[],
        conflict_files=["f.txt"],
        conflict_diff="<<<<<<< HEAD\na\n=======\nb\n>>>>>>> feature/x\n",
    )
    import asyncio
    result: ConflictResolutionAttempt = asyncio.run(
        resolve_one_conflict(project_dir=tmp_path, config={}, ctx=ctx)
    )

    assert result.success is True
    assert "Bash" in captured["disallowed_tools"], (
        f"Bash must be disallowed (no shell escape); got {captured['disallowed_tools']!r}"
    )
    # Write should NOT be in the disallowed list — F12 was reverted because
    # forcing Edit-only triggered multi-pass thinking and 2-3× slowdown.
    assert "Write" not in captured["disallowed_tools"], (
        f"Write should be allowed (F12 reverted — see findings F12). "
        f"If you re-disallow Write, retune merger-conflict.md to avoid "
        f"multi-pass loops, and re-measure. Got: {captured['disallowed_tools']!r}"
    )
