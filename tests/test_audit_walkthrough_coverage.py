"""Tests for `_validate_walkthrough_jsonl` (A2.1, research §A2).

Walkthrough Feature-tagging coverage validator wired into run_audit.
The helper reads `<walk_log_dir>/walkthrough.jsonl`, parses each line
into a `WalkthroughEntry` against the spec, and computes per-Feature
coverage stats. Returns a JSON-friendly dict for AuditResult.
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.audit import _validate_walkthrough_jsonl
from otto.spec_compile import Feature, Group, Spec


def _spec_with_features(*ids: str) -> Spec:
    return Spec(
        intent="x",
        groups=[Group(id="g", name="G")],
        features=[
            Feature(id=fid, name=fid.replace("-", " ").title(), group_id="g")
            for fid in ids
        ],
    )


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_no_jsonl_returns_none(tmp_path: Path) -> None:
    """Walkthroughs without a jsonl artifact (e.g. no-op) → None.
    Audit proceeds; coverage just isn't surfaced."""
    spec = _spec_with_features("f1")
    _, out = _validate_walkthrough_jsonl(tmp_path, spec)
    assert out is None


def test_full_coverage(tmp_path: Path) -> None:
    spec = _spec_with_features("login", "logout")
    _write_jsonl(
        tmp_path / "walkthrough.jsonl",
        [
            {"t": "0:01", "action_kind": "click", "feature_ids": ["login"]},
            {"t": "0:02", "action_kind": "assert", "feature_ids": ["login"]},
            {"t": "0:03", "action_kind": "click", "feature_ids": ["logout"]},
        ],
    )
    _, out = _validate_walkthrough_jsonl(tmp_path, spec)
    assert out is not None
    assert out["total_entries"] == 3
    assert out["non_exploration_total"] == 3
    assert out["tagged_entries"] == 3
    assert out["untagged_entries"] == 0
    assert out["coverage_ratio"] == 1.0
    assert out["meets_threshold"] is True
    assert out["per_feature_evidence_count"]["login"] == 2
    assert out["per_feature_evidence_count"]["logout"] == 1


def test_below_threshold(tmp_path: Path) -> None:
    spec = _spec_with_features("login")
    # 8 non-exploration entries, only 7 tagged → 7/8 = 87.5% < 90%
    entries = [
        {"t": f"0:{i:02d}", "action_kind": "click", "feature_ids": ["login"]}
        for i in range(7)
    ]
    entries.append({"t": "0:99", "action_kind": "click", "feature_ids": []})
    _write_jsonl(tmp_path / "walkthrough.jsonl", entries)
    _, out = _validate_walkthrough_jsonl(tmp_path, spec)
    assert out is not None
    assert out["tagged_entries"] == 7
    assert out["untagged_entries"] == 1
    assert out["coverage_ratio"] == 7 / 8
    assert out["meets_threshold"] is False


def test_exploration_entries_excluded_from_threshold(tmp_path: Path) -> None:
    """Exploration entries don't need feature_ids; they're allowed bare."""
    spec = _spec_with_features("login")
    _write_jsonl(
        tmp_path / "walkthrough.jsonl",
        [
            {"t": "0:01", "action_kind": "exploration"},  # no feature_ids ok
            {"t": "0:02", "action_kind": "exploration"},
            {"t": "0:03", "action_kind": "click", "feature_ids": ["login"]},
        ],
    )
    _, out = _validate_walkthrough_jsonl(tmp_path, spec)
    assert out is not None
    assert out["exploration_entries"] == 2
    assert out["non_exploration_total"] == 1
    assert out["tagged_entries"] == 1
    assert out["coverage_ratio"] == 1.0
    assert out["meets_threshold"] is True


def test_empty_jsonl_vacuous_pass(tmp_path: Path) -> None:
    """An empty walkthrough.jsonl is degenerate but not an error."""
    spec = _spec_with_features("login")
    (tmp_path / "walkthrough.jsonl").write_text("")
    _, out = _validate_walkthrough_jsonl(tmp_path, spec)
    assert out is not None
    assert out["total_entries"] == 0
    assert out["coverage_ratio"] == 1.0  # vacuous pass per CoverageReport
    assert out["meets_threshold"] is True


def test_unknown_feature_id_surfaces(tmp_path: Path) -> None:
    spec = _spec_with_features("login")
    _write_jsonl(
        tmp_path / "walkthrough.jsonl",
        [
            {"t": "0:01", "action_kind": "click", "feature_ids": ["typo-id"]},
        ],
    )
    _, out = _validate_walkthrough_jsonl(tmp_path, spec)
    assert out is not None
    assert "typo-id" in out["unknown_feature_id_refs"]


def test_malformed_json_line_recorded(tmp_path: Path) -> None:
    spec = _spec_with_features("login")
    p = tmp_path / "walkthrough.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"t":"0:01","action_kind":"click","feature_ids":["login"]}\n'
        "this is not json\n"
        '{"t":"0:02","action_kind":"click","feature_ids":["login"]}\n',
    )
    _, out = _validate_walkthrough_jsonl(tmp_path, spec)
    assert out is not None
    # Two valid entries, one parse error
    assert out["total_entries"] == 2
    assert len(out["parse_errors"]) == 1
    assert "line 2" in out["parse_errors"][0]


def test_blank_lines_skipped(tmp_path: Path) -> None:
    spec = _spec_with_features("login")
    p = tmp_path / "walkthrough.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"t":"0:01","action_kind":"click","feature_ids":["login"]}\n'
        "\n"
        "   \n"
        '{"t":"0:02","action_kind":"click","feature_ids":["login"]}\n',
    )
    _, out = _validate_walkthrough_jsonl(tmp_path, spec)
    assert out is not None
    assert out["total_entries"] == 2
    assert out["parse_errors"] == []
