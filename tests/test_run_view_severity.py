"""Regression tests for severity-vocabulary normalization in
`otto/mission_control/run_view.py:_build_findings`.

Background — A4 RUA report (tick 62 W6-C):
  `VerdictHeader.tsx:50` filters `findings.filter(f => f.severity === "critical")`
  but the seed-fixture / legacy proof packets emit `severity: "blocking"`. The
  result: real blocking findings never count. Canonical severity ladder is
  `critical` / `important` / `polish` (see `otto/spec_compile.py:FINDING_SEVERITIES`
  and the TS `FindingSeverity` union in `otto/web/client/src/types/run.ts`).

These tests pin the translation contract: `_build_findings` MUST surface the
canonical names regardless of which legacy synonym appears in the proof packet.
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.mission_control.run_view import build_run_view


def _setup_session(tmp_path: Path, *, proof: dict) -> Path:
    session_dir = tmp_path / "session-1"
    session_dir.mkdir()
    (session_dir / "proof-packet.json").write_text(json.dumps(proof))
    return session_dir


def _proof_with_findings(findings: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "intent": "test product",
        "project_kind": "webapp",
        "verdict": "blocked",
        "wall_s": 1.0,
        "cost_usd": 0.1,
        "groups": [],
        "features": [],
        "components": [],
        "guardrails": [],
        "quality_findings": findings,
    }


def test_blocking_severity_translated_to_critical(tmp_path: Path) -> None:
    """Seed-fixture / legacy proof packets emit `severity: "blocking"` —
    `_build_findings` MUST surface `severity: "critical"` so VerdictHeader
    counts these correctly."""
    proof = _proof_with_findings([
        {
            "severity": "blocking",
            "text": "Cart count badge stale after add.",
            "feature_id": "add-to-cart",
        }
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert len(view["findings"]) == 1
    assert view["findings"][0]["severity"] == "critical"
    assert view["findings"][0]["text"] == "Cart count badge stale after add."
    assert view["findings"][0]["feature_id"] == "add-to-cart"


def test_critical_severity_passes_through_unchanged(tmp_path: Path) -> None:
    """New-shape proof packets emit `critical` directly — must round-trip."""
    proof = _proof_with_findings([
        {"severity": "critical", "text": "8s page load.", "feature_id": "home"}
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert view["findings"][0]["severity"] == "critical"


def test_high_severity_translated_to_important(tmp_path: Path) -> None:
    """Legacy `high` synonym maps to `important` (research §4 middle rung)."""
    proof = _proof_with_findings([
        {"severity": "high", "text": "Form errors not styled.", "feature_id": ""}
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert view["findings"][0]["severity"] == "important"


def test_low_severity_translated_to_polish(tmp_path: Path) -> None:
    """Legacy `low` synonym maps to `polish` (cosmetic)."""
    proof = _proof_with_findings([
        {"severity": "low", "text": "Color palette generic.", "feature_id": ""}
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert view["findings"][0]["severity"] == "polish"


def test_minor_severity_translated_to_polish(tmp_path: Path) -> None:
    """`minor` is another legacy synonym for cosmetic findings."""
    proof = _proof_with_findings([
        {"severity": "minor", "text": "Whitespace trim.", "feature_id": ""}
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert view["findings"][0]["severity"] == "polish"


def test_medium_severity_translated_to_important(tmp_path: Path) -> None:
    """`medium` is a legacy synonym for the middle rung."""
    proof = _proof_with_findings([
        {"severity": "medium", "text": "Could be smoother.", "feature_id": ""}
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert view["findings"][0]["severity"] == "important"


def test_unknown_severity_falls_back_to_important(tmp_path: Path) -> None:
    """Unknown values fall back to `important` rather than dropping the finding
    or emitting a non-canonical value the frontend can't render."""
    proof = _proof_with_findings([
        {"severity": "weirdvalue", "text": "Something off.", "feature_id": ""}
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert view["findings"][0]["severity"] == "important"
    assert view["findings"][0]["text"] == "Something off."


def test_missing_severity_defaults_to_important(tmp_path: Path) -> None:
    """A finding dict without a `severity` key defaults to `important`."""
    proof = _proof_with_findings([
        {"text": "No severity tag.", "feature_id": ""}
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert view["findings"][0]["severity"] == "important"


def test_severity_case_insensitive(tmp_path: Path) -> None:
    """Severity normalization is case-insensitive — `BLOCKING` → `critical`."""
    proof = _proof_with_findings([
        {"severity": "BLOCKING", "text": "Loud finding.", "feature_id": ""}
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    assert view["findings"][0]["severity"] == "critical"


def test_mixed_severities_all_translated(tmp_path: Path) -> None:
    """Verifies VerdictHeader's `critical` count works on a realistic seed
    fixture with mixed legacy + canonical severities."""
    proof = _proof_with_findings([
        {"severity": "blocking", "text": "A", "feature_id": "f1"},
        {"severity": "critical", "text": "B", "feature_id": "f2"},
        {"severity": "polish", "text": "C", "feature_id": "f3"},
        {"severity": "high", "text": "D", "feature_id": "f4"},
    ])
    view = build_run_view(_setup_session(tmp_path, proof=proof))
    severities = [f["severity"] for f in view["findings"]]
    # Two `blocking`/`critical` should both surface as `critical`.
    assert severities.count("critical") == 2
    assert severities.count("important") == 1  # high → important
    assert severities.count("polish") == 1
