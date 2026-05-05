"""Tests for A1b.7 strict-mode walkthrough parsing (research §2.7).

`parse_walkthrough_entry(strict=True)` and `validate_walkthrough_jsonl_strict`
enforce the WalkthroughEntry action_kind/feature_id contract by
dropping non-compliant entries instead of warning.

The default permissive mode must keep its existing back-compat
behaviour so the audit pass (otto/audit.py) keeps surfacing oddities
as advisory warnings.
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.spec_compile import (
    Feature,
    Group,
    Spec,
    WALKTHROUGH_ACTION_KINDS,
    parse_walkthrough_entry,
    validate_walkthrough_jsonl_strict,
)


def _spec(*feature_ids: str) -> Spec:
    return Spec(
        intent="x",
        groups=[Group(id="g", name="G")],
        features=[
            Feature(id=fid, name=fid.replace("-", " ").title(), group_id="g")
            for fid in feature_ids
        ],
    )


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


# ---------------------------------------------------------------------------
# parse_walkthrough_entry(strict=True): rejection cases
# ---------------------------------------------------------------------------


def test_strict_unknown_action_kind_rejected() -> None:
    spec = _spec("login")
    payload = {
        "t": "0:01",
        "action_kind": "click",  # not in WALKTHROUGH_ACTION_KINDS
        "feature_ids": ["login"],
    }
    entry, messages = parse_walkthrough_entry(payload, spec, strict=True)
    assert entry is None
    assert any("unknown_action_kind" in m for m in messages)


def test_strict_untagged_non_exploration_rejected() -> None:
    spec = _spec("login")
    payload = {
        "t": "0:01",
        "action_kind": "browser_navigation",  # valid kind, but no feature_ids
        "feature_ids": [],
    }
    entry, messages = parse_walkthrough_entry(payload, spec, strict=True)
    assert entry is None
    assert any("untagged_non_exploration" in m for m in messages)


def test_strict_unknown_feature_id_rejected() -> None:
    spec = _spec("login")
    payload = {
        "t": "0:01",
        "action_kind": "browser_navigation",
        "feature_ids": ["does-not-exist"],
    }
    entry, messages = parse_walkthrough_entry(payload, spec, strict=True)
    assert entry is None
    assert any("unknown_feature_id" in m and "does-not-exist" in m for m in messages)


def test_strict_exploration_with_no_feature_ids_accepted() -> None:
    """Exploration is the catch-all for setup/cleanup — never rejected."""
    spec = _spec("login")
    payload = {
        "t": "0:00",
        "action_kind": "exploration",
        "feature_ids": [],
    }
    entry, messages = parse_walkthrough_entry(payload, spec, strict=True)
    assert entry is not None
    assert entry.action_kind == "exploration"
    assert messages == []


def test_strict_happy_path_known_kind_and_feature() -> None:
    spec = _spec("login")
    assert "browser_navigation" in WALKTHROUGH_ACTION_KINDS
    payload = {
        "t": "0:01",
        "action_kind": "browser_navigation",
        "feature_ids": ["login"],
        "narrative": "navigated to /login",
        "url": "/login",
    }
    entry, messages = parse_walkthrough_entry(payload, spec, strict=True)
    assert entry is not None
    assert entry.feature_ids == ["login"]
    assert entry.extras.get("url") == "/login"
    assert messages == []


def test_strict_collects_multiple_violations_per_entry() -> None:
    """A single entry can violate several rules; all surface."""
    spec = _spec("login")
    payload = {
        "t": "0:01",
        "action_kind": "click",                     # unknown kind
        "feature_ids": ["login", "ghost-feature"],  # one unknown ref
    }
    entry, messages = parse_walkthrough_entry(payload, spec, strict=True)
    assert entry is None
    assert any("unknown_action_kind" in m for m in messages)
    assert any("ghost-feature" in m for m in messages)


# ---------------------------------------------------------------------------
# parse_walkthrough_entry default (permissive) preserves back-compat
# ---------------------------------------------------------------------------


def test_permissive_unknown_action_kind_warns_not_rejects() -> None:
    spec = _spec("login")
    payload = {
        "t": "0:01",
        "action_kind": "click",
        "feature_ids": ["login"],
    }
    entry, warnings = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    assert entry.action_kind == "click"
    assert any("unknown_action_kind" in w for w in warnings)


def test_permissive_untagged_non_exploration_warns_not_rejects() -> None:
    spec = _spec("login")
    payload = {
        "t": "0:01",
        "action_kind": "browser_navigation",
        "feature_ids": [],
    }
    entry, warnings = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    assert any("untagged_non_exploration" in w for w in warnings)


def test_permissive_unknown_feature_id_warns_not_rejects() -> None:
    spec = _spec("login")
    payload = {
        "t": "0:01",
        "action_kind": "browser_navigation",
        "feature_ids": ["does-not-exist"],
    }
    entry, warnings = parse_walkthrough_entry(payload, spec)
    assert entry is not None
    assert entry.feature_ids == ["does-not-exist"]
    assert any("unknown_feature_id" in w for w in warnings)


# ---------------------------------------------------------------------------
# validate_walkthrough_jsonl_strict
# ---------------------------------------------------------------------------


def test_strict_jsonl_happy_path_meets_threshold(tmp_path: Path) -> None:
    spec = _spec("login", "logout")
    jsonl = tmp_path / "walkthrough.jsonl"
    _write_jsonl(
        jsonl,
        [
            {"t": "0:00", "action_kind": "exploration", "feature_ids": []},
            {
                "t": "0:01",
                "action_kind": "browser_navigation",
                "feature_ids": ["login"],
                "url": "/login",
            },
            {
                "t": "0:02",
                "action_kind": "api_request",
                "feature_ids": ["login"],
                "method": "POST",
                "path": "/api/login",
                "response_status": 200,
            },
            {
                "t": "0:03",
                "action_kind": "browser_navigation",
                "feature_ids": ["logout"],
                "url": "/logout",
            },
        ],
    )

    report, rejections = validate_walkthrough_jsonl_strict(jsonl, spec)
    assert rejections == []
    assert report.total_entries == 4
    assert report.exploration_entries == 1
    assert report.tagged_entries == 3
    assert report.untagged_entries == 0
    assert report.meets_threshold()
    assert report.per_feature_evidence_count["login"] == 2
    assert report.per_feature_evidence_count["logout"] == 1


def test_strict_jsonl_rejects_violators_and_keeps_compliant(tmp_path: Path) -> None:
    spec = _spec("login")
    jsonl = tmp_path / "walkthrough.jsonl"
    _write_jsonl(
        jsonl,
        [
            # 1: compliant tagged
            {
                "t": "0:01",
                "action_kind": "browser_navigation",
                "feature_ids": ["login"],
            },
            # 2: unknown action_kind
            {
                "t": "0:02",
                "action_kind": "click",
                "feature_ids": ["login"],
            },
            # 3: untagged non-exploration
            {
                "t": "0:03",
                "action_kind": "api_request",
                "feature_ids": [],
            },
            # 4: unknown feature_id
            {
                "t": "0:04",
                "action_kind": "browser_navigation",
                "feature_ids": ["ghost"],
            },
            # 5: compliant exploration (no feature_ids OK)
            {"t": "0:05", "action_kind": "exploration", "feature_ids": []},
        ],
    )

    report, rejections = validate_walkthrough_jsonl_strict(jsonl, spec)

    assert len(rejections) == 3
    assert any("line 2" in r and "unknown_action_kind" in r for r in rejections)
    assert any("line 3" in r and "untagged_non_exploration" in r for r in rejections)
    assert any("line 4" in r and "unknown_feature_id" in r for r in rejections)

    # Only lines 1 + 5 survive.
    assert report.total_entries == 2
    assert report.exploration_entries == 1
    assert report.tagged_entries == 1
    assert report.untagged_entries == 0


def test_strict_jsonl_missing_file_returns_empty_report(tmp_path: Path) -> None:
    spec = _spec("login")
    report, rejections = validate_walkthrough_jsonl_strict(
        tmp_path / "does-not-exist.jsonl", spec,
    )
    assert rejections == []
    assert report.total_entries == 0
    # Vacuous truth — empty walkthrough trivially "meets" the threshold.
    assert report.meets_threshold()


def test_strict_jsonl_invalid_json_line_rejected(tmp_path: Path) -> None:
    spec = _spec("login")
    jsonl = tmp_path / "walkthrough.jsonl"
    jsonl.write_text(
        json.dumps({"t": "0:01", "action_kind": "exploration", "feature_ids": []})
        + "\n"
        + "not-json\n"
    )
    report, rejections = validate_walkthrough_jsonl_strict(jsonl, spec)
    assert any("line 2" in r and "invalid JSON" in r for r in rejections)
    assert report.total_entries == 1
