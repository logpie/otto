"""Tests for A3.1 walkthrough-entry plumbing through AuditResult.

Closes the gap surfaced by A3 sub-agent (tick 60): parsed walkthrough
entries from `walkthrough.jsonl` flow

    `_validate_walkthrough_jsonl`
        → `AuditResult.walkthrough_entries`
        → `compose_proof_packet`
        → `build_feature_proof_blocks`
        → `FeatureProofBlock.walkthrough_entries`

so per-Feature proof blocks render their walkthrough trace.
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.audit import (
    AuditResult,
    AuditVerdict,
    FeatureAudit,
    _validate_walkthrough_jsonl,
)
from otto.build import BuildResult, GroupResult, GroupStatus
from otto.checks import Evidence
from otto.merge_queue import MergeQueueResult, MergeResult, MergeStatus
from otto.render import compose_proof_packet
from otto.spec_compile import (
    Feature,
    Group,
    RepoTestCheck,
    Spec,
    StructureDecisions,
    WalkthroughEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_with_features(*ids: str) -> Spec:
    """Spec with one Group plus Features, suitable for run_audit-style use."""
    return Spec(
        intent="x",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(
                id="g",
                name="G",
                dependencies=[],
                owned_paths=["src/**"],
                feature_ids=["build"],
                checks=[RepoTestCheck(command=("true",), timeout_s=30)],
            ),
        ],
        features=[
            Feature(
                id=fid,
                name=fid.replace("-", " ").title(),
                description=f"feature {fid}",
                group_id="g",
            )
            for fid in ids
        ],
        non_goals=[],
        done_means=[f"{fid} works" for fid in ids],
    )


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _evidence(passed: bool = True) -> Evidence:
    return Evidence(
        passed=passed,
        started_at="2026-05-04T00:00:00Z",
        duration_s=0.1,
        detail="ok",
        artifacts=[],
        raw={},
    )


def _passing_build(tmp_path: Path) -> BuildResult:
    return BuildResult(
        spec_session_dir=tmp_path,
        group_results=[
            GroupResult(
                group_id="g",
                status=GroupStatus.PASSING,
                attempts=1,
                branch="i2p/x/g",
                worktree=tmp_path,
                last_evidence=[_evidence(True)],
            ),
        ],
    )


def _landed_merge() -> MergeQueueResult:
    return MergeQueueResult(
        landed_ids=["g"],
        results=[
            MergeResult(
                group_id="g",
                status=MergeStatus.LANDED,
                landed_commit="abc1234",
                group_recheck_evidence=[_evidence(True)],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# `_validate_walkthrough_jsonl` returns (entries, coverage)
# ---------------------------------------------------------------------------


def test_validate_returns_entries_and_coverage(tmp_path: Path) -> None:
    """The helper now returns a tuple: parsed entries + coverage dict."""
    spec = _spec_with_features("login", "logout")
    _write_jsonl(
        tmp_path / "walkthrough.jsonl",
        [
            {"t": "0:01", "action_kind": "browser_navigation", "feature_ids": ["login"]},
            {"t": "0:02", "action_kind": "api_request", "feature_ids": ["logout"]},
        ],
    )
    entries, coverage = _validate_walkthrough_jsonl(tmp_path, spec)

    # Entries are parsed WalkthroughEntry objects in input order.
    assert len(entries) == 2
    assert all(isinstance(e, WalkthroughEntry) for e in entries)
    assert entries[0].feature_ids == ["login"]
    assert entries[0].action_kind == "browser_navigation"
    assert entries[1].feature_ids == ["logout"]

    # Coverage dict shape unchanged.
    assert coverage is not None
    assert coverage["total_entries"] == 2
    assert coverage["tagged_entries"] == 2


def test_validate_returns_empty_entries_when_no_jsonl(tmp_path: Path) -> None:
    """No walkthrough.jsonl → ([], None) so callers can splat directly."""
    spec = _spec_with_features("login")
    entries, coverage = _validate_walkthrough_jsonl(tmp_path, spec)
    assert entries == []
    assert coverage is None


def test_validate_drops_unparseable_lines_from_entries(tmp_path: Path) -> None:
    """Malformed JSON lines are recorded in parse_errors but excluded from entries."""
    spec = _spec_with_features("login")
    p = tmp_path / "walkthrough.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"t":"0:01","action_kind":"browser_navigation","feature_ids":["login"]}\n'
        "this is not json\n"
        '{"t":"0:02","action_kind":"browser_navigation","feature_ids":["login"]}\n',
    )
    entries, coverage = _validate_walkthrough_jsonl(tmp_path, spec)
    assert len(entries) == 2
    assert coverage is not None
    assert len(coverage["parse_errors"]) == 1


# ---------------------------------------------------------------------------
# `AuditResult.walkthrough_entries` field
# ---------------------------------------------------------------------------


def test_audit_result_walkthrough_entries_default_empty() -> None:
    """Default: AuditResult constructed without entries gives empty list."""
    r = AuditResult(verdict=AuditVerdict.PASSED, narrative="ok")
    assert r.walkthrough_entries == []


def test_audit_result_walkthrough_entries_carries_parsed_objects() -> None:
    """AuditResult holds the parsed WalkthroughEntry list verbatim."""
    e1 = WalkthroughEntry(
        t="0:01", feature_ids=["login"], action_kind="browser_navigation"
    )
    e2 = WalkthroughEntry(t="0:02", feature_ids=["login"], action_kind="api_request")
    r = AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="ok",
        walkthrough_entries=[e1, e2],
    )
    assert r.walkthrough_entries == [e1, e2]


# ---------------------------------------------------------------------------
# End-to-end: entries flow through compose_proof_packet → FeatureProofBlock
# ---------------------------------------------------------------------------


def test_compose_proof_packet_threads_walkthrough_entries_into_feature_blocks(
    tmp_path: Path,
) -> None:
    """The crux: an entry tagged with `feature_ids=["login"]` MUST appear in
    the `login` Feature's proof block. Closes A3.1 gap (tick 60)."""
    spec = _spec_with_features("login", "logout")
    login_entry = WalkthroughEntry(
        t="0:01",
        feature_ids=["login"],
        action_kind="browser_navigation",
        narrative="user logs in",
        extras={"url": "/login"},
    )
    logout_entry = WalkthroughEntry(
        t="0:02",
        feature_ids=["logout"],
        action_kind="browser_navigation",
        narrative="user logs out",
    )
    audit = AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="all good",
        feature_audits=[
            FeatureAudit(name="Login", status="passed", detail="login ok"),
            FeatureAudit(name="Logout", status="passed", detail="logout ok"),
        ],
        walkthrough_entries=[login_entry, logout_entry],
    )

    packet = compose_proof_packet(
        spec,
        _passing_build(tmp_path),
        _landed_merge(),
        audit,
        wall_s=1.0,
        cost_usd=0.0,
    )

    # `feature_proofs` carries one block per Feature with walkthrough entries
    # routed by feature_id.
    by_id = {fp["feature_id"]: fp for fp in packet.features}
    assert set(by_id.keys()) == {"login", "logout"}

    login_block = by_id["login"]
    logout_block = by_id["logout"]

    # Login Feature gets exactly the login-tagged entry.
    assert len(login_block["walkthrough_entries"]) == 1
    assert login_block["walkthrough_entries"][0]["feature_ids"] == ["login"]
    assert login_block["walkthrough_entries"][0]["action_kind"] == "browser_navigation"

    # Logout Feature gets exactly the logout-tagged entry.
    assert len(logout_block["walkthrough_entries"]) == 1
    assert logout_block["walkthrough_entries"][0]["feature_ids"] == ["logout"]


def test_compose_proof_packet_empty_walkthrough_entries_renders_no_trace(
    tmp_path: Path,
) -> None:
    """When AuditResult has no walkthrough entries, blocks render but with
    empty `walkthrough_entries` — research §4 honesty rule."""
    spec = _spec_with_features("login")
    audit = AuditResult(
        verdict=AuditVerdict.PASSED,
        narrative="ok",
        feature_audits=[
            FeatureAudit(name="Login", status="passed", detail="login ok"),
        ],
        # walkthrough_entries omitted → defaults to []
    )

    packet = compose_proof_packet(
        spec,
        _passing_build(tmp_path),
        _landed_merge(),
        audit,
        wall_s=1.0,
        cost_usd=0.0,
    )

    by_id = {fp["feature_id"]: fp for fp in packet.features}
    assert by_id["login"]["walkthrough_entries"] == []
