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
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from otto.audit import (
    AuditAgentInput,
    AuditAgentOutput,
    AuditBudget,
    AuditVerdict,
    FeatureAudit,
    GroupVerdict,
    WalkthroughResult,
    _audit_prompt,
    _fallback_contract_test_argv,
    _parse_audit_output,
    _run_project_contract_test,
    _write_audit_evidence_packet,
    default_walkthrough_from_spec,
    run_audit,
)
from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildResult,
    ContractDelta,
    GroupResult,
    GroupStatus,
)
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.spec_compile import (
    BehaviorJourney,
    BehaviorStep,
    Feature,
    Group,
    SharedContract,
    Spec,
    StateInvariant,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(group_ids: list[str], cross_checks=None) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(id=sid, name=sid.upper(), dependencies=[], owned_paths=[], feature_ids=[], checks=[])
            for sid in group_ids
        ],
        cross_group_checks=cross_checks or [],
    )


def _build_result(passing_ids: list[str], project_dir: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=project_dir,
        group_results=[
            GroupResult(
                group_id=sid,
                status=GroupStatus.PASSING,
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
            MergeResult(group_id=sid, status=MergeStatus.LANDED, landed_commit="abc1234")
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
            group_verdicts=[
                GroupVerdict(group_id="s1", passed=True),
                GroupVerdict(group_id="s2", passed=True),
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
    assert len(result.group_verdicts) == 2


def test_run_audit_allocates_new_attempt_dir_across_calls(tmp_path: Path) -> None:
    """Separate re-audit calls in one session must not overwrite artifacts."""
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    seen_log_dirs: list[str] = []

    async def passing_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        assert input_.log_dir is not None
        input_.log_dir.mkdir(parents=True, exist_ok=True)
        (input_.log_dir / "marker.txt").write_text("ok", encoding="utf-8")
        seen_log_dirs.append(input_.log_dir.relative_to(session_dir).as_posix())
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="all good",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True)],
        )

    for _ in range(2):
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

    assert seen_log_dirs == ["audit/attempt-00/judge", "audit/attempt-01/judge"]
    assert (session_dir / "audit" / "attempt-00" / "judge" / "marker.txt").exists()
    assert (session_dir / "audit" / "attempt-01" / "judge" / "marker.txt").exists()
    events = [
        json.loads(line)
        for line in (session_dir / "spec-state.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    attempts = [
        event["attempt"]
        for event in events
        if event.get("kind") == "audit.attempt.finished"
    ]
    assert attempts == [0, 1]


def test_run_audit_feature_scope_filters_feature_audits(tmp_path: Path) -> None:
    """Layer-2 re-audit scope returns only affected Feature verdicts."""
    spec = _spec(["s1"])
    spec.features = [
        Feature(id="f1", name="Feature one", group_id="s1"),
        Feature(id="f2", name="Feature two", group_id="s1"),
    ]
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    seen_scope: tuple[str, ...] | None = None

    async def scoped_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        nonlocal seen_scope
        seen_scope = input_.feature_scope_ids
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="scoped",
            feature_audits=[
                FeatureAudit(feature_id="f1", name="Feature one", status="passed"),
                FeatureAudit(feature_id="f2", name="Feature two", status="blocked"),
            ],
        )

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=scoped_agent,
            feature_scope_ids=["f1"],
        )
    )

    assert seen_scope == ("f1",)
    assert [fa.feature_id for fa in result.feature_audits] == ["f1"]


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
                group_verdicts=[
                    GroupVerdict(group_id="s1", passed=True),
                    GroupVerdict(group_id="s2", passed=True),
                ],
            )
        return AuditAgentOutput(
            verdict=AuditVerdict.PARTIAL,
            narrative="s1 broken",
            group_verdicts=[
                GroupVerdict(group_id="s1", passed=False, detail="missing route"),
                GroupVerdict(group_id="s2", passed=True),
            ],
        )

    async def fix_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        fix_calls.append(input_.group.id)
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


def test_run_audit_brownfield_fix_repairs_integrated_worktree(tmp_path: Path) -> None:
    """Brownfield improve has no build slice branch; repair runs on main."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@otto.local"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Otto Tester"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    (tmp_path / "app.js").write_text("reset = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.js"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    generated = tmp_path / ".playwright-cli" / "page.yml"
    generated.parent.mkdir()
    generated.write_text("runtime artifact\n", encoding="utf-8")

    spec = _spec(["counter"])
    spec.groups[0].owned_paths = ["app.js"]
    session_dir = tmp_path / "otto_logs" / "sessions" / "test-session"
    session_dir.mkdir(parents=True)
    fix_calls: list[Path] = []

    async def audit_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        fixed = "reset = 0" in (tmp_path / "app.js").read_text(encoding="utf-8")
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED if fixed else AuditVerdict.BLOCKED,
            narrative="ok" if fixed else "reset broken",
            group_verdicts=[GroupVerdict(group_id="counter", passed=fixed)],
        )

    async def fix_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        fix_calls.append(input_.worktree)
        (input_.worktree / "app.js").write_text("reset = 0\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, detail="fixed")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=BuildResult(spec_session_dir=session_dir, group_results=[]),
            merge_result=_merge_result([]),
            audit_agent=audit_agent,
            fix_agent=fix_agent,
            budget=AuditBudget(audit_retries=1),
            base_branch="main",
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert fix_calls == [tmp_path]
    assert "reset = 0" in (tmp_path / "app.js").read_text(encoding="utf-8")
    committed_files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert committed_files == ["app.js"]
    journal = (session_dir / "spec-state.jsonl").read_text(encoding="utf-8")
    assert '"kind": "group.merge.landed"' in journal


def test_run_audit_skips_downstream_repair_when_dependency_blocked(
    tmp_path: Path,
) -> None:
    spec = _spec(["counter_app", "counter_tests"])
    spec.groups[1].dependencies = ["counter_app"]
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    fix_calls: list[str] = []

    async def audit_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.BLOCKED,
            narrative="app and tests blocked",
            group_verdicts=[
                GroupVerdict(group_id="counter_app", passed=False, detail="app missing"),
                GroupVerdict(group_id="counter_tests", passed=False, detail="tests missing app"),
            ],
        )

    async def fix_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        fix_calls.append(input_.group.id)
        return BuildAgentOutput(succeeded=False, detail="app still blocked")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["counter_app", "counter_tests"], tmp_path),
            merge_result=MergeQueueResult(blocked_ids=["counter_app"]),
            audit_agent=audit_agent,
            fix_agent=fix_agent,
            budget=AuditBudget(audit_retries=1),
        )
    )

    assert result.verdict == AuditVerdict.BLOCKED
    assert fix_calls == ["counter_app"]
    journal = (session_dir / "spec-state.jsonl").read_text(encoding="utf-8")
    assert "repair skipped because dependency group(s) are blocked: counter_app" in journal


def test_run_audit_no_fix_agent_returns_first_verdict(tmp_path: Path) -> None:
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    async def partial_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PARTIAL,
            narrative="s1 has issues",
            group_verdicts=[GroupVerdict(group_id="s1", passed=False, detail="x")],
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
            group_verdicts=[GroupVerdict(group_id="s1", passed=False)],
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
    """Verdict says blocked but group_verdicts is empty → no actionable fix → return."""
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()

    async def vague_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.BLOCKED,
            narrative="something is wrong but I can't say which slice",
            group_verdicts=[],
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


def test_run_audit_writes_compact_evidence_packet_for_judge(tmp_path: Path) -> None:
    (tmp_path / "otto.yaml").write_text("", encoding="utf-8")
    session_dir = tmp_path / "session"
    spec_dir = session_dir / "spec"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "spec.json"
    spec_path.write_text(json.dumps({"intent": "test intent"}), encoding="utf-8")
    captured: dict[str, str] = {}

    async def passing_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        captured["prompt"] = _audit_prompt(input_)
        captured["packet_path"] = str(input_.evidence_packet_path)
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="ok")

    asyncio.run(
        run_audit(
            _spec(["s1"]),
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
            budget=AuditBudget(audit_retries=0),
        )
    )

    packet_path = Path(captured["packet_path"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["kind"] == "audit_evidence_packet"
    assert packet["full_spec_path"] == str(spec_path)
    assert packet["deterministic_first_order"][:4] == [
        "contract_test",
        "cross_slice_evidence",
        "planned_behavior_journeys",
        "walkthrough_artifacts",
    ]
    assert "messages.jsonl" in " ".join(packet["notes"])
    assert str(packet_path) in captured["prompt"]
    assert "Do not bulk-read `messages.jsonl`" in captured["prompt"]
    assert "Do not run broad `rg`, `find`, `cat`, or `sed` sweeps" in captured["prompt"]
    assert "`node_modules/**`" in captured["prompt"]
    assert "`dist/assets/**`" in captured["prompt"]
    assert "Deterministic-first rule" in captured["prompt"]


def test_audit_prompt_and_packet_include_planned_behavior_and_shared_contracts(
    tmp_path: Path,
) -> None:
    spec = _spec(["feed"])
    spec.features = [Feature(id="compose-post", name="Compose post", group_id="feed")]
    spec.behavior_journeys = [
        BehaviorJourney(
            id="planned-main-user-flow",
            name="Main feed flow",
            feature_ids=["compose-post"],
            steps=[
                BehaviorStep(
                    action="Type a post and submit it.",
                    expectation="The post appears in the feed.",
                    assertion="The submitted text is visible after refresh.",
                    feature_ids=["compose-post"],
                )
            ],
        )
    ]
    spec.shared_contracts = [
        SharedContract(
            id="shared-product-core",
            name="Shared product core",
            owner_id="feed",
            paths=["src/store/**"],
            invariants=["Feed state persists across refresh."],
        )
    ]
    packet_path = tmp_path / "evidence-packet.json"
    input_ = AuditAgentInput(
        spec=spec,
        project_dir=tmp_path,
        integrated_worktree=tmp_path,
        build_summary={
            "contract_deltas": [
                ContractDelta(
                    group_id="feed",
                    contract_id="shared-product-core",
                    owner_id="foundation",
                    paths=["src/store/index.ts"],
                    invariants=["Feed state persists across refresh."],
                ).to_dict()
            ]
        },
        merge_summary={},
        cross_slice_evidence=[],
        walkthrough_artifacts=[],
        evidence_packet_path=packet_path,
    )
    _write_audit_evidence_packet(input_)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    prompt = _audit_prompt(input_)

    assert packet["planned_behavior_journeys"][0]["steps"][0]["feature_ids"] == [
        "compose-post"
    ]
    assert packet["shared_contracts"][0]["paths"] == ["src/store/**"]
    assert "Planned behavior journeys" in prompt
    assert "Type a post and submit it." in prompt
    assert "The post appears in the feed." in prompt
    assert "Shared contracts to inspect" in prompt
    assert "Feed state persists across refresh." in prompt
    assert "Contract deltas to verify" in prompt
    assert "Project contract test" in prompt


def test_run_audit_expands_group_feature_ids_for_audit_prompt(tmp_path: Path) -> None:
    spec = Spec(
        intent="build a tiny feed",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="timeline",
                name="Timeline",
                dependencies=[],
                owned_paths=[],
                feature_ids=["create posts", "newest-first ordering"],
                checks=[],
            )
        ],
        features=[],
        cross_group_checks=[],
    )
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    captured: dict[str, Any] = {}

    async def passing_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        captured["feature_ids"] = [feature.id for feature in input_.spec.features]
        captured["prompt"] = _audit_prompt(input_)
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="ok",
            feature_audits=[
                FeatureAudit(
                    feature_id=feature.id,
                    name=feature.name,
                    status="passed",
                    detail="ok",
                )
                for feature in input_.spec.features
            ],
        )

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["timeline"], tmp_path),
            merge_result=_merge_result(["timeline"]),
            audit_agent=passing_agent,
            budget=AuditBudget(audit_retries=0),
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert captured["feature_ids"] == ["create posts", "newest-first ordering"]
    assert "## Spec Features" in captured["prompt"]
    assert "- create posts: create posts (group timeline)" in captured["prompt"]


def test_run_audit_resolves_relative_session_paths_for_judge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    project_dir = Path("project")
    project_dir.mkdir()
    session_dir = Path("relative-session")
    captured: dict[str, Any] = {}

    async def passing_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        captured["project_dir"] = input_.project_dir
        captured["log_dir"] = input_.log_dir
        captured["walkthrough_jsonl_path"] = input_.walkthrough_jsonl_path
        captured["evidence_packet_path"] = input_.evidence_packet_path
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="ok")

    result = asyncio.run(
        run_audit(
            _spec(["s1"]),
            project_dir=project_dir,
            session_dir=session_dir,
            build_result=_build_result(["s1"], project_dir),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
            budget=AuditBudget(audit_retries=0),
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert captured["project_dir"].is_absolute()
    assert captured["log_dir"].is_absolute()
    assert captured["walkthrough_jsonl_path"].is_absolute()
    assert captured["evidence_packet_path"].is_absolute()


def test_run_audit_passes_judge_timeout_to_agent_input(tmp_path: Path) -> None:
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    seen: dict[str, int | None] = {}

    async def timeout_aware_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        seen["timeout"] = input_.judge_timeout_s
        return AuditAgentOutput(verdict=AuditVerdict.PASSED, narrative="ok")

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=timeout_aware_agent,
            budget=AuditBudget(judge_timeout_s=17),
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert seen["timeout"] == 17


def test_run_audit_threads_resume_session_to_judge(tmp_path: Path) -> None:
    spec = _spec(["s1"])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    seen: dict[str, str] = {}

    async def resume_aware_agent(input_: AuditAgentInput) -> AuditAgentOutput:
        seen["session"] = input_.agent_session_id
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="ok",
            session_id="audit-thread-next",
        )

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=resume_aware_agent,
            budget=AuditBudget(audit_retries=0),
            resume_agent_session_id="audit-thread-prior",
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert seen["session"] == "audit-thread-prior"
    assert result.agent_session_id == "audit-thread-next"


def test_default_audit_agent_uses_judge_timeout_from_input(tmp_path: Path, monkeypatch) -> None:
    from otto.audit import default_audit_agent

    captured: dict[str, int | None] = {}

    def fake_make_agent_options(*_args, **_kwargs):
        return SimpleNamespace(cwd="", permission_mode="")

    async def fake_run_agent_with_timeout(*_args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return (
            '```json\n{"verdict":"passed","narrative":"ok","quality_score":3}\n```',
            0.0,
            "session-1",
            {},
        )

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_agent_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    result = asyncio.run(
        default_audit_agent(
            AuditAgentInput(
                spec=_spec(["s1"]),
                project_dir=tmp_path,
                integrated_worktree=tmp_path,
                build_summary={},
                merge_summary={},
                cross_slice_evidence=[],
                walkthrough_artifacts=[],
                config={"agents": {}},
                judge_timeout_s=23,
            )
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert captured["timeout"] == 23


def test_default_audit_agent_passes_resume_session(tmp_path: Path, monkeypatch) -> None:
    from otto.audit import default_audit_agent

    options = SimpleNamespace(cwd="", permission_mode="", resume="")
    captured: dict[str, str] = {}

    def fake_make_agent_options(*_args, **_kwargs):
        return options

    async def fake_run_agent_with_timeout(_prompt, seen_options, **_kwargs):
        captured["resume"] = seen_options.resume
        return (
            '```json\n{"verdict":"passed","narrative":"ok","quality_score":3}\n```',
            0.0,
            "audit-thread-next",
            {},
        )

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_agent_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    result = asyncio.run(
        default_audit_agent(
            AuditAgentInput(
                spec=_spec(["s1"]),
                project_dir=tmp_path,
                integrated_worktree=tmp_path,
                build_summary={},
                merge_summary={},
                cross_slice_evidence=[],
                walkthrough_artifacts=[],
                config={"agents": {}},
                agent_session_id="audit-thread-prior",
            )
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert captured["resume"] == "audit-thread-prior"
    assert result.session_id == "audit-thread-next"


def test_default_audit_agent_prefers_structured_output(tmp_path: Path, monkeypatch) -> None:
    from otto.audit import default_audit_agent

    options = SimpleNamespace(cwd="", permission_mode="")

    def fake_make_agent_options(*_args, **_kwargs):
        return options

    async def fake_run_agent_with_timeout(_prompt, seen_options, **_kwargs):
        assert seen_options is options
        assert options.output_format["json_schema"]["name"] == "otto_audit_result"
        return (
            "this is deliberately not JSON",
            0.0,
            "session-1",
            {
                "structured_output": {
                    "verdict": "passed",
                    "narrative": "structured ok",
                    "group_verdicts": [
                        {"group_id": "g1", "passed": True, "detail": "ok"}
                    ],
                    "feature_audits": [
                        {
                            "feature_id": "f1",
                            "name": "Feature 1",
                            "status": "passed",
                            "detail": "ok",
                            "evidence_refs": ["walkthrough.jsonl#L1"],
                        }
                    ],
                    "quality_score": 4,
                    "quality_findings": ["usable"],
                }
            },
        )

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_agent_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    result = asyncio.run(
        default_audit_agent(
            AuditAgentInput(
                spec=_spec(["f1"]),
                project_dir=tmp_path,
                integrated_worktree=tmp_path,
                build_summary={},
                merge_summary={},
                cross_slice_evidence=[],
                walkthrough_artifacts=[],
                config={"agents": {}},
                judge_timeout_s=23,
            )
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert result.narrative == "structured ok"
    assert result.group_verdicts[0].group_id == "g1"
    assert result.feature_audits[0].feature_id == "f1"
    assert result.quality_score == 4


def test_default_audit_agent_sets_search_guard_env(tmp_path: Path, monkeypatch) -> None:
    from otto.audit import default_audit_agent

    captured: dict[str, Any] = {}

    def fake_make_agent_options(*_args, **_kwargs):
        return SimpleNamespace(cwd="", permission_mode="", env={"PATH": "/bin"})

    async def fake_run_agent_with_timeout(_prompt, options, **_kwargs):
        captured["env"] = options.env
        return (
            '```json\n{"verdict":"passed","narrative":"ok","quality_score":3}\n```',
            0.0,
            "session-1",
            {},
        )

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_agent_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    result = asyncio.run(
        default_audit_agent(
            AuditAgentInput(
                spec=_spec(["s1"]),
                project_dir=tmp_path,
                integrated_worktree=tmp_path,
                build_summary={},
                merge_summary={},
                cross_slice_evidence=[],
                walkthrough_artifacts=[],
                log_dir=tmp_path / "audit" / "attempt-00" / "judge",
                config={"agents": {}},
            )
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    env = captured["env"]
    assert env["PATH"] == "/bin"
    config_path = Path(env["RIPGREP_CONFIG_PATH"])
    assert config_path.exists()
    config_text = config_path.read_text(encoding="utf-8")
    assert "--glob=!**/otto_logs/**" in config_text
    assert "--glob=!**/_otto_build_logs/**" in config_text
    assert "--glob=!**/messages.jsonl" in config_text


def test_default_audit_agent_recovers_complete_feature_verdicts_on_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    from otto.agent import AgentCallError
    from otto.audit import default_audit_agent

    spec = Spec(
        intent="build a feed",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="ui",
                name="UI",
                dependencies=[],
                owned_paths=[],
                feature_ids=["username", "timeline"],
                checks=[],
            )
        ],
        features=[
            Feature(id="username", name="Persist username", group_id="ui"),
            Feature(id="timeline", name="Newest-first timeline", group_id="ui"),
        ],
        cross_group_checks=[],
    )
    attempt_dir = tmp_path / "audit" / "attempt-00"
    (attempt_dir / "judge").mkdir(parents=True)
    (attempt_dir / "walkthrough").mkdir()
    (attempt_dir / "feature-verdicts.json").write_text(
        json.dumps({
            "schema_version": 1,
            "verdicts": [
                {
                    "feature_id": "username",
                    "verdict": "passed",
                    "detail": "username persisted",
                    "evidence_refs": ["walkthrough/walkthrough.jsonl#L2"],
                },
                {
                    "feature_id": "timeline",
                    "verdict": "passed",
                    "detail": "timeline sorted newest first",
                    "evidence_refs": ["walkthrough/walkthrough.jsonl#L3"],
                },
            ],
        }),
        encoding="utf-8",
    )

    def fake_make_agent_options(*_args, **_kwargs):
        return SimpleNamespace(cwd="", permission_mode="")

    async def fake_run_agent_with_timeout(*_args, **_kwargs):
        raise AgentCallError("Agent timed out after 600s", total_cost_usd=1.25)

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_agent_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    result = asyncio.run(
        default_audit_agent(
            AuditAgentInput(
                spec=spec,
                project_dir=tmp_path,
                integrated_worktree=tmp_path,
                build_summary={},
                merge_summary={},
                cross_slice_evidence=[],
                walkthrough_artifacts=[],
                log_dir=attempt_dir / "judge",
                walkthrough_jsonl_path=attempt_dir / "walkthrough" / "walkthrough.jsonl",
                config={"agents": {}},
                judge_timeout_s=600,
            )
        )
    )

    assert result.verdict == AuditVerdict.PASSED
    assert result.cost_usd == 1.25
    assert [audit.feature_id for audit in result.feature_audits] == ["username", "timeline"]
    assert result.group_verdicts[0].group_id == "ui"
    assert result.group_verdicts[0].passed is True
    assert result.quality_score == 3
    assert "recovered 2 complete Feature verdict" in result.narrative


def test_default_audit_agent_does_not_pass_incomplete_recovered_verdicts(
    tmp_path: Path, monkeypatch
) -> None:
    from otto.agent import AgentCallError
    from otto.audit import default_audit_agent

    spec = Spec(
        intent="build a feed",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[Group(id="ui", name="UI", feature_ids=["username", "timeline"])],
        features=[
            Feature(id="username", name="Persist username", group_id="ui"),
            Feature(id="timeline", name="Newest-first timeline", group_id="ui"),
        ],
    )
    attempt_dir = tmp_path / "audit" / "attempt-00"
    (attempt_dir / "judge").mkdir(parents=True)
    (attempt_dir / "walkthrough").mkdir()
    (attempt_dir / "feature-verdicts.json").write_text(
        json.dumps({
            "schema_version": 1,
            "verdicts": [
                {
                    "feature_id": "username",
                    "verdict": "passed",
                    "detail": "username persisted",
                    "evidence_refs": ["walkthrough/walkthrough.jsonl#L2"],
                }
            ],
        }),
        encoding="utf-8",
    )

    def fake_make_agent_options(*_args, **_kwargs):
        return SimpleNamespace(cwd="", permission_mode="")

    async def fake_run_agent_with_timeout(*_args, **_kwargs):
        raise AgentCallError("Agent timed out after 600s")

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_agent_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)

    result = asyncio.run(
        default_audit_agent(
            AuditAgentInput(
                spec=spec,
                project_dir=tmp_path,
                integrated_worktree=tmp_path,
                build_summary={},
                merge_summary={},
                cross_slice_evidence=[],
                walkthrough_artifacts=[],
                log_dir=attempt_dir / "judge",
                walkthrough_jsonl_path=attempt_dir / "walkthrough" / "walkthrough.jsonl",
                config={"agents": {}},
                judge_timeout_s=600,
            )
        )
    )

    assert result.verdict == AuditVerdict.BLOCKED
    assert "feature-verdicts.json was incomplete" in result.narrative
    assert "timeline" in result.narrative


def test_audit_prompt_includes_timeout_stop_rule(tmp_path: Path) -> None:
    prompt = _audit_prompt(
        AuditAgentInput(
            spec=_spec(["s1"]),
            project_dir=tmp_path,
            integrated_worktree=tmp_path,
            build_summary={},
            merge_summary={},
            cross_slice_evidence=[],
            walkthrough_artifacts=[],
            judge_timeout_s=300,
            evidence_packet_path=tmp_path / "evidence-packet.json",
        )
    )

    assert "hard 300s timeout" in prompt
    assert "required fenced JSON verdict" in prompt
    assert "return `partial` with the specific missing evidence instead of timing out" in prompt
    assert "translating those observed actions into tagged `walkthrough.jsonl` entries" in prompt


def test_audit_default_judge_timeout_allows_real_web_audit() -> None:
    assert AuditBudget().judge_timeout_s == 600


# ---------------------------------------------------------------------------
# _parse_audit_output
# ---------------------------------------------------------------------------


def test_parse_audit_output_fenced_json() -> None:
    text = """Some narrative text.
```json
{
  "verdict": "passed",
  "narrative": "all good",
  "group_verdicts": [
    {"group_id": "s1", "passed": true, "detail": "ok"},
    {"group_id": "s2", "passed": false, "detail": "missing"}
  ]
}
```
End."""
    parsed = _parse_audit_output(text)
    assert parsed.verdict == AuditVerdict.PASSED
    assert parsed.narrative == "all good"
    assert len(parsed.group_verdicts) == 2
    assert parsed.group_verdicts[0].group_id == "s1"
    assert parsed.group_verdicts[0].passed is True
    assert parsed.group_verdicts[1].passed is False


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


def test_contract_test_tox_falls_back_to_uvx_when_tox_missing(
    tmp_path: Path, monkeypatch
) -> None:
    import shutil
    original_which = shutil.which

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_path = tmp_path / "uvx.args"
    uvx = fake_bin / "uvx"
    uvx.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" > '{args_path}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uvx.chmod(0o755)

    def fake_which(cmd: str, path: str | None = None) -> str | None:
        if cmd == "tox":
            return None
        if cmd == "uvx":
            return str(uvx)
        return original_which(cmd, path=path)

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setenv("PATH", str(fake_bin))
    _seed_otto_yaml(tmp_path, "tox -e py")

    passed, detail = _run_project_contract_test(
        tmp_path,
        log_dir=tmp_path / "contract",
    )

    assert passed is True
    assert "uvx --with tox-uv tox -e py" in detail
    assert "fallback from 'tox -e py'" in detail
    assert args_path.read_text(encoding="utf-8").strip() == "--with tox-uv tox -e py"


def test_contract_test_pytest_retries_otto_runtime_on_import_env_failure(
    tmp_path: Path, monkeypatch
) -> None:
    import sys

    user_bin = tmp_path / "user-bin"
    otto_bin = tmp_path / "otto-bin"
    user_bin.mkdir()
    otto_bin.mkdir()
    user_pytest = user_bin / "pytest"
    user_pytest.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"ImportError while loading conftest '/tmp/project/tests/conftest.py'.\" >&2\n"
        "printf '%s\\n' \"E   ModuleNotFoundError: No module named 'fastapi'\" >&2\n"
        "exit 4\n",
        encoding="utf-8",
    )
    user_pytest.chmod(0o755)
    otto_python = otto_bin / "python"
    otto_python.write_text(
        "#!/bin/sh\n"
        "test \"$1\" = '-m' && test \"$2\" = 'pytest' || exit 99\n"
        "printf 'runtime pytest passed\\n'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    otto_python.chmod(0o755)

    monkeypatch.setenv("PATH", str(user_bin))
    monkeypatch.setattr(sys, "executable", str(otto_python))
    _seed_otto_yaml(tmp_path, "pytest tests/visible -q")

    passed, detail = _run_project_contract_test(
        tmp_path,
        log_dir=tmp_path / "contract",
    )

    assert passed is True
    assert f"{otto_python} -m pytest tests/visible -q" in detail
    assert "retried with Otto runtime after pytest import failure" in detail
    assert "runtime pytest passed" in detail


def test_contract_test_tox_fallback_not_used_when_tox_exists() -> None:
    def fake_which(cmd: str, path: str | None = None) -> str | None:
        if cmd == "tox":
            return "/usr/bin/tox"
        if cmd == "uvx":
            return "/usr/bin/uvx"
        return None

    assert _fallback_contract_test_argv(
        ["tox"],
        env={"PATH": "/usr/bin"},
        which=fake_which,
    ) is None


# ---------------------------------------------------------------------------
# default_walkthrough_from_spec — production wiring
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Audit-final-quality: prompt asks for quality, parser reads it,
# verdict caps at PARTIAL when quality_score < 3
# ---------------------------------------------------------------------------


def test_audit_prompt_requests_quality_assessment(tmp_path: Path) -> None:
    """The audit prompt MUST ask for quality_score + quality_findings.
    A functionally-correct but bare-bones product (forms stacked
    vertically, no styling, no nav) should not be allowed to claim
    PASSED — the audit prompt now requires a concrete quality grade."""
    from otto.audit import _audit_prompt

    spec = Spec(intent="x", project_kind="webapp",
                groups=[Group(id="s", name="t")])
    inp = AuditAgentInput(
        spec=spec, project_dir=tmp_path, integrated_worktree=tmp_path,
        build_summary={}, merge_summary={}, cross_slice_evidence=[],
        walkthrough_artifacts=[],
    )
    prompt = _audit_prompt(inp)
    assert "quality_score" in prompt
    assert "quality_findings" in prompt
    assert "1-5" in prompt or "1 to 5" in prompt
    # Must include quality criteria for the project_kind dimensions.
    assert "webapp" in prompt
    assert "static-site" in prompt or "blog" in prompt
    # Calibration anchors (anti-grade-inflation, root fix found by
    # observing audit gave 4/5 to bare-bones products). MVP must
    # be explicitly the default 3/5 score.
    assert "MVP" in prompt
    assert "3/5" in prompt or "3 = MVP" in prompt or "3=MVP" in prompt
    # Required minimum-2-findings rule.
    assert "at least 2" in prompt or "Empty list is NOT" in prompt
    assert "horizontal overflow" in prompt
    assert "clipped primary controls" in prompt
    assert "affected Feature MUST be marked partial or blocked" in prompt


def test_audit_prompt_requires_exact_edge_case_evidence(tmp_path: Path) -> None:
    from otto.audit import _audit_prompt

    spec = Spec(
        intent="invalid comma strings should be returned unchanged",
        project_kind="library",
        groups=[Group(id="number", name="Number")],
        features=[
            Feature(
                id="intword",
                name="intword",
                group_id="number",
                description="parse comma-separated numeric strings",
            )
        ],
    )
    inp = AuditAgentInput(
        spec=spec,
        project_dir=tmp_path,
        integrated_worktree=tmp_path,
        build_summary={},
        merge_summary={},
        cross_slice_evidence=[],
        walkthrough_artifacts=[],
    )

    prompt = _audit_prompt(inp)

    assert "exact acceptance examples" in prompt
    assert "edge/error cases" in prompt
    assert "different invalid value" in prompt
    assert "same changed parser/normalizer/validation path" in prompt
    assert "focused regression test" in prompt
    assert "Tests, docstrings, or comments added by the repair agent" in prompt
    assert "NOT allowed to redefine that contract" in prompt
    assert "contradicts the user's intent" in prompt
    assert "exact string equality with the original input" in prompt
    assert "including punctuation/separators" in prompt
    assert "native test/lint command actually runs doctests" in prompt


def test_audit_parser_reads_quality_fields() -> None:
    """The parser must read quality_score + quality_findings from JSON."""
    from otto.audit import _parse_audit_output

    output = """```json
{
  "verdict": "passed",
  "narrative": "all good",
  "group_verdicts": [],
  "quality_score": 2,
  "quality_findings": [
    "home page has no nav bar",
    "forms have no labels"
  ]
}
```"""
    result = _parse_audit_output(output)
    assert result.quality_score == 2
    assert result.quality_findings == [
        "home page has no nav bar",
        "forms have no labels",
    ]


def test_audit_parser_clamps_quality_score() -> None:
    """Score outside 1-5 clamped; absent → 0 (not assessed)."""
    from otto.audit import _parse_audit_output

    high = _parse_audit_output('```json\n{"verdict":"passed","quality_score":9}\n```')
    assert high.quality_score == 5
    low = _parse_audit_output('```json\n{"verdict":"passed","quality_score":-3}\n```')
    assert low.quality_score == 0  # max(0, min(5, -3)) = 0
    absent = _parse_audit_output('```json\n{"verdict":"passed"}\n```')
    assert absent.quality_score == 0
    assert absent.quality_findings == []


def test_compose_verdict_caps_severe_quality_findings_to_partial() -> None:
    from otto.audit import _compose_verdict
    from otto.spec_amend import ChainVerification

    agent_output = AuditAgentOutput(
        verdict=AuditVerdict.PASSED,
        narrative="functional pass",
        group_verdicts=[],
        feature_audits=[],
        quality_score=3,
        quality_findings=[
            "At 390px viewport width, the filter bar overflows horizontally: "
            "document scrollWidth was 662 against innerWidth 390, and the "
            "Assignee control is clipped.",
            "The row click affordance could be clearer.",
        ],
    )

    verdict, narrative = _compose_verdict(
        agent_output=agent_output,
        contract_passed=True,
        contract_detail="",
        chain_review=ChainVerification(verdict_cap="passed", findings=[]),
    )

    assert verdict == AuditVerdict.PARTIAL
    assert "quality severity cap" in narrative
    assert "overflows horizontally" in narrative


def test_compose_verdict_does_not_cap_negated_quality_terms() -> None:
    from otto.audit import _compose_verdict
    from otto.spec_amend import ChainVerification

    agent_output = AuditAgentOutput(
        verdict=AuditVerdict.PASSED,
        narrative="functional pass",
        quality_score=3,
        quality_findings=[
            "No horizontal overflow was observed at 390px.",
            "The visual hierarchy could be stronger.",
        ],
    )

    verdict, narrative = _compose_verdict(
        agent_output=agent_output,
        contract_passed=True,
        contract_detail="",
        chain_review=ChainVerification(verdict_cap="passed", findings=[]),
    )

    assert verdict == AuditVerdict.PASSED
    assert "quality severity cap" not in narrative


def test_audit_prompt_requests_feature_audits(tmp_path: Path) -> None:
    """v2.6 (A0.4): prompt asks for per-feature audits (one per done_means)."""
    from otto.audit import _audit_prompt

    spec = Spec(
        intent="x", project_kind="webapp",
        groups=[Group(id="s", name="t")],
        done_means=["users can sign up", "RSS feed is reachable from /"],
    )
    inp = AuditAgentInput(
        spec=spec, project_dir=tmp_path, integrated_worktree=tmp_path,
        build_summary={}, merge_summary={}, cross_slice_evidence=[],
        walkthrough_artifacts=[],
    )
    prompt = _audit_prompt(inp)
    assert "feature_audits" in prompt
    # Legacy key must NOT appear in the prompt — back-compat is gone.
    assert "capability_verdicts" not in prompt
    assert "evidence_refs" in prompt
    assert "passed" in prompt and "partial" in prompt and "blocked" in prompt
    # done_means anchor reference
    assert "done_means" in prompt


def test_audit_parser_reads_feature_audits() -> None:
    """v2.6 (A0.4): parser reads feature_audits list with status, detail, evidence."""
    from otto.audit import _parse_audit_output

    output = """```json
{
  "verdict": "partial",
  "narrative": "mostly works",
  "group_verdicts": [],
  "feature_audits": [
    {"feature_id": "signup", "name": "user signup", "status": "passed",
     "detail": "Signup form works",
     "evidence_refs": ["templates/home.html:5"]},
    {"feature_id": "rss", "name": "RSS discoverability", "status": "blocked",
     "detail": "no <link> in head, no nav link",
     "evidence_refs": ["output/index.html"]}
  ]
}
```"""
    result = _parse_audit_output(output)
    assert len(result.feature_audits) == 2
    assert result.feature_audits[0].name == "user signup"
    assert result.feature_audits[0].feature_id == "signup"
    assert result.feature_audits[0].status == "passed"
    assert result.feature_audits[0].evidence_refs == ["templates/home.html:5"]
    assert result.feature_audits[1].feature_id == "rss"
    assert result.feature_audits[1].status == "blocked"


def test_audit_parser_unknown_capability_status_defaults_blocked() -> None:
    """v2.6 permissive: malformed status → 'blocked' (defensive)."""
    from otto.audit import _parse_audit_output

    output = """```json
{"verdict":"passed",
 "feature_audits":[{"name":"x","status":"works fine"}]}```"""
    result = _parse_audit_output(output)
    assert result.feature_audits[0].status == "blocked"


def test_audit_blocked_capability_caps_passed_to_partial(tmp_path: Path) -> None:
    """v2.6 cap: any blocked feature prevents PASSED. Real damage
    surfaces — even if everything else is green."""
    from otto.audit import FeatureAudit

    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = Spec(
        intent="x", project_kind="webapp",
        groups=[Group(id="s1", name="t",
                      checks=[StateInvariant(description="exists",
                                             expression="True")])],
        done_means=["users can sign up", "RSS reachable"],
    )

    async def passing_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="functional",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True, detail="ok")],
            feature_audits=[
                FeatureAudit(name="users can sign up", status="passed", detail="ok"),
                FeatureAudit(name="RSS reachable", status="blocked",
                                  detail="no link in head"),
            ],
            quality_score=4,
            quality_findings=["minor"],
        )

    result = asyncio.run(
        run_audit(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
        )
    )
    # Functional says PASSED + quality 4/5 OK, BUT capability blocked → PARTIAL.
    assert result.verdict == AuditVerdict.PARTIAL
    assert len(result.feature_audits) == 2
    blocked = [c for c in result.feature_audits if c.status == "blocked"]
    assert len(blocked) == 1
    assert "feature cap" in result.narrative


def test_audit_missing_feature_audits_caps_passed_to_partial(tmp_path: Path) -> None:
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = Spec(
        intent="x",
        project_kind="webapp",
        groups=[Group(id="s1", name="t")],
        features=[
            Feature(id="f1", name="Feed", group_id="s1"),
            Feature(id="f2", name="Post composer", group_id="s1"),
        ],
    )

    async def incomplete_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="agent says complete",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True, detail="ok")],
            feature_audits=[],
            quality_score=4,
        )

    result = asyncio.run(
        run_audit(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=incomplete_agent,
        )
    )

    assert result.verdict == AuditVerdict.PARTIAL
    assert "feature audit coverage missing" in result.narrative
    assert "f1" in result.narrative
    assert "f2" in result.narrative


def test_audit_cross_slice_failure_caps_passed_to_partial(tmp_path: Path) -> None:
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        ["s1"],
        cross_checks=[StateInvariant(description="must fail", expression="False")],
    )

    async def passing_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="agent ignored deterministic failure",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True, detail="ok")],
            quality_score=4,
        )

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

    assert result.verdict == AuditVerdict.PARTIAL
    assert "cross-slice deterministic checks FAILED" in result.narrative


def test_malformed_otto_yaml_caps_contract_gate(tmp_path: Path) -> None:
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    (tmp_path / "otto.yaml").write_text(":\n", encoding="utf-8")
    spec = _spec(["s1"])

    async def passing_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="agent ignored malformed config",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True, detail="ok")],
            quality_score=4,
        )

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

    assert result.verdict == AuditVerdict.PARTIAL
    assert result.contract_test_passed is False
    assert "otto.yaml unreadable" in result.contract_test_detail
    assert "contract test FAILED" in result.narrative


def test_audit_majority_partial_capabilities_caps_passed(tmp_path: Path) -> None:
    """v2.6: >50% partial caps PASSED → PARTIAL even with no blocked."""
    from otto.audit import FeatureAudit

    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = Spec(
        intent="x", project_kind="webapp",
        groups=[Group(id="s1", name="t",
                      checks=[StateInvariant(description="x", expression="True")])],
    )

    async def majority_partial_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="works",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True, detail="ok")],
            feature_audits=[
                FeatureAudit(name="a", status="passed", detail="ok"),
                FeatureAudit(name="b", status="partial", detail="caveat"),
                FeatureAudit(name="c", status="partial", detail="caveat"),
                FeatureAudit(name="d", status="partial", detail="caveat"),
            ],
            quality_score=4,
        )

    result = asyncio.run(
        run_audit(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=majority_partial_agent,
        )
    )
    assert result.verdict == AuditVerdict.PARTIAL


def test_caps_compose_order_independent(tmp_path: Path) -> None:
    """Pattern B regression: when contract test fails AND quality is low
    AND a feature is blocked, the narrative must mention ALL THREE,
    not just the first one to fire its cap. Previously each cap had
    its own `if verdict == PASSED` guard and silently no-op'd later
    caps' narratives."""
    from otto.audit import FeatureAudit

    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    # otto.yaml seeds a contract test that exits non-zero.
    (tmp_path / "otto.yaml").write_text(
        'test_command: "python -c \\"import sys; sys.exit(1)\\""\n'
    )

    spec = Spec(
        intent="x", project_kind="webapp",
        groups=[Group(id="s1", name="t",
                      checks=[StateInvariant(description="x", expression="True")])],
    )

    async def multi_cap_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="LLM thinks it works",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True, detail="ok")],
            feature_audits=[
                FeatureAudit(name="signup", status="passed", detail="ok"),
                FeatureAudit(name="search", status="blocked", detail="not implemented"),
            ],
            quality_score=2,
            quality_findings=["bare-bones UI", "no error states"],
        )

    result = asyncio.run(
        run_audit(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=multi_cap_agent,
        )
    )
    # Strictest cap wins: contract failed → PARTIAL, quality<3 → PARTIAL,
    # 1/2 blocked feature → PARTIAL. All three are at most PARTIAL,
    # so verdict = PARTIAL.
    assert result.verdict == AuditVerdict.PARTIAL
    # ALL THREE caps must appear in narrative — this was the order-
    # dependent bug.
    assert "contract test FAILED" in result.narrative
    assert "quality assessment" in result.narrative or "2/5" in result.narrative
    assert "feature cap" in result.narrative
    assert "bare-bones UI" in result.narrative
    assert "search" in result.narrative


def test_capability_cap_can_escalate_to_blocked(tmp_path: Path) -> None:
    """Pattern B fix: when more than half of capabilities are blocked,
    verdict must escalate to BLOCKED, not just PARTIAL. Previously the
    cap could only downgrade PASSED→PARTIAL."""
    from otto.audit import FeatureAudit

    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = Spec(
        intent="x", project_kind="webapp",
        groups=[Group(id="s1", name="t",
                      checks=[StateInvariant(description="x", expression="True")])],
    )

    async def mostly_blocked(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="agent says ok",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True, detail="ok")],
            feature_audits=[
                FeatureAudit(name="a", status="blocked", detail="missing"),
                FeatureAudit(name="b", status="blocked", detail="missing"),
                FeatureAudit(name="c", status="blocked", detail="missing"),
                FeatureAudit(name="d", status="passed", detail="works"),
            ],
            quality_score=4,
        )

    result = asyncio.run(
        run_audit(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=mostly_blocked,
        )
    )
    # 3/4 blocked > 50% → BLOCKED escalation
    assert result.verdict == AuditVerdict.BLOCKED


def test_audit_quality_low_caps_verdict_at_partial(tmp_path: Path) -> None:
    """Functional verdict PASSED + quality_score < 3 → final verdict
    PARTIAL. This is the audit-final-quality check enforcing that
    bare-bones products don't claim PASSED."""
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = Spec(intent="x", project_kind="webapp",
                groups=[Group(id="s1", name="t",
                              checks=[StateInvariant(description="exists",
                                                     expression="True")])])

    async def passing_agent(_input: AuditAgentInput) -> AuditAgentOutput:
        return AuditAgentOutput(
            verdict=AuditVerdict.PASSED,
            narrative="functional",
            group_verdicts=[GroupVerdict(group_id="s1", passed=True, detail="ok")],
            quality_score=2,
            quality_findings=["bare-bones UI", "no nav"],
        )

    result = asyncio.run(
        run_audit(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_result=_build_result(["s1"], tmp_path),
            merge_result=_merge_result(["s1"]),
            audit_agent=passing_agent,
        )
    )
    # Functional was PASSED but quality 2 caps to PARTIAL.
    assert result.verdict == AuditVerdict.PARTIAL
    assert result.quality_score == 2
    assert "bare-bones UI" in result.quality_findings
    assert "quality assessment" in result.narrative


def test_default_walkthrough_no_browser_journey_non_webapp_returns_no_op(tmp_path: Path) -> None:
    """Non-webapp spec without a BrowserJourney → callable returns
    no-op with a clear diagnostic. webapp kinds get a synthesized
    walkthrough (see test below).
    """
    from otto.spec_compile import PytestCheck

    spec = Spec(
        intent="x",
        project_kind="cli",
        groups=[Group(id="s", name="t", checks=[PytestCheck(selector="x")])],
    )
    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    assert result.succeeded is True
    assert result.artifacts == []
    assert "no synthesized fallback" in result.detail


def _fake_playwright_capture(_url: str, log_dir: Path, *, timeout_s: int):
    _ = timeout_s
    screenshot = log_dir / "screenshot-home.png"
    dom = log_dir / "dom-home.html"
    video = log_dir / "walkthrough.webm"
    capture_log = log_dir / "browser-capture.log"
    screenshot.write_bytes(b"fake-png")
    dom.write_text("<html><body>Hello synthesized walkthrough Static site index</body></html>")
    video.write_bytes(b"fake-webm")
    capture_log.write_text("fake browser capture\n")
    return [capture_log, screenshot, dom, video], "fake playwright capture"


def test_synthesized_walkthrough_static_site_branch(tmp_path: Path, monkeypatch) -> None:
    """Project has output/index.html (static site) but no create_app
    → synthesized walkthrough detects and reads the static index.
    Generalization: webapp shape isn't only Flask."""
    from otto.spec_compile import PytestCheck

    spec = Spec(intent="x", project_kind="webapp",
                groups=[Group(id="s", name="t", checks=[PytestCheck(selector="x")])])

    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "index.html").write_text(
        "<html><body><h1>Static site index</h1></body></html>"
    )
    monkeypatch.setattr("otto.audit._capture_playwright_page", _fake_playwright_capture)

    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    assert result.succeeded is True
    log_text = (tmp_path / "log" / "synthesized-webapp.log").read_text()
    assert "static-site" in log_text or "Static site index" in log_text


def test_synthesized_walkthrough_root_index_static_site(tmp_path: Path, monkeypatch) -> None:
    """Vanilla static apps often serve index.html directly from the repo root."""
    from otto.spec_compile import PytestCheck

    spec = Spec(
        intent="x",
        project_kind="webapp",
        groups=[Group(id="s", name="t", checks=[PytestCheck(selector="x")])],
    )

    (tmp_path / "index.html").write_text(
        "<html><body><h1>Root static index</h1></body></html>"
    )
    monkeypatch.setattr("otto.audit._capture_playwright_page", _fake_playwright_capture)

    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)

    assert result.succeeded is True
    log_text = (tmp_path / "log" / "synthesized-webapp.log").read_text()
    assert '"index_path": "index.html"' in log_text
    artifact_names = {path.name for path in result.artifacts}
    assert "screenshot-home.png" in artifact_names
    assert "walkthrough.webm" in artifact_names


def test_synthesized_walkthrough_not_applicable_returns_succeeded(tmp_path: Path) -> None:
    """No create_app AND no static index.html → not a webapp shape.
    Walkthrough returns succeeded=True with 'not-applicable' diagnostic.
    Audit verdict shouldn't be penalized for non-webapp projects.
    """
    from otto.spec_compile import PytestCheck

    spec = Spec(intent="x", project_kind="webapp",
                groups=[Group(id="s", name="t", checks=[PytestCheck(selector="x")])])
    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    assert result.succeeded is True
    log_text = (tmp_path / "log" / "synthesized-webapp.log").read_text()
    assert "not-applicable" in log_text


def test_default_walkthrough_no_browser_journey_webapp_synthesizes(tmp_path: Path, monkeypatch) -> None:
    """Webapp spec without a BrowserJourney → synthesized walkthrough
    boots the app via create_app and hits /. v2 phase 3: audit verdict
    must NEVER come from 'LLM read code' alone for webapps.
    """
    from otto.spec_compile import PytestCheck

    spec = Spec(
        intent="x",
        project_kind="webapp",
        features=[Feature(id="hello", name="Hello synthesized walkthrough")],
        groups=[Group(id="s", name="t", checks=[PytestCheck(selector="x")])],
    )
    # Seed a minimal Flask app at the project root.
    (tmp_path / "flask.py").write_text(
        "class _Response:\n"
        "    status_code = 200\n"
        "    def __init__(self, body): self._body = body\n"
        "    def get_data(self, as_text=False):\n"
        "        return self._body if as_text else self._body.encode()\n"
        "class Flask:\n"
        "    def __init__(self, name): self.routes = {}\n"
        "    def get(self, path):\n"
        "        def decorator(fn):\n"
        "            self.routes[path] = fn\n"
        "            return fn\n"
        "        return decorator\n"
        "    def test_client(self):\n"
        "        app = self\n"
        "        class Client:\n"
        "            def get(self, path): return _Response(app.routes[path]())\n"
        "        return Client()\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "from flask import Flask\n"
        "def create_app(config=None):\n"
        "    app = Flask(__name__)\n"
            "    @app.get('/')\n"
            "    def home(): return '<h1>Hello synthesized walkthrough</h1>'\n"
            "    return app\n"
    )
    monkeypatch.setattr("otto.audit._capture_playwright_page", _fake_playwright_capture)
    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    assert result.succeeded is True
    artifact_names = {path.name for path in result.artifacts}
    assert "screenshot-home.png" in artifact_names
    assert "walkthrough.webm" in artifact_names
    assert "dom-home.html" in artifact_names
    jsonl = (tmp_path / "log" / "walkthrough.jsonl").read_text(encoding="utf-8")
    assert '"action_kind": "browser_navigation"' in jsonl
    assert '"feature_ids": ["hello"]' in jsonl
    # The synthesized log captures the home-page response.
    log_text = (tmp_path / "log" / "synthesized-webapp.log").read_text()
    assert "Hello synthesized walkthrough" in log_text or '"status": 200' in log_text


def test_synthesized_walkthrough_finds_package_create_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Brownfield Flask apps often expose create_app from a package."""
    from otto.spec_compile import PytestCheck

    spec = Spec(
        intent="x",
        project_kind="webapp",
        features=[Feature(id="home", name="Package Flask home")],
        groups=[Group(id="s", name="t", checks=[PytestCheck(selector="x")])],
    )
    (tmp_path / "flask.py").write_text(
        "class _Response:\n"
        "    status_code = 200\n"
        "    def __init__(self, body): self._body = body\n"
        "    def get_data(self, as_text=False):\n"
        "        return self._body if as_text else self._body.encode()\n"
        "class Flask:\n"
        "    def __init__(self, name): self.routes = {}\n"
        "    def get(self, path):\n"
        "        def decorator(fn):\n"
        "            self.routes[path] = fn\n"
        "            return fn\n"
        "        return decorator\n"
        "    def test_client(self):\n"
        "        app = self\n"
        "        class Client:\n"
        "            def get(self, path): return _Response(app.routes[path]())\n"
        "        return Client()\n",
        encoding="utf-8",
    )
    package_dir = tmp_path / "expense_portal"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        "from flask import Flask\n"
        "def create_app(config=None):\n"
        "    app = Flask(__name__)\n"
        "    @app.get('/')\n"
        "    def home(): return '<h1>Package Flask home</h1>'\n"
        "    return app\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("otto.audit._capture_playwright_page", _fake_playwright_capture)

    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)

    assert result.succeeded is True
    log_text = (tmp_path / "log" / "synthesized-webapp.log").read_text()
    assert '"module": "expense_portal"' in log_text
    assert "Package Flask home" in log_text
    artifact_names = {path.name for path in result.artifacts}
    assert "screenshot-home.png" in artifact_names


def test_synthesized_walkthrough_uses_linked_worktree_project_python(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Linked worktree apps may depend on the parent project's .venv."""
    import stat
    import sys

    from otto.spec_compile import PytestCheck

    project_root = tmp_path / "project"
    project_dir = project_root / ".worktrees" / "task"
    package_dir = project_dir / "expense_portal"
    venv_bin = project_root / ".venv" / "bin"
    package_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    python_shim = venv_bin / "python"
    python_shim.write_text(
        "#!/bin/sh\n"
        "OTTO_TEST_PROJECT_PYTHON=1 exec "
        f"{sys.executable!s} \"$@\"\n",
        encoding="utf-8",
    )
    python_shim.chmod(python_shim.stat().st_mode | stat.S_IXUSR)
    (venv_bin / "python3").symlink_to("python")
    (package_dir / "__init__.py").write_text(
        "import os\n"
        "if os.environ.get('OTTO_TEST_PROJECT_PYTHON') != '1':\n"
        "    raise ModuleNotFoundError('No module named flask')\n"
        "class _Response:\n"
        "    status_code = 200\n"
        "    def get_data(self, as_text=False):\n"
        "        body = '<h1>Project runtime home</h1>'\n"
        "        return body if as_text else body.encode()\n"
        "class _Client:\n"
        "    def get(self, path): return _Response()\n"
        "class _App:\n"
        "    def test_client(self): return _Client()\n"
        "def create_app(config=None): return _App()\n",
        encoding="utf-8",
    )
    spec = Spec(
        intent="x",
        project_kind="webapp",
        features=[Feature(id="home", name="Project runtime home")],
        groups=[Group(id="s", name="t", checks=[PytestCheck(selector="x")])],
    )
    monkeypatch.setattr("otto.audit._capture_playwright_page", _fake_playwright_capture)

    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(project_dir, tmp_path / "log", 60)

    assert result.succeeded is True
    log_text = (tmp_path / "log" / "synthesized-webapp.log").read_text()
    assert str(python_shim) in log_text
    assert '"module": "expense_portal"' in log_text
    assert "Project runtime home" in log_text


def test_default_walkthrough_picks_cross_slice_journey_first(tmp_path: Path) -> None:
    """When both cross-slice and slice-level BrowserJourney exist, the
    cross-slice one wins (it's the integrated test)."""
    from otto.spec_compile import BrowserJourney

    cross = BrowserJourney(command=("echo", "cross"), evidence_globs=("c-*.png",))
    slice_journey = BrowserJourney(command=("echo", "slice"), evidence_globs=("s-*.png",))
    spec = Spec(
        intent="x",
        cross_group_checks=[cross],
        groups=[Group(id="s", name="t", checks=[slice_journey])],
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
        groups=[Group(id="s", name="t", checks=[slice_journey])],
    )
    callable_ = default_walkthrough_from_spec(spec)
    result = callable_(tmp_path, tmp_path / "log", 60)
    assert result.succeeded is True
    log_path = tmp_path / "log" / "browser-journey.log"
    assert log_path.exists()
    assert "slice" in log_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# V4: verdict caps on merge BLOCKED
# ---------------------------------------------------------------------------


def test_compose_verdict_caps_at_partial_when_merge_blocked() -> None:
    """V4: a PASSED LLM verdict must be downgraded to PARTIAL if any
    slice was BLOCKED at merge time. PASSED while
    merge_result.blocked_ids is non-empty is the false-positive class
    observed in the P1 E2E run.
    """
    from otto.audit import _compose_verdict, AuditAgentOutput, AuditVerdict
    from otto.spec_amend import ChainVerification

    agent_output = AuditAgentOutput(
        verdict=AuditVerdict.PASSED,
        narrative="all good",
        group_verdicts=[],
        feature_audits=[],
        quality_score=4,
        quality_findings=[],
    )
    chain = ChainVerification(verdict_cap="passed", findings=[])

    verdict, narrative = _compose_verdict(
        agent_output=agent_output,
        contract_passed=True,
        contract_detail="",
        chain_review=chain,
        merge_blocked_ids=["home_page"],
        total_passing_groups=3,
    )
    assert verdict == AuditVerdict.PARTIAL, (
        f"PASSED with 1/3 blocked must downgrade to PARTIAL; got {verdict}"
    )
    assert "1 group(s) blocked" in narrative
    assert "home_page" in narrative


def test_compose_verdict_caps_at_blocked_when_majority_blocked() -> None:
    """V4: when more than half of expected passing Groups were blocked,
    cap at BLOCKED (not just PARTIAL) — most of the product is missing.
    """
    from otto.audit import _compose_verdict, AuditAgentOutput, AuditVerdict
    from otto.spec_amend import ChainVerification

    agent_output = AuditAgentOutput(
        verdict=AuditVerdict.PASSED,
        narrative="",
        group_verdicts=[],
        feature_audits=[],
        quality_score=4,
        quality_findings=[],
    )
    chain = ChainVerification(verdict_cap="passed", findings=[])
    verdict, narrative = _compose_verdict(
        agent_output=agent_output,
        contract_passed=True,
        contract_detail="",
        chain_review=chain,
        merge_blocked_ids=["a", "b"],
        total_passing_groups=3,
    )
    assert verdict == AuditVerdict.BLOCKED


def test_compose_verdict_passes_when_no_merge_blocked() -> None:
    """V4: when merge_blocked_ids is empty, the cap doesn't fire."""
    from otto.audit import _compose_verdict, AuditAgentOutput, AuditVerdict
    from otto.spec_amend import ChainVerification

    agent_output = AuditAgentOutput(
        verdict=AuditVerdict.PASSED,
        narrative="ok",
        group_verdicts=[],
        feature_audits=[],
        quality_score=5,
        quality_findings=[],
    )
    chain = ChainVerification(verdict_cap="passed", findings=[])
    verdict, _ = _compose_verdict(
        agent_output=agent_output,
        contract_passed=True,
        contract_detail="",
        chain_review=chain,
        merge_blocked_ids=[],
        total_passing_groups=3,
    )
    assert verdict == AuditVerdict.PASSED


def test_compose_verdict_narrates_redundant_merge_without_downgrade() -> None:
    """No-diff groups are dependency-satisfied, but the audit must say so."""
    from otto.audit import _compose_verdict, AuditAgentOutput, AuditVerdict
    from otto.spec_amend import ChainVerification

    agent_output = AuditAgentOutput(
        verdict=AuditVerdict.PASSED,
        narrative="ok",
        group_verdicts=[],
        feature_audits=[],
        quality_score=5,
        quality_findings=[],
    )
    chain = ChainVerification(verdict_cap="passed", findings=[])
    verdict, narrative = _compose_verdict(
        agent_output=agent_output,
        contract_passed=True,
        contract_detail="",
        chain_review=chain,
        merge_blocked_ids=[],
        merge_redundant_ids=["profile-actions"],
        total_passing_groups=3,
    )

    assert verdict == AuditVerdict.PASSED
    assert "redundant/no-diff" in narrative
    assert "profile-actions" in narrative
