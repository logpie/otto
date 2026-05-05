"""Regression tests for `evidence_kinds` precedence in RunView features.

Bug: `_build_features` previously read `evidence_kinds` only from the
proof-packet entry. When the spec declared kinds but no proof-packet
entry existed (or its entry omitted the field), the FeatureDrilldown
rendered "No evidence kinds declared" — even though the spec carried
them.

Fix: per-feature merge —
  1. proof-packet entry's evidence_kinds (if non-empty)
  2. fall back to spec.features[*].evidence_kinds matched by id
  3. empty list only when neither has data
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.mission_control.run_view import build_run_view


def _write_session(
    tmp_path: Path,
    *,
    proof: dict | None = None,
    spec: dict | None = None,
) -> Path:
    session_dir = tmp_path / "session-evidence"
    session_dir.mkdir()
    if proof is not None:
        (session_dir / "proof-packet.json").write_text(json.dumps(proof))
    if spec is not None:
        (session_dir / "spec").mkdir()
        (session_dir / "spec" / "spec.json").write_text(json.dumps(spec))
    return session_dir


def test_evidence_kinds_fallback_to_spec_when_no_proof_packet(
    tmp_path: Path,
) -> None:
    """Spec declares evidence_kinds; no proof packet → spec kinds surface."""
    spec = {
        "schema_version": 1,
        "intent": "tiny webapp",
        "project_kind": "webapp",
        "features": [
            {
                "feature_id": "login",
                "name": "Login",
                "evidence_kinds": ["BrowserJourney"],
            }
        ],
    }
    session_dir = _write_session(tmp_path, spec=spec)

    view = build_run_view(session_dir)

    assert len(view["features"]) == 1
    assert view["features"][0]["id"] == "login"
    assert view["features"][0]["evidence_kinds"] == ["BrowserJourney"]


def test_evidence_kinds_fallback_to_spec_when_proof_entry_empty(
    tmp_path: Path,
) -> None:
    """Proof feature entry omits evidence_kinds → fall back to spec."""
    spec = {
        "schema_version": 1,
        "features": [
            {
                "feature_id": "login",
                "name": "Login",
                "evidence_kinds": ["BrowserJourney", "ScreenshotPair"],
            }
        ],
    }
    proof = {
        "schema_version": 1,
        "verdict": "passed",
        "features": [
            # Proof entry has no evidence_kinds — common A4 bug surface.
            {"feature_id": "login", "name": "Login", "verdict": "passed"}
        ],
    }
    session_dir = _write_session(tmp_path, proof=proof, spec=spec)

    view = build_run_view(session_dir)

    assert view["features"][0]["evidence_kinds"] == [
        "BrowserJourney",
        "ScreenshotPair",
    ]


def test_evidence_kinds_proof_wins_when_non_empty(tmp_path: Path) -> None:
    """Proof's evidence_kinds (when non-empty) take precedence over spec."""
    spec = {
        "features": [
            {
                "feature_id": "login",
                "evidence_kinds": ["BrowserJourney"],
            }
        ],
    }
    proof = {
        "verdict": "passed",
        "features": [
            {
                "feature_id": "login",
                "evidence_kinds": ["ScreenshotPair"],
            }
        ],
    }
    session_dir = _write_session(tmp_path, proof=proof, spec=spec)

    view = build_run_view(session_dir)

    assert view["features"][0]["evidence_kinds"] == ["ScreenshotPair"]


def test_evidence_kinds_empty_when_neither_has_data(tmp_path: Path) -> None:
    """No spec or proof evidence_kinds → empty list survives."""
    spec = {
        "features": [{"feature_id": "login", "name": "Login"}],
    }
    session_dir = _write_session(tmp_path, spec=spec)

    view = build_run_view(session_dir)

    assert view["features"][0]["evidence_kinds"] == []
