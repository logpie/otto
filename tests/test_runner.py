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
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from otto import paths
from otto.audit import AuditResult, AuditVerdict
from otto.audit_loop import RepairAttempt, RepairResult
from otto.build import BuildAgentOutput, BuildResult, GroupResult, GroupStatus
from otto.merge_queue import MergeQueueResult
from otto.runner import (
    RunResult,
    _feature_audits_to_verdicts,
    _invalidated_group_ids,
    _repair_verdicts_for_audit,
    run_pipeline,
)
from otto.seed import SeedResult
from otto.spec_compile import (
    AuditFixture,
    Feature,
    Group,
    Spec,
    persist_spec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _spec(*, with_features: bool = False, with_audit_fixtures: bool = False) -> Spec:
    groups = [Group(id="g", name="G")]
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


def test_feature_audits_to_verdicts_prefers_feature_id() -> None:
    """Layer 2 repair routing must use Feature.id as the stable join key."""
    from otto.audit import FeatureAudit

    spec = _spec(with_features=True)
    audit = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="x",
        feature_audits=[
            FeatureAudit(
                feature_id="f1",
                name="display name drifted",
                status="partial",
                detail="needs work",
            )
        ],
    )

    assert _feature_audits_to_verdicts(spec, audit) == [
        {
            "feature_id": "f1",
            "verdict": "partial",
            "detail": "needs work",
            "evidence_refs": [],
        }
    ]


def test_feature_audits_to_verdicts_preserves_evidence_gate_fields() -> None:
    from otto.audit import FeatureAudit

    spec = _spec(with_features=True)
    audit = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="x",
        feature_audits=[
            FeatureAudit(
                feature_id="f1",
                name="f1",
                status="partial",
                detail="Save button did not persist the row",
                evidence_refs=["walkthrough.jsonl#L4"],
                surface="DOM",
                methodology="live-ui-events",
                evidence_completeness="full",
                coverage_confidence="high",
            )
        ],
    )

    assert _feature_audits_to_verdicts(spec, audit) == [
        {
            "feature_id": "f1",
            "verdict": "partial",
            "detail": "Save button did not persist the row",
            "evidence_refs": ["walkthrough.jsonl#L4"],
            "surface": "DOM",
            "methodology": "live-ui-events",
            "evidence_completeness": "full",
            "coverage_confidence": "high",
        }
    ]


def test_recovered_feature_verdicts_preserve_repair_actionability(tmp_path: Path) -> None:
    from otto.audit import _recover_audit_output_from_feature_verdicts
    from otto.audit_loop import features_to_repair

    spec = Spec(
        intent="recover timeout",
        project_kind="webapp",
        groups=[
            Group(id="calc", name="Calculator"),
            Group(id="profile", name="Profile"),
        ],
        features=[
            Feature(id="calc_total", name="Calculate total", group_id="calc"),
            Feature(id="profile_save", name="Save profile", group_id="profile"),
        ],
    )
    verdict_path = tmp_path / "feature-verdicts.json"
    verdict_path.write_text(
        json.dumps({
            "schema_version": 1,
            "verdicts": [
                {
                    "feature_id": "calc_total",
                    "verdict": "failed",
                    "detail": "total is wrong",
                },
                {
                    "feature_id": "profile_save",
                    "verdict": "blocked",
                    "detail": "save could not be completed",
                    "check_evidence_refs": ["checks.json#profile_save"],
                    "severity_findings": ["important: save button did not persist"],
                    "quality_findings": ["profile save has no success state"],
                    "coverage_confidence": "medium",
                    "evidence_completeness": "partial",
                    "surface": "DOM",
                    "methodology": "live-ui-events",
                },
            ],
        }),
        encoding="utf-8",
    )

    recovered = _recover_audit_output_from_feature_verdicts(
        verdict_path,
        spec,
        "audit timed out",
    )
    assert recovered is not None
    audit_result = AuditResult(
        verdict=recovered.verdict,
        narrative=recovered.narrative,
        feature_audits=recovered.feature_audits,
    )

    verdicts = _repair_verdicts_for_audit(spec, audit_result)
    assert verdicts[0]["verdict"] == "failed"
    assert verdicts[0]["evidence_refs"] == []
    assert verdicts[1]["check_evidence_refs"] == ["checks.json#profile_save"]
    assert verdicts[1]["severity_findings"] == ["important: save button did not persist"]
    assert verdicts[1]["quality_findings"] == ["profile save has no success state"]
    assert verdicts[1]["coverage_confidence"] == "medium"
    assert verdicts[1]["evidence_completeness"] == "partial"
    assert verdicts[1]["surface"] == "DOM"
    assert verdicts[1]["methodology"] == "live-ui-events"

    candidates = features_to_repair(spec, verdicts, max_attempts_per_run=10)
    assert [candidate.feature_id for candidate in candidates] == [
        "calc_total",
        "profile_save",
    ]


def test_feature_audits_to_verdicts_maps_group_id_to_best_matching_feature() -> None:
    """A group-level audit miss must still route to a concrete Feature repair."""
    from otto.audit import FeatureAudit

    spec = Spec(
        intent="micro twitter",
        groups=[
            Group(
                id="foundation",
                name="Vite app shell, shared styling, and README",
                feature_ids=["shell", "readme_commands"],
            )
        ],
        features=[
            Feature(
                id="shell",
                name="Initialize a Vite React TypeScript SPA",
                group_id="foundation",
            ),
            Feature(
                id="readme_commands",
                name="Document install, dev server, build, and test commands in README",
                group_id="foundation",
            ),
        ],
    )
    audit = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="x",
        feature_audits=[
            FeatureAudit(
                feature_id="foundation",
                name="Vite app shell, shared styling, and README",
                status="partial",
                detail="README omits the separate browser-test command.",
            )
        ],
    )

    assert _feature_audits_to_verdicts(spec, audit) == [
        {
            "feature_id": "readme_commands",
            "verdict": "partial",
            "detail": "README omits the separate browser-test command.",
            "evidence_refs": [],
        }
    ]


def test_layer2_repair_runs_for_group_level_feature_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: group-level audit ids used to bypass Layer 2 entirely."""
    from otto.audit import FeatureAudit

    spec = Spec(
        intent="micro twitter",
        groups=[
            Group(
                id="foundation",
                name="Vite app shell, shared styling, and README",
                feature_ids=["shell", "readme_commands"],
            )
        ],
        features=[
            Feature(
                id="shell",
                name="Initialize a Vite React TypeScript SPA",
                group_id="foundation",
            ),
            Feature(
                id="readme_commands",
                name="Document install, dev server, build, and test commands in README",
                group_id="foundation",
            ),
        ],
    )
    audit = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="some group failed",
        feature_audits=[
            FeatureAudit(
                feature_id="foundation",
                name="Vite app shell, shared styling, and README",
                status="partial",
                detail="README omits the separate browser-test command.",
            )
        ],
    )
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(monkeypatch, audit=audit, order=order)

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
            fix_agent=_stub_agent,
            spec=spec,
            enable_audit_repair=True,
        )
    )

    assert "repair" in order.events
    assert captured["repair_calls"] == 1
    assert captured["repair_feature_verdicts"] == [
        {
            "feature_id": "readme_commands",
            "verdict": "partial",
            "detail": "README omits the separate browser-test command.",
            "evidence_refs": [],
        }
    ]
    assert result.repair_result is not None


def test_layer2_repair_runs_for_compact_group_feature_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: compact specs with no top-level features still repair."""
    from otto.audit import FeatureAudit
    from otto.spec_compile import parse_spec

    spec, _warnings = parse_spec(
        {
            "intent": "team kanban",
            "project_kind": "webapp",
            "groups": [
                {
                    "id": "cards_movement",
                    "name": "Card creation, editing, deletion, and movement",
                    "feature_ids": [
                        "create cards with title, description, labels, assignee, and due date",
                        "move cards between columns with visible controls",
                    ],
                    "owned_paths": ["src/features/cards/**"],
                }
            ],
        }
    )
    assert spec.features == []

    audit = AuditResult(
        verdict=AuditVerdict.BLOCKED,
        narrative="cards missing",
        feature_audits=[
            FeatureAudit(
                feature_id="cards_movement",
                name="Card creation, editing, deletion, and movement",
                status="missing",
                detail="Create/edit/delete card controls are absent.",
            )
        ],
    )
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(monkeypatch, audit=audit, order=order)

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
            fix_agent=_stub_agent,
            spec=spec,
            enable_audit_repair=True,
        )
    )

    assert "repair" in order.events
    assert captured["repair_calls"] == 1
    repaired_ids = {
        verdict["feature_id"] for verdict in captured["repair_feature_verdicts"]
    }
    assert "create cards with title, description, labels, assignee, and due date" in repaired_ids
    assert "move cards between columns with visible controls" in repaired_ids
    assert result.repair_result is not None


def test_repair_verdicts_include_product_quality_findings_when_features_pass() -> None:
    """Quality-only partial audits must still produce actionable repairs."""
    from otto.audit import FeatureAudit

    spec = Spec(
        intent="micro twitter",
        groups=[
            Group(
                id="foundation",
                name="App shell, backend, database, base styling",
                feature_ids=["base_styling"],
            ),
            Group(
                id="post-creation",
                name="Create post form and submission",
                feature_ids=["character_counter"],
            ),
        ],
        features=[
            Feature(
                id="base_styling",
                name="Setup base styling with responsive layout and cohesive color scheme",
                group_id="foundation",
            ),
            Feature(
                id="character_counter",
                name="Show character count indicator as user types",
                group_id="post-creation",
            ),
        ],
    )
    audit = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="all stories pass, product quality is still weak",
        feature_audits=[
            FeatureAudit(
                feature_id="base_styling",
                name="base styling",
                status="passed",
                detail="works",
            ),
            FeatureAudit(
                feature_id="character_counter",
                name="character counter",
                status="passed",
                detail="works",
            ),
        ],
        quality_findings=[
            "Color palette is minimal and needs stronger visual hierarchy.",
            "Character counter styling is understated near the 280 character limit.",
        ],
    )

    verdicts = _repair_verdicts_for_audit(spec, audit)
    partial_by_id = {
        str(v["feature_id"]): str(v["detail"])
        for v in verdicts
        if v.get("verdict") == "partial"
    }

    assert set(partial_by_id) == {"base_styling", "character_counter"}
    assert "Color palette is minimal" in partial_by_id["base_styling"]
    assert "Character counter styling" in partial_by_id["character_counter"]


def test_layer2_repair_runs_for_product_quality_only_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression from live Microfeed: 25/25 stories passed but quality partial stopped."""
    from otto.spec_compile import parse_spec

    spec, _warnings = parse_spec(
        {
            "intent": "build a micro twitter",
            "project_kind": "webapp",
            "groups": [
                {
                    "id": "foundation",
                    "name": "App shell, Express backend, database, base styling",
                    "feature_ids": [
                        "Setup base styling with responsive layout and cohesive color scheme"
                    ],
                },
                {
                    "id": "post-creation",
                    "name": "Create post form and submission",
                    "feature_ids": [
                        "Show character count indicator as user types",
                        "Clear form fields and show brief success message after post creation",
                    ],
                },
            ],
        }
    )
    assert spec.features == []

    audit = AuditResult(
        verdict=AuditVerdict.PARTIAL,
        narrative="all functional requirements pass, but polish is weak",
        quality_findings=[
            "Color palette is minimal and needs stronger visual hierarchy.",
            "Character counter styling is understated near the 280 character limit.",
        ],
    )
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(monkeypatch, audit=audit, order=order)

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
            fix_agent=_stub_agent,
            spec=spec,
            enable_audit_repair=True,
        )
    )

    assert "repair" in order.events
    assert captured["repair_calls"] == 1
    repaired_ids = {
        verdict["feature_id"] for verdict in captured["repair_feature_verdicts"]
        if verdict["verdict"] == "partial"
    }
    assert "Setup base styling with responsive layout and cohesive color scheme" in repaired_ids
    assert "Show character count indicator as user types" in repaired_ids
    assert result.repair_result is not None


def _ok_build(spec: Spec, session_dir: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="g",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="b",
                worktree=session_dir,
            )
        ],
        total_cost_usd=0.05,
        total_wall_s=1.0,
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


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
    merge: MergeQueueResult | None = None,
) -> dict[str, Any]:
    """Patch the runner's phase callables to record + return canned results.

    Returns a dict the test can inspect (call counts, captured kwargs).
    """
    captured: dict[str, Any] = {
        "seed_calls": 0,
        "build_calls": 0,
        "merge_calls": 0,
        "audit_calls": 0,
        "repair_calls": 0,
        "render_calls": 0,
        "seed_specs": [],
        "build_specs": [],
    }

    def _seed(spec, project_dir, session_dir=None):
        captured["seed_calls"] += 1
        captured["seed_specs"].append(spec)
        order.add("seed")
        if seed_result is not None:
            return seed_result
        return SeedResult(succeeded=True, detail="no fixtures")

    async def _build(spec, *, project_dir, session_dir, **kwargs):
        captured["build_calls"] += 1
        captured["build_specs"].append(spec)
        captured["build_kwargs"] = kwargs
        order.add("build")
        return build or _ok_build(spec, session_dir)

    async def _merge(spec, build_result, *, project_dir, session_dir, **kwargs):
        captured["merge_calls"] += 1
        captured["merge_kwargs"] = kwargs
        order.add("merge")
        return merge or MergeQueueResult(landed_ids=["g"])

    async def _audit(spec, **kwargs):
        captured["audit_calls"] += 1
        captured["audit_kwargs"] = kwargs
        order.add("audit")
        return audit

    async def _repair(*, spec, feature_verdicts, fix_agent, **kwargs):
        captured["repair_calls"] += 1
        captured["repair_feature_verdicts"] = feature_verdicts
        captured["repair_fix_agent"] = fix_agent
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


def test_run_pipeline_rebuilds_dependency_setup_blocked_group_after_merge_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A downstream group blocked on raw dep branches gets one post-merge pass."""
    spec = Spec(
        intent="team kanban",
        groups=[
            Group(id="foundation", name="Foundation"),
            Group(id="board-editor", name="Board editor", dependencies=["foundation"]),
            Group(id="filters-search", name="Filters", dependencies=["foundation"]),
            Group(id="import-export", name="Import/export", dependencies=["foundation"]),
            Group(
                id="docs-and-quality",
                name="Docs and quality",
                dependencies=["board-editor", "filters-search", "import-export"],
            ),
        ],
    )
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured: dict[str, Any] = {
        "build_calls": 0,
        "merge_calls": 0,
        "second_build_skip": set(),
        "second_merge_skip": set(),
    }

    def _seed(spec, project_dir, session_dir=None):
        order.add("seed")
        return SeedResult(succeeded=True, detail="ok")

    async def _build(spec, *, project_dir, session_dir, **kwargs):
        captured["build_calls"] += 1
        order.add("build")
        if captured["build_calls"] == 1:
            return BuildResult(
                spec_session_dir=session_dir,
                group_results=[
                    GroupResult(
                        group_id="foundation",
                        status=GroupStatus.PASSING,
                        attempts=1,
                        branch="i2p/run/foundation",
                        worktree=project_dir,
                    ),
                    GroupResult(
                        group_id="board-editor",
                        status=GroupStatus.PASSING,
                        attempts=1,
                        branch="i2p/run/board-editor",
                        worktree=project_dir,
                    ),
                    GroupResult(
                        group_id="filters-search",
                        status=GroupStatus.PASSING,
                        attempts=1,
                        branch="i2p/run/filters-search",
                        worktree=project_dir,
                    ),
                    GroupResult(
                        group_id="import-export",
                        status=GroupStatus.PASSING,
                        attempts=1,
                        branch="i2p/run/import-export",
                        worktree=project_dir,
                    ),
                    GroupResult(
                        group_id="docs-and-quality",
                        status=GroupStatus.BLOCKED,
                        attempts=0,
                        branch="i2p/run/docs-and-quality",
                        worktree=project_dir,
                        failure_narrative=(
                            "dependency branch setup failed: could not create an "
                            "integrated branch for docs-and-quality"
                        ),
                    ),
                ],
                total_cost_usd=0.40,
                total_wall_s=4.0,
            )
        captured["second_build_skip"] = set(kwargs.get("skip_components") or [])
        return BuildResult(
            spec_session_dir=session_dir,
            group_results=[
                GroupResult(
                    group_id="docs-and-quality",
                    status=GroupStatus.PASSING,
                    attempts=1,
                    branch="i2p/run/docs-and-quality",
                    worktree=project_dir,
                )
            ],
            total_cost_usd=0.10,
            total_wall_s=1.0,
        )

    async def _merge(spec, build_result, *, project_dir, session_dir, **kwargs):
        captured["merge_calls"] += 1
        order.add("merge")
        if captured["merge_calls"] == 1:
            return MergeQueueResult(
                landed_ids=[
                    "foundation",
                    "board-editor",
                    "filters-search",
                    "import-export",
                ]
            )
        captured["second_merge_skip"] = set(kwargs.get("skip_components") or [])
        return MergeQueueResult(
            landed_ids=[
                "foundation",
                "board-editor",
                "filters-search",
                "import-export",
                "docs-and-quality",
            ]
        )

    async def _audit(spec, **kwargs):
        order.add("audit")
        return _passing_audit(spec)

    def _render(spec, *, session_dir, **kwargs):
        order.add("render")
        html = session_dir / "proof-packet.html"
        json_ = session_dir / "proof-packet.json"
        html.write_text("<html/>")
        json_.write_text("{}")
        return html, json_

    monkeypatch.setattr("otto.runner.seed_fixtures", _seed)
    monkeypatch.setattr("otto.runner.run_build", _build)
    monkeypatch.setattr("otto.runner.run_merge_queue", _merge)
    monkeypatch.setattr("otto.runner.run_audit", _audit)
    monkeypatch.setattr("otto.runner.render_run", _render)

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

    assert order.events == ["seed", "build", "merge", "build", "merge", "audit", "render"]
    assert captured["build_calls"] == 2
    assert captured["merge_calls"] == 2
    assert "docs-and-quality" not in captured["second_build_skip"]
    assert {"board-editor", "filters-search", "import-export"}.issubset(
        captured["second_build_skip"]
    )
    assert captured["second_merge_skip"] == {
        "foundation",
        "board-editor",
        "filters-search",
        "import-export",
    }
    assert result.build_result is not None
    latest = {r.group_id: r for r in result.build_result.group_results}
    assert latest["docs-and-quality"].status == GroupStatus.PASSING
    assert result.merge_result is not None
    assert "docs-and-quality" in result.merge_result.landed_ids


def test_run_pipeline_writes_resume_checkpoint_and_clears_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    """Real i2p runs must write the checkpoint/pointer consumed by --resume."""
    spec = _spec()
    session_id = "2026-05-04-120000-abc123"
    session_dir = paths.session_dir(tmp_path, session_id)
    spec_path = session_dir / "spec" / "spec.json"
    persist_spec(spec, spec_path, allow_initial=True)
    original_hash = _sha256(spec_path)
    order = _Order()
    _wire_stubs(monkeypatch, audit=_passing_audit(spec), order=order)

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
            command="build",
        )
    )

    checkpoint = json.loads((session_dir / "checkpoint.json").read_text())
    assert checkpoint["status"] == "completed"
    assert checkpoint["command"] == "build"
    assert checkpoint["phase"] == "i2p"
    assert checkpoint["spec_hash"] == original_hash
    assert result.verdict == AuditVerdict.PASSED
    assert paths.resolve_pointer(tmp_path, paths.PAUSED_POINTER) is None
    assert paths.resolve_pointer(tmp_path, paths.LATEST_POINTER) == session_dir.resolve()
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


def test_greenfield_layer2_repair_runs_by_default_with_fix_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """Autonomous builds repair non-PASS final audit verdicts when possible."""
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
            fix_agent=_stub_agent,
            spec=spec,
        )
    )

    assert "repair" in order.events
    assert captured["audit_kwargs"]["fix_agent"] is None
    assert captured["repair_calls"] == 1
    assert result.repair_result is not None


def test_greenfield_layer2_repair_can_be_disabled_by_config(
    tmp_path: Path, monkeypatch
) -> None:
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
            project_kind="webapp", brownfield=False, base_url=None,
            config={"workflow": {"enable_audit_repair": False}},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=_stub_agent,
            spec=spec,
        )
    )

    assert "repair" not in order.events
    assert captured["repair_calls"] == 0
    assert result.repair_result is None


def test_greenfield_layer2_repair_can_be_enabled_by_config(
    tmp_path: Path, monkeypatch
) -> None:
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
            project_kind="webapp", brownfield=False, base_url=None,
            config={"workflow": {"enable_audit_repair": True}},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=_stub_agent,
            spec=spec,
        )
    )

    assert "repair" in order.events
    assert captured["repair_calls"] == 1
    assert result.repair_result is not None


def test_repair_called_on_non_pass_with_explicit_audit_repair(
    tmp_path: Path, monkeypatch
) -> None:
    """Layer 2 repair is still available as an explicit workflow choice."""
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
            enable_audit_repair=True,
        )
    )

    assert "repair" in order.events
    assert captured["audit_kwargs"]["fix_agent"] is None
    assert captured["repair_fix_agent"] is not None
    assert captured["repair_calls"] == 1
    # Layer 2 received the failing feature verdict from feature_audits.
    fvs = captured["repair_feature_verdicts"]
    assert any(v["feature_id"] == "f1" for v in fvs)
    assert result.repair_result is not None


def test_layer2_repair_skipped_when_merge_has_blocked_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer 2 cannot fix blocked slice branches from the integrated root."""
    spec = _spec(with_features=True)
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_partial_audit_with_failing_feature(spec),
        order=order,
        merge=MergeQueueResult(landed_ids=[], blocked_ids=["g"]),
    )

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=_stub_agent,
            spec=spec,
        )
    )

    assert "repair" not in order.events
    assert captured["repair_calls"] == 0
    assert result.repair_result is None


def test_structural_build_block_skips_expensive_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing group cannot become green by spending on product audit."""
    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    blocked_build = BuildResult(
        spec_session_dir=session_dir,
        group_results=[
            GroupResult(
                group_id="g",
                status=GroupStatus.BLOCKED,
                attempts=1,
                branch="b",
                worktree=session_dir,
                failure_narrative="build failed",
            )
        ],
    )
    captured = _wire_stubs(
        monkeypatch,
        audit=_passing_audit(spec),
        order=order,
        build=blocked_build,
        merge=MergeQueueResult(landed_ids=[], blocked_ids=["g"]),
    )

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=_stub_agent,
            spec=spec,
        )
    )

    assert order.events == ["seed", "build", "merge", "render"]
    assert captured["audit_calls"] == 0
    assert result.audit_result is not None
    assert result.audit_result.verdict == AuditVerdict.BLOCKED
    assert "structurally incomplete" in result.audit_result.narrative
    assert captured["repair_calls"] == 0


def test_layer2_repair_reaudits_and_updates_final_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    """Feature-level repair must close the loop with a real re-audit."""
    spec = _spec(with_features=True)
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured: dict[str, Any] = {"audit_calls": 0, "fix_calls": 0}

    def _seed(spec, project_dir, session_dir=None):
        order.add("seed")
        return SeedResult(succeeded=True, detail="ok")

    async def _build(spec, *, project_dir, session_dir, **kwargs):
        order.add("build")
        return _ok_build(spec, session_dir)

    async def _merge(spec, build_result, *, project_dir, session_dir, **kwargs):
        order.add("merge")
        return MergeQueueResult(landed_ids=["g"])

    async def _audit(spec, **kwargs):
        captured["audit_calls"] += 1
        order.add("audit")
        if captured["audit_calls"] == 1:
            return _partial_audit_with_failing_feature(spec)
        from otto.audit import FeatureAudit

        passed = _passing_audit(spec)
        passed.feature_audits = [
            FeatureAudit(name="f1", status="passed", detail="ok")
        ]
        return passed

    def _render(spec, *, session_dir, audit_result, cost_usd, **kwargs):
        order.add("render")
        html = session_dir / "proof-packet.html"
        json_ = session_dir / "proof-packet.json"
        html.write_text("<html/>")
        json_.write_text("{}")
        assert audit_result.verdict == AuditVerdict.PASSED
        assert cost_usd >= 0.25
        return html, json_

    async def _fix(agent_input):
        captured["fix_calls"] += 1
        assert agent_input.feature_id == "f1"
        return BuildAgentOutput(
            succeeded=True,
            cost_usd=0.05,
            wall_s=0.5,
            detail="fixed feature",
        )

    monkeypatch.setattr("otto.runner.seed_fixtures", _seed)
    monkeypatch.setattr("otto.runner.run_build", _build)
    monkeypatch.setattr("otto.runner.run_merge_queue", _merge)
    monkeypatch.setattr("otto.runner.run_audit", _audit)
    monkeypatch.setattr("otto.runner.render_run", _render)

    result = asyncio.run(
        run_pipeline(
            "x",
            tmp_path,
            session_dir,
            project_kind="webapp",
            brownfield=False,
            base_url=None,
            config={},
            build_agent=_fix,
            audit_agent=_stub_agent,
            fix_agent=_fix,
            spec=spec,
            enable_audit_repair=True,
        )
    )

    assert captured["audit_calls"] == 2
    assert captured["fix_calls"] == 1
    assert result.verdict == AuditVerdict.PASSED
    assert result.repair_result is not None
    assert result.repair_result.attempts[0].new_verdict == "passed"
    assert order.events == ["seed", "build", "merge", "audit", "audit", "render"]


def test_brownfield_layer2_repairs_features_with_group_name_alias(
    tmp_path: Path, monkeypatch
) -> None:
    """Brownfield compile aliases must still give Layer 2 a Group route."""
    from otto.audit import FeatureAudit
    from otto.spec_compile import parse_spec

    spec, warnings = parse_spec(
        {
            "intent": "library improvement",
            "project_kind": "library",
            "structure": {"payload": {}},
            "groups": [
                {
                    "id": "group_0",
                    "name": "Number",
                    "features": ["intword"],
                    "owned_paths": ["src/humanize/number.py"],
                }
            ],
            "features": [
                {
                    "name": "intword",
                    "module": "Number",
                }
            ],
        }
    )
    assert spec.features[0].id == "intword"
    assert spec.features[0].group_id == "group_0"
    assert any(w.code == "spec.coerce.feature_group_id" for w in warnings)

    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    captured: dict[str, Any] = {
        "audit_calls": 0,
        "audit_configs": [],
        "fix_inputs": [],
    }

    def _seed(spec, project_dir, session_dir=None):
        return SeedResult(succeeded=True, detail="ok")

    async def _audit(spec, **kwargs):
        captured["audit_calls"] += 1
        captured["audit_configs"].append(dict(kwargs.get("config") or {}))
        if captured["audit_calls"] == 1:
            return AuditResult(
                verdict=AuditVerdict.BLOCKED,
                narrative="missing comma support",
                feature_audits=[
                    FeatureAudit(
                        feature_id="intword",
                        name="intword",
                        status="blocked",
                        detail="intword does not parse comma strings",
                        evidence_refs=["walkthrough.jsonl#L2"],
                    )
                ],
            )
        return AuditResult(
            verdict=AuditVerdict.PASSED,
            narrative="fixed",
            feature_audits=[
                FeatureAudit(
                    feature_id="intword",
                    name="intword",
                    status="passed",
                    detail="ok",
                )
            ],
        )

    async def _fix(agent_input):
        captured["fix_inputs"].append(agent_input)
        return BuildAgentOutput(
            succeeded=True,
            cost_usd=0.05,
            wall_s=0.5,
            detail="fixed feature",
        )

    def _render(spec, *, session_dir, audit_result, **kwargs):
        html = session_dir / "proof-packet.html"
        json_ = session_dir / "proof-packet.json"
        html.write_text("<html/>")
        json_.write_text("{}")
        assert audit_result.verdict == AuditVerdict.PASSED
        return html, json_

    monkeypatch.setattr("otto.runner.seed_fixtures", _seed)
    monkeypatch.setattr("otto.runner.run_audit", _audit)
    monkeypatch.setattr("otto.runner.render_run", _render)

    result = asyncio.run(
        run_pipeline(
            "x",
            tmp_path,
            session_dir,
            project_kind="library",
            brownfield=True,
            base_url=None,
            config={"provider": "codex", "_cli_overrides": {"provider": "codex"}},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=_fix,
            spec=spec,
        )
    )

    assert captured["audit_calls"] == 2
    assert captured["audit_configs"] == [
        {"provider": "codex", "_cli_overrides": {"provider": "codex"}},
        {"provider": "codex", "_cli_overrides": {"provider": "codex"}},
    ]
    assert len(captured["fix_inputs"]) == 1
    assert captured["fix_inputs"][0].group.id == "group_0"
    assert captured["fix_inputs"][0].feature_id == "intword"
    assert captured["fix_inputs"][0].config == {
        "provider": "codex",
        "_cli_overrides": {"provider": "codex"},
    }
    assert result.verdict == AuditVerdict.PASSED


def test_layer2_repairs_multiple_actionable_features_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    from otto.audit import FeatureAudit

    spec = Spec(
        intent="comma numeric strings",
        project_kind="library",
        groups=[
            Group(id="number-module", name="Number", owned_paths=["src/number.py"]),
            Group(id="filesize-module", name="Filesize", owned_paths=["src/filesize.py"]),
        ],
        features=[
            Feature(id="intword", name="intword", group_id="number-module"),
            Feature(id="clamp", name="clamp", group_id="number-module"),
            Feature(id="naturalsize", name="naturalsize", group_id="filesize-module"),
        ],
    )
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    captured: dict[str, Any] = {"audit_calls": 0, "fix_inputs": []}

    def _seed(spec, project_dir, session_dir=None):
        return SeedResult(succeeded=True, detail="ok")

    async def _audit(spec, **kwargs):
        captured["audit_calls"] += 1
        if captured["audit_calls"] == 1:
            return AuditResult(
                verdict=AuditVerdict.BLOCKED,
                narrative="two actionable failures",
                feature_audits=[
                    FeatureAudit(
                        feature_id="intword",
                        name="intword",
                        status="partial",
                        detail="returns the old value",
                        evidence_refs=["walkthrough.jsonl#L2"],
                    ),
                    FeatureAudit(
                        feature_id="clamp",
                        name="clamp",
                        status="blocked",
                        detail="No direct test evidence collected; not evaluated.",
                    ),
                    FeatureAudit(
                        feature_id="naturalsize",
                        name="naturalsize",
                        status="blocked",
                        detail="raises ValueError for comma strings",
                        evidence_refs=["walkthrough.jsonl#L4"],
                    ),
                ],
            )
        return AuditResult(
            verdict=AuditVerdict.PASSED,
            narrative="fixed",
            feature_audits=[
                FeatureAudit(
                    feature_id="intword",
                    name="intword",
                    status="passed",
                    detail="ok",
                ),
                FeatureAudit(
                    feature_id="naturalsize",
                    name="naturalsize",
                    status="passed",
                    detail="ok",
                ),
            ],
        )

    async def _fix(agent_input):
        captured["fix_inputs"].append(agent_input)
        return BuildAgentOutput(
            succeeded=True,
            cost_usd=0.05,
            wall_s=0.5,
            detail=f"fixed {agent_input.feature_id}",
        )

    def _render(spec, *, session_dir, audit_result, **kwargs):
        html = session_dir / "proof-packet.html"
        json_ = session_dir / "proof-packet.json"
        html.write_text("<html/>")
        json_.write_text("{}")
        assert audit_result.verdict == AuditVerdict.PASSED
        return html, json_

    monkeypatch.setattr("otto.runner.seed_fixtures", _seed)
    monkeypatch.setattr("otto.runner.run_audit", _audit)
    monkeypatch.setattr("otto.runner.render_run", _render)

    result = asyncio.run(
        run_pipeline(
            "x",
            tmp_path,
            session_dir,
            project_kind="library",
            brownfield=True,
            base_url=None,
            config={},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=_fix,
            spec=spec,
        )
    )

    assert captured["audit_calls"] == 2
    assert [i.feature_id for i in captured["fix_inputs"]] == ["intword", "naturalsize"]
    assert [i.group.id for i in captured["fix_inputs"]] == [
        "number-module",
        "filesize-module",
    ]
    assert result.verdict == AuditVerdict.PASSED


def test_layer2_reaudits_product_wide_when_retry_cap_leaves_failures(
    tmp_path: Path, monkeypatch
) -> None:
    from otto.audit import FeatureAudit

    spec = Spec(
        intent="comma numeric strings",
        project_kind="library",
        groups=[
            Group(id="number-module", name="Number", owned_paths=["src/number.py"]),
            Group(id="filesize-module", name="Filesize", owned_paths=["src/filesize.py"]),
        ],
        features=[
            Feature(id="intword", name="intword", group_id="number-module"),
            Feature(id="naturalsize", name="naturalsize", group_id="filesize-module"),
        ],
    )
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    captured: dict[str, Any] = {
        "audit_calls": 0,
        "audit_scopes": [],
        "fix_inputs": [],
        "render_verdict": None,
    }

    def _seed(spec, project_dir, session_dir=None):
        return SeedResult(succeeded=True, detail="ok")

    async def _audit(spec, **kwargs):
        captured["audit_calls"] += 1
        scope = tuple(kwargs.get("feature_scope_ids") or ())
        captured["audit_scopes"].append(scope)
        if scope:
            return AuditResult(
                verdict=AuditVerdict.PASSED,
                narrative="scoped false pass",
                feature_audits=[
                    FeatureAudit(
                        feature_id=scope[0],
                        name=scope[0],
                        status="passed",
                        detail="scoped repair passed",
                    )
                ],
            )
        if captured["audit_calls"] == 1:
            audits = [
                FeatureAudit(
                    feature_id="intword",
                    name="intword",
                    status="partial",
                    detail="malformed grouped input accepted",
                    evidence_refs=["walkthrough.jsonl#L4"],
                ),
                FeatureAudit(
                    feature_id="naturalsize",
                    name="naturalsize",
                    status="partial",
                    detail="malformed grouped input accepted",
                    evidence_refs=["walkthrough.jsonl#L5"],
                ),
            ]
        elif captured["audit_calls"] == 2:
            audits = [
                FeatureAudit(
                    feature_id="intword",
                    name="intword",
                    status="partial",
                    detail="still accepts malformed grouping",
                    evidence_refs=["walkthrough.jsonl#L4"],
                ),
                FeatureAudit(
                    feature_id="naturalsize",
                    name="naturalsize",
                    status="partial",
                    detail="still accepts malformed grouping",
                    evidence_refs=["walkthrough.jsonl#L5"],
                ),
            ]
        else:
            audits = [
                FeatureAudit(
                    feature_id="intword",
                    name="intword",
                    status="passed",
                    detail="fixed",
                    evidence_refs=["walkthrough.jsonl#L4"],
                ),
                FeatureAudit(
                    feature_id="naturalsize",
                    name="naturalsize",
                    status="partial",
                    detail="not fixed before repair cap exhausted",
                    evidence_refs=["walkthrough.jsonl#L5"],
                ),
            ]
        return AuditResult(
            verdict=AuditVerdict.PARTIAL,
            narrative="product still has unresolved feature failures",
            feature_audits=audits,
        )

    async def _fix(agent_input):
        captured["fix_inputs"].append(agent_input)
        return BuildAgentOutput(
            succeeded=True,
            cost_usd=0.05,
            wall_s=0.5,
            detail=f"fixed {agent_input.feature_id}",
        )

    def _render(spec, *, session_dir, audit_result, **kwargs):
        captured["render_verdict"] = audit_result.verdict
        html = session_dir / "proof-packet.html"
        json_ = session_dir / "proof-packet.json"
        html.write_text("<html/>")
        json_.write_text("{}")
        return html, json_

    monkeypatch.setattr("otto.runner.seed_fixtures", _seed)
    monkeypatch.setattr("otto.runner.run_audit", _audit)
    monkeypatch.setattr("otto.runner.render_run", _render)
    monkeypatch.setattr("otto.audit_loop._repair_cap_default", lambda: 3)

    result = asyncio.run(
        run_pipeline(
            "x",
            tmp_path,
            session_dir,
            project_kind="library",
            brownfield=True,
            base_url=None,
            config={},
            build_agent=_stub_agent,
            audit_agent=_stub_agent,
            fix_agent=_fix,
            spec=spec,
        )
    )

    assert captured["audit_scopes"] == [(), (), ()]
    assert [i.feature_id for i in captured["fix_inputs"]] == [
        "intword",
        "naturalsize",
        "intword",
    ]
    assert result.verdict == AuditVerdict.PARTIAL
    assert captured["render_verdict"] == AuditVerdict.PARTIAL
    assert result.repair_result is not None
    assert result.repair_result.halted_reason == "repair_attempts_cap_exhausted"


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
    assert result.build_result.group_results == []
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
        agent_session_ids={"g": "provider-thread-g"},
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
    assert build_kwargs["resume_agent_sessions"] == {"g": "provider-thread-g"}
    merge_kwargs = captured["merge_kwargs"]
    assert "skip_components" in merge_kwargs
    assert "g" in set(merge_kwargs["skip_components"])
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
        audit_agent_session_id="audit-thread-prior",
        layer2_agent_session_ids={"f1": "repair-thread-prior"},
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
    assert captured["audit_kwargs"]["resume_agent_session_id"] == "audit-thread-prior"


def test_resume_plan_threads_layer2_agent_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    from otto.resume import ResumePlan

    spec = _spec(with_features=True)
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch,
        audit=_partial_audit_with_failing_feature(spec),
        order=order,
    )

    async def _unused_bridge(*_args, **_kwargs):
        raise AssertionError("repair_failing_features is stubbed")

    def _capture_layer2_bridge(**kwargs):
        captured["layer2_resume_agent_sessions"] = kwargs.get("resume_agent_sessions")
        return _unused_bridge

    monkeypatch.setattr("otto.runner._make_layer2_fix_agent", _capture_layer2_bridge)

    plan = ResumePlan(
        session_id="sess",
        paused_session_dir=session_dir,
        spec_hash="deadbeef",
        landed_components=frozenset(),
        pending_components=frozenset({"g"}),
        audit_finished=False,
        layer2_agent_session_ids={"f1": "repair-thread-prior"},
    )

    asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=_stub_agent,
            spec=spec,
            resume_plan=plan,
            enable_audit_repair=True,
        )
    )

    assert captured["repair_calls"] == 1
    assert captured["layer2_resume_agent_sessions"] == {"f1": "repair-thread-prior"}


# ---------------------------------------------------------------------------
# A6 — mid-build spec edit invalidation re-dispatch
# ---------------------------------------------------------------------------


def test_spec_edit_invalidation_redispatches_affected_groups(
    tmp_path: Path, monkeypatch
) -> None:
    """Mid-build spec edit fires a journal event; runner re-dispatches the
    affected Group with a fresh `run_build` pass.

    Wiring: stub `run_build` to (1) emit a `group.invalidated_by_spec_edit`
    event on its first call, (2) write a post-edit Spec to disk so
    re-load picks it up, (3) return distinct results across the two
    calls so the merge is observable.
    """
    from otto.spec_compile import Group as G, Spec as S, persist_spec

    spec = S(intent="x", groups=[G(id="g", name="G")])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    (session_dir / "spec").mkdir()
    persist_spec(spec, session_dir / "spec" / "spec.json", allow_initial=True)

    order = _Order()

    call_count = {"n": 0}

    async def _build(spec_arg, *, project_dir, session_dir, **kwargs):
        call_count["n"] += 1
        order.add(f"build-{call_count['n']}")
        if call_count["n"] == 1:
            # First pass: simulate a mid-build edit. Persist the
            # post-edit spec to disk so re-dispatch can re-load it,
            # and emit the invalidation event.
            new_spec = S(intent="x",
                         groups=[G(id="g", name="G renamed")])
            persist_spec(
                new_spec, session_dir / "spec" / "spec.json",
                allow_initial=True,
            )
            from otto.spec_state import emit as _emit
            _emit(
                session_dir,
                "group.invalidated_by_spec_edit",
                group_id="g",
                detail="spec edit changed name",
                direct=True,
            )
            # Return BLOCKED for "g" — the build saw the invalidation
            # mid-flight and aborted.
            return BuildResult(
                spec_session_dir=session_dir,
                group_results=[
                    GroupResult(
                        group_id="g",
                        status=GroupStatus.BLOCKED,
                        attempts=1,
                        branch="b",
                        worktree=session_dir,
                        failure_narrative=(
                            "invalidated by spec edit: spec edit changed name"
                        ),
                        cost_usd=0.05,
                    ),
                ],
                total_cost_usd=0.05,
                total_wall_s=1.0,
            )
        else:
            # Second pass: re-dispatch with fresh spec — succeeds.
            return BuildResult(
                spec_session_dir=session_dir,
                group_results=[
                    GroupResult(
                        group_id="g",
                        status=GroupStatus.PASSING,
                        attempts=1,
                        branch="b2",
                        worktree=session_dir,
                        cost_usd=0.07,
                    ),
                ],
                total_cost_usd=0.07,
                total_wall_s=1.0,
            )

    async def _merge(*args, **kwargs):
        order.add("merge")
        return MergeQueueResult(landed_ids=["g"])

    async def _audit(spec_arg, **kwargs):
        order.add("audit")
        return _passing_audit(spec_arg)

    def _render(spec_arg, *, session_dir, **kwargs):
        order.add("render")
        return session_dir / "h.html", session_dir / "j.json"

    def _seed(spec_arg, project_dir, session_dir=None):
        order.add("seed")
        return SeedResult(succeeded=True, detail="no fixtures")

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
            allow_in_flight_spec_edits=True,
        )
    )

    # run_build was called twice (initial + re-dispatch) and the order
    # is preserved.
    assert call_count["n"] == 2
    assert "build-1" in order.events and "build-2" in order.events
    # Lifecycle was set + cleared.
    lifecycle_path = session_dir / "spec" / "lifecycle.json"
    assert lifecycle_path.exists()
    import json as _json
    data = _json.loads(lifecycle_path.read_text())
    # Final state is "approved" (the editing window closed).
    assert data["lifecycle"] == "approved"
    # Final BuildResult reflects the SECOND pass for "g" (PASSING),
    # not the first pass's BLOCKED.
    assert result.build_result is not None
    g_result = next(
        r for r in result.build_result.group_results if r.group_id == "g"
    )
    assert g_result.status == GroupStatus.PASSING
    # Costs accumulate across both passes.
    assert result.build_result.total_cost_usd == pytest.approx(0.05 + 0.07)


def test_spec_edit_invalidation_does_not_redispatch_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    """Default builds execute the approved spec as a frozen run contract."""
    from otto.spec_compile import Group as G, Spec as S, persist_spec

    spec = S(intent="x", groups=[G(id="g", name="G")])
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    (session_dir / "spec").mkdir()
    persist_spec(spec, session_dir / "spec" / "spec.json", allow_initial=True)
    call_count = {"n": 0}

    async def _build(spec_arg, *, project_dir, session_dir, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            from otto.spec_state import emit as _emit

            _emit(
                session_dir,
                "group.invalidated_by_spec_edit",
                group_id="g",
                detail="spec edit changed name",
                direct=True,
            )
        return BuildResult(
            spec_session_dir=session_dir,
            group_results=[
                GroupResult(
                    group_id="g",
                    status=GroupStatus.PASSING,
                    attempts=1,
                    branch="b",
                    worktree=session_dir,
                ),
            ],
        )

    async def _merge(*args, **kwargs):
        return MergeQueueResult(landed_ids=["g"])

    def _seed(spec_arg, project_dir, session_dir=None):
        return SeedResult(succeeded=True, detail="no fixtures")

    async def _audit(spec_arg, **kwargs):
        return _passing_audit(spec_arg)

    def _render(spec_arg, *, session_dir, **kwargs):
        return session_dir / "h.html", session_dir / "j.json"

    monkeypatch.setattr("otto.runner.seed_fixtures", _seed)
    monkeypatch.setattr("otto.runner.run_build", _build)
    monkeypatch.setattr("otto.runner.run_merge_queue", _merge)
    monkeypatch.setattr("otto.runner.run_audit", _audit)
    monkeypatch.setattr("otto.runner.render_run", _render)

    asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
        )
    )

    assert call_count["n"] == 1
    assert not (session_dir / "spec" / "lifecycle.json").exists()


def test_spec_edit_invalidation_no_op_when_no_event(
    tmp_path: Path, monkeypatch
) -> None:
    """Without any invalidation event, run_build is called exactly once."""
    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    (session_dir / "spec").mkdir()  # for lifecycle write target

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
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
        )
    )

    assert captured["build_calls"] == 1


def test_spec_edit_invalidation_clears_after_user_abort(tmp_path: Path) -> None:
    """A Group invalidated by spec edit but then aborted by the operator is
    terminal; the runner must not re-dispatch it.
    """
    from otto.spec_state import emit

    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    emit(
        session_dir,
        "group.invalidated_by_spec_edit",
        group_id="g",
        detail="feature_ids changed",
    )
    assert _invalidated_group_ids(session_dir) == {"g"}

    emit(session_dir, "group.aborted_by_user", group_id="g", detail="abort")
    assert _invalidated_group_ids(session_dir) == set()


# ---------------------------------------------------------------------------
# A13 — review-gate (compile → review-gate → build)
# ---------------------------------------------------------------------------


def test_review_gate_pauses_until_approved(
    tmp_path: Path, monkeypatch
) -> None:
    """When review_gate=True, the pipeline pauses after compile, emits
    spec.review_pending, and resumes once spec.review_approved appears
    in the journal."""
    from otto.spec_state import emit, iter_events

    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch, audit=_passing_audit(spec), order=order,
    )

    # The gate's poll loop is async-friendly. We approve via a separate
    # coroutine that fires after a short delay so the gate has to wait
    # at least one poll iteration. This proves the runner observes the
    # journal mid-flight, not just the eventual final state.
    async def driver():
        # Schedule the approval before kicking off the pipeline so it
        # races the gate's first poll.
        async def approve_after_delay():
            await asyncio.sleep(0.05)
            emit(session_dir, "spec.review_approved", detail="user approved")

        approval_task = asyncio.create_task(approve_after_delay())
        try:
            return await run_pipeline(
                "x", tmp_path, session_dir,
                project_kind="webapp", brownfield=False, base_url=None,
                config={},
                build_agent=_stub_agent, audit_agent=_stub_agent,
                fix_agent=None,
                spec=spec,
                review_gate=True,
                gate_timeout_s=10.0,
                gate_poll_s=0.01,
            )
        finally:
            await approval_task

    result = asyncio.run(driver())

    # Gate did NOT halt the run.
    assert result.verdict == AuditVerdict.PASSED
    assert "build" in order.events
    assert captured["build_calls"] == 1

    # spec.review_pending was emitted exactly once before build ran.
    kinds = [ev.kind for ev in iter_events(session_dir)]
    assert "spec.review_pending" in kinds
    assert "spec.review_approved" in kinds
    # Gate emits review_pending BEFORE the user's approval lands.
    pending_idx = kinds.index("spec.review_pending")
    approved_idx = kinds.index("spec.review_approved")
    assert pending_idx < approved_idx


def test_review_gate_reloads_approved_spec_before_seed_and_build(
    tmp_path: Path, monkeypatch
) -> None:
    from otto.spec_state import emit

    original = _spec()
    edited = _spec()
    edited.intent = "edited intent"
    edited.groups[0].name = "Edited Group"
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    (session_dir / "spec").mkdir()
    persist_spec(edited, session_dir / "spec" / "spec.json", allow_initial=True)
    order = _Order()
    captured = _wire_stubs(
        monkeypatch, audit=_passing_audit(edited), order=order,
    )

    async def driver():
        async def approve_after_delay():
            await asyncio.sleep(0.05)
            emit(session_dir, "spec.review_approved", detail="user approved")

        approval_task = asyncio.create_task(approve_after_delay())
        try:
            return await run_pipeline(
                "x", tmp_path, session_dir,
                project_kind="webapp", brownfield=False, base_url=None,
                config={},
                build_agent=_stub_agent, audit_agent=_stub_agent,
                fix_agent=None,
                spec=original,
                review_gate=True,
                gate_timeout_s=10.0,
                gate_poll_s=0.01,
            )
        finally:
            await approval_task

    result = asyncio.run(driver())

    assert result.spec.intent == "edited intent"
    assert captured["seed_specs"][0].intent == "edited intent"
    assert captured["build_specs"][0].groups[0].name == "Edited Group"


def test_review_gate_times_out_blocks_build(
    tmp_path: Path, monkeypatch
) -> None:
    """When the gate timeout expires with no approval, the run halts
    with verdict=BLOCKED and build never runs."""
    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    captured = _wire_stubs(
        monkeypatch, audit=_passing_audit(spec), order=order,
    )

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
            review_gate=True,
            gate_timeout_s=0.1,
            gate_poll_s=0.02,
        )
    )

    assert result.verdict == AuditVerdict.BLOCKED
    assert "review_gate_timeout" in result.halted_reason
    # No phase past the gate ran.
    assert captured["build_calls"] == 0
    assert captured["merge_calls"] == 0
    assert captured["audit_calls"] == 0


def test_review_gate_off_by_default(tmp_path: Path, monkeypatch) -> None:
    """Default invocation (review_gate omitted) does NOT pause."""
    from otto.spec_state import iter_events

    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    _wire_stubs(monkeypatch, audit=_passing_audit(spec), order=order)

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
        )
    )
    assert result.verdict == AuditVerdict.PASSED
    kinds = [ev.kind for ev in iter_events(session_dir)]
    # Crucially: no review_pending event when the gate is off.
    assert "spec.review_pending" not in kinds


def test_review_gate_announce_called_with_session_id(
    tmp_path: Path, monkeypatch
) -> None:
    """The gate_announce callback fires once with the session id so the
    CLI can print the operator-facing URL."""
    from otto.spec_state import emit

    spec = _spec()
    session_dir = tmp_path / "sess-id-xyz"
    session_dir.mkdir()
    _wire_stubs(monkeypatch, audit=_passing_audit(spec), order=_Order())

    seen: list[str] = []

    async def driver():
        async def approve_after_delay():
            await asyncio.sleep(0.02)
            emit(session_dir, "spec.review_approved", detail="ok")

        task = asyncio.create_task(approve_after_delay())
        try:
            return await run_pipeline(
                "x", tmp_path, session_dir,
                project_kind="webapp", brownfield=False, base_url=None,
                config={},
                build_agent=_stub_agent, audit_agent=_stub_agent,
                fix_agent=None,
                spec=spec,
                review_gate=True,
                gate_timeout_s=5.0,
                gate_poll_s=0.01,
                gate_announce=seen.append,
            )
        finally:
            await task

    asyncio.run(driver())
    assert seen == ["sess-id-xyz"]


def test_review_gate_skipped_on_resume(tmp_path: Path, monkeypatch) -> None:
    """Resume path bypasses the gate even when --review-gate is set —
    the operator already approved on the prior session."""
    from otto.resume import ResumePlan
    from otto.spec_state import iter_events

    spec = _spec()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    order = _Order()
    _wire_stubs(monkeypatch, audit=_passing_audit(spec), order=order)

    plan = ResumePlan(
        session_id="sess",
        paused_session_dir=session_dir,
        spec_hash="deadbeef",
        landed_components=frozenset(),
        pending_components=frozenset({"g"}),
        audit_finished=False,
    )

    result = asyncio.run(
        run_pipeline(
            "x", tmp_path, session_dir,
            project_kind="webapp", brownfield=False, base_url=None, config={},
            build_agent=_stub_agent, audit_agent=_stub_agent, fix_agent=None,
            spec=spec,
            resume_plan=plan,
            review_gate=True,
            gate_timeout_s=0.1,  # would time out fast if the gate ran
            gate_poll_s=0.02,
        )
    )
    assert result.verdict == AuditVerdict.PASSED
    kinds = [ev.kind for ev in iter_events(session_dir)]
    assert "spec.review_pending" not in kinds


# ---------------------------------------------------------------------------
# A7 — abort-a-group lands honestly through the build phase
# ---------------------------------------------------------------------------


def test_abort_group_marks_target_blocked_and_continues_other_groups(
    tmp_path: Path,
) -> None:
    """End-to-end-ish: with a Spec of two groups, aborting one mid-build
    causes the build phase to mark it BLOCKED while the other group runs
    to completion. Uses the real `run_build` (so the abort poll path is
    exercised), with a fake build agent and no LLM cost.
    """
    import subprocess

    from otto.build import (
        BuildAgentInput,
        BuildAgentOutput,
        run_build,
    )
    from otto.mission_control.actions import execute_abort_group
    from otto.spec_compile import (
        RepoTestCheck,
        StructureDecisions,
    )

    # Initialize an empty git repo so build's git-diff scope check works.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True
    )
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init", "--no-verify"], cwd=tmp_path, check=True
    )

    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    passing = RepoTestCheck(command=("python", "-c", "print('ok')"), timeout_s=10)
    spec = Spec(
        intent="abort test",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="g_kill",
                name="will-be-aborted",
                dependencies=[],
                owned_paths=[],
                feature_ids=[],
                checks=[passing],
            ),
            Group(
                id="g_live",
                name="should-still-run",
                dependencies=[],
                owned_paths=[],
                feature_ids=[],
                checks=[passing],
            ),
        ],
    )

    # Pre-emit the abort event BEFORE build starts. This exercises the
    # abort-poll branch without the timing fragility of mid-flight aborts.
    abort_result = execute_abort_group(
        session_dir, "g_kill", reason="operator pulled it"
    )
    assert abort_result.ok is True

    async def fake_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True, cost_usd=0.0, wall_s=0.1, detail="ok")

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )

    by_id = {r.group_id: r for r in result.group_results}
    assert by_id["g_kill"].status == GroupStatus.BLOCKED
    assert "aborted_by_user" in by_id["g_kill"].failure_narrative
    assert by_id["g_live"].status == GroupStatus.PASSING
    assert "g_kill" not in result.passing_ids
    assert "g_live" in result.passing_ids


def test_pause_flag_blocks_phase_callback_until_resume(tmp_path: Path) -> None:
    """`_wait_while_paused` blocks while the journal is paused and
    returns immediately once a resume event is appended. We verify the
    behavior directly to avoid wiring a long-lived async pipeline.
    """
    import threading

    from otto import runner as runner_mod
    from otto.mission_control.actions import (
        execute_pause_run,
        execute_resume_run,
    )

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    # Tighter poll interval keeps the test deterministic and fast.
    runner_mod.PAUSE_POLL_INTERVAL_S = 0.01

    # Not paused → returns immediately.
    runner_mod._wait_while_paused(session_dir)

    # Now pause; spawn a thread that resumes after a brief delay; verify
    # the wait-loop returns once it observes the resume event.
    execute_pause_run(session_dir)

    def _resume_after_delay() -> None:
        import time as _t

        _t.sleep(0.05)
        execute_resume_run(session_dir)

    waiter = threading.Thread(target=_resume_after_delay, daemon=True)
    waiter.start()

    runner_mod._wait_while_paused(session_dir)  # should return after the resume event
    waiter.join(timeout=2.0)
    from otto import spec_state

    assert spec_state.is_run_paused_by_user(session_dir) is False
