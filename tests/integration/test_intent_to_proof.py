"""A4 — real-Sonnet end-to-end intent-to-proof integration test.

Drives `otto build` against a tmp project with a tiny but non-trivial
intent ("hello world plus a counter button") and asserts the canonical
i2p artifacts land on disk.

This is a real-cost test: it spawns a real Claude Sonnet session via
`uv run otto build --provider claude ...`. It is gated behind
`OTTO_ALLOW_REAL_COST=1` so CI/local default runs never burn money.
Run it via the `i2p-e2e` tier:

    OTTO_ALLOW_REAL_COST=1 python scripts/test_tiers.py i2p-e2e

Asserts (happy path):
    - `<session_dir>/spec/spec.json` exists with required fields
      (`groups`, `project_kind`, `intent`)
    - all groups landed (no `blocked` status)
    - `<session_dir>/proof-packet.html` exists
    - `<session_dir>/proof-packet.json` exists with audit verdict
      in {"passed", "partial"}
    - audit verdict == "passed" (strict — happy path should pass)
    - at least one screenshot artifact under `audit/` (lenient: zero
      is accepted with a comment, since gap A8 documents that Otto's
      default walkthrough is BYO-Playwright and produces no
      screenshots/video out of the box)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REAL_COST_ENV = "OTTO_ALLOW_REAL_COST"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get(REAL_COST_ENV) != "1",
        reason=(
            f"Real-cost E2E test gated by {REAL_COST_ENV}=1. "
            "Run via `python scripts/test_tiers.py i2p-e2e` with the "
            "env var set."
        ),
    ),
]


# 15-minute wall budget. Subprocess timeout is set slightly higher than
# the in-product budget so we still see otto's own honest wrap-up rather
# than a hard SIGKILL from the test runner.
_BUDGET_SECONDS = 15 * 60
_SUBPROCESS_TIMEOUT_SECONDS = _BUDGET_SECONDS + 120

_FIXTURE_INTENT = (
    "a webpage that shows hello world plus a button that increments a counter"
)


def _init_tmp_project(root: Path) -> None:
    """Initialise a minimal git repo with a default `otto.yaml`.

    Mirrors what `otto build` expects on entry: a clean git tree with
    `otto.yaml` committed (matches `tests/conftest.py::tmp_git_repo`).
    """
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=root,
        check=True,
    )
    from otto.config import create_config

    create_config(root)
    subprocess.run(
        ["git", "add", "otto.yaml"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add config"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _resolve_session_dir(project_dir: Path) -> Path:
    """Find the session dir produced by the otto build invocation.

    `otto_logs/latest` is a symlink (or junction file) to the most
    recent session. We resolve it relative to `otto_logs/` so the test
    works regardless of how otto wrote it.
    """
    latest = project_dir / "otto_logs" / "latest"
    if not latest.exists() and not latest.is_symlink():
        raise AssertionError(
            f"otto_logs/latest does not exist at {latest} — "
            "did `otto build` ever create a session?"
        )
    target = os.readlink(latest) if latest.is_symlink() else latest.read_text().strip()
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = (project_dir / "otto_logs" / target_path).resolve()
    if not target_path.exists():
        raise AssertionError(
            f"otto_logs/latest -> {target_path} but the directory is missing"
        )
    return target_path


def test_intent_to_proof_real_sonnet(tmp_path: Path) -> None:
    """Drive a real `otto build --provider claude` and assert artifacts."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _init_tmp_project(project_dir)

    cmd = [
        "uv",
        "run",
        "otto",
        "build",
        _FIXTURE_INTENT,
        "--provider",
        "claude",
        "--budget",
        str(_BUDGET_SECONDS),
    ]

    # Inherit env so OTTO_ALLOW_REAL_COST + Anthropic creds flow through.
    env = os.environ.copy()

    completed = subprocess.run(
        cmd,
        cwd=project_dir,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )

    # Don't gate on exit code alone — i2p path can exit nonzero on a
    # blocked verdict yet still produce all the artifacts we care
    # about. We assert artifacts directly below.
    print("STDOUT:\n", completed.stdout)
    print("STDERR:\n", completed.stderr)

    session_dir = _resolve_session_dir(project_dir)

    # ---- spec.json: shape + required fields ----
    spec_path = session_dir / "spec" / "spec.json"
    assert spec_path.exists(), f"spec.json missing at {spec_path}"
    spec_data = json.loads(spec_path.read_text())
    for key in ("groups", "project_kind", "intent"):
        assert key in spec_data, (
            f"spec.json missing required field '{key}'; got keys={list(spec_data)}"
        )
    assert isinstance(spec_data["groups"], list) and spec_data["groups"], (
        "spec.json `groups` must be a non-empty list"
    )
    assert spec_data["intent"], "spec.json `intent` must be non-empty"

    # ---- all groups landed (no blocked) ----
    blocked_groups = [
        g.get("id") for g in spec_data["groups"]
        if str(g.get("status", "")).lower() == "blocked"
    ]
    assert not blocked_groups, (
        f"some groups are blocked: {blocked_groups}; spec.json={spec_path}"
    )

    # ---- proof-packet.{html,json} ----
    proof_html = session_dir / "proof-packet.html"
    proof_json = session_dir / "proof-packet.json"
    assert proof_html.exists(), f"proof-packet.html missing at {proof_html}"
    assert proof_json.exists(), f"proof-packet.json missing at {proof_json}"

    packet = json.loads(proof_json.read_text())
    verdict = packet.get("verdict")
    assert verdict in {"passed", "partial"}, (
        f"proof-packet.json verdict must be passed|partial, got {verdict!r}"
    )
    # Strict: happy-path tiny intent should certify cleanly.
    assert verdict == "passed", (
        f"happy-path E2E expected verdict=passed, got {verdict!r}; "
        f"see {proof_json}"
    )

    # ---- screenshot evidence (lenient — see gap A8) ----
    # gap A8 (docs/codex-followups.md): Otto's default walkthrough hits
    # the rendered HTML body via Flask test_client and does NOT produce
    # screenshots or a recording on its own. Real video/screenshots
    # require the project to ship a Playwright/Cypress BrowserJourney
    # check (BYO). We accept zero screenshots here and document the
    # caveat. Tighten to >=1 once A8 ships otto-bundled capture.
    audit_dir = session_dir / "audit"
    screenshot_count = 0
    if audit_dir.exists():
        screenshot_count = sum(1 for _ in audit_dir.rglob("*.png"))
    assert screenshot_count >= 0, "sentinel"  # always true; intentional
    # If A8 is fixed (otto ships default capture), flip this assertion to:
    #     assert screenshot_count >= 1, "expected ≥1 screenshot under audit/"
