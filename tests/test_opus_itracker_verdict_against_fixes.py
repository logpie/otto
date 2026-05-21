"""Targeted validation: load the EXACT Opus verdict.json shape captured
from the iTracker cc-i2p-2 OPUS run (2026-05-20, session
2026-05-20-095321-36c7b3, child v5-83da4b4ba629 = auth-workspace) and
confirm the centralized verdict-evidence check now passes it.

This is option (3) from the post-mortem — empirical $0/1-min validation
of the schema-fix (`e16a78b66`) against real-world failure data, without
having to re-run otto.

The shape was extracted from the agent's Write tool_use payload at
ts=2026-05-20T10:17:13.956Z. Pre-fix, this triggered the demotion to
`unverified` → cascade → merge_blocked + ~$120 wasted. Post-fix, it
should evaluate as having recognized evidence.
"""
from __future__ import annotations

import pytest

from otto.lead import _pass_payload_has_evidence, _tests_explicitly_fail, _value_indicates_failure
from otto.v5_verification_plan import _check_local_scope_evidence


# Verbatim payload from
# otto_logs/sessions/2026-05-20-095321-36c7b3/lead/messages.jsonl
# (Write tool_use input.content, parsed). Sliced to relevant fields.
OPUS_ITRACKER_AUTH_VERDICT = {
    "task_id": "v5-83da4b4ba629",
    "verdict": "pass",
    "summary": (
        "Auth & Workspace Management feature complete: register/verify/login/"
        "password-reset, profile + avatar upload, workspaces with admins, "
        "invites, teams (key prefix), personal access tokens (revealed once), "
        "audit log, plus full frontend surface and Cmd+K commands. "
        "All tests pass; end-to-end integration verified."
    ),
    "tests": {
        "backend": {
            "framework": "pytest",
            "command": "uv run --python 3.12 pytest -q",
            "passed": 24,
            "failed": 0,
            "files": [
                "backend/app/tests/test_auth_register_verify_login.py",
                "backend/app/tests/test_auth_password_reset.py",
                "backend/app/tests/test_workspaces_and_invites.py",
                "backend/app/tests/test_teams.py",
                "backend/app/tests/test_tokens.py",
                "backend/app/tests/test_avatar_upload.py",
                "backend/app/tests/test_audit_log.py",
                "backend/app/tests/test_integration_e2e.py",
            ],
        },
        "frontend": {
            "framework": "vitest",
            "command": "npx vitest run",
            "passed": 12,
            "failed": 0,
            "files": [
                "frontend/src/features/auth/pages/RegisterPage.test.tsx",
                "frontend/src/features/auth/pages/LoginPage.test.tsx",
                "frontend/src/features/settings/components/AvatarUploader.test.tsx",
                "frontend/src/features/settings/components/TokenCreateModal.test.tsx",
            ],
        },
        "type_check": {"command": "tsc -b", "status": "clean"},
        "build": {"command": "npm run build", "status": "ok"},
    },
    "integration_check": {
        "name": "register → verify-email → login → PAT create → PAT-authenticated /me → list hides plaintext",
        "test_id": "app/tests/test_integration_e2e.py::test_register_verify_login_pat_flow",
        "status": "pass",
    },
    "backend_files": [
        "backend/app/schemas/auth.py",
        "backend/app/routers/auth/router.py",
        "backend/app/routers/users/router.py",
        "backend/app/routers/workspaces/router.py",
        "backend/app/routers/invites/router.py",
        "backend/app/routers/teams/router.py",
        "backend/app/routers/tokens/router.py",
        "backend/app/routers/audit/router.py",
    ],
    "frontend_files": [
        "frontend/src/features/auth/store.ts",
        "frontend/src/features/auth/routes.tsx",
    ],
    "notes": [
        "Foundation render_template collides with template placeholders; auth router uses a private _render helper.",
        "Personal access tokens are pat_<43 base62 chars>, stored as sha256 hashes.",
    ],
}


def test_value_indicates_failure_on_passed_count_is_now_false():
    """Bug pre-2026-05-20: `_value_indicates_failure({passed: 24, failed: 0})`
    returned True because `passed: 24` was recursed into and `24 != 0`
    was interpreted as failure. Fix: don't recurse on int `passed` values
    (it's a success-count, not a failure signal)."""
    sub = {"framework": "pytest", "passed": 24, "failed": 0}
    assert not _value_indicates_failure(sub), (
        "Sub-test dict with 24 passed, 0 failed must NOT indicate failure. "
        "The pre-fix bug was recursing into `passed: 24` and applying the "
        "generic non-zero=failure rule, which is wrong for count semantics."
    )


def test_passed_with_genuine_failures_still_indicates_failure():
    """Sanity: when there ARE real failures, the check still catches them."""
    bad = {"framework": "pytest", "passed": 8, "failed": 3}
    assert _value_indicates_failure(bad), (
        "Sub-test dict with 8 passed, 3 failed MUST indicate failure."
    )


def test_opus_itracker_auth_verdict_passes_evidence_check():
    """The real-world Opus verdict.json that triggered the cascade now
    has recognized evidence under the centralized check."""
    # Belt + suspenders: both the canonicalizer's predicate AND the
    # verification_plan's check should accept this shape.
    assert _pass_payload_has_evidence(OPUS_ITRACKER_AUTH_VERDICT), (
        "Centralized evidence predicate must accept the real-world Opus "
        "verdict shape that bit iTracker — `tests` dict with passing counts "
        "is valid evidence."
    )

    check = _check_local_scope_evidence(OPUS_ITRACKER_AUTH_VERDICT)
    assert check[0]["status"] == "pass", (
        f"Verification-plan check must pass on Opus shape now. "
        f"Detail: {check[0]['detail']}"
    )


def test_tests_explicitly_fail_is_false_for_opus_real_data():
    """Direct check on the function that was the upstream trigger."""
    assert not _tests_explicitly_fail(OPUS_ITRACKER_AUTH_VERDICT), (
        "The Opus verdict's `tests` field reports all-passing — "
        "`_tests_explicitly_fail` must NOT report a failure."
    )


@pytest.mark.parametrize("variant", [
    "opus_auth",      # the documented case
    "opus_issues",    # other Opus children had similar shape
    "opus_search",
    "opus_cycles",
])
def test_all_opus_shape_variants_pass(variant):
    """Sanity that the fix applies broadly across Opus's natural verdict
    shapes — not just the exact auth-workspace data."""
    base = dict(OPUS_ITRACKER_AUTH_VERDICT)
    # Mutate the test counts to vary the shape slightly
    if variant == "opus_issues":
        base["tests"] = {
            "backend": {"passed": 15, "failed": 0},
            "frontend": {"passed": 8, "failed": 0},
        }
    elif variant == "opus_search":
        base["tests"] = {
            "backend": {"passed": 12, "failed": 0},
            "type_check": {"status": "clean"},
        }
    elif variant == "opus_cycles":
        base["tests"] = {
            "backend": {"passed": 18, "failed": 0},
            "frontend": {"passed": 6, "failed": 0},
            "build": {"status": "ok"},
        }
    check = _check_local_scope_evidence(base)
    assert check[0]["status"] == "pass", (
        f"Variant {variant} should pass: {check[0]['detail']}"
    )
