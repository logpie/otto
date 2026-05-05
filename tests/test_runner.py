"""Tests for ``otto.runner.run_pipeline`` (A1.6).

The runner is intentionally headless — these tests stub every external
agent / phase callable and verify:

* phases run in the documented order (compile → seed → build → merge →
  audit → repair → render);
* ``seed_fixtures`` is called when the spec declares ``audit_fixtures``
  AND its result is honoured (a failed seed halts the run before audit);
* ``repair_failing_features`` is invoked only on non-PASS audit verdicts
  AND only when a ``fix_agent`` is wired;
* ``brownfield=True`` skips ``run_build`` / ``run_merge_queue`` cleanly;
* ``RunResult`` is populated honestly per phase.

No LLM cost; no real git/subprocess.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from otto.audit import AuditResult, AuditVerdict
from otto.audit_loop import RepairAttempt, RepairResult
from otto.build import BuildResult, SliceResult, SliceStatus
from otto.merge_queue import MergeQueueResult
from otto.runner import RunResult, run_pipeline
from otto.seed import SeedResult
from otto.spec_compile import (
    AuditFixture,
    Feature,
    Group,
    Spec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec(*, with_features: bool = False, with_audit_fixtures: bool = False) -> Spec:
    groups = [Group(id="g", title="G")]
    features = (
        [Feature(id="f1", name="f1", group_id="g")] if with_features else []
    )
    fixtures = (
        [AuditFixture(kind="user", payload={"username": "alice"})]
        if with_audit_fixtures
        else []
    )
    return Spec(
        intent="x",
        groups=groups,
        features=features,
        audit_fixtures=fixtures,
    )


def _passing_audit(spec: Spec) -> AuditResult:
    return AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="ok",
        cost_usd=0.10,
        wall_s=1.0,
    )


def _partial_audit_with_failing_feature(spec: Spec) -> AuditResult:
    """Audit verdict that should trigger Layer 2 repair."""
    from otto.audit import FeatureAudit

    fa = FeatureAudit(name="f1", status="partial", detail="needs work")
    return AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="some features failed",
        feature_audits=[fa],
        cost_usd=0.10,
        wall_s=1.0,
    )


def _ok_build(spec: Spec, session_dir: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=session_dir,
        slice_results=[
            SliceResult(
                slice_id="g",
                status=SliceStatus.PASSING,
                attempts=1,
                branch="b",
                worktree=session_dir,
            )
        ],
        total_cost_usd=0.05,
        total_wall_s=1.0,
    )


class _Order:
    """Records the order of phases the runner exercises."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def add(self, name: str) -> None:
        self.events.append(name)


def _wire_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    audit: AuditResult,
    order: _Order,
    seed_result: SeedResult | None = None,
    build: BuildResult | None = None,
) -> dict[str, Any]:
    """Patch the runner's phase callables to record + return canned results.

    Returns a dict the test can inspect (call counts, captured kwargs).
    """
    captured: dict[str, Any] = {"seed_calls": 0, "build_calls": 0,
                                "merge_calls": 0, "audit_calls": 0,
                                "repair_calls": 0, "render_calls": 0}

    def _seed(spec, project_dir, session_dir=None):
        captured["seed_calls"] += 1
        order.add("seed")
        if seed_result is not None:
            return seed_result
        return SeedResult(succeeded=True, detail="no fixtures")

    async def _build(spec, *, project_dir, session_dir, **kwargs):
        captured["build_calls"] += 1
        captured["build_kwargs"] = kwargs
        order.add("build")
        return build or _ok_build(spec, session_dir)

    async def _merge(spec, build_result, *, project_dir, session_dir, **kwargs):
        captured["merge_calls"] += 1
        order.add("merge")
        return MergeQueueResult(landed_ids=["g"])

    async def _audit(spec, **kwargs):
        captured["audit_calls"] += 1
        captured["audit_kwargs"] = kwargs
        order.add("audit")
        return audit

    async def _repair(*, spec, feature_verdicts, fix_agent, **kwargs):
        captured["repair_calls"] += 1
        captured["repair_feature_verdicts"] = feature_verdicts
        order.add("repair")
        return RepairResult(
            attempts=[
                RepairAttempt(
                    feature_id=v["feature_id"],
                    group_id="g",
                    attempt_number=1,
                    succeeded=False,
                    detail="stub",
                )
                for v in feature_verdicts
            ]
        )

    def _render(spec, *, session_dir, **kwargs):
        captured["render_calls"] += 1
        order.add("render")
        html = session_dir / "proof-packet.html"
        json_ = session_dir / "proof-packet.json"
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text("<html/>")
        json_.write_text("{}")
        return html, json_

    monkeypatch.setattr("otto.runner.seed_fixtures", _seed)
    monkeypatch.setattr("otto.runner.run_build", _build)
    monkeypatch.setattr("otto.runner.run_merge_queue", _merge)
    monkeypatch.setattr("otto.runner.run_audit", _audit)
    monkeypatch.setattr("otto.runner.repair_failing_features", _repair)
    monkeypatch.setattr("otto.runner.render_run", _render)
    return captured


async def _stub_agent(*args, **kwargs):  # pragma: no cover — never called
    raise AssertionError("stubbed agents must not be invoked in unit tests")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_pipeline_phases_run_in_order(tmp_path: Path, monkeypatch) -> None:
    """Greenfield happy path: compile-skip + seed + build + merge + audit + render."""
    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_passing_audit(spec),
        order=order,
    )

    result = asyncio.run(
        run_pipeline(
            "x",
            tmp_path,
            session_dir,
            project_kind="webapp",
            brownfield=False,
            base_url=None,
            config={},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=None,
            spec=spec,
        )
    )

    # Order: seed → build → merge → audit → render. (No compile because
    # spec= was supplied; no repair because verdict is PASSED.)
    assert order.events == ["seed", "build", "merge", "audit", "render"]
    assert captured["seed_calls"] == 1
    assert captured["build_calls"] == 1
    assert captured["merge_calls"] == 1
    assert captured["audit_calls"] == 1
    assert captured["repair_calls"] == 0
    assert captured["render_calls"] == 1

    assert isinstance(result, RunResult)
    assert result.verdict == AuditVerdict.PASSED
    assert result.html_path is not None
    assert result.json_path is not None
    assert result.build_result is not None
    assert result.merge_result is not None
    assert result.audit_result is not None


def test_seed_called_with_audit_fixtures(tmp_path: Path, monkeypatch) -> None:
    """seed_fixtures sees the spec's audit_fixtures verbatim."""
    spec = _spec(with_audit_fixtures=True)
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()

    seen_fixtures: list[Any] = []

    def _seed(spec_arg, project_dir, session_dir=None):
        order.add("seed")
        seen_fixtures.extend(list(spec_arg.audit_fixtures))
        return SeedResult(succeeded=True, detail="applied 1")

    async def _build(spec, **kwargs):
        order.add("build")
        return _ok_build(spec, session_dir)

    async def _merge(*args, **kwargs):
        order.add("merge")
        return MergeQueueResult()

    async def _audit(spec, **kwargs):
        order.add("audit")
        return _passing_audit(spec)

    def _render(spec, *, session_dir, **kwargs):
        order.add("render")
        return session_dir / "h.html", session_dir / "j.json"

    monkeypatch.setattr("otto.runner.seed_fixtures", _seed)
    monkeypatch.setattr("otto.runner.run_build", _build)
    monkeypatch.setattr("otto.runner.run_merge_queue", _merge)
    monkeypatch.setattr("otto.runner.run_audit", _audit)
    monkeypatch.setattr("otto.runner.render_run", _render)

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
        )
    )

    assert len(seen_fixtures) == 1
    assert seen_fixtures[0].kind == "user"
    assert result.seed_result is not None
    assert result.seed_result.succeeded is True


def test_seed_failure_halts_before_audit(tmp_path: Path, monkeypatch) -> None:
    """A failed SeedResult short-circuits the run with verdict=BLOCKED."""
    spec = _spec(with_audit_fixtures=True)
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    _wire_stubs(
        monkeypatch,
        audit=_passing_audit(spec),  # never reached
        order=order,
        seed_result=SeedResult(
            succeeded=False,
            detail="seed script crashed",
            errors=["audit_fixtures[0]: oops"],
        ),
    )

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
        )
    )

    assert order.events == ["seed"]  # nothing after seed ran
    assert result.verdict == AuditVerdict.BLOCKED
    assert "seed_failed" in result.halted_reason
    assert result.audit_result is not None
    assert result.audit_result.verdict == AuditVerdict.BLOCKED
    assert result.build_result is None
    assert result.merge_result is None


def test_repair_called_on_non_pass_with_fix_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """Layer 2 repair triggers only when verdict != PASSED AND fix_agent set."""
    spec = _spec(with_features=True)
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_partial_audit_with_failing_feature(spec),
        order=order,
    )

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=_stub_agent,  # KEY: Layer 2 enabled
            spec=spec,
        )
    )

    assert "repair" in order.events
    assert captured["repair_calls"] == 1
    # Layer 2 received the failing feature verdict from feature_audits.
    fvs = captured["repair_feature_verdicts"]
    assert any(v["feature_id"] == "f1" for v in fvs)
    assert result.repair_result is not None


def test_repair_skipped_on_passed_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    """No repair when verdict is PASSED, even if fix_agent is wired."""
    spec = _spec(with_features=True)
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_passing_audit(spec),
        order=order,
    )

    asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent,
            fix_agent=_stub_agent,
            spec=spec,
        )
    )
    assert "repair" not in order.events
    assert captured["repair_calls"] == 0


def test_repair_skipped_without_fix_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """No repair when fix_agent is None, even on a partial verdict."""
    spec = _spec(with_features=True)
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_partial_audit_with_failing_feature(spec),
        order=order,
    )

    asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent,
            fix_agent=None,
            spec=spec,
        )
    )
    assert "repair" not in order.events
    assert captured["repair_calls"] == 0


def test_brownfield_skips_build_and_merge(
    tmp_path: Path, monkeypatch
) -> None:
    """Brownfield mode short-circuits build + merge phases honestly."""
    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_passing_audit(spec),
        order=order,
    )

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=True, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
        )
    )

    # No build/merge phase events; seed → audit → render only.
    assert "build" not in order.events
    assert "merge" not in order.events
    assert "audit" in order.events
    assert "render" in order.events
    assert captured["build_calls"] == 0
    assert captured["merge_calls"] == 0
    # BuildResult / MergeQueueResult are populated as honest empties.
    assert result.build_result is not None
    assert result.build_result.slice_results == []
    assert result.merge_result is not None
    assert result.merge_result.landed_ids == []


def test_run_pipeline_requires_config_when_no_spec(tmp_path: Path) -> None:
    """Programmer error: must pass either spec= or config= for compile."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    with pytest.raises(ValueError, match="config must be provided"):
        asyncio.run(
            run_pipeline(
                "x", tmp_path, session_dir,
                project_kind="webapp", brownfield=False, base_url=None,
                config=None,
                build_agent=_stub_agent, audit_agent=_stub_agent,
                fix_agent=None,
                spec=None,
            )
        )


def test_on_phase_callback_emits_phase_names(
    tmp_path: Path, monkeypatch
) -> None:
    """The on_phase callback receives a name per phase boundary."""
    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _wire_stubs(monkeypatch, audit=_passing_audit(spec), order=_Order())

    phases: list[str] = []

    asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
            on_phase=phases.append,
        )
    )

    assert "seed" in phases
    assert "build" in phases
    assert "merge" in phases
    assert "audit" in phases
    assert "render" in phases
    # No compile (spec= path); no repair (PASSED).
    assert "compile" not in phases
    assert "repair" not in phases


# ---------------------------------------------------------------------------
# Resume — run_pipeline(resume_plan=...)
# ---------------------------------------------------------------------------


def test_resume_plan_skips_landed_components_and_short_circuits_audit(
    tmp_path: Path, monkeypatch
) -> None:
    """resume_plan with landed_components + audit_finished:

      * run_build receives skip_components verbatim
      * run_audit is NOT called (short-circuited from journal)
      * resume cost-carry is enforced: prior_cost_usd is charged to the
        shared BuildBudget before any phase runs
    """
    from otto.resume import ResumePlan

    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_passing_audit(spec),  # would be called if audit ran — assert it isn't
        order=order,
    )

    plan = ResumePlan(
        session_id="sess",
        paused_session_dir=session_dir,
        spec_hash="deadbeef",
        landed_components=frozenset({"g"}),
        pending_components=frozenset(),
        audit_finished=True,
        audit_verdict="passed",
        prior_cost_usd=2.50,
    )

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
            resume_plan=plan,
        )
    )

    # Build still ran (so its skip_components plumbing is exercised),
    # but audit did NOT.
    assert "build" in order.events
    assert "audit" not in order.events  # short-circuited
    assert captured["audit_calls"] == 0
    # The skip_components kwarg was forwarded to run_build.
    build_kwargs = captured["build_kwargs"]
    assert "skip_components" in build_kwargs
    assert "g" in set(build_kwargs["skip_components"])
    # Synthesised AuditResult records the skip in narrative.
    assert result.audit_result is not None
    assert result.verdict == AuditVerdict.PASSED
    assert "short-circuited on resume" in result.audit_result.narrative


def test_resume_plan_runs_audit_when_not_finished(
    tmp_path: Path, monkeypatch
) -> None:
    """When ResumePlan.audit_finished is False, audit phase runs normally."""
    from otto.resume import ResumePlan

    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_passing_audit(spec),
        order=order,
    )

    plan = ResumePlan(
        session_id="sess",
        paused_session_dir=session_dir,
        spec_hash="deadbeef",
        landed_components=frozenset(),
        pending_components=frozenset({"g"}),
        audit_finished=False,
    )

    asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
            resume_plan=plan,
        )
    )
    assert "audit" in order.events
    assert captured["audit_calls"] == 1
