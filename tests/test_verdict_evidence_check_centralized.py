"""Regression: `_check_local_scope_evidence` in the verification plan must
use the SAME evidence-detection logic as `_pass_payload_has_evidence` in the
canonicalizer. Pre-fix (2026-05-20), they diverged: canonicalizer accepted
many shapes (tests/intent_coverage/runner_checks/...); the verification
plan only accepted top-level `evidence` list or `test_command` string.

The mismatch caused Opus-shape verdicts (which populate `tests` /
`integration_check` / `backend_files` etc. but omit the canonical
`evidence` list) to demote `pass` → `unverified`, triggering the child
verify-repair packet → composite landing gate refusal → upward_merge_gate
blocked. iTracker Opus run wasted ~$120 on the cascade across 3 children.

This test pins the centralization: both layers go through
`_pass_payload_has_evidence`.
"""
from __future__ import annotations

from otto.lead import _pass_payload_has_evidence
from otto.v5_verification_plan import _check_local_scope_evidence


def test_check_uses_same_logic_as_canonicalizer():
    """The check should agree with the canonicalizer on every shape."""
    # Shapes both should accept (per `_pass_payload_has_evidence`)
    shapes_should_pass = [
        # canonical: evidence list with paths
        {"verdict": "pass", "evidence": ["test_auth.log"]},
        # canonical: test_command string
        {"verdict": "pass", "test_command": "pytest -q"},
        # canonical: intent_coverage.built non-empty
        {"verdict": "pass", "intent_coverage": {"built": ["feature X"]}},
        # opus-shape: 'tests' dict present, no explicit failures
        {"verdict": "pass", "tests": {"backend": 24, "frontend": 12}},
        # opus-shape: 'checks' list with passes
        {"verdict": "pass", "checks": [{"name": "build", "passed": True}]},
        # journey-shape: non-empty journeys list
        {"verdict": "pass", "journeys": [{"id": "x", "passed": True}]},
        # deliverables-shape
        {"verdict": "pass", "deliverables": ["RegisterPage.tsx"]},
        # artifacts-shape
        {"verdict": "pass", "artifacts": ["build.log"]},
        # runner_checks shape
        {"verdict": "pass", "runner_checks": [{"id": "x", "passed": True}]},
    ]
    for shape in shapes_should_pass:
        canon_says = _pass_payload_has_evidence(shape)
        check = _check_local_scope_evidence(shape)
        assert canon_says is True, f"canonicalizer should accept: {shape}"
        assert check[0]["status"] == "pass", (
            f"check should also accept {shape} — divergence reintroduced. "
            f"check returned: {check[0]['detail']}"
        )
        assert check[0]["passed"] is True

    # Shape that genuinely lacks evidence: should fail both
    no_evidence = {"verdict": "pass", "summary": "trust me"}
    assert _pass_payload_has_evidence(no_evidence) is False
    check = _check_local_scope_evidence(no_evidence)
    assert check[0]["status"] == "fail"
    assert check[0]["passed"] is False


def test_opus_shape_initial_verdict_passes_check():
    """The exact verdict.json shape Opus emitted on iTracker (which
    triggered the wasted cascade) must now pass the check."""
    opus_shape = {
        "task_id": "v5-83da4b4ba629",
        "verdict": "pass",
        "summary": "Auth & Workspace Management feature complete: …",
        "backend_files": [
            "backend/app/routers/auth/router.py",
            "backend/app/routers/users/router.py",
        ],
        "frontend_files": [
            "frontend/src/features/auth/pages/RegisterPage.tsx",
        ],
        "tests": {"backend": {"passed": 24, "failed": 0}},
        "integration_check": {
            "name": "register → verify-email → login → PAT",
            "result": "passed",
        },
        "notes": ["…"],
    }
    # No top-level `evidence` list, no top-level `test_command` string.
    # Pre-fix: this triggered the cascade. Post-fix: passes via `tests`.
    check = _check_local_scope_evidence(opus_shape)
    assert check[0]["status"] == "pass", (
        f"Opus-shape verdict should pass the local_scope_check now. "
        f"Detail: {check[0]['detail']}"
    )


def test_canonical_shape_still_passes():
    """Sanity: Sonnet-shape (canonical) verdict still passes — fix is
    additive."""
    sonnet_shape = {
        "task_id": "v5-b76fbb1ea7e4",
        "verdict": "pass",
        "summary": "All 11 auth/workspace tests pass.",
        "test_command": "cd backend && uv run pytest tests/test_auth.py",
        "evidence": ["otto_logs/sessions/.../test_auth.log"],
        "journeys": [
            {"id": "onboard_and_first_issue", "passed": True, "detail": "…"},
        ],
        "intent_coverage": {
            "built": ["POST /auth/register with email uniqueness check", "…"],
        },
        "decisions_appended": [{"decision_id": "dec-008", "summary": "…"}],
    }
    check = _check_local_scope_evidence(sonnet_shape)
    assert check[0]["status"] == "pass"


def test_genuine_no_evidence_still_fails():
    """An honestly empty verdict (no tests, no evidence, no journeys) must
    still fail the check — we kept the strictness on real omissions."""
    bare = {"verdict": "pass", "summary": "trust me"}
    check = _check_local_scope_evidence(bare)
    assert check[0]["status"] == "fail"
    assert "lacks ANY recognized evidence" in check[0]["detail"]
