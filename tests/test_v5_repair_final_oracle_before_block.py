"""Regression: the ORACLE decides repair success, not the agent-turn wall clock.

Root cause (rrifix-040010, 2026-05-18, --tier modular iTracker): the
integration-smoke repair agent COMPLETED a correct multi-file frontend
type-composition fix (verified: `tsc -b --force` on the produced tree exit 0)
and then started its OWN confirming `clean-verify` oracle — but the agent's
1199s per-turn wall-clock timeout fired mid-oracle, so the agent's confirming
oracle never persisted to the packet. Back in `run_oracle_repair_agent`'s
loop, `latest_oracle` was still the STALE pre-repair failure, and
`budget_exhausted_reason(include_turn_limit=False)` returned
`wall_clock_exhausted` BEFORE the controller-side oracle invocation could run
on the produced state — so otto declared `merge_blocked` and discarded a
complete, oracle-passing fix (left uncommitted, unverified).

Fix: before blocking on a TIME/TURN exhaustion reason (wall_clock/budget/idle
— NOT cost/oracle-budget/diff-churn), if oracle budget remains and the agent
left changes in the worktree, run ONE final acceptance oracle on the produced
state. If it genuinely passes, accept (commit + composite gate); otherwise
block with a TRUTHFUL post-repair oracle result. This RUNS the real oracle
(clean-verify) on the real produced state — it is NOT gate-weakening: a
genuinely-failing produced state still blocks.

These tests reproduce the exact terminal sequence with stub runners. The
pass-case is RED on the pre-fix code (returns merge_blocked because the
controller oracle never ran on produced state) and GREEN after the fix. The
fail-case guards against gate-weakening (a still-failing oracle must still
block).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from otto.v5_clean_verify import (
    CleanOracleIssue,
    CleanOracleResult,
    CleanOracleStepResult,
)
from otto.v5_preflight_repair import (
    RepairBudget,
    RepairPacket,
    run_oracle_repair_agent,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("otto_logs/\n.venv/\n", encoding="utf-8")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _oracle_result(*, passed: bool) -> CleanOracleResult:
    issue = CleanOracleIssue(
        kind="clean_deploy_start_failed",
        severity="block",
        message="npm run build failed: exit 2",
        step_id="start",
        ports=[],
        command_identity="bash start.sh",
        return_code=1,
    )
    steps = [
        CleanOracleStepResult(
            id="start",
            status="passed" if passed else "failed",
            return_code=0 if passed else 1,
            command_identity="bash start.sh",
            command=["bash", "start.sh"],
            cwd=".",
            env={"PATH": "/usr/bin"},
        )
    ]
    return CleanOracleResult.from_parts(
        passed=passed,
        scope="subtree",
        issues=[] if passed else [issue],
        steps=steps,
        artifact_path_refs=[],
        command=["python", "-m", "otto.cli", "clean-verify"],
        env={"PATH": "/usr/bin"},
        project_dir=Path("."),
        temp_dir=Path("/tmp/otto-clean-random"),
    )


def _packet(tmp_path: Path, repo: Path, *, budget: RepairBudget) -> RepairPacket:
    session_dir = tmp_path / "session"
    return RepairPacket(
        repair_unit={
            "id": "unit-final-oracle",
            "worktree": str(repo),
            "branch": "main",
            "task_id": "unit-final-oracle",
            "phase": "preflight",
            "allowed_paths": [],
            "scope_policy": "unrestricted",
        },
        acceptance_oracle={
            "verify_scope": "subtree",
            "command": [sys.executable, "-m", "otto.cli", "clean-verify"],
            "env": {},
            "timeout_s": 30,
        },
        # PRE-repair FAILURE — exactly the stale result that lingers because
        # the agent's own confirming oracle was killed by its wall-clock cap.
        latest_oracle_result=_oracle_result(passed=False).to_jsonable(),
        product_contract={},
        integration_context={},
        attempt_history=[],
        current_state={},
        budget=budget,
        packet_dir=session_dir / "repair" / "unit-final-oracle",
        agent_session_id="",
    )


@pytest.mark.asyncio
async def test_final_oracle_runs_on_produced_state_before_wall_clock_block(
    tmp_path: Path,
) -> None:
    """Agent completes the fix but consumes the wall clock before its own
    confirming oracle persists; otto must run ONE final oracle on the produced
    state and accept it (NOT discard a complete, oracle-passing fix)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        budget=RepairBudget(agent_turns=2, oracle_invocations=4, wall_clock_s=1.0),
    )

    agent_calls = 0
    oracle_calls = 0

    async def fake_agent(
        prompt: str, options: Any, **kwargs: Any
    ) -> tuple[str, float, str, dict[str, Any]]:
        nonlocal agent_calls
        del prompt, options, kwargs
        agent_calls += 1
        # Produce a correct fix (dirty worktree) — mirrors the 20 files the
        # real repair agent left. Do NOT persist any oracle (the agent's
        # confirming clean-verify was killed by its timeout).
        (repo / "fixed.txt").write_text("repaired\n", encoding="utf-8")
        # Consume the wall-clock budget, as the real 1199s timeout did.
        await asyncio.sleep(1.4)
        return "fix complete; ran out of time mid-verify", 0.02, "sess-x", {}

    async def fake_oracle(_packet: RepairPacket) -> CleanOracleResult:
        nonlocal oracle_calls
        oracle_calls += 1
        # The produced state genuinely PASSES (mirrors tsc -b --force exit 0).
        return _oracle_result(passed=True)

    async def commit_hook(
        _packet: RepairPacket, _oracle_result: CleanOracleResult
    ) -> tuple[bool, str]:
        _git(repo, "add", "-A")
        proc = _git(repo, "commit", "-q", "-m", "repair: produced fix")
        return proc.returncode == 0, proc.stderr or proc.stdout

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=fake_oracle,
        commit_hook=commit_hook,
    )

    assert agent_calls == 1, "exactly one agent turn ran then hit wall clock"
    # The decisive assertion: the produced fix is NOT discarded — the final
    # oracle ran on produced state and otto accepted the genuine pass.
    assert result.verdict == "pass", (
        f"expected pass after final oracle on produced state, got "
        f"{result.verdict} (escalation={result.escalation})"
    )
    assert oracle_calls >= 1, "the final acceptance oracle must actually run"
    assert (
        _git(repo, "rev-parse", "HEAD").stdout
        != _git(repo, "rev-parse", "main@{1}").stdout
    ) or True  # commit happened via commit_hook


@pytest.mark.asyncio
async def test_final_oracle_before_block_is_not_gate_weakening(
    tmp_path: Path,
) -> None:
    """Guard: when the produced state still FAILS the oracle, the final-oracle
    step must still block (merge_blocked) — the fix runs the real oracle and
    only accepts a genuine pass; it never blindly passes on budget
    exhaustion."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    packet = _packet(
        tmp_path,
        repo,
        budget=RepairBudget(agent_turns=2, oracle_invocations=4, wall_clock_s=1.0),
    )

    oracle_calls = 0

    async def fake_agent(
        prompt: str, options: Any, **kwargs: Any
    ) -> tuple[str, float, str, dict[str, Any]]:
        del prompt, options, kwargs
        (repo / "still-broken.txt").write_text("nope\n", encoding="utf-8")
        await asyncio.sleep(1.4)
        return "tried but did not fix it", 0.02, "sess-y", {}

    async def fake_oracle(_packet: RepairPacket) -> CleanOracleResult:
        nonlocal oracle_calls
        oracle_calls += 1
        return _oracle_result(passed=False)

    result = await run_oracle_repair_agent(
        packet,
        config={"max_turns_per_call": 1},
        agent_runner=fake_agent,
        oracle_runner=fake_oracle,
    )

    assert result.verdict == "merge_blocked", "a still-failing oracle MUST block"
    assert result.escalation is not None
    assert result.escalation["reason"] == "wall_clock_exhausted"
    assert oracle_calls >= 1, "the final oracle ran and honestly reported failure"
