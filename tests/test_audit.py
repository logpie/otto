"""Tests for otto/audit.py — final audit pass + fix-loop routing.

Coverage:
- run_audit happy path: audit agent returns PASSED → done in one pass
- run_audit partial verdict + fix loop routes to specific slices
- run_audit retries exhausted → returns last verdict
- run_audit with no fix_agent: returns first verdict (no repair attempted)
- run_audit walkthrough hook produces artifacts
- _parse_audit_output: JSON fence extraction, malformed input
- Build/merge summary helpers
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otto.audit import (
    AuditAgentInput,
    AuditAgentOutput,
    AuditBudget,
    AuditVerdict,
    SliceVerdict,
    WalkthroughResult,
    _parse_audit_output,
    default_walkthrough_from_spec,
    run_audit,
)
from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildResult,
    SliceResult,
    SliceStatus,
)
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.spec_compile import (
    Slice,
    Spec,
    StateInvariant,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(slice_ids: list[str], cross_checks=None) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        slices=[
            Slice(id=sid, title=sid.upper(), deps=[], owned_paths=[], tasks=[], checks=[])
            for sid in slice_ids
        ],
        cross_slice_checks=cross_checks or [],
    )


def _build_result(passing_ids: list[str], project_dir: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=project_dir,
        slice_results=[
            SliceResult(
                slice_id=sid,
                status=SliceStatus.PASSING,
                attempts=1,
                branch=f"i2p/x/{sid}",
                worktree=project_dir,
            )
            for sid in passing_ids
        ],
    )


def _merge_result(landed_ids: list[str]) -> MergeQueueResult:
    return MergeQueueResult(
        landed_ids=landed_ids,
        results=[
            MergeResult(slice_id=sid, status=MergeStatus.LANDED, landed_commit="abc1234")
            for sid in landed_ids
        ],
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_audit_passed_in_one_pass(tmp_path: Path) -> None:
    spec = _spec(["s1", "s2"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    async def passing_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="all good",
            slice_verdicts=[
                SliceVerdict(slice_id="s1", passed=True),
                SliceVerdict(slice_id="s2", passed=True),
            ],
            cost_usd=0.10,
        )

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1", "s2"], tmp_path),
            merge_result=_merge_result(["s1", "s2"]),
            audit_agent=passing_agent,
        )
    )
    assert result.verdict == AuditVerdict.PASSED
    assert result.retries == 0
    assert result.cost_usd == 0.10
    assert len(result.slice_verdicts) == 2


def test_run_audit_runs_cross_slice_checks(tmp_path: Path) -> None:
    spec = _spec(
        ["s1"],
        cross_checks=[StateInvariant(description="ok", expression="True")],
    )
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    async def passing_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        # Verify the cross-slice evidence was passed to the agent.
        assert len(input_.cross_slice_evidence) == 1
        assert input_.cross_slice_evidence[0].passed is True
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="ok")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
        )
    )
    assert result.verdict == AuditVerdict.PASSED
    assert len(result.cross_slice_evidence) == 1


# ---------------------------------------------------------------------------
# Fix-loop routing
# ---------------------------------------------------------------------------


def test_run_audit_routes_to_fix_loop_for_failing_slice(tmp_path: Path) -> None:
    spec = _spec(["s1", "s2"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    fix_calls: list[str] = []
    pass_state = {"after_fix": False}

    async def audit_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        if pass_state["after_fix"]:
            return AuditAgentOutput(
                verdict=AuditVerdict.PASSED,
                narrative="repaired",
                slice_verdicts=[
                    SliceVerdict(slice_id="s1", passed=True),
                    SliceVerdict(slice_id="s2", passed=True),
                ],
            )
        return AuditAgentOutput(
            verdict=AuditVerdict.PARTIAL,
            narrative="s1 broken",
            slice_verdicts=[
                SliceVerdict(slice_id="s1", passed=False, detail="missing route"),
                SliceVerdict(slice_id="s2", passed=True),
            ],
        )

    async def fix_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        fix_calls.append(input_.slice.id)
        pass_state["after_fix"] = True
        return BuildAgentOutput(succeeded=True, cost_usd=0.05, detail="fixed")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1", "s2"], tmp_path),
            merge_result=_merge_result(["s1", "s2"]),
            audit_agent=audit_agent,
            fix_agent=fix_agent,
        )
    )
    assert result.verdict == AuditVerdict.PASSED
    assert fix_calls == ["s1"]  # only the failing slice got the fix call
    assert result.retries == 1


def test_run_audit_no_fix_agent_returns_first_verdict(tmp_path: Path) -> None:
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    async def partial_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PARTIAL,
            narrative="s1 has issues",
            slice_verdicts=[SliceVerdict(slice_id="s1", passed=False, detail="x")],
        )

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=partial_agent,
            # no fix_agent
        )
    )
    assert result.verdict == AuditVerdict.PARTIAL
    assert result.retries == 0


def test_run_audit_retries_exhausted_returns_last(tmp_path: Path) -> None:
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    async def stubborn_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PARTIAL,
            narrative="still failing",
            slice_verdicts=[SliceVerdict(slice_id="s1", passed=False)],
        )

    async def useless_fix_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=stubborn_agent,
            fix_agent=useless_fix_agent,
            budget=AuditBudget(audit_retries=2),
        )
    )
    assert result.verdict == AuditVerdict.PARTIAL
    assert result.retries == 2


def test_run_audit_with_blocked_verdict_and_no_failing_slices(tmp_path: Path) -> None:
    """Verdict says blocked but slice_verdicts is empty → no actionable fix → return."""
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    async def vague_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.BLOCKED,
            narrative="something is wrong but I can't say which slice",
            slice_verdicts=[],
        )

    async def fix_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=vague_agent,
            fix_agent=fix_agent,
        )
    )
    assert result.verdict == AuditVerdict.BLOCKED
    assert result.retries == 0


# ---------------------------------------------------------------------------
# Walkthrough hook
# ---------------------------------------------------------------------------


def test_run_audit_walkthrough_artifacts_passed_to_agent(tmp_path: Path) -> None:
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    captured: dict = {}

    def fake_walkthrough(project_dir: Path, log_dir: Path, _timeout_s: int) -> WalkthroughResult:
        # Simulate writing a screenshot.
        artifact = log_dir / "screenshot-001.png"
        artifact.write_bytes(b"fake-png")
        return WalkthroughResult(
            succeeded=True, detail="walked through", artifacts=[artifact]
        )

    async def passing_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        captured["walkthrough_artifacts"] = list(input_.walkthrough_artifacts)
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="ok")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
            walkthrough=fake_walkthrough,
        )
    )
    assert result.verdict == AuditVerdict.PASSED
    assert len(captured["walkthrough_artifacts"]) == 1
    assert captured["walkthrough_artifacts"][0].name == "screenshot-001.png"
    assert len(result.walkthrough_artifacts) == 1


# ---------------------------------------------------------------------------
# _parse_audit_output
# ---------------------------------------------------------------------------


def test_parse_audit_output_fenced_json() -> None:
    text = """Some narrative text.
```json
{
  "verdict": "passed",
  "narrative": "all good",
  "slice_verdicts": [
    {"slice_id": "s1", "passed": true, "detail": "ok"},
    {"slice_id": "s2", "passed": false, "detail": "missing"}
  ]
}
```
End."""
    parsed = _parse_audit_output(text)
    assert parsed.verdict == AuditVerdict.PASSED
    assert parsed.narrative == "all good"
    assert len(parsed.slice_verdicts) == 2
    assert parsed.slice_verdicts[0].slice_id == "s1"
    assert parsed.slice_verdicts[0].passed is True
    assert parsed.slice_verdicts[1].passed is False


def test_parse_audit_output_partial_verdict() -> None:
    text = '```json\n{"verdict": "partial", "narrative": "x"}\n```'
    parsed = _parse_audit_output(text)
    assert parsed.verdict == AuditVerdict.PARTIAL


def test_parse_audit_output_blocked_verdict() -> None:
    text = '```json\n{"verdict": "blocked", "narrative": "x"}\n```'
    parsed = _parse_audit_output(text)
    assert parsed.verdict == AuditVerdict.BLOCKED


def test_parse_audit_output_malformed_returns_blocked() -> None:
    text = "not json at all"
    parsed = _parse_audit_output(text)
    assert parsed.verdict == AuditVerdict.BLOCKED
    assert "non-JSON" in parsed.narrative


def test_parse_audit_output_unknown_verdict_defaults_to_blocked() -> None:
    text = '```json\n{"verdict": "weird", "narrative": "x"}\n```'
    parsed = _parse_audit_output(text)
    assert parsed.verdict == AuditVerdict.BLOCKED


# ---------------------------------------------------------------------------
# Contract-test gate (otto.yaml test_command)
# ---------------------------------------------------------------------------


def _seed_otto_yaml(project_dir: Path, test_command: str) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    # Use a YAML-quoted string (single-quoted, escape internal single quotes
    # by doubling them per YAML 1.2). repr() uses Python quoting rules which
    # don't parse cleanly in YAML.
    quoted = "'" + test_command.replace("'", "''") + "'"
    (project_dir / "otto.yaml").write_text(
        f"default_branch: main\ntest_command: {quoted}\n",
        encoding="utf-8",
    )


def test_audit_runs_test_command_and_passes_when_zero_exit(tmp_path: Path) -> None:
    project_dir = tmp_path
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _seed_otto_yaml(project_dir, "python -c \"print('contract ok')\"")
    spec = _spec(["s1"])

    async def passing_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="walkthrough ok")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=project_dir,
            session_dir=session_dir,
            build_result=_build_result(["s1"], project_dir),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
        )
    )
    assert result.verdict == AuditVerdict.PASSED
    assert result.contract_test_passed is True
    assert "exit=0" in result.contract_test_detail


def test_audit_downgrades_passed_to_partial_when_contract_test_fails(tmp_path: Path) -> None:
    """The contract test OVERRIDES the LLM verdict — the audit can't claim
    PASSED when the project's own test_command is failing.
    """
    project_dir = tmp_path
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _seed_otto_yaml(project_dir, "python -c \"import sys; sys.exit(1)\"")
    spec = _spec(["s1"])

    async def overconfident_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="all great according to me",
        )

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=project_dir,
            session_dir=session_dir,
            build_result=_build_result(["s1"], project_dir),
            merge_result=_merge_result(["s1"]),
            audit_agent=overconfident_agent,
        )
    )
    assert result.verdict == AuditVerdict.PARTIAL
    assert result.contract_test_passed is False
    assert "[contract test FAILED]" in result.narrative
    assert "exit=1" in result.contract_test_detail


def test_audit_no_test_command_keeps_llm_verdict(tmp_path: Path) -> None:
    """When otto.yaml has no test_command, the contract gate is a no-op."""
    project_dir = tmp_path
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    # No otto.yaml at all.
    spec = _spec(["s1"])

    async def passing_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="ok")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=project_dir,
            session_dir=session_dir,
            build_result=_build_result(["s1"], project_dir),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
        )
    )
    assert result.verdict == AuditVerdict.PASSED
    assert result.contract_test_passed is None
    assert "no test_command" in result.contract_test_detail


def test_audit_writes_contract_test_log(tmp_path: Path) -> None:
    project_dir = tmp_path
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _seed_otto_yaml(project_dir, "python -c \"print('hello-from-test_command')\"")
    spec = _spec(["s1"])

    async def passing_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="ok")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=project_dir,
            session_dir=session_dir,
            build_result=_build_result(["s1"], project_dir),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
        )
    )
    log_path = session_dir / "audit" / "attempt-00" / "contract" / "test_command.log"
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "hello-from-test_command" in contents
    assert result.verdict == AuditVerdict.PASSED


# ---------------------------------------------------------------------------
# default_walkthrough_from_spec — production wiring
# ---------------------------------------------------------------------------


def test_default_walkthrough_no_browser_journey_non_webapp_returns_no_op(tmp_path: Path) -> None:
    """Non-webapp spec without a BrowserJourney → callable returns
    no-op with a clear diagnostic. webapp kinds get a synthesized
    walkthrough (see test below).
    """
    from otto.spec_compile import PytestCheck

    spec = Spec(
        intent="x",
        project_kind="cli",
        slices=[Slice(id="s", title="t", checks=[PytestCheck(selector="x")])],
    )
    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    assert result.succeeded is True
    assert result.artifacts == []
    assert "no synthesized fallback" in result.detail


def test_default_walkthrough_no_browser_journey_webapp_synthesizes(tmp_path: Path) -> None:
    """Webapp spec without a BrowserJourney → synthesized walkthrough
    boots the app via create_app and hits /. v2 phase 3: audit verdict
    must NEVER come from 'LLM read code' alone for webapps.
    """
    from otto.spec_compile import PytestCheck

    spec = Spec(
        intent="x",
        project_kind="webapp",
        slices=[Slice(id="s", title="t", checks=[PytestCheck(selector="x")])],
    )
    # Seed a minimal Flask app at the project root.
    (tmp_path / "app.py").write_text(
        "from flask import Flask\n"
        "def create_app(config=None):\n"
        "    app = Flask(__name__)\n"
        "    @app.get('/')\n"
        "    def home(): return '<h1>Hello synthesized walkthrough</h1>'\n"
        "    return app\n"
    )
    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    assert result.succeeded is True
    # log + body artifacts present
    assert len(result.artifacts) >= 1
    # The synthesized log captures the home-page response.
    log_text = (tmp_path / "log" / "synthesized-webapp.log").read_text()
    assert "Hello synthesized walkthrough" in log_text or '"status": 200' in log_text


def test_default_walkthrough_picks_cross_slice_journey_first(tmp_path: Path) -> None:
    """When both cross-slice and slice-level BrowserJourney exist, the
    cross-slice one wins (it's the integrated test)."""
    from otto.spec_compile import BrowserJourney

    cross = BrowserJourney(command=("echo", "cross"), evidence_globs=("c-*.png",))
    slice_journey = BrowserJourney(command=("echo", "slice"), evidence_globs=("s-*.png",))
    spec = Spec(
        intent="x",
        cross_slice_checks=[cross],
        slices=[Slice(id="s", title="t", checks=[slice_journey])],
    )
    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    # Subprocess ran (echo exits 0); detail comes from BrowserJourney runner.
    assert result.succeeded is True
    log_path = tmp_path / "log" / "browser-journey.log"
    assert log_path.exists()
    assert "cross" in log_path.read_text(encoding="utf-8")


def test_default_walkthrough_falls_back_to_slice_journey(tmp_path: Path) -> None:
    """No cross-slice BrowserJourney → first slice's BrowserJourney is used."""
    from otto.spec_compile import BrowserJourney

    slice_journey = BrowserJourney(command=("echo", "slice"), evidence_globs=("*.png",))
    spec = Spec(
        intent="x",
        slices=[Slice(id="s", title="t", checks=[slice_journey])],
    )
    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    assert result.succeeded is True
    log_path = tmp_path / "log" / "browser-journey.log"
    assert log_path.exists()
    assert "slice" in log_path.read_text(encoding="utf-8")
