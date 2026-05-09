#!/usr/bin/env python3
"""Phase 5 live web-as-user harness for Mission Control.

Drives a real browser (Playwright) against a real `otto web` server backed by
a throwaway git project running real LLM builds.

Per `plan-mc-audit.md` Phase 5:

- 5A architecture: one throwaway repo + one `otto web` server per scenario,
  Playwright drives a real browser against `http://127.0.0.1:<port>/`.
- 5B scenarios: W1..W13 (W12 split into W12a + W12b → 14 scenarios total).
  W1 and W11 are fully implemented; the remaining scenarios are
  shape-complete stubs that raise NotImplementedError. The orchestrator
  fills them in.
- 5C cadence + tier mappings: `nightly` = W11 + W1 + W7; `weekly` = all 14.
- 5D verdict semantics + product-verification matrix.
- 5E real-cost guardrails: `OTTO_ALLOW_REAL_COST=1` required, `--dry-run`,
  `--list`, INFRA classification, process-group cleanup on exit.
- 5H soft-assert + auto-mine pattern (mandatory for W2/W11/W12a/W12b/W13).

Note: a future ``~/.claude/skills/web-as-user/`` skill could mirror the
existing ``~/.claude/skills/otto-as-user/SKILL.md`` pattern. Out of scope
for this scaffolding pass.

CLI::

    python scripts/web_as_user.py --list
    python scripts/web_as_user.py --mode quick
    python scripts/web_as_user.py --mode full --provider codex-app-server
    python scripts/web_as_user.py --scenario W11
    python scripts/web_as_user.py --scenario W1,W7,W11 --scenario-delay 10
    python scripts/web_as_user.py --tier nightly  # W11 + W1 + W7
    python scripts/web_as_user.py --tier weekly   # all W1..W13
    python scripts/web_as_user.py --dry-run --scenario W1
    python scripts/web_as_user.py --scenario W1 --user-behavior mc-realistic --user-seed 42
    python scripts/web_as_user.py --bail-fast --scenario W11
    python scripts/web_as_user.py --keep-failed-only
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import random
import re
import shutil
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from real_cost_guard import require_real_cost_opt_in  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "bench-results" / "web-as-user"
DEFAULT_SCENARIO_DELAY_S = 5.0
INFRA_RETRY_DELAY_S = 30.0
INFRA_RETRY_ATTEMPTS = 2
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"
HEAVY_BROWSER_ARTIFACT_SUFFIXES = {".har"}
HEAVY_BROWSER_TRACE_NAMES = {"trace.zip", "traces.zip"}

ScenarioStatus = Literal["PASS", "FAIL", "INFRA"]
FailureClassification = Literal["INFRA", "FAIL"]
UserBehaviorMode = Literal["off", "mc-realistic", "mc-stress"]

MC_EVIDENCE_BUTTON_SELECTORS: dict[str, list[str]] = {
    "logs": [
        '[data-testid="run-quick-action-logs"]',
        '[data-testid="open-logs-button"]',
        '[data-testid="run-detail-open-logs-button"]',
    ],
    "diff": [
        '[data-testid="run-quick-action-diff"]',
        '[data-testid="open-diff-button"]',
        '[data-testid="proof-open-diff-button"]',
    ],
    "artifacts": [
        '[data-testid="run-quick-action-artifacts"]',
        '[data-testid="open-artifacts-button"]',
        '[data-testid="proof-open-artifacts-button"]',
    ],
    "proof": [
        '[data-testid="run-quick-action-proof"]',
        '[data-testid="open-proof-button"]',
    ],
}

MC_EVIDENCE_PANEL_SELECTORS: dict[str, list[str]] = {
    "logs": [
        '[data-testid="run-resource-panel-logs"]',
        '[data-testid="run-log-pane"]',
        '[data-testid="logs-pane"]',
    ],
    "diff": [
        '[data-testid="run-resource-panel-diff"]',
        '[data-testid="diff-pane"]',
    ],
    "artifacts": [
        '[data-testid="run-resource-panel-artifacts"]',
        '[data-testid^="artifact-list-item-"]',
        '[data-testid="artifact-download"]',
        '[data-testid="artifact-image"]',
    ],
    "proof": [
        '[data-testid="proof-pane"]',
    ],
}

PROVIDER_INFRA_FAILURE_PATTERNS: tuple[str, ...] = (
    "usagelimitexceeded",
    "usage limit",
    "purchase more credits",
    "rate limit",
    "rate_limit",
    "429",
    "throttle",
    "not logged in",
    "please run /login",
    "provider capacity",
    "provider infrastructure",
)


# ---------------------------------------------------------------------------
# Tier mappings (Phase 5C)
# ---------------------------------------------------------------------------

# Composite tiers — keep in sync with plan-mc-audit.md 5C.
TIER_NIGHTLY = ["W11", "W1", "W7"]
TIER_WEEKLY = [
    "W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W9", "W10",
    "W11", "W12a", "W12b", "W13",
]
QUICK_SCENARIOS = ["W1", "W11"]  # `--mode quick`


# ---------------------------------------------------------------------------
# Soft-assert + auto-mine (Phase 5H)
# ---------------------------------------------------------------------------


class RunFailures:
    """Accumulator for soft-assert failures.

    Long live scenarios MUST replace fail-fast assert/raise with this so a
    $5/25-min run finds every issue per attempt, not one per attempt.
    """

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def soft_assert(self, cond: bool, msg: str) -> bool:
        if not cond:
            self.failures.append(msg)
        return cond

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def summary(self) -> str:
        if not self.failures:
            return "no failures"
        if len(self.failures) == 1:
            return self.failures[0]
        return (
            f"{len(self.failures)} failure(s):\n  - "
            + "\n  - ".join(self.failures)
        )


def _text_is_provider_infra_failure(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(pattern in lowered for pattern in PROVIDER_INFRA_FAILURE_PATTERNS)


def _session_provider_infra_failure_reason(session_dir: Path | None) -> str:
    """Return a bounded provider-infra reason from crash artifacts, if present."""
    if session_dir is None or not session_dir.exists():
        return ""
    crash_paths = sorted(session_dir.glob("*/crash.json")) + sorted(session_dir.glob("crash.json"))
    for crash_path in crash_paths[:8]:
        try:
            payload = json.loads(crash_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        text = json.dumps(payload, default=str)
        if not _text_is_provider_infra_failure(text):
            continue
        message = (
            str(payload.get("exception_message") or "")
            or str(payload.get("message") or "")
            or str(payload.get("phase") or "provider infrastructure failure")
        )
        return " ".join(message.split())[:500]
    return ""


def _queue_task_provider_infra_failure(project_dir: Path, task_state: dict[str, Any]) -> str:
    session_text = str(task_state.get("session_dir") or "").strip()
    session_dir = Path(session_text) if session_text else None
    reason = _session_provider_infra_failure_reason(session_dir)
    if reason:
        return reason
    checkpoint_text = str(task_state.get("checkpoint_path") or "").strip()
    if checkpoint_text:
        reason = _session_provider_infra_failure_reason(Path(checkpoint_text).parent)
        if reason:
            return reason
    ready_path = task_state.get("ready_path")
    if ready_path:
        try:
            ready_payload = json.loads(Path(str(ready_path)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ready_payload = {}
        if isinstance(ready_payload, dict):
            ready_session = ready_payload.get("session_dir")
            if ready_session:
                reason = _session_provider_infra_failure_reason(Path(str(ready_session)))
                if reason:
                    return reason
    return ""


def artifact_mine_pass(project_dir: Path, failures: RunFailures) -> None:
    """Post-run on-disk invariant scan (Phase 5H).

    Scans the project tree for invariants that are cheap to verify after the
    fact: queue files coherent, run registry consistent with sessions, no
    orphan worktrees, no leaked Otto runtime files in project root, gitignore
    properly excludes Otto runtime files. Append findings to ``failures``.

    Implementation outline (orchestrator extends):
      - load `otto.paths.queue_state_path(project_dir)` and verify each
        listed task has a manifest under `paths.queue_dir(project_dir)`
      - load `otto.paths.live_runs_dir(project_dir)` and verify each live
        run has a sibling session dir under `paths.sessions_root(...)`
      - check `git worktree list` for orphan worktrees
      - assert no `.otto-queue-state.json` lookalike files leaked into
        project root from agent prompts
      - assert `.gitignore` (if present) excludes `otto_logs/` and
        `otto_artifacts/`
    """
    from otto import paths

    queue_state = paths.queue_state_path(project_dir)
    if queue_state.exists():
        try:
            state = json.loads(queue_state.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.fail(f"queue state JSON malformed at {queue_state}: {exc}")
            state = {}
        for task_id, task_state in (state.get("tasks") or {}).items():
            try:
                manifest = paths.queue_manifest_path(project_dir, task_id)
            except ValueError as exc:
                failures.fail(f"queue task ID {task_id!r} invalid: {exc}")
                continue
            status = ""
            if isinstance(task_state, dict):
                status = str(task_state.get("status") or "").lower()
            if status not in {"done", "failed", "cancelled", "removed"}:
                continue
            if not manifest.is_file():
                if (
                    status == "failed"
                    and isinstance(task_state, dict)
                    and _queue_task_provider_infra_failure(project_dir, task_state)
                ):
                    continue
                failures.fail(
                    f"queue task {task_id!r} listed in state but has no manifest at {manifest}"
                )

    # Walk live records via the registry so we get each record's recorded
    # cwd — queue-domain runs live in a worktree (`<project>/.worktrees/<task>/`)
    # and their session dir lives under that worktree, not under project_dir.
    # Merge-domain runs have no real session dir at all (artifacts live under
    # `otto_logs/merge/<merge_id>/`). `session_dir_for_record` handles both.
    from otto.runs import read_live_records

    for record in read_live_records(project_dir):
        sess = paths.session_dir_for_record(record, project_dir=project_dir)
        if sess is None:
            # Merge-domain records have no sessions/<id>/ dir by design.
            continue
        if not sess.exists():
            failures.fail(
                f"live run {record.run_id!r} has no session dir at {sess}"
            )

    _artifact_mine_gitignore_pass(project_dir, failures)


def _artifact_mine_gitignore_pass(project_dir: Path, failures: RunFailures) -> None:
    """Verify Otto runtime evidence cannot dirty real project source state."""
    if not (project_dir / ".git").exists():
        return
    gitignore = project_dir / ".gitignore"
    if not gitignore.exists():
        runtime_dirs = [project_dir / "otto_logs", project_dir / "otto_artifacts"]
        if any(path.exists() for path in runtime_dirs):
            failures.fail(
                "project created Otto runtime artifacts but has no gitignore for "
                "otto_logs/ and otto_artifacts/"
            )
        return
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        failures.fail(f"project gitignore could not be read: {exc}")
        return
    missing = [
        pattern for pattern in ("otto_logs/", "otto_artifacts/")
        if not _gitignore_has_dir_pattern(lines, pattern)
    ]
    if missing:
        failures.fail(
            "project gitignore is missing Otto runtime ignores: " + ", ".join(missing)
        )


def _gitignore_has_dir_pattern(lines: list[str], pattern: str) -> bool:
    bare = pattern.rstrip("/")
    accepted = {bare, f"{bare}/", f"{bare}/*"}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line in accepted:
            return True
    return False


# ---------------------------------------------------------------------------
# Verdict / outcome dataclasses
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    passed: bool
    note: str
    details: list[str] = field(default_factory=list)


@dataclass
class ScenarioOutcome:
    scenario_id: str
    description: str
    outcome: ScenarioStatus
    note: str
    artifact_dir: Path
    wall_duration_s: float
    attempt_count: int = 1
    retried_after_infra: bool = False
    failures: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    id: str
    description: str
    tier: str  # "nightly" | "weekly"
    estimated_cost: float
    estimated_seconds: int
    needs_product_verification: bool
    target_recordings: list[str]  # Phase 3.5B-bis W → R drift gate target
    run_fn: Callable[["ScenarioContext"], "ScenarioRunResult"] = field(repr=False)


@dataclass
class ScenarioContext:
    """Per-scenario runtime context."""

    scenario: Scenario
    project_dir: Path
    artifact_dir: Path
    provider: str
    failures: RunFailures
    debug_log: Path
    run_id: str = ""
    user_behavior: UserBehaviorMode = "off"
    user_seed: Optional[int] = None
    web_port: Optional[int] = None
    web_url: Optional[str] = None
    backend: Any = None
    intent_override: Optional[str] = None
    build_timeout_s: Optional[int] = None
    group_concurrent: Optional[int] = None


@dataclass
class ScenarioRunResult:
    outcome: ScenarioStatus
    note: str
    duration_s: float
    failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scenario implementations
# ---------------------------------------------------------------------------


W1_INTENT = (
    "Build a small kanban board web app with To-Do / In-Progress / Done columns. "
    "Tasks can be added, dragged between columns, and removed."
)
W11_BUILD_INTENT = "Add a GET /tasks endpoint that returns the current task list as JSON."
W11_POST_INTENT = "Add POST /tasks endpoint."
W11_DELETE_INTENT = "Add DELETE /tasks/<id> endpoint."

# W2 — three quick build intents (simpler than seeding fake prior runs for
# improve/certify; per plan-mc-audit.md 5B the orchestrator may collapse to
# three builds with different intents).
W2_INTENT_A = "Create a tiny calculator HTML page with add/subtract buttons."
W2_INTENT_B = "Add a / endpoint that returns 'hello world' as plain text."
W2_INTENT_C = "Add a date utility module that exports today() returning ISO date string."

# W12a — atomic CLI build to be inspected/cancelled from web.
W12A_INTENT = "Add a tiny ping endpoint that returns pong."

# W12b — queue-CLI build to be merged from web.
W12B_INTENT = "Add a tiny version endpoint that returns the package name."

# W13 — outage recovery build (must take at least ~1 min so we can kill mid-run).
W13_INTENT = (
    "Build a small TODO list app with HTML + JS (no framework) that lets the user "
    "add tasks, mark them done, and clear completed."
)

# How long the harness will wait for the LLM build to reach a terminal state
# before bailing. W1 is now used for true Mission Control Web pressure tests
# with real multi-group products; keep the default aligned with Otto's queue
# task budget rather than the old tiny-kanban estimate.
W1_BUILD_TIMEOUT_S = 60 * 60
W1_VISIBLE_PROGRESS_IDLE_TIMEOUT_S = 20 * 60
W11_BUILD_TIMEOUT_S = 25 * 60
W2_BUILD_TIMEOUT_S = 20 * 60   # 3 sequential builds, generous bound
W12A_BUILD_TIMEOUT_S = 10 * 60
W12B_BUILD_TIMEOUT_S = 12 * 60
W13_BUILD_TIMEOUT_S = 15 * 60

# W3 — improve loop. First seeds a small-but-real build, then submits an
# improve-bugs job that references the prior run via the queue subcommand.
W3_BUILD_INTENT = (
    "Add a small Python module greet.py that exports greet(name) returning "
    "'Hello, <name>!'. Include test_greet.py with a basic pytest test."
)
W3_IMPROVE_FOCUS = (
    "Make greet() handle empty/None name by returning 'Hello, world!' as a "
    "safe default. Add a pytest case for the empty-string path."
)
W3_BUILD_TIMEOUT_S = 12 * 60
W3_IMPROVE_TIMEOUT_S = 15 * 60

# W4 — merge happy path. A trivially small build that should succeed and
# expose the Merge action.
W4_INTENT = "Add a tiny Python module hello.py exporting hello() returning 'world'."
W4_BUILD_TIMEOUT_S = 10 * 60

# W5 — merge blocking. We'll seed a dirty file in the project root (the
# user-facing way to make a merge be blocked even when a build succeeds).
W5_INTENT = "Add a tiny Python module ping.py exporting ping() returning 'pong'."
W5_BUILD_TIMEOUT_S = 10 * 60

# W6 — deterministic failure + retry. We seed a malformed otto.yaml so the
# build fails reliably (non-INFRA), then the harness fixes the yaml and the
# user clicks Retry.
W6_INTENT = "Add a tiny Python module mod.py exporting one() returning 1."
W6_BUILD_TIMEOUT_S = 10 * 60

# W7 — same intent as W1 (mobile is a layout-only diff).
W7_INTENT = W1_INTENT
W7_BUILD_TIMEOUT_S = 12 * 60

# W8 — power-user keyboard-only. 3 quick builds (same shape as W2) but driven
# entirely via Tab / Enter / Space / arrow keys. We re-use the W2 intents so
# the cost/duration profile matches and we can compare keyboard- vs mouse-
# driven UX directly.
W8_INTENT_A = W2_INTENT_A
W8_INTENT_B = W2_INTENT_B
W8_INTENT_C = W2_INTENT_C
W8_BUILD_TIMEOUT_S = 20 * 60   # same bound as W2; 3 sequential builds

# W9 — backgrounded tab + return. Submit one realistic build, then flip
# document.visibilityState to hidden (and switch focus elsewhere) for ~2 min
# while it runs. Return after expected completion and verify the SPA caught
# up with no double-fire / stale state, and (best-effort) verify the
# Notification API was invoked.
W9_INTENT = W2_INTENT_A     # tiny calculator — small, ~3-5 min build
W9_BUILD_TIMEOUT_S = 12 * 60
W9_HIDE_S = 120             # how long the tab stays "hidden"

# W10 — two-tab consistency. Submit a job from tab A, observe it propagate to
# tab B within the poll window; then cancel from tab B and verify A reflects
# it. Build is intentionally small (will be cancelled mid-flight; we are
# testing UI propagation, not build correctness).
W10_INTENT = W2_INTENT_B    # `/` -> "hello world" — small surface
W10_PROPAGATION_TIMEOUT_S = 30   # generous bound for poll-based propagation
W10_TERMINAL_TIMEOUT_S = 8 * 60


# ---------------------------------------------------------------------------
# Web-as-user helpers (Playwright + in-process server)
# ---------------------------------------------------------------------------


def _import_start_backend():
    """Import ``tests/browser/_helpers/server.start_backend`` lazily."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.browser._helpers.server import start_backend  # type: ignore

    return start_backend


def _terminate_process_group(proc: Optional["subprocess.Popen[str]"]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _capture_console_and_network(page: Any, artifact_dir: Path) -> dict[str, list]:
    """Wire console + response listeners; return a mutable bag of captured records."""
    captured = {"console": [], "network_errors": [], "page_errors": []}

    def _on_console(msg: Any) -> None:
        try:
            entry = {"type": msg.type, "text": msg.text, "location": dict(msg.location or {})}
        except Exception:  # noqa: BLE001
            entry = {"type": getattr(msg, "type", "?"), "text": str(msg)}
        captured["console"].append(entry)

    def _on_response(response: Any) -> None:
        try:
            status = response.status
            url = response.url
        except Exception:  # noqa: BLE001
            return
        if status >= 400:
            captured["network_errors"].append({"status": status, "url": url})

    def _on_pageerror(err: Any) -> None:
        captured["page_errors"].append({"text": str(err)})

    page.on("console", _on_console)
    page.on("response", _on_response)
    page.on("pageerror", _on_pageerror)
    return captured


def _flush_captured(captured: dict[str, list], artifact_dir: Path) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "console.json").write_text(
        json.dumps(captured["console"], indent=2, default=str), encoding="utf-8"
    )
    (artifact_dir / "network-errors.json").write_text(
        json.dumps(captured["network_errors"], indent=2, default=str), encoding="utf-8"
    )
    (artifact_dir / "page-errors.json").write_text(
        json.dumps(captured["page_errors"], indent=2, default=str), encoding="utf-8"
    )


def _is_expected_network_error(entry: dict[str, Any]) -> bool:
    status = entry.get("status")
    url = str(entry.get("url") or "")
    path = urlparse(url).path
    if status == 404:
        return True
    # Launcher mode intentionally returns 409 from /api/state while no project
    # is selected. Real-user project-switch probes can hit that transition.
    return status == 409 and path == "/api/state"


def _unexpected_network_errors(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if not _is_expected_network_error(entry)]


def _unexpected_console_errors(captured: dict[str, list]) -> list[dict[str, Any]]:
    expected_urls = {
        str(entry.get("url") or "")
        for entry in captured.get("network_errors", [])
        if _is_expected_network_error(entry)
    }
    errors: list[dict[str, Any]] = []
    for entry in captured.get("console", []):
        if entry.get("type") != "error":
            continue
        location = entry.get("location") if isinstance(entry.get("location"), dict) else {}
        url = str(location.get("url") or "")
        text = str(entry.get("text") or "")
        if url in expected_urls and text.startswith("Failed to load resource:"):
            continue
        errors.append(entry)
    return errors


def _safe_screenshot(page: Any, artifact_dir: Path, name: str) -> Optional[Path]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:  # noqa: BLE001
        return None


def _mc_slug(raw: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in raw)
    return "-".join(part for part in slug.split("-") if part)[:64] or "event"


def _mc_user_seed(ctx: ScenarioContext) -> int:
    if ctx.user_seed is not None:
        return ctx.user_seed
    source = f"{ctx.run_id}:{ctx.scenario.id}:{ctx.user_behavior}"
    return int(hashlib.sha256(source.encode("utf-8")).hexdigest()[:16], 16)


def _scenario_intent(ctx: ScenarioContext, default: str) -> str:
    return ctx.intent_override or default


def _mc_phase_rng(ctx: ScenarioContext, phase: str) -> random.Random:
    source = f"{_mc_user_seed(ctx)}:{ctx.scenario.id}:{phase}"
    seed = int(hashlib.sha256(source.encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(seed)


def _record_mc_user_action(
    ctx: ScenarioContext,
    *,
    phase: str,
    action: str,
    **details: Any,
) -> None:
    if ctx.user_behavior == "off":
        return
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenario": ctx.scenario.id,
        "mode": ctx.user_behavior,
        "seed": _mc_user_seed(ctx),
        "phase": phase,
        "action": action,
        **details,
    }
    path = ctx.artifact_dir / "mc-user-behavior.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


def _agent_browser_session(ctx: ScenarioContext) -> str:
    source = f"{ctx.run_id}-{ctx.scenario.id}-{ctx.project_dir.name}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    scenario = re.sub(r"[^a-zA-Z0-9_.-]+", "-", ctx.scenario.id).strip("-").lower()
    return f"tw{scenario[:3] or 'w'}{digest}"


def _agent_browser_socket_dir(ctx: ScenarioContext) -> Path:
    digest = hashlib.sha256(str(ctx.artifact_dir).encode("utf-8")).hexdigest()[:8]
    socket_dir = Path("/tmp") / "ab" / digest
    socket_dir.mkdir(parents=True, exist_ok=True)
    return socket_dir


def _record_agent_browser_action(
    ctx: ScenarioContext,
    *,
    phase: str,
    command: list[str],
    exit_code: int,
    stdout: str,
    stderr: str,
    expectation: str,
    artifact: Path | None = None,
) -> None:
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenario": ctx.scenario.id,
        "phase": phase,
        "tool": "agent-browser",
        "session": _agent_browser_session(ctx),
        "expectation": expectation,
        "command": command,
        "exit_code": exit_code,
        "stdout_sample": stdout[:2_000],
        "stderr_sample": stderr[:2_000],
    }
    if artifact is not None:
        payload["artifact"] = str(artifact.relative_to(ctx.artifact_dir))
    with (ctx.artifact_dir / "agent-browser-actions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


def _run_agent_browser(
    ctx: ScenarioContext,
    phase: str,
    args: list[str],
    *,
    expectation: str,
    artifact: Path | None = None,
    timeout_s: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    command = ["agent-browser", "--session", _agent_browser_session(ctx), *args]
    env = os.environ.copy()
    env["AGENT_BROWSER_SOCKET_DIR"] = str(_agent_browser_socket_dir(ctx))
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    _record_agent_browser_action(
        ctx,
        phase=phase,
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        expectation=expectation,
        artifact=artifact,
    )
    return completed


def _agent_browser_mc_probe(
    ctx: ScenarioContext,
    *,
    phase: str,
    url: str,
    expectation: str,
    log_fn: Callable[[str], None],
    failures: RunFailures,
    hard: bool = False,
) -> None:
    if ctx.user_behavior == "off":
        return
    if shutil.which("agent-browser") is None:
        message = "agent-browser is not installed; true-web cannot collect agent-browser evidence"
        (failures.fail if hard else failures.note)(message)
        _record_mc_user_action(ctx, phase=phase, action="agent-browser-unavailable", expectation=expectation)
        return

    artifact = ctx.artifact_dir / f"agent-browser-{_mc_slug(phase)}.png"
    commands = [
        ["set", "viewport", "1440", "900"],
        ["open", url],
        ["wait", '[data-mc-shell="ready"]'],
        ["snapshot", "-i"],
        ["screenshot", str(artifact)],
    ]
    for args in commands:
        try:
            completed = _run_agent_browser(
                ctx,
                phase,
                args,
                expectation=expectation,
                artifact=artifact if args[0] == "screenshot" else None,
            )
        except subprocess.TimeoutExpired as exc:
            message = f"agent-browser {' '.join(args)} timed out during {phase}: {exc}"
            log_fn(f"  {message}")
            (failures.fail if hard else failures.note)(message)
            return
        if completed.returncode != 0:
            message = (
                f"agent-browser {' '.join(args)} failed during {phase}: "
                f"{(completed.stderr or completed.stdout or '').strip()[:500]}"
            )
            log_fn(f"  {message}")
            (failures.fail if hard else failures.note)(message)
            return
    _record_mc_user_action(
        ctx,
        phase=phase,
        action="agent-browser-probe",
        expectation=expectation,
        url=url,
        screenshot=artifact.name,
        session=_agent_browser_session(ctx),
    )


def _mc_selector_summary(page: Any, selectors: list[str]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = loc.count()
            first = loc.first
            visible = count > 0 and first.is_visible()
            enabled = visible and first.is_enabled()
            text = ""
            if visible:
                try:
                    text = first.inner_text(timeout=500).strip()[:200]
                except Exception:  # noqa: BLE001
                    text = ""
            summary[selector] = {
                "count": count,
                "visible": visible,
                "enabled": enabled,
                "text": text,
            }
        except Exception as exc:  # noqa: BLE001
            summary[selector] = {"error": str(exc)}
    return summary


def _mc_observe_page(page: Any, selectors: list[str]) -> dict[str, Any]:
    try:
        document_text = page.locator("body").inner_text(timeout=1_000).strip()
    except Exception:  # noqa: BLE001
        document_text = ""
    return {
        "url": page.url,
        "title": page.title() if hasattr(page, "title") else "",
        "text_sample": document_text[:500],
        "selectors": _mc_selector_summary(page, selectors),
    }


def _mc_visible_text(page: Any, *, timeout_ms: int = 1_000) -> str:
    try:
        return page.locator("body").inner_text(timeout=timeout_ms).strip()
    except Exception:  # noqa: BLE001
        return ""


def _mc_run_detail_semantic_findings(body_text: str) -> list[str]:
    """Find user-visible Mission Control contradictions that selectors miss."""
    text = " ".join(body_text.split())
    if not text:
        return []
    lower = text.casefold()
    findings: list[str] = []

    if "spec review pending" in lower and (
        "build active" in lower
        or "audit active" in lower
        or "render active" in lower
        or "land active" in lower
    ):
        findings.append(
            "stage contradiction: Spec review is pending while a later stage is active"
        )

    if (
        re.search(r"\bstages?\b", lower)
        and re.search(r"\bseed\b", lower)
        and "prepare fixtures" not in lower
    ):
        findings.append(
            "internal stage label exposed: Seed is visible instead of a user-facing fixture/setup label"
        )

    max_elapsed_s = _mc_max_visible_elapsed_seconds(text)
    if re.search(r"\bwall\s+0s\b", lower) and max_elapsed_s >= 30:
        findings.append(
            f"active run wall time is stale: drawer shows WALL 0s while page shows elapsed time around {max_elapsed_s}s"
        )

    if (
        "no changes from base" in lower
        and max_elapsed_s >= 30
        and any(marker in lower for marker in (" running", " building", "● running"))
    ):
        findings.append(
            "diff evidence is misleading: active long-running job shows 'No changes from base'"
        )

    return findings


def _mc_max_visible_elapsed_seconds(text: str) -> int:
    max_seen = 0
    for minutes, seconds in re.findall(r"(?<!\d)(\d{1,3}):([0-5]\d)\b", text):
        max_seen = max(max_seen, int(minutes) * 60 + int(seconds))
    return max_seen


def _mc_check_expectation(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    action: str,
    expectation: str,
    log_fn: Callable[[str], None],
    failures: RunFailures,
    any_visible: Optional[list[str]] = None,
    all_visible: Optional[list[str]] = None,
    text_any: Optional[list[str]] = None,
    hard: bool = False,
) -> bool:
    """Record visible post-action state against the user's expectation."""
    any_visible = any_visible or []
    all_visible = all_visible or []
    text_any = text_any or []
    selectors = list(dict.fromkeys([*any_visible, *all_visible]))
    observation = _mc_observe_page(page, selectors)
    selector_summary = observation["selectors"]
    any_ok = True
    if any_visible:
        any_ok = any(
            bool(selector_summary.get(selector, {}).get("visible"))
            for selector in any_visible
        )
    all_ok = all(
        bool(selector_summary.get(selector, {}).get("visible"))
        for selector in all_visible
    )
    text_ok = True
    if text_any:
        body = str(observation.get("text_sample", "")).lower()
        text_ok = any(fragment.lower() in body for fragment in text_any)
    semantic_findings = _mc_run_detail_semantic_findings(_mc_visible_text(page))
    matched = any_ok and all_ok and text_ok and not semantic_findings
    verdict = "expected" if matched else ("blocked" if hard else "confusing UX")
    screenshot = None
    if not matched:
        screenshot = _safe_screenshot(
            page,
            ctx.artifact_dir,
            f"mc-expectation-{_mc_slug(phase)}-{_mc_slug(action)}",
        )
        msg = (
            f"mc-user expectation mismatch after {action}: {expectation} "
            f"(phase={phase}, url={observation.get('url')})"
        )
        if hard:
            failures.fail(msg)
        else:
            failures.note(msg)
        log_fn(f"  {msg}")
    for finding in semantic_findings:
        failures.fail(f"mc-user semantic contradiction after {action}: {finding}")
        log_fn(f"  mc-user semantic contradiction after {action}: {finding}")
    _record_mc_user_action(
        ctx,
        phase=phase,
        action="expectation-check",
        user_action=action,
        expectation=expectation,
        matched=matched,
        verdict=verdict,
        observation=observation,
        semantic_findings=semantic_findings,
        screenshot=screenshot.name if screenshot else None,
    )
    return matched


def _mc_click_first(
    page: Any,
    selectors: list[str],
    *,
    log_fn: Callable[[str], None],
    timeout_ms: int = 3_000,
) -> Optional[str]:
    last_error: Optional[str] = None
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            if not loc.is_visible():
                continue
            if not loc.is_enabled():
                continue
            loc.click(timeout=timeout_ms)
            return selector
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
    if last_error:
        log_fn(f"  mc-user click skipped after error: {last_error}")
    return None


def _mc_visible_selector(page: Any, selectors: list[str]) -> Optional[str]:
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                return selector
        except Exception:  # noqa: BLE001
            continue
    return None


def _mc_wait_for_visible_selector(
    page: Any,
    selectors: list[str],
    *,
    timeout_s: float = 6.0,
) -> Optional[str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        visible = _mc_visible_selector(page, selectors)
        if visible:
            return visible
        time.sleep(0.2)
    return _mc_visible_selector(page, selectors)


def _mc_assert_evidence_panel(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    kind: str,
    log_fn: Callable[[str], None],
    failures: RunFailures,
    hard: bool = True,
    clicked_selector: Optional[str] = None,
) -> bool:
    """Require the specific evidence surface requested by a real user action."""
    if kind == "proof" and clicked_selector == '[data-testid="run-quick-action-proof"]':
        try:
            pages = getattr(page.context, "pages", [])
            if len(pages) > 1:
                return True
        except Exception:  # noqa: BLE001
            pass
        failures.fail("proof quick action did not open a proof packet page")
        return False
    selectors = MC_EVIDENCE_PANEL_SELECTORS.get(kind, [])
    if not selectors:
        return True
    _mc_wait_for_visible_selector(page, selectors, timeout_s=6.0)
    matched = _mc_check_expectation(
        page,
        ctx,
        phase=phase,
        action=f"inspect {kind}",
        expectation=f"Opening {kind} shows the {kind}-specific evidence panel, not just the existing drawer.",
        any_visible=selectors,
        log_fn=log_fn,
        failures=failures,
        hard=hard,
    )
    if kind == "diff":
        deadline = time.monotonic() + 8.0
        while "Loading diff..." in _mc_visible_text(page) and time.monotonic() < deadline:
            time.sleep(0.4)
        if "Loading diff..." in _mc_visible_text(page):
            failures.fail("diff evidence panel stayed on Loading diff... instead of resolving")
            matched = False
    return matched


_RUN_ACTION_SELECTORS: dict[str, list[str]] = {
    "cancel": [
        '[data-testid^="task-card-cancel-"]',
        '[data-testid="recovery-action-cancel"]',
        '[data-testid="advanced-action-cancel"]',
        '[data-testid="review-next-action-button"]',
    ],
    "merge": [
        '[data-testid="review-next-action-button"]',
        '[data-testid="advanced-action-merge"]',
    ],
    "retry": [
        '[data-testid="recovery-action-retry"]',
        '[data-testid="advanced-action-retry"]',
        '[data-testid="review-next-action-button"]',
        '[data-testid="log-retry-button"]',
    ],
}

_RUN_ACTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cancel": ("cancel", "stop", "interrupt"),
    "merge": ("merge", "land"),
    "retry": ("retry", "requeue", "try again"),
}


def _mc_action_button_text(locator: Any) -> str:
    parts: list[str] = []
    for getter in (
        lambda: locator.text_content(timeout=500),
        lambda: locator.get_attribute("aria-label", timeout=500),
        lambda: locator.get_attribute("title", timeout=500),
        lambda: locator.get_attribute("data-testid", timeout=500),
    ):
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            parts.append(str(value))
    return " ".join(parts).strip()


def _mc_open_run_by_id(
    page: Any,
    run_id: str,
    *,
    failures: RunFailures,
    log_fn: Callable[[str], None],
) -> bool:
    selectors = [
        f'[data-run-id="{run_id}"] .queue-list-row-main',
        f'[data-testid="history-row-activator-{run_id}"]',
        f'[data-testid="live-row-activator-{run_id}"]',
    ]
    clicked = _mc_click_first(page, selectors, log_fn=log_fn, timeout_ms=5_000)
    if not clicked:
        failures.fail(f"could not open run {run_id} from a visible row/card")
        return False
    time.sleep(0.7)
    if not _mc_wait_for_visible_selector(
        page,
        [
            '[data-testid="run-drawer"]',
            '[data-testid="run-list-detail-drawer"]',
            '[data-testid="run-detail-panel"]',
        ],
        timeout_s=6.0,
    ):
        failures.fail(f"clicking visible row for run {run_id} did not open a run detail surface")
        return False
    return True


def _mc_confirm_dialog_if_present(
    page: Any,
    *,
    failures: RunFailures,
    log_fn: Callable[[str], None],
) -> None:
    try:
        dialog = page.locator(".confirm-dialog").first
        if dialog.count() == 0:
            try:
                page.wait_for_selector(".confirm-dialog", timeout=2_000)
            except Exception:  # noqa: BLE001
                return
            dialog = page.locator(".confirm-dialog").first
        if not dialog.is_visible():
            return
        ack = page.locator('[data-testid="confirm-dialog-ack-checkbox"]').first
        if ack.count() > 0 and ack.is_visible() and ack.is_enabled():
            ack.check(timeout=3_000)
        confirm = page.locator('[data-testid="confirm-dialog-confirm-button"]').first
        if confirm.count() == 0:
            failures.fail("confirm dialog opened but confirm button is missing")
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if confirm.is_enabled():
                break
            time.sleep(0.2)
        if not confirm.is_enabled():
            failures.fail("confirm dialog confirm button stayed disabled")
            return
        confirm.click(timeout=5_000)
        time.sleep(0.7)
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"confirm dialog interaction failed: {exc}")
        log_fn(f"  confirm dialog interaction failed: {exc}")


def _mc_click_run_action_from_ui(
    page: Any,
    run_id: str,
    action: Literal["cancel", "merge", "retry"],
    *,
    failures: RunFailures,
    log_fn: Callable[[str], None],
) -> bool:
    """Drive a run action through visible Mission Control controls."""
    selectors = list(_RUN_ACTION_SELECTORS[action])
    scoped_selectors = [
        f'[data-run-id="{run_id}"] [data-testid^="task-card-cancel-"]',
        *selectors,
    ] if action == "cancel" else selectors

    for selector in scoped_selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0 or not loc.is_visible() or not loc.is_enabled():
                continue
            label = _mc_action_button_text(loc).lower()
            if selector == '[data-testid="review-next-action-button"]':
                if not any(keyword in label for keyword in _RUN_ACTION_KEYWORDS[action]):
                    continue
            loc.click(timeout=5_000)
            log_fn(f"  clicked visible {action} control {selector} for run {run_id}")
            _mc_confirm_dialog_if_present(page, failures=failures, log_fn=log_fn)
            return True
        except Exception as exc:  # noqa: BLE001
            log_fn(f"  visible {action} control {selector} failed: {exc}")

    if action == "cancel":
        failures.fail(f"no visible cancel control was usable for run {run_id}")
        return False

    if not _mc_open_run_by_id(page, run_id, failures=failures, log_fn=log_fn):
        return False
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0 or not loc.is_visible() or not loc.is_enabled():
                continue
            label = _mc_action_button_text(loc).lower()
            if selector == '[data-testid="review-next-action-button"]':
                if not any(keyword in label for keyword in _RUN_ACTION_KEYWORDS[action]):
                    continue
            loc.click(timeout=5_000)
            log_fn(f"  clicked visible {action} control {selector} for run {run_id}")
            _mc_confirm_dialog_if_present(page, failures=failures, log_fn=log_fn)
            return True
        except Exception as exc:  # noqa: BLE001
            log_fn(f"  visible {action} control {selector} failed: {exc}")
    failures.fail(f"no visible {action} control was usable for run {run_id}")
    return False


def _mc_click_land_ready_from_ui(
    page: Any,
    *,
    failures: RunFailures,
    log_fn: Callable[[str], None],
) -> bool:
    """Drive the current task-board landing path through visible controls."""
    try:
        button = page.locator('[data-testid="mission-land-ready-button"]').first
        if button.count() == 0 or not button.is_visible():
            failures.note("land-ready button is not visible")
            return False
        if not button.is_enabled():
            failures.note("land-ready button is visible but disabled")
            return False
        label = _mc_action_button_text(button)
        log_fn(f"  clicking land-ready button: {label!r}")
        button.click(timeout=5_000)
        _mc_confirm_dialog_if_present(page, failures=failures, log_fn=log_fn)
        time.sleep(1.0)
        return True
    except Exception as exc:  # noqa: BLE001
        failures.note(f"land-ready click failed: {exc}")
        log_fn(f"  land-ready click failed: {exc}")
        return False


def _mc_layout_snapshot(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
) -> None:
    slug = _mc_slug(phase)
    try:
        metrics = page.evaluate(
            """() => {
              const visible = Array.from(document.querySelectorAll(
                'button,a,input,textarea,select,[data-testid]'
              )).map((el) => {
                const r = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return {
                  tag: el.tagName,
                  testid: el.getAttribute('data-testid') || '',
                  text: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 80),
                  x: Math.round(r.x),
                  y: Math.round(r.y),
                  w: Math.round(r.width),
                  h: Math.round(r.height),
                  visible: r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
                };
              }).filter((item) => item.visible).slice(0, 80);
              return {
                url: window.location.href,
                viewport: {w: window.innerWidth, h: window.innerHeight},
                activeTestId: document.activeElement?.getAttribute?.('data-testid') || '',
                visibleInteractive: visible,
              };
            }"""
        )
        path = ctx.artifact_dir / f"mc-layout-{slug}.json"
        path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-layout-{slug}")
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="layout-snapshot",
            expectation="Mission Control remains readable, with visible controls and no obvious layout collapse.",
            url=metrics.get("url"),
            artifact=path.name,
            screenshot=screenshot.name if screenshot else None,
        )
        _mc_check_expectation(
            page,
            ctx,
            phase=phase,
            action="layout snapshot",
            expectation="Mission Control shell or launcher/workspace remains visible after observing layout.",
            any_visible=[
                '[data-mc-shell="ready"]',
                '[data-testid="launcher-subhead"]',
                '[data-testid="project-workspace"]',
                '[data-testid="task-board"]',
            ],
            log_fn=log_fn,
            failures=ctx.failures,
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f"  mc-user layout snapshot failed: {exc}")
        _record_mc_user_action(ctx, phase=phase, action="layout-snapshot-failed", error=str(exc))


def _mc_scroll_visible_panel(page: Any, ctx: ScenarioContext, *, phase: str) -> None:
    try:
        result = page.evaluate(
            """() => {
              const selectors = [
                '[data-testid="run-resource-panel-logs"]',
                '[data-testid="run-resource-panel-diff"]',
                '[data-testid="run-resource-panel-artifacts"]',
                '[data-testid="run-log-pane"]',
                '[data-testid="logs-pane"]',
                '[data-testid="diff-pane"]',
                '[data-testid="proof-pane"]',
                '[data-testid="run-drawer"]',
                '[data-testid="run-list-detail-drawer"]',
                'pre',
                'main'
              ];
              const candidates = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
              const target = candidates.find((el) => el.scrollHeight > el.clientHeight + 24);
              if (target) {
                target.scrollTop = Math.min(target.scrollTop + Math.floor(target.clientHeight * 0.65), target.scrollHeight);
                return {target: target.getAttribute('data-testid') || target.tagName, scrollTop: target.scrollTop};
              }
              window.scrollBy({top: Math.floor(window.innerHeight * 0.5), left: 0, behavior: 'instant'});
              return {target: 'window', scrollY: window.scrollY};
            }"""
        )
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="scroll",
            expectation="Scrolling exposes more Mission Control content while preserving the current context.",
            result=result,
        )
        _mc_check_expectation(
            page,
            ctx,
            phase=phase,
            action="scroll",
            expectation="After scrolling, Mission Control content is still visible and usable.",
            any_visible=[
                '[data-mc-shell="ready"]',
                '[data-testid="launcher-subhead"]',
                '[data-testid="project-workspace"]',
                '[data-testid="task-board"]',
                '[data-testid="run-drawer"]',
                '[data-testid="run-list-detail-drawer"]',
            ],
            log_fn=lambda _msg: None,
            failures=ctx.failures,
        )
    except Exception as exc:  # noqa: BLE001
        _record_mc_user_action(ctx, phase=phase, action="scroll-failed", error=str(exc))


def _mc_inspect_run_surface(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> None:
    rng = _mc_phase_rng(ctx, f"{phase}:inspect")
    opened = _mc_click_first(
        page,
        [
            ".task-card-main",
            ".history-activity",
            '[data-testid^="history-row-activator-"]',
            '[data-testid="run-list-detail-drawer"]',
        ],
        log_fn=log_fn,
    )
    if opened:
        time.sleep(0.5)
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="open-run-detail",
            expectation="Clicking a run/card opens a detail surface without losing Mission Control context.",
            selector=opened,
            url=page.url,
        )
        _mc_check_expectation(
            page,
            ctx,
            phase=phase,
            action="open run detail",
            expectation="A run detail drawer, run drawer, or inspector controls are visible.",
            any_visible=[
                '[data-testid="run-list-detail-drawer"]',
                '[data-testid="run-drawer"]',
                *MC_EVIDENCE_BUTTON_SELECTORS["logs"],
                *MC_EVIDENCE_BUTTON_SELECTORS["diff"],
                *MC_EVIDENCE_BUTTON_SELECTORS["artifacts"],
                *MC_EVIDENCE_BUTTON_SELECTORS["proof"],
            ],
            log_fn=log_fn,
            failures=failures,
        )
        _mc_inspect_group_evidence(page, ctx, phase=phase, log_fn=log_fn, failures=failures)

    buttons = [
        ("logs", MC_EVIDENCE_BUTTON_SELECTORS["logs"]),
        ("proof", MC_EVIDENCE_BUTTON_SELECTORS["proof"]),
        ("diff", MC_EVIDENCE_BUTTON_SELECTORS["diff"]),
        ("artifacts", MC_EVIDENCE_BUTTON_SELECTORS["artifacts"]),
        ("history", ['[data-testid="open-history-button"]', '[data-testid="mission-history-button"]']),
    ]
    rng.shuffle(buttons)
    limit = len(buttons)
    clicked_any = False
    for name, selectors in buttons[:limit]:
        clicked = _mc_click_first(page, selectors, log_fn=log_fn)
        if not clicked:
            continue
        clicked_any = True
        time.sleep(0.5)
        _mc_scroll_visible_panel(page, ctx, phase=f"{phase}:{name}")
        screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-user-{_mc_slug(phase)}-{name}")
        _record_mc_user_action(
            ctx,
            phase=phase,
            action=f"inspect-{name}",
            expectation=f"Opening {name} shows the requested Mission Control evidence without hiding the run context.",
            selector=clicked,
            url=page.url,
            screenshot=screenshot.name if screenshot else None,
        )
        if name in MC_EVIDENCE_PANEL_SELECTORS:
            _mc_assert_evidence_panel(
                page,
                ctx,
                phase=phase,
                kind=name,
                log_fn=log_fn,
                failures=failures,
                hard=True,
                clicked_selector=clicked,
            )
        else:
            _mc_check_expectation(
                page,
                ctx,
                phase=phase,
                action=f"inspect {name}",
                expectation=f"The {name} view gives visible feedback or keeps the run detail visible.",
                any_visible=[
                    '[data-testid="history-pagination"]',
                    '[data-testid^="history-row-activator-"]',
                    '[data-testid="run-drawer"]',
                    '[data-testid="run-list-detail-drawer"]',
                ],
                log_fn=log_fn,
                failures=failures,
            )
    if opened and not clicked_any:
        failures.fail(f"mc-user {phase}: opened run detail but no logs/proof/diff/artifact controls were available")


def _mc_inspect_group_evidence(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> None:
    try:
        group_list = page.locator('[data-testid="group-list"]').first
        if group_list.count() == 0 or not group_list.is_visible():
            return
        summary = page.locator('[data-testid="group-list"] summary').first
        if summary.count() > 0 and summary.is_visible():
            try:
                summary.click(timeout=2_000)
                time.sleep(0.3)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"could not expand group list for evidence inspection: {exc}")
                return

        actions = [
            ("group logs", '[data-testid^="group-logs-"]', '[data-testid="run-resource-panel-logs"]'),
            ("group diff", '[data-testid^="group-diff-"]', '[data-testid="run-resource-panel-diff"]'),
        ]
        for label, button_selector, panel_selector in actions:
            button = page.locator(button_selector).first
            if button.count() == 0:
                failures.fail(f"{label} button missing from visible group list")
                continue
            if not button.is_visible() or not button.is_enabled():
                failures.fail(f"{label} button is not usable from visible group list")
                continue
            before = _mc_visible_text(page)
            try:
                button.click(timeout=3_000)
                time.sleep(0.6)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"{label} click failed: {exc}")
                continue
            after = _mc_visible_text(page)
            screenshot = _safe_screenshot(
                page,
                ctx.artifact_dir,
                f"mc-user-{_mc_slug(phase)}-{_mc_slug(label)}",
            )
            panel = page.locator(panel_selector).first
            panel_visible = panel.count() > 0 and panel.is_visible()
            _record_mc_user_action(
                ctx,
                phase=phase,
                action=f"inspect-{_mc_slug(label)}",
                expectation=f"Clicking {label} opens group-scoped evidence, not an inert button.",
                selector=button_selector,
                panel_selector=panel_selector,
                panel_visible=panel_visible,
                before_excerpt=before[:500],
                after_excerpt=after[:500],
                screenshot=screenshot.name if screenshot else None,
            )
            if not panel_visible:
                failures.fail(f"{label} click did not open {panel_selector}")
            elif before == after:
                failures.fail(f"{label} click did not visibly change the run detail surface")
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"group evidence inspection raised: {exc}")
        log_fn(f"  group evidence inspection raised: {exc}")


def _mc_browser_back_forward(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
) -> None:
    before = page.url
    back_url: Optional[str] = None
    forward_url: Optional[str] = None
    try:
        page.go_back(wait_until="domcontentloaded", timeout=5_000)
        time.sleep(0.5)
        back_url = page.url
        if ctx.web_url and not page.url.startswith(ctx.web_url.rstrip("/")):
            page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=15_000)
        else:
            page.go_forward(wait_until="domcontentloaded", timeout=5_000)
            time.sleep(0.5)
            forward_url = page.url
        _wait_for_mc_ready(page, timeout_ms=10_000)
        screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-user-{_mc_slug(phase)}-back-forward")
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="browser-back-forward",
            expectation="Browser back/forward preserves a usable Mission Control shell and does not strand the user.",
            before=before,
            after_back=back_url,
            after_forward=forward_url,
            current=page.url,
            screenshot=screenshot.name if screenshot else None,
        )
        _mc_check_expectation(
            page,
            ctx,
            phase=phase,
            action="browser back/forward",
            expectation="Mission Control is still actionable after browser back/forward.",
            any_visible=[
                '[data-mc-shell="ready"]',
                '[data-testid="launcher-subhead"]',
                '[data-testid="project-workspace"]',
                '[data-testid="task-board"]',
            ],
            log_fn=log_fn,
            failures=ctx.failures,
            hard=True,
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f"  mc-user back/forward failed: {exc}")
        ctx.failures.fail(f"mc-user browser back/forward failed: {exc}")
        if ctx.web_url:
            try:
                page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=15_000)
                _wait_for_mc_ready(page, timeout_ms=10_000)
            except Exception:  # noqa: BLE001
                pass
        _record_mc_user_action(ctx, phase=phase, action="browser-back-forward-failed", before=before, error=str(exc))


def _mc_refresh(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
) -> None:
    try:
        page.reload(wait_until="domcontentloaded", timeout=15_000)
        _wait_for_mc_ready(page, timeout_ms=10_000)
        screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-user-{_mc_slug(phase)}-reload")
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="reload",
            expectation="Reload returns to the same usable Mission Control context with honest loading/recovery state.",
            url=page.url,
            screenshot=screenshot.name if screenshot else None,
        )
        _mc_check_expectation(
            page,
            ctx,
            phase=phase,
            action="reload",
            expectation="Mission Control is still actionable after reload.",
            any_visible=[
                '[data-mc-shell="ready"]',
                '[data-testid="launcher-subhead"]',
                '[data-testid="project-workspace"]',
                '[data-testid="task-board"]',
            ],
            log_fn=log_fn,
            failures=ctx.failures,
            hard=True,
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f"  mc-user reload failed: {exc}")
        ctx.failures.fail(f"mc-user reload failed: {exc}")
        _record_mc_user_action(ctx, phase=phase, action="reload-failed", error=str(exc))


def _mc_click_ui_refresh(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
) -> None:
    clicked = _mc_click_first(
        page,
        [
            '[data-testid="project-workspace-refresh"]',
            '[data-testid="launcher-refresh-button"]',
        ],
        log_fn=log_fn,
    )
    if not clicked:
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="ui-refresh-skipped",
            expectation="A visible refresh control can be used when the user wants fresh state.",
            reason="no visible enabled Mission Control refresh control",
            url=page.url,
        )
        return
    time.sleep(0.8)
    screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-user-{_mc_slug(phase)}-ui-refresh")
    _record_mc_user_action(
        ctx,
        phase=phase,
        action="ui-refresh",
        expectation="Clicking the visible refresh control keeps the current Mission Control context actionable.",
        selector=clicked,
        url=page.url,
        screenshot=screenshot.name if screenshot else None,
    )
    _mc_check_expectation(
        page,
        ctx,
        phase=phase,
        action="click UI refresh",
        expectation="Mission Control remains actionable after using its visible refresh control.",
        any_visible=[
            '[data-mc-shell="ready"]',
            '[data-testid="launcher-subhead"]',
            '[data-testid="project-workspace"]',
            '[data-testid="task-board"]',
        ],
        log_fn=log_fn,
        failures=ctx.failures,
    )


def _mc_project_roundtrip(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> None:
    """Visit the launcher/project switcher and return through visible UI."""
    switchers = page.locator('[data-testid="switch-project-button"]')
    switcher = None
    try:
        for idx in range(switchers.count()):
            candidate = switchers.nth(idx)
            if candidate.is_visible():
                switcher = candidate
                break
    except Exception:  # noqa: BLE001
        switcher = None
    if switcher is None:
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="project-roundtrip-skipped",
            expectation="A launcher-enabled project can be left and reopened without losing the work context.",
            reason="project switch control is not visible in this scenario",
            url=page.url,
        )
        return
    if not switcher.is_enabled():
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="project-roundtrip-skipped",
            expectation="A launcher-enabled project can be left and reopened without losing the work context.",
            reason="project switch control is disabled, likely because launcher mode is off",
            url=page.url,
        )
        return

    current_name = ""
    current_path = ""
    if ctx.web_url:
        status, body = _api_get(ctx.web_url, "/api/projects", timeout=5.0)
        if status == 200 and isinstance(body, dict):
            current = body.get("current") or {}
            current_name = str(current.get("name") or "")
            current_path = str(current.get("path") or "")
            projects = body.get("projects") or []
            if current_path and not any(str(project.get("path") or "") == current_path for project in projects):
                _record_mc_user_action(
                    ctx,
                    phase=phase,
                    action="project-roundtrip-skipped",
                    expectation="The current project appears in the launcher so a user can come back to it.",
                    reason="current project is not listed by the launcher project payload",
                    current_name=current_name,
                    current_path=current_path,
                    url=page.url,
                )
                return
    if not current_name:
        try:
            current_name = (switcher.inner_text(timeout=500) or "").replace("Projects", "").strip()
        except Exception:  # noqa: BLE001
            current_name = ""

    before_url = page.url
    try:
        switcher.click(timeout=5_000)
        page.wait_for_selector('[data-testid="launcher-subhead"]', timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"project switcher did not open launcher: {exc}")
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="project-roundtrip-open-failed",
            expectation="Clicking Projects opens the launcher.",
            before=before_url,
            error=str(exc),
        )
        return

    launcher_screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-user-{_mc_slug(phase)}-launcher")
    _record_mc_user_action(
        ctx,
        phase=phase,
        action="project-roundtrip-open-launcher",
        expectation="The project launcher is visible, with project rows the user can choose from.",
        before=before_url,
        url=page.url,
        current_name=current_name,
        screenshot=launcher_screenshot.name if launcher_screenshot else None,
    )
    _mc_check_expectation(
        page,
        ctx,
        phase=phase,
        action="open project launcher",
        expectation="The launcher shows project choices after leaving the workspace.",
        any_visible=[
            '[data-testid="launcher-subhead"]',
            ".project-row",
            '[data-testid="launcher-empty-state"]',
        ],
        log_fn=log_fn,
        failures=failures,
        hard=True,
    )

    rows = page.locator(".project-row")
    target = None
    if current_name:
        candidate = rows.filter(has_text=current_name).first
        if candidate.count() > 0 and candidate.is_visible():
            target = candidate
    if target is None:
        failures.fail(
            "project roundtrip could not find the original project row in the launcher"
        )
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="project-roundtrip-return-failed",
            expectation="The original project row is visible so the user can return.",
            current_name=current_name,
            current_path=current_path,
            url=page.url,
        )
        return

    try:
        target.click(timeout=5_000)
        page.wait_for_selector('[data-testid="project-workspace"]', timeout=10_000)
        _wait_for_mc_ready(page, timeout_ms=10_000)
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"project row click did not return to workspace: {exc}")
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="project-roundtrip-return-failed",
            expectation="Clicking the original project row returns to the workspace.",
            current_name=current_name,
            current_path=current_path,
            error=str(exc),
        )
        return

    restored_path = ""
    if ctx.web_url and current_path:
        status, body = _api_get(ctx.web_url, "/api/projects", timeout=5.0)
        if status == 200 and isinstance(body, dict):
            restored = body.get("current") or {}
            restored_path = str(restored.get("path") or "")
            if restored_path != current_path:
                failures.fail(
                    f"project roundtrip returned to {restored_path!r}, expected {current_path!r}"
                )
    screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-user-{_mc_slug(phase)}-project-return")
    _record_mc_user_action(
        ctx,
        phase=phase,
        action="project-roundtrip-return",
        expectation="Returning from the launcher restores the original project workspace and live run context.",
        before=before_url,
        url=page.url,
        current_name=current_name,
        current_path=current_path,
        restored_path=restored_path,
        screenshot=screenshot.name if screenshot else None,
    )
    _mc_check_expectation(
        page,
        ctx,
        phase=phase,
        action="return from project launcher",
        expectation="The original project workspace is visible and actionable after switching away and back.",
        any_visible=[
            '[data-testid="project-workspace"]',
            '[data-testid="task-board"]',
            ".task-card-main",
        ],
        log_fn=log_fn,
        failures=failures,
        hard=True,
    )


def _mc_select_project_from_launcher_if_needed(
    page: Any,
    ctx: ScenarioContext,
    *,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> bool:
    """Select the scenario project through the visible launcher when shown."""
    if page.locator('[data-testid="project-workspace"], [data-testid="task-board"]').count() > 0:
        return True
    launcher = page.locator('[data-testid="launcher-subhead"]').first
    if launcher.count() == 0 or not launcher.is_visible():
        failures.fail("Mission Control showed neither a project workspace nor the project launcher")
        return False

    project_name = ctx.project_dir.name
    search = page.locator('[data-testid="launcher-project-search"]').first
    if search.count() > 0 and search.is_visible() and search.is_enabled():
        try:
            search.fill(project_name, timeout=3_000)
            time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            failures.fail(f"launcher search for project {project_name!r} failed: {exc}")
            return False

    row = page.locator(".project-row").filter(has_text=project_name).first
    if row.count() == 0:
        failures.fail(f"launcher did not list scenario project {project_name!r}")
        return False
    if not row.is_visible() or not row.is_enabled():
        failures.fail(f"launcher project row for {project_name!r} was not clickable")
        return False
    try:
        row.click(timeout=5_000)
        page.wait_for_selector('[data-testid="project-workspace"]', timeout=15_000)
        _wait_for_mc_ready(page, timeout_ms=15_000)
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"selecting launcher project {project_name!r} failed: {exc}")
        return False

    _record_mc_user_action(
        ctx,
        phase="project-select",
        action="open-project-from-launcher",
        expectation="A real Mission Control user starts from the launcher and opens the target project before queuing work.",
        project_name=project_name,
        url=page.url,
    )
    _mc_check_expectation(
        page,
        ctx,
        phase="project-select",
        action="select project from launcher",
        expectation="Selecting the project opens the actionable workspace.",
        any_visible=[
            '[data-testid="project-workspace"]',
            '[data-testid="task-board"]',
            '[data-testid="mission-new-job-button"]',
            '[data-testid="new-job-button"]',
        ],
        log_fn=log_fn,
        failures=failures,
        hard=True,
    )
    return True


def _mc_background_return(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
) -> None:
    other = None
    try:
        other = page.context.new_page()
        other.goto("about:blank", wait_until="domcontentloaded", timeout=5_000)
        time.sleep(0.6)
        page.bring_to_front()
        time.sleep(0.6)
        _wait_for_mc_ready(page, timeout_ms=10_000)
        visibility = ""
        try:
            visibility = page.evaluate("() => document.visibilityState")
        except Exception:  # noqa: BLE001
            visibility = ""
        screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-user-{_mc_slug(phase)}-background-return")
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="background-return",
            expectation="After the user visits another tab and returns, Mission Control resumes with honest current state.",
            url=page.url,
            visibility=visibility,
            screenshot=screenshot.name if screenshot else None,
        )
        _mc_check_expectation(
            page,
            ctx,
            phase=phase,
            action="background and return",
            expectation="Mission Control remains actionable after being backgrounded and foregrounded.",
            any_visible=[
                '[data-mc-shell="ready"]',
                '[data-testid="launcher-subhead"]',
                '[data-testid="project-workspace"]',
                '[data-testid="task-board"]',
            ],
            log_fn=log_fn,
            failures=ctx.failures,
        )
    except Exception as exc:  # noqa: BLE001
        log_fn(f"  mc-user background/return failed: {exc}")
        _record_mc_user_action(ctx, phase=phase, action="background-return-failed", error=str(exc))
    finally:
        if other is not None:
            try:
                other.close()
            except Exception:  # noqa: BLE001
                pass


def _mc_keyboard_probe(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> None:
    workspace = page.locator('[data-testid="project-workspace"]').first
    if workspace.count() == 0 or not workspace.is_visible():
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="keyboard-probe-skipped",
            expectation="Keyboard users can reach primary Mission Control actions without the mouse.",
            reason="project workspace is not visible",
            url=page.url,
        )
        return

    try:
        page.keyboard.press("Escape")
        page.evaluate("() => document.body.focus()")
        time.sleep(0.1)
        landed = _kbd_focus_testid(page, "mission-new-job-button", max_tabs=80)
        chain = list(getattr(page, "_w8_focus_chain", []))
        if not landed:
            page.evaluate("() => document.body.focus()")
            landed = _kbd_focus_testid(page, "new-job-button", max_tabs=80)
            chain = list(getattr(page, "_w8_focus_chain", []))
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="keyboard-focus-primary-action",
            expectation="Tab navigation can reach the primary new-job action.",
            matched=landed,
            focus_chain=chain[:80],
            url=page.url,
        )
        if not landed:
            failures.fail(
                f"keyboard probe could not Tab to a new-job button (focus chain length={len(chain)})"
            )
            return

        page.keyboard.press("Enter")
        page.wait_for_selector('[data-testid="job-dialog-intent"]', timeout=10_000)
        if _kbd_focused_testid(page) != "job-dialog-intent":
            landed_intent = _kbd_focus_testid(page, "job-dialog-intent", max_tabs=20)
            if not landed_intent:
                failures.fail("keyboard probe opened JobDialog but could not focus the intent field")
                return
        page.keyboard.type("Maybe add another follow-up later, but not now.", delay=2)
        screenshot = _safe_screenshot(page, ctx.artifact_dir, f"mc-user-{_mc_slug(phase)}-keyboard-dialog")
        _record_mc_user_action(
            ctx,
            phase=phase,
            action="keyboard-open-type-cancel-job-dialog",
            expectation="A keyboard user can open the job dialog, type an intent, and cancel without queuing accidental work.",
            url=page.url,
            screenshot=screenshot.name if screenshot else None,
        )
        page.keyboard.press("Escape")
        time.sleep(0.4)
        if page.locator('[data-testid="job-dialog-intent"]').count() > 0:
            close_button = page.locator('[data-testid="job-dialog-close-button"]').first
            if close_button.count() > 0 and close_button.is_visible() and close_button.is_enabled():
                close_button.click(timeout=3_000)
        _mc_check_expectation(
            page,
            ctx,
            phase=phase,
            action="keyboard cancel job dialog",
            expectation="Closing the keyboard-opened dialog returns to the workspace without submitting.",
            any_visible=[
                '[data-testid="project-workspace"]',
                '[data-testid="task-board"]',
                '[data-testid="mission-new-job-button"]',
                '[data-testid="new-job-button"]',
            ],
            log_fn=log_fn,
            failures=failures,
            hard=True,
        )
        if page.locator('[data-testid="job-dialog-intent"]').count() > 0:
            failures.fail("keyboard probe could not close the job dialog after typing")
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"keyboard probe raised: {exc}")
        _record_mc_user_action(ctx, phase=phase, action="keyboard-probe-failed", error=str(exc))


def _mc_running_poll_index(phase: str) -> Optional[int]:
    match = re.search(r"running-poll-(\d+)", phase)
    if not match:
        return None
    return max(0, (int(match.group(1)) - 1) // 12)


def _mc_probe_actions(ctx: ScenarioContext, phase: str) -> list[str]:
    """Return a stratified user-behavior plan for this phase.

    Pure randomness can miss whole classes of behavior. Keep the exact order
    seeded/replayable, but force coverage across evidence inspection,
    reloads, project switching, background/return, keyboard paths, UI refresh,
    back/forward navigation, scroll, and layout checks.
    """
    actions = ["layout", "inspect-run", "scroll"]
    if "running" in phase or "terminal" in phase:
        actions.extend([
            "back-forward",
            "reload",
            "ui-refresh",
            "project-roundtrip",
            "background-return",
            "keyboard-probe",
        ])
    if ctx.user_behavior == "mc-stress":
        actions.extend([
            "inspect-run",
            "scroll",
            "back-forward",
            "reload",
            "ui-refresh",
            "project-roundtrip",
            "background-return",
            "keyboard-probe",
        ])

    rng = _mc_phase_rng(ctx, phase)
    rng.shuffle(actions)
    unique_actions = list(dict.fromkeys(actions))

    if ctx.user_behavior == "mc-stress":
        return unique_actions[: min(4, len(unique_actions))]

    forced: list[str] = []
    if "terminal" in phase:
        forced = ["inspect-run", "reload"]
    elif "running-start" in phase:
        forced = ["inspect-run", "layout"]
    else:
        poll_index = _mc_running_poll_index(phase)
        if poll_index is not None:
            cycle = [
                "inspect-run",
                "reload",
                "project-roundtrip",
                "inspect-run",
                "background-return",
                "back-forward",
                "keyboard-probe",
                "scroll",
                "ui-refresh",
                "layout",
            ]
            forced = [cycle[poll_index % len(cycle)]]

    for action in unique_actions:
        if action not in forced:
            forced.append(action)
        if len(forced) >= 2:
            break
    return forced[:2]


def _mc_user_probe(
    page: Any,
    ctx: ScenarioContext,
    *,
    phase: str,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> None:
    if ctx.user_behavior == "off":
        return

    _record_mc_user_action(ctx, phase=phase, action="probe-start", url=page.url)
    actions = _mc_probe_actions(ctx, phase)
    _record_mc_user_action(ctx, phase=phase, action="probe-plan", actions=actions)

    for action in actions:
        log_fn(f"  mc-user {phase}: {action}")
        try:
            if action == "layout":
                _mc_layout_snapshot(page, ctx, phase=phase, log_fn=log_fn)
            elif action == "inspect-run":
                _mc_inspect_run_surface(page, ctx, phase=phase, log_fn=log_fn, failures=failures)
            elif action == "scroll":
                _mc_scroll_visible_panel(page, ctx, phase=phase)
            elif action == "back-forward":
                _mc_browser_back_forward(page, ctx, phase=phase, log_fn=log_fn)
            elif action == "reload":
                _mc_refresh(page, ctx, phase=phase, log_fn=log_fn)
            elif action == "ui-refresh":
                _mc_click_ui_refresh(page, ctx, phase=phase, log_fn=log_fn)
            elif action == "project-roundtrip":
                _mc_project_roundtrip(page, ctx, phase=phase, log_fn=log_fn, failures=failures)
            elif action == "background-return":
                _mc_background_return(page, ctx, phase=phase, log_fn=log_fn)
            elif action == "keyboard-probe":
                _mc_keyboard_probe(page, ctx, phase=phase, log_fn=log_fn, failures=failures)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"mc-user {phase}/{action} raised: {exc}")
            _record_mc_user_action(
                ctx,
                phase=phase,
                action=f"{action}-raised",
                error=str(exc),
            )


def _wait_for_mc_ready(page: Any, *, timeout_ms: int = 20_000) -> None:
    """Wait until the Mission Control SPA shell is ready for interaction.

    Uses the durable ``data-mc-shell="ready"`` attribute introduced for
    W3-CRITICAL-2 (cluster G ready marker). Falls back to the launcher
    subhead testid so launcher-mode entries (which intentionally do not
    flip ``data-mc-shell``) still resolve.

    The bare ``#root.children > 0`` probe used previously raced the
    ``"Loading Mission Control…"`` skeleton card and short-circuited
    before any actionable UI mounted (W3-CRITICAL-2, W4-CRITICAL-1,
    W5-CRITICAL-1).
    """
    page.wait_for_selector(
        '[data-mc-shell="ready"], [data-testid="launcher-subhead"]',
        timeout=timeout_ms,
    )


def _api_get(base_url: str, path: str, *, timeout: float = 10.0) -> tuple[int, Any]:
    """Tiny stdlib HTTP GET to avoid an extra dep."""
    import urllib.request

    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def _api_post(
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    """Tiny stdlib HTTP POST to avoid an extra dep."""
    import urllib.request

    url = base_url.rstrip("/") + path
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_product_command(
    command: list[str] | str,
    *,
    cwd: Path,
    artifact_dir: Path,
    label: str,
    timeout_s: int,
    log_fn: Callable[[str], None],
    failures: RunFailures,
    hard: bool,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> bool:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifact_dir / f"{label}.log"
    log_fn(f"  product command: {command!r} cwd={cwd}")
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(
            f"TIMEOUT after {timeout_s}s\nstdout:\n{exc.stdout or ''}\nstderr:\n{exc.stderr or ''}\n",
            encoding="utf-8",
        )
        msg = f"product command {label} timed out after {timeout_s}s"
        failures.fail(msg) if hard else failures.note(msg)
        return False
    except Exception as exc:  # noqa: BLE001
        log_path.write_text(f"RAISED: {exc}\n", encoding="utf-8")
        msg = f"product command {label} raised: {exc}"
        failures.fail(msg) if hard else failures.note(msg)
        return False
    log_path.write_text(
        f"$ {command if isinstance(command, str) else ' '.join(command)}\n"
        f"cwd={cwd}\nexit={proc.returncode}\n\nstdout:\n{proc.stdout or ''}\n\nstderr:\n{proc.stderr or ''}\n",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        msg = f"product command {label} failed with exit={proc.returncode}; see {log_path.name}"
        failures.fail(msg) if hard else failures.note(msg)
        return False
    return True


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _package_manager_for(root: Path) -> str | None:
    for lockfile, binary in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
    ):
        if (root / lockfile).exists() and shutil.which(binary):
            return binary
    return "npm" if shutil.which("npm") else None


def _package_scripts(root: Path) -> dict[str, str]:
    package = _read_json_file(root / "package.json") or {}
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in scripts.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _candidate_product_roots(
    ctx: ScenarioContext,
    *,
    state: dict[str, Any] | None,
    submitted_task_id: str | None,
    submitted_run_id: str | None,
) -> list[Path]:
    candidates: list[Path] = []
    submitted_scope = bool(submitted_task_id or submitted_run_id)

    def add(path: Path | str | None) -> None:
        if not path:
            return
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = ctx.project_dir / candidate
        candidate = candidate.resolve(strict=False)
        if candidate.exists() and candidate.is_dir() and candidate not in candidates:
            candidates.append(candidate)

    for row in _state_task_rows(state):
        keys = _task_row_keys(row)
        if submitted_task_id and submitted_task_id not in keys:
            continue
        if submitted_run_id and submitted_run_id not in keys:
            continue
        add(row.get("cwd"))
        add(row.get("worktree"))
    if submitted_task_id:
        add(ctx.project_dir / ".worktrees" / submitted_task_id)
    if not submitted_scope:
        for path in sorted(
            ctx.project_dir.glob(".worktrees/*"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        ):
            add(path)
        add(ctx.project_dir)

    # Agents often scaffold into a nested app/ or frontend/ directory.
    # Treat nested package.json/index.html parents as candidate product roots
    # instead of only scanning the queue worktree shell.
    for base in list(candidates):
        seen_nested = 0
        for marker in ("package.json", "index.html"):
            for marker_path in base.rglob(marker):
                if any(part in {".git", "node_modules", "otto_logs", "otto_artifacts"} for part in marker_path.parts):
                    continue
                add(marker_path.parent)
                seen_nested += 1
                if seen_nested >= 40:
                    break
            if seen_nested >= 40:
                break

    scored: list[tuple[int, Path]] = []
    for root in candidates:
        score = 0
        for rel, points in (
            ("package.json", 5),
            ("index.html", 4),
            ("src", 3),
            ("app.py", 2),
            ("pyproject.toml", 2),
            ("requirements.txt", 1),
        ):
            if (root / rel).exists():
                score += points
        if score > 0:
            scored.append((score, root))
    scored.sort(key=lambda item: (-item[0], str(item[1])))
    return [root for _score, root in scored]


def _scan_product_files(root: Path, artifact_dir: Path) -> list[str]:
    file_list: list[str] = []
    for path in sorted(root.rglob("*"))[:500]:
        if not path.is_file():
            continue
        if any(part in {".git", "node_modules", "otto_logs", "otto_artifacts"} for part in path.parts):
            continue
        rel = path.relative_to(root)
        file_list.append(str(rel))
    (artifact_dir / "project-files.txt").write_text(
        "\n".join(file_list), encoding="utf-8"
    )
    return file_list


def _latest_session_dir(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    sessions = [path for path in root.iterdir() if path.is_dir()]
    if not sessions:
        return None
    return max(sessions, key=lambda path: path.stat().st_mtime)


def _find_session_dir_for_run(
    ctx: ScenarioContext,
    run_id: str | None,
    *,
    queue_task_id: str | None = None,
) -> Path | None:
    if run_id:
        if queue_task_id:
            candidate = (
                ctx.project_dir
                / ".worktrees"
                / queue_task_id
                / "otto_logs"
                / "sessions"
                / run_id
            )
            if candidate.is_dir():
                return candidate
        for candidate in ctx.project_dir.glob(f".worktrees/*/otto_logs/sessions/{run_id}"):
            if candidate.is_dir():
                return candidate
        candidate = ctx.project_dir / "otto_logs" / "sessions" / run_id
        if candidate.is_dir():
            return candidate
    if queue_task_id:
        return _latest_session_dir(
            ctx.project_dir / ".worktrees" / queue_task_id / "otto_logs" / "sessions"
        )
    return None


def _parse_event_ts_s(event: dict[str, Any]) -> float:
    raw = str(event.get("ts") or event.get("timestamp") or "")
    if not raw:
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _assert_group_concurrency_observed(
    ctx: ScenarioContext,
    *,
    run_id: str | None,
    queue_task_id: str | None = None,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> None:
    expected = ctx.group_concurrent
    if expected is None or expected <= 1:
        return
    session_dir = _find_session_dir_for_run(
        ctx,
        run_id,
        queue_task_id=queue_task_id,
    )
    if session_dir is None:
        failures.fail("concurrency requested but could not locate the run session journal")
        return
    events_path = session_dir / "spec-state.jsonl"
    if not events_path.exists():
        failures.fail("concurrency requested but spec-state.jsonl is missing")
        return
    events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    proof = _read_json_file(session_dir / "proof-packet.json") or {}
    groups = proof.get("groups") or []
    group_count = len(groups) if isinstance(groups, list) else 0
    if group_count == 0:
        spec_json = _read_json_file(session_dir / "spec" / "spec.json") or {}
        spec_groups = spec_json.get("groups") or []
        group_count = len(spec_groups) if isinstance(spec_groups, list) else 0
    started = [
        event
        for event in events
        if str(event.get("event") or event.get("kind") or "") == "group.started"
    ]
    execution_started = [
        event
        for event in events
        if str(event.get("event") or event.get("kind") or "") == "group.execution.started"
    ]
    execution_finished = [
        event
        for event in events
        if str(event.get("event") or event.get("kind") or "") == "group.execution.finished"
    ]
    terminal = [
        event
        for event in events
        if str(event.get("event") or event.get("kind") or "")
        in {
            "group.merge.eligible",
            "group.blocked",
            "group.failed_scope",
            "group.merge.landed",
            "group.merge.redundant",
        }
    ]
    first_terminal_s = min((_parse_event_ts_s(event) for event in terminal), default=0.0)
    first_execution_finished_s = min(
        (_parse_event_ts_s(event) for event in execution_finished),
        default=first_terminal_s,
    )
    overlapping_starts = [
        event
        for event in started
        if first_terminal_s <= 0 or _parse_event_ts_s(event) <= first_terminal_s
    ]
    overlapping_execution = [
        event
        for event in execution_started
        if (
            first_execution_finished_s <= 0
            or _parse_event_ts_s(event) <= first_execution_finished_s
        )
    ]
    execution_finish_by_group: dict[str, float] = {}
    for event in [*execution_finished, *terminal]:
        group_id = str(event.get("group_id") or "")
        if not group_id:
            continue
        ts_s = _parse_event_ts_s(event)
        if ts_s <= 0:
            continue
        previous = execution_finish_by_group.get(group_id)
        if previous is None or ts_s < previous:
            execution_finish_by_group[group_id] = ts_s

    timeline: list[tuple[float, int, str]] = []
    for event in execution_started:
        group_id = str(event.get("group_id") or "")
        start_s = _parse_event_ts_s(event)
        if not group_id or start_s <= 0:
            continue
        finish_s = execution_finish_by_group.get(group_id)
        if finish_s is not None and finish_s <= start_s:
            continue
        timeline.append((start_s, 1, group_id))
        if finish_s is not None:
            timeline.append((finish_s, 0, group_id))
    active_groups: set[str] = set()
    max_overlap = 0
    max_overlap_groups: list[str] = []
    for _ts_s, kind_order, group_id in sorted(timeline):
        if kind_order == 0:
            active_groups.discard(group_id)
            continue
        active_groups.add(group_id)
        if len(active_groups) > max_overlap:
            max_overlap = len(active_groups)
            max_overlap_groups = sorted(active_groups)

    report = {
        "requested_group_concurrent": expected,
        "group_count": group_count,
        "started_count": len(started),
        "started_before_first_terminal": [
            str(event.get("group_id") or "") for event in overlapping_starts
        ],
        "execution_started_count": len(execution_started),
        "execution_started_before_first_finished": [
            str(event.get("group_id") or "") for event in overlapping_execution
        ],
        "max_execution_overlap": max_overlap,
        "max_execution_overlap_groups": max_overlap_groups,
        "session_dir": str(session_dir),
    }
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    (ctx.artifact_dir / "group-concurrency.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    log_fn(f"  concurrency report: {report}")
    if group_count < 2:
        failures.fail(
            f"concurrency requested ({expected}) but the generated spec had only {group_count} group(s)"
        )
        return
    if not execution_started:
        failures.fail(
            "concurrency requested but the run has no group.execution.started events; "
            "the harness cannot prove real overlapping execution"
        )
        return
    if max_overlap < min(expected, group_count):
        failures.fail(
            "concurrency requested but the run did not overlap multiple group executions "
            "within any dependency-ready wave; see group-concurrency.json"
        )


def _run_native_product_tests(
    root: Path,
    *,
    artifact_dir: Path,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> None:
    from otto.config import detect_test_command

    command = detect_test_command(root)
    if not command:
        failures.note("product verification: no native test command detected")
        return
    if (root / "package.json").exists() and _install_node_dependencies(
        root,
        artifact_dir=artifact_dir,
        log_fn=log_fn,
        failures=failures,
    ) is None:
        return
    env = dict(os.environ)
    env.setdefault("CI", "1")
    _run_product_command(
        command,
        cwd=root,
        artifact_dir=artifact_dir,
        label="product-native-tests",
        timeout_s=180,
        log_fn=log_fn,
        failures=failures,
        hard=True,
        env=env,
        shell=True,
    )


def _install_node_dependencies(
    root: Path,
    *,
    artifact_dir: Path,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> str | None:
    if not (root / "package.json").exists():
        return None
    manager = _package_manager_for(root)
    if manager is None:
        failures.fail("product has package.json but no supported package manager is available")
        return None
    if (root / "node_modules").exists():
        return manager
    if manager == "pnpm":
        command = ["pnpm", "install", "--frozen-lockfile=false"]
    elif manager == "yarn":
        command = ["yarn", "install", "--non-interactive"]
    elif manager == "bun":
        command = ["bun", "install"]
    else:
        command = ["npm", "install", "--no-audit", "--no-fund"]
    if not _run_product_command(
        command,
        cwd=root,
        artifact_dir=artifact_dir,
        label=f"product-{manager}-install",
        timeout_s=240,
        log_fn=log_fn,
        failures=failures,
        hard=True,
    ):
        return None
    return manager


def _wait_for_http_ready(url: str, *, timeout_s: float) -> bool:
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < 500:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def _start_static_product_server(root: Path, port: int) -> tuple[str, Callable[[], None]]:
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, _format: str, *args: Any) -> None:  # noqa: A002
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}/", stop


def _start_node_product_server(
    root: Path,
    *,
    script: str,
    manager: str,
    port: int,
    artifact_dir: Path,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> tuple[str, Callable[[], None]] | None:
    if manager == "npm":
        command = ["npm", "run", script, "--", "--host", "127.0.0.1", "--port", str(port)]
    else:
        command = [manager, "run", script, "--host", "127.0.0.1", "--port", str(port)]
    log_path = artifact_dir / f"product-{script}-server.log"
    env = dict(os.environ)
    env.update({"HOST": "127.0.0.1", "PORT": str(port), "BROWSER": "none"})
    handle = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            command,
            cwd=root,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            preexec_fn=os.setsid,
        )
    except Exception as exc:  # noqa: BLE001
        handle.close()
        failures.fail(f"could not start product script {script}: {exc}")
        return None
    url = f"http://127.0.0.1:{port}/"
    if not _wait_for_http_ready(url, timeout_s=45):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
        handle.close()
        failures.fail(f"product script {script} did not serve {url}; see {log_path.name}")
        return None

    def stop() -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
        handle.close()

    log_fn(f"  product server started: {url} via {command!r}")
    return url, stop


def _start_generated_web_app(
    root: Path,
    *,
    artifact_dir: Path,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> tuple[str, Callable[[], None]] | None:
    for static_dir in (root / "dist", root / "build", root):
        if (static_dir / "index.html").exists() and not (root / "package.json").exists():
            port = _free_port()
            url, stop = _start_static_product_server(static_dir, port)
            if _wait_for_http_ready(url, timeout_s=5):
                log_fn(f"  static product server started: {url} root={static_dir}")
                return url, stop

    scripts = _package_scripts(root)
    manager: str | None = None
    if (root / "package.json").exists():
        manager = _install_node_dependencies(
            root,
            artifact_dir=artifact_dir,
            log_fn=log_fn,
            failures=failures,
        )
        if manager is None:
            return None
        if "build" in scripts:
            _run_product_command(
                [manager, "run", "build"] if manager != "npm" else ["npm", "run", "build"],
                cwd=root,
                artifact_dir=artifact_dir,
                label="product-build",
                timeout_s=240,
                log_fn=log_fn,
                failures=failures,
                hard=True,
            )
            for static_dir in (root / "dist", root / "build"):
                if (static_dir / "index.html").exists():
                    port = _free_port()
                    url, stop = _start_static_product_server(static_dir, port)
                    if _wait_for_http_ready(url, timeout_s=5):
                        log_fn(f"  built product served: {url} root={static_dir}")
                        return url, stop
        for script in ("dev", "preview", "start"):
            if script in scripts:
                started = _start_node_product_server(
                    root,
                    script=script,
                    manager=manager,
                    port=_free_port(),
                    artifact_dir=artifact_dir,
                    log_fn=log_fn,
                    failures=failures,
                )
                if started is not None:
                    return started

    for static_dir in (root / "dist", root / "build", root):
        if (static_dir / "index.html").exists():
            port = _free_port()
            url, stop = _start_static_product_server(static_dir, port)
            if _wait_for_http_ready(url, timeout_s=5):
                log_fn(f"  static product server started: {url} root={static_dir}")
                return url, stop

    failures.fail(
        "build reported success but product verification could not find a launchable web app "
        "(package script or index.html)"
    )
    return None


def _fill_first_product_composer(page: Any, text: str) -> bool:
    selectors = [
        "textarea",
        'input[type="text"]',
        "input:not([type])",
        '[contenteditable="true"]',
        '[role="textbox"]',
    ]
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if loc.count() == 0 or not loc.is_visible() or not loc.is_enabled():
                continue
            loc.fill(text, timeout=3_000)
            return True
        except Exception:  # noqa: BLE001
            try:
                loc.click(timeout=1_000)
                page.keyboard.type(text, delay=10)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _click_product_submit(page: Any) -> bool:
    patterns = re.compile(r"(post|tweet|submit|publish|send|add)", re.I)
    for selector in ("button", 'input[type="submit"]', '[role="button"]'):
        loc = page.locator(selector).filter(has_text=patterns).first
        try:
            if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                loc.click(timeout=3_000)
                return True
        except Exception:  # noqa: BLE001
            continue
    try:
        page.keyboard.press("Meta+Enter")
        return True
    except Exception:  # noqa: BLE001
        return False


def _product_layout_findings(page: Any) -> list[str]:
    try:
        metrics = page.evaluate(
            """() => {
              const doc = document.documentElement;
              const body = document.body;
              const text = (body?.innerText || '').trim();
              const vw = doc.clientWidth;
              const overflowing = [];
              for (const el of Array.from(document.querySelectorAll('body *'))) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && (rect.right > vw + 4 || rect.left < -4)) {
                  overflowing.push((el.getAttribute('data-testid') || el.tagName || 'element') + ':' + Math.round(rect.left) + '-' + Math.round(rect.right));
                  if (overflowing.length >= 5) break;
                }
              }
              return { textLength: text.length, scrollWidth: doc.scrollWidth, clientWidth: vw, overflowing };
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        return [f"layout inspection raised: {exc}"]
    findings: list[str] = []
    if int(metrics.get("textLength") or 0) < 20:
        findings.append("product page looks blank or nearly blank")
    if int(metrics.get("scrollWidth") or 0) > int(metrics.get("clientWidth") or 0) + 8:
        findings.append("product page has horizontal overflow")
    overflowing = metrics.get("overflowing") or []
    if overflowing:
        findings.append("visible elements overflow viewport: " + ", ".join(map(str, overflowing[:5])))
    return findings


def _record_product_expectation(
    artifact_dir: Path,
    *,
    action: str,
    expectation: str,
    observation: str,
    verdict: str,
) -> None:
    with (artifact_dir / "product-expectations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "action": action,
                    "expectation": expectation,
                    "observation": observation,
                    "verdict": verdict,
                },
                sort_keys=True,
            )
            + "\n"
        )


def _verify_micro_twitter_product(
    page: Any,
    *,
    artifact_dir: Path,
    failures: RunFailures,
    log_fn: Callable[[str], None],
) -> None:
    del log_fn
    unique_post = f"otto true web post {int(time.time())}"
    if not _fill_first_product_composer(page, unique_post):
        _record_product_expectation(
            artifact_dir,
            action="find composer",
            expectation="Micro Twitter exposes a text composer for creating a post.",
            observation="No visible editable textbox was usable.",
            verdict="blocked",
        )
        failures.fail("micro Twitter product has no usable post composer")
        return
    _safe_screenshot(page, artifact_dir, "product-01-composer-filled")
    _record_product_expectation(
        artifact_dir,
        action="type post",
        expectation="Typed post text remains visible in the composer.",
        observation=f"typed={unique_post!r}",
        verdict="expected",
    )

    if not _click_product_submit(page):
        failures.fail("micro Twitter product has no usable submit/post action")
        return
    try:
        page.get_by_text(unique_post, exact=True).wait_for(timeout=6_000)
    except Exception:  # noqa: BLE001
        _safe_screenshot(page, artifact_dir, "product-02-post-missing")
        failures.fail("micro Twitter product did not show the submitted post in the feed")
        return
    _safe_screenshot(page, artifact_dir, "product-02-post-visible")
    _record_product_expectation(
        artifact_dir,
        action="submit post",
        expectation="Submitted post appears in the feed without a hidden API/disk shortcut.",
        observation=f"post visible={unique_post!r}",
        verdict="expected",
    )

    action_pattern = re.compile(r"(like|favorite|heart|delete|remove|reply)", re.I)
    action_count = 0
    for selector in ("button", '[role="button"]'):
        try:
            action_count += page.locator(selector).filter(has_text=action_pattern).count()
        except Exception:  # noqa: BLE001
            continue
    if action_count == 0:
        failures.fail(
            "micro Twitter product has posting, but no visible social/feed item action "
            "(like/favorite/delete/reply/remove)"
        )
    else:
        _record_product_expectation(
            artifact_dir,
            action="inspect feed actions",
            expectation="A micro Twitter feed exposes at least one visible post action.",
            observation=f"action_count={action_count}",
            verdict="expected",
        )

    try:
        page.reload(wait_until="networkidle", timeout=10_000)
        page.get_by_text(unique_post, exact=True).wait_for(timeout=6_000)
        _safe_screenshot(page, artifact_dir, "product-03-post-persists-after-refresh")
        _record_product_expectation(
            artifact_dir,
            action="refresh product",
            expectation="A reasonable micro Twitter keeps the created post visible after refresh.",
            observation="post still visible after reload",
            verdict="expected",
        )
    except Exception:  # noqa: BLE001
        _safe_screenshot(page, artifact_dir, "product-03-post-lost-after-refresh")
        failures.fail("micro Twitter product lost the submitted post after browser refresh")


def _verify_generic_web_product(
    page: Any,
    *,
    artifact_dir: Path,
    failures: RunFailures,
) -> None:
    _safe_screenshot(page, artifact_dir, "product-00-desktop")
    findings = _product_layout_findings(page)
    for finding in findings:
        failures.fail(f"generated product UX/layout issue: {finding}")
    try:
        page.set_viewport_size({"width": 390, "height": 844})
        time.sleep(0.5)
        _safe_screenshot(page, artifact_dir, "product-00-mobile")
        mobile_findings = _product_layout_findings(page)
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"mobile product layout inspection raised: {exc}")
        return
    for finding in mobile_findings:
        failures.fail(f"generated product mobile UX/layout issue: {finding}")


def _verify_generated_product(
    page: Any,
    ctx: ScenarioContext,
    *,
    build_intent: str,
    terminal_outcome: str | None,
    final_state: dict[str, Any] | None,
    submitted_task_id: str | None,
    submitted_run_id: str | None,
    log_fn: Callable[[str], None],
    failures: RunFailures,
) -> None:
    log_fn("Step 11: product verification — launch and interact")
    product_artifacts = ctx.artifact_dir / "product-verification"
    product_artifacts.mkdir(parents=True, exist_ok=True)
    if terminal_outcome != "success":
        failures.note("product verification skipped because the build did not report success")
        return

    roots = _candidate_product_roots(
        ctx,
        state=final_state,
        submitted_task_id=submitted_task_id,
        submitted_run_id=submitted_run_id,
    )
    (product_artifacts / "candidate-roots.json").write_text(
        json.dumps([str(root) for root in roots], indent=2), encoding="utf-8"
    )
    if not roots:
        failures.fail("build reported success but no plausible product root was found")
        return
    root = roots[0]
    file_list = _scan_product_files(root, product_artifacts)
    created = [
        f for f in file_list
        if f.endswith((".html", ".js", ".jsx", ".ts", ".tsx", ".css", ".py"))
    ]
    if not created:
        failures.fail("build reported success but no source files appeared in the product root")
        return

    _run_native_product_tests(
        root,
        artifact_dir=product_artifacts,
        log_fn=log_fn,
        failures=failures,
    )
    server = _start_generated_web_app(
        root,
        artifact_dir=product_artifacts,
        log_fn=log_fn,
        failures=failures,
    )
    if server is None:
        return
    url, stop_server = server
    product_page = None
    try:
        product_page = page.context.new_page()
        product_page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        time.sleep(1.0)
        _verify_generic_web_product(
            product_page,
            artifact_dir=product_artifacts,
            failures=failures,
        )
        try:
            product_page.set_viewport_size({"width": 1280, "height": 800})
        except Exception:  # noqa: BLE001
            pass
        intent_l = build_intent.lower()
        if (
            "micro twitter" in intent_l
            or "micro-twitter" in intent_l
            or "microblog" in intent_l
            or "micro feed" in intent_l
            or "microfeed" in intent_l
        ):
            _verify_micro_twitter_product(
                product_page,
                artifact_dir=product_artifacts,
                failures=failures,
                log_fn=log_fn,
            )
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"generated product browser verification raised: {exc}")
    finally:
        if product_page is not None:
            try:
                product_page.close()
            except Exception:  # noqa: BLE001
                pass
        stop_server()


def _web_watcher_running(base_url: Optional[str]) -> bool:
    if not base_url:
        return False
    _, body = _api_get(base_url, "/api/state", timeout=5.0)
    if not isinstance(body, dict):
        return False
    watcher = body.get("watcher") or {}
    health = watcher.get("health") or {}
    return bool(
        watcher.get("alive")
        or str(health.get("state") or "").lower() == "running"
        or health.get("watcher_process_alive")
    )


def _start_otto_web_in_process(
    project_dir: Path,
    artifact_dir: Path,
    *,
    project_launcher: bool = False,
) -> Any:
    """Bring up otto's FastAPI in a thread on a free port; return the handle."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.browser._helpers.build_bundle import ensure_bundle_built  # type: ignore

    ensure_bundle_built()
    start_backend = _import_start_backend()
    projects_root = project_dir.parent if project_launcher else artifact_dir / "managed-projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    return start_backend(project_dir, projects_root=projects_root, project_launcher=project_launcher)


def _provider_extra_args(provider: str) -> list[str]:
    return ["--provider", provider]


def _stub_scenario(ctx: ScenarioContext) -> ScenarioRunResult:
    raise NotImplementedError(
        f"scenario {ctx.scenario.id} not yet implemented; see plan-mc-audit.md 5B"
    )


def _run_w1(ctx: ScenarioContext) -> ScenarioRunResult:
    """W1 — first-time user end-to-end through Mission Control + real LLM build."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug = open(ctx.debug_log, "a", encoding="utf-8")

    def _log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S", time.gmtime())
        line = f"[{ts}] [W1] {msg}\n"
        debug.write(line)
        debug.flush()
        print(line, end="", flush=True)

    if not failures.soft_assert(ctx.web_url is not None, "harness did not provide ctx.web_url"):
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url", duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")
    if ctx.user_behavior != "off":
        _log(f"mc_user_behavior={ctx.user_behavior} seed={_mc_user_seed(ctx)}")

    final_state: Optional[dict[str, Any]] = None
    build_intent = _scenario_intent(ctx, W1_INTENT)
    submitted_task: dict[str, Any] | None = None
    submitted_task_id: Optional[str] = None
    submitted_run_id: Optional[str] = None
    terminal_provider_infra_reason = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed (non-fatal): {exc}")

        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: page loads ----------
            _log("Step 1: page.goto")
            try:
                page.goto(ctx.web_url, wait_until="networkidle", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"page.goto failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-loaded")

            # Wait for the SPA shell to be ready (W3-CRITICAL-2 fix).
            try:
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"react never hydrated: {exc}")
            _mc_select_project_from_launcher_if_needed(
                page,
                ctx,
                log_fn=_log,
                failures=failures,
            )
            _agent_browser_mc_probe(
                ctx,
                phase="shell-loaded",
                url=page.url,
                expectation="Agent-browser sees the same actionable Mission Control shell a real user sees.",
                log_fn=_log,
                failures=failures,
                hard=True,
            )
            if ctx.user_behavior != "off":
                _mc_check_expectation(
                    page,
                    ctx,
                    phase="shell-loaded",
                    action="open Mission Control",
                    expectation="Mission Control loads into an actionable launcher or project workspace.",
                    any_visible=[
                        '[data-mc-shell="ready"]',
                        '[data-testid="launcher-subhead"]',
                        '[data-testid="project-workspace"]',
                        '[data-testid="task-board"]',
                    ],
                    log_fn=_log,
                    failures=failures,
                    hard=True,
                )

            # ---------- Step 2: tasks UI present ----------
            # In our setup we ran with project_launcher=False, so the project
            # is already selected. Verify either the launcher is visible
            # (pre-selection) OR the task board is.
            _log("Step 2: check root UI loaded")
            has_task_board = page.locator('[data-testid="task-board"]').count() > 0
            has_launcher = page.locator('[data-testid="launcher-subhead"]').count() > 0
            failures.soft_assert(
                has_task_board or has_launcher,
                f"neither task-board nor launcher-subhead present (testid count: tb={has_task_board}, l={has_launcher})",
            )
            _safe_screenshot(page, artifact_dir, "02-shell")
            _mc_user_probe(
                page,
                ctx,
                phase="shell-loaded",
                log_fn=_log,
                failures=failures,
            )

            # ---------- Step 3+5: open JobDialog ----------
            _log("Step 3: click new-job/start-first-build")
            new_job_button = page.locator(
                '[data-testid="mission-new-job-button"], [data-testid="new-job-button"]'
            ).first
            if not failures.soft_assert(
                new_job_button.count() > 0, "new-job button not found"
            ):
                _safe_screenshot(page, artifact_dir, "03-no-new-job-button")
            else:
                try:
                    new_job_button.click(timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"new-job click failed: {exc}")

            # Dialog should appear
            try:
                page.wait_for_selector(
                    '[data-testid="job-dialog-intent"]', timeout=10_000
                )
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"job dialog did not open: {exc}")
            _safe_screenshot(page, artifact_dir, "04-job-dialog")
            if ctx.user_behavior != "off":
                _mc_check_expectation(
                    page,
                    ctx,
                    phase="job-dialog-open",
                    action="click new job",
                    expectation="The job dialog opens with an intent field and a submit button.",
                    all_visible=[
                        '[data-testid="job-dialog-intent"]',
                        '[data-testid="job-dialog-submit-button"]',
                    ],
                    log_fn=_log,
                    failures=failures,
                    hard=True,
                )

            # ---------- Step 6: submit intent ----------
            _log("Step 6: fill intent + submit")
            intent_field = page.locator('[data-testid="job-dialog-intent"]')
            if intent_field.count() > 0:
                try:
                    intent_field.fill(build_intent, timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"intent fill failed: {exc}")
            # Set provider in advanced options (optional — server picks default if blank)
            provider_select = page.locator('[data-testid="job-provider-select"]')
            if provider_select.count() > 0:
                try:
                    # Open advanced section first
                    advanced = page.locator("details.job-advanced summary").first
                    if advanced.count() > 0:
                        advanced.click()
                    provider_select.select_option(value=ctx.provider, timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"could not set provider in dialog: {exc}")
            if ctx.build_timeout_s is not None:
                budget_input = page.locator('[data-testid="job-budget-input"]')
                if budget_input.count() > 0:
                    try:
                        budget_input.fill(str(ctx.build_timeout_s), timeout=5_000)
                    except Exception as exc:  # noqa: BLE001
                        failures.note(f"could not set visible budget in dialog: {exc}")
                else:
                    failures.note("job budget input not found; relying on project default run budget")

            submit = page.locator('[data-testid="job-dialog-submit-button"]')
            before_submit_keys = _state_task_keys(_state(ctx.web_url))
            if not failures.soft_assert(
                submit.count() > 0, "submit button missing in JobDialog"
            ):
                _safe_screenshot(page, artifact_dir, "05-no-submit")
            else:
                try:
                    submit.click(timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"submit click failed: {exc}")

            try:
                page.wait_for_selector(
                    '[data-testid="job-dialog-intent"]', state="detached", timeout=15_000
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"job dialog did not close after submit within 15s: {exc}")
            submitted_task = _wait_for_submitted_task(
                ctx.web_url,
                before_keys=before_submit_keys,
                timeout_s=20,
                expected_intent=build_intent,
                log_fn=_log,
            )
            submitted_to_queue = submitted_task is not None
            if submitted_task is not None:
                submitted_task_id = str(
                    submitted_task.get("queue_task_id") or submitted_task.get("task_id") or ""
                ).strip() or None
                submitted_run_id = str(submitted_task.get("run_id") or "").strip() or None
            _safe_screenshot(page, artifact_dir, "05-submitted")
            if not submitted_to_queue:
                failures.fail(
                    "submit did not create a new queued/running task matching the entered intent within 20s; "
                    "not starting watcher or waiting for a terminal build"
                )
            if ctx.user_behavior != "off":
                _mc_check_expectation(
                    page,
                    ctx,
                    phase="job-submitted",
                    action="submit build intent",
                    expectation="After submitting, the dialog closes or Mission Control shows queue/workspace feedback.",
                    any_visible=[
                        '[data-testid="mission-start-watcher-button"]',
                        '[data-testid="start-watcher-button"]',
                        '[data-testid="project-workspace"]',
                        '[data-testid="task-board"]',
                    ],
                    log_fn=_log,
                    failures=failures,
                    hard=True,
                )

            # ---------- Step 7: start watcher (queue won't run otherwise) ----------
            _log("Step 7: start watcher")
            if submitted_to_queue:
                time.sleep(1.5)  # let dialog close + state refresh
                _click_start_watcher(page, failures=failures, label="W1", base_url=ctx.web_url)
            else:
                _log("skipping watcher start because no queued task exists")
            _safe_screenshot(page, artifact_dir, "06-watcher-started")
            if ctx.user_behavior != "off":
                _mc_check_expectation(
                    page,
                    ctx,
                    phase="watcher-started",
                    action="start queue watcher",
                    expectation="Mission Control remains on the queue/workspace view and gives visible run progress context.",
                    any_visible=[
                        '[data-testid="project-workspace"]',
                        '[data-testid="task-board"]',
                        ".task-card-main",
                    ],
                    log_fn=_log,
                    failures=failures,
                )
            _mc_user_probe(
                page,
                ctx,
                phase="running-start",
                log_fn=_log,
                failures=failures,
            )

            # ---------- Step 8: wait for terminal state ----------
            _log("Step 8: poll /api/state for terminal status")
            build_budget_s = ctx.build_timeout_s or W1_BUILD_TIMEOUT_S
            terminal_wait_s = _terminal_wait_for_build_timeout(build_budget_s)
            _log(
                f"  Otto build budget={build_budget_s}s; true-web terminal wait={terminal_wait_s}s"
            )
            deadline = time.monotonic() + terminal_wait_s
            terminal_outcome: Optional[str] = None
            poll_count = 0
            last_state: Optional[dict[str, Any]] = None
            last_progress_signature: Optional[str] = None
            last_progress_at = time.monotonic()
            stalled = False
            if not submitted_to_queue:
                _log("skipping terminal poll because submit produced no queue row")
            while submitted_to_queue and time.monotonic() < deadline:
                poll_count += 1
                status, body = _api_get(ctx.web_url, "/api/state")
                if status != 200 or not isinstance(body, dict):
                    if poll_count % 6 == 0:
                        _log(f"/api/state returned status={status}")
                    time.sleep(5)
                    continue
                last_state = body
                progress_signature = _submitted_task_progress_signature(
                    body,
                    submitted_task_id=submitted_task_id,
                    submitted_run_id=submitted_run_id,
                )
                if progress_signature and progress_signature != last_progress_signature:
                    last_progress_signature = progress_signature
                    last_progress_at = time.monotonic()
                # /api/state schema: live.items[]/history.items[] flat dicts.
                history = (body.get("history") or {}).get("items") or []
                live = (body.get("live") or {}).get("items") or []
                live_statuses = [item.get("status") for item in live]
                history_outcomes = [item.get("terminal_outcome") for item in history]
                if poll_count % 12 == 1:  # ~every minute
                    _log(
                        f"poll#{poll_count}: live={live_statuses[:3]} history_outcomes={history_outcomes[:3]}"
                    )
                    _agent_browser_mc_probe(
                        ctx,
                        phase=f"running-poll-{poll_count}",
                        url=page.url,
                        expectation="Agent-browser can inspect Mission Control while the build is running.",
                        log_fn=_log,
                        failures=failures,
                    )
                    _mc_user_probe(
                        page,
                        ctx,
                        phase=f"running-poll-{poll_count}",
                        log_fn=_log,
                        failures=failures,
                    )
                terminal_outcome, submitted_run_id = _terminal_outcome_for_submitted_task(
                    body,
                    submitted_task_id=submitted_task_id,
                    submitted_run_id=submitted_run_id,
                )
                if terminal_outcome:
                    break
                if (
                    last_progress_signature
                    and time.monotonic() - last_progress_at
                    >= W1_VISIBLE_PROGRESS_IDLE_TIMEOUT_S
                ):
                    stalled = True
                    _write_meta_debug_packet(
                        ctx,
                        reason="visible-progress-stall",
                        state=last_state,
                        submitted_task_id=submitted_task_id,
                        submitted_run_id=submitted_run_id,
                        poll_count=poll_count,
                        progress_signature=last_progress_signature,
                        terminal_outcome=terminal_outcome,
                        log_fn=_log,
                    )
                    failures.fail(
                        "submitted build showed no Mission Control-visible progress change "
                        f"for {W1_VISIBLE_PROGRESS_IDLE_TIMEOUT_S}s; true-web stopped "
                        "waiting and treated this as a user-visible stall"
                    )
                    break
                time.sleep(5)
            _log(f"terminal_outcome={terminal_outcome}")
            if not stalled:
                failures.soft_assert(
                    terminal_outcome is not None,
                    f"build did not reach terminal in {terminal_wait_s}s "
                    f"(Otto build budget {build_budget_s}s)",
                )
            if terminal_outcome and terminal_outcome != "success":
                _write_meta_debug_packet(
                    ctx,
                    reason="terminal-failure",
                    state=last_state,
                    submitted_task_id=submitted_task_id,
                    submitted_run_id=submitted_run_id,
                    poll_count=poll_count,
                    progress_signature=last_progress_signature,
                    terminal_outcome=terminal_outcome,
                    log_fn=_log,
                )
                session_dir = _find_session_dir_for_run(
                    ctx,
                    submitted_run_id,
                    queue_task_id=submitted_task_id,
                )
                terminal_provider_infra_reason = _session_provider_infra_failure_reason(session_dir)
                if terminal_provider_infra_reason:
                    failures.fail(
                        "provider infrastructure failure before product build: "
                        f"{terminal_provider_infra_reason}"
                    )
                else:
                    failures.fail(f"build terminal_outcome={terminal_outcome!r} (expected success)")
            _safe_screenshot(page, artifact_dir, "07-build-terminal")
            _mc_user_probe(
                page,
                ctx,
                phase="terminal-state",
                log_fn=_log,
                failures=failures,
            )
            _agent_browser_mc_probe(
                ctx,
                phase="terminal-state",
                url=page.url,
                expectation="Agent-browser can inspect the terminal Mission Control state and evidence controls.",
                log_fn=_log,
                failures=failures,
            )

            # ---------- Step 9: refresh page + final state ----------
            try:
                page.reload(wait_until="networkidle", timeout=15_000)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"reload after build failed: {exc}")
            _safe_screenshot(page, artifact_dir, "08-after-reload")
            final_state = _state(ctx.web_url) or last_state

            # ---------- Step 10: walk inspector tabs ----------
            _log("Step 10: walk inspector tabs")
            try:
                if terminal_provider_infra_reason:
                    failures.note(
                        "final evidence controls skipped because provider infrastructure "
                        "failed before product build"
                    )
                    raise StopIteration
                if terminal_outcome is None:
                    failures.note("skipping final evidence controls because the build never reached terminal")
                    raise StopIteration
                if submitted_run_id:
                    _mc_open_run_by_id(page, submitted_run_id, failures=failures, log_fn=_log)
                elif submitted_task_id:
                    task_card = page.locator(f'[data-queue-task-id="{submitted_task_id}"] .task-card-main').first
                    if task_card.count() > 0 and task_card.is_enabled():
                        try:
                            task_card.click(timeout=5_000)
                        except Exception as exc:  # noqa: BLE001
                            failures.fail(f"task-card click failed for submitted task {submitted_task_id}: {exc}")
                        time.sleep(1)
                    else:
                        failures.fail(f"could not find visible task card for submitted task {submitted_task_id}")
                # Walk evidence controls. A successful build must expose real
                # evidence surfaces; screenshots alone are not a user oracle.
                for tab_name, kind in [
                    ("Logs", "logs"),
                    ("Diff", "diff"),
                    ("Proof", "proof"),
                    ("Artifacts", "artifacts"),
                ]:
                    clicked = _mc_click_first(
                        page,
                        MC_EVIDENCE_BUTTON_SELECTORS[kind],
                        log_fn=_log,
                        timeout_ms=5_000,
                    )
                    if not clicked:
                        failures.fail(f"evidence control {tab_name} missing or disabled after terminal state")
                        continue
                    time.sleep(1)
                    _safe_screenshot(page, artifact_dir, f"09-tab-{tab_name.lower()}")
                    _mc_assert_evidence_panel(
                        page,
                        ctx,
                        phase=f"final-inspector-{kind}",
                        kind=kind,
                        log_fn=_log,
                        failures=failures,
                        hard=True,
                        clicked_selector=clicked,
                    )
            except StopIteration:
                pass
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"walking inspector tabs raised: {exc}")

            # ---------- Step 11: product verification ----------
            if not submitted_run_id and final_state is not None:
                for row in _state_task_rows(final_state):
                    if submitted_task_id and submitted_task_id in _task_row_keys(row):
                        candidate_run_id = str(row.get("run_id") or "").strip()
                        if candidate_run_id:
                            submitted_run_id = candidate_run_id
                            break
            _verify_generated_product(
                page,
                ctx,
                build_intent=build_intent,
                terminal_outcome=terminal_outcome,
                final_state=final_state,
                submitted_task_id=submitted_task_id,
                submitted_run_id=submitted_run_id,
                log_fn=_log,
                failures=failures,
            )

            # ---------- Step 12: group concurrency evidence ----------
            _log("Step 12: group concurrency evidence")
            if terminal_provider_infra_reason:
                failures.note(
                    "group concurrency evidence skipped because provider infrastructure "
                    "failed during spec compile"
                )
            else:
                _assert_group_concurrency_observed(
                    ctx,
                    run_id=submitted_run_id,
                    queue_task_id=submitted_task_id,
                    log_fn=_log,
                    failures=failures,
                )

            # Final state snapshot
            try:
                if final_state is not None:
                    (artifact_dir / "final-state.json").write_text(
                        json.dumps(final_state, indent=2, default=str), encoding="utf-8"
                    )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"final state snapshot failed: {exc}")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    # Treat console errors as soft failures (user-facing UX bugs)
    errors = _unexpected_console_errors(captured)
    if errors:
        failures.fail(
            f"console errors during W1 ({len(errors)}); see console.json"
        )
    if captured["page_errors"]:
        failures.fail(f"page errors during W1: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = _unexpected_network_errors(captured["network_errors"])
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx responses: {len(unexpected)}; see network-errors.json")

    debug.close()
    return ScenarioRunResult(
        outcome=(
            "PASS"
            if not failures.failures
            else ("INFRA" if terminal_provider_infra_reason else "FAIL")
        ),
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


def _run_w11(ctx: ScenarioContext) -> ScenarioRunResult:
    """W11 — operator day (nightly core, modeled on N9).

    Soft-asserts the entire operator day so we collect every failure per run.
    """
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug = open(ctx.debug_log, "a", encoding="utf-8")

    def _log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S", time.gmtime())
        line = f"[{ts}] [W11] {msg}\n"
        debug.write(line)
        debug.flush()
        print(line, end="", flush=True)

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url", duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    cli_build_proc: Optional[subprocess.Popen[str]] = None
    cli_build_log = artifact_dir / "cli-build.log"
    cli_build_log_handle = cli_build_log.open("w", encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")

        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1+2: open Mission Control ----------
            _log("Step 1+2: page.goto, verify shell")
            try:
                page.goto(ctx.web_url, wait_until="networkidle", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            # ---------- Step 3: standalone CLI build ----------
            _log("Step 3: spawn standalone otto build subprocess")
            if not OTTO_BIN.exists():
                failures.fail(f"otto bin missing at {OTTO_BIN}")
            else:
                env = dict(os.environ)
                env.setdefault("OTTO_ALLOW_REAL_COST", "1")
                argv = [
                    str(OTTO_BIN), "build",
                    "--provider", ctx.provider,
                    "--allow-dirty",
                    W11_BUILD_INTENT,
                ]
                try:
                    cli_build_proc = subprocess.Popen(
                        argv, cwd=ctx.project_dir, env=env,
                        stdout=cli_build_log_handle,
                        stderr=subprocess.STDOUT,
                        text=True,
                        preexec_fn=os.setsid,
                    )
                    _log(f"  cli build pid={cli_build_proc.pid}")
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"failed to spawn cli build: {exc}")

            # ---------- Step 4: web shows the standalone run ----------
            _log("Step 4: poll /api/state for standalone live row")
            standalone_run_id: Optional[str] = None
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                _, body = _api_get(ctx.web_url, "/api/state")
                if isinstance(body, dict):
                    live = (body.get("live") or {}).get("items") or []
                    for item in live:
                        if item.get("domain") == "build":
                            standalone_run_id = item.get("run_id")
                            break
                if standalone_run_id:
                    break
                time.sleep(2)
            failures.soft_assert(
                standalone_run_id is not None,
                "standalone build never appeared in /api/state live runs (60s)",
            )
            _log(f"  standalone_run_id={standalone_run_id}")
            _safe_screenshot(page, artifact_dir, "04-standalone-live")

            # ---------- Step 5: enqueue 2 more jobs from web ----------
            _log("Step 5: enqueue 2 jobs via JobDialog")
            for label, intent in [("post", W11_POST_INTENT), ("delete", W11_DELETE_INTENT)]:
                try:
                    new_job = page.locator(
                        '[data-testid="mission-new-job-button"], [data-testid="new-job-button"]'
                    ).first
                    if new_job.count() == 0:
                        failures.fail(f"new-job button missing (enqueue {label})")
                        continue
                    new_job.click(timeout=5_000)
                    page.wait_for_selector('[data-testid="job-dialog-intent"]', timeout=10_000)
                    page.locator('[data-testid="job-dialog-intent"]').fill(intent, timeout=5_000)
                    page.locator('[data-testid="job-dialog-submit-button"]').click(timeout=5_000)
                    # Wait for dialog to close
                    try:
                        page.wait_for_selector(
                            '[data-testid="job-dialog-intent"]', state="detached", timeout=10_000
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    _safe_screenshot(page, artifact_dir, f"05-enqueued-{label}")
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"enqueue {label} failed: {exc}")
                time.sleep(1.5)

            # ---------- Step 6: start watcher from web ----------
            _log("Step 6: start watcher")
            _click_start_watcher(page, failures=failures, label="W11", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "06-watcher")

            # ---------- Step 7: heartbeat / events streaming ----------
            _log("Step 7: confirm event stream advances")
            t0_status, t0_body = _api_get(ctx.web_url, "/api/events?limit=20")
            time.sleep(8)
            t1_status, t1_body = _api_get(ctx.web_url, "/api/events?limit=20")
            advanced = False
            try:
                if isinstance(t0_body, dict) and isinstance(t1_body, dict):
                    e0 = t0_body.get("events") or []
                    e1 = t1_body.get("events") or []
                    advanced = len(e1) > len(e0) or (
                        e0 and e1 and e0[-1] != e1[-1]
                    )
            except Exception:  # noqa: BLE001
                pass
            failures.soft_assert(advanced, "event stream did not advance in 8s of poll window")

            # ---------- Step 8: cancel one queue task via UI ----------
            # We rely on the queue's task IDs surfacing in /api/state.
            _log("Step 8: cancel one queue task through visible UI")
            cancelled_task_id: Optional[str] = None
            cancelled_run_id: Optional[str] = None
            _, state_body = _api_get(ctx.web_url, "/api/state")
            queue_items = []
            if isinstance(state_body, dict):
                live = (state_body.get("live") or {}).get("items") or []
                queue_items = [item for item in live if item.get("domain") == "queue"]
            if not queue_items:
                failures.fail("no queue live items found to cancel")
            else:
                # Pick the second one if available (first might already be running)
                victim = queue_items[-1]
                cancelled_run_id = victim.get("run_id")
                cancelled_task_id = victim.get("queue_task_id")
                _log(f"  cancelling task={cancelled_task_id} run={cancelled_run_id}")
                _mc_click_run_action_from_ui(
                    page,
                    cancelled_run_id,
                    "cancel",
                    failures=failures,
                    log_fn=_log,
                )

            # ---------- Step 9-11: wait for jobs ----------
            _log("Step 9-11: wait for queue/standalone to settle (≤25min total)")
            deadline = time.monotonic() + W11_BUILD_TIMEOUT_S
            while time.monotonic() < deadline:
                _, body = _api_get(ctx.web_url, "/api/state")
                if not isinstance(body, dict):
                    time.sleep(5)
                    continue
                live = (body.get("live") or {}).get("items") or []
                history = (body.get("history") or {}).get("items") or []
                # Are all queue records terminal?
                non_terminal = [
                    item for item in live
                    if item.get("status")
                    not in ("done", "failed", "cancelled", "interrupted", "removed")
                ]
                # Standalone build done?
                standalone_done = (
                    cli_build_proc is None or cli_build_proc.poll() is not None
                )
                _log(
                    f"  live_non_terminal={len(non_terminal)} history={len(history)} "
                    f"standalone_done={standalone_done}"
                )
                if not non_terminal and standalone_done:
                    break
                time.sleep(15)
            _safe_screenshot(page, artifact_dir, "10-after-settle")

            # ---------- Step 12: merge succeeded queue row from web ----------
            _log("Step 12: try merge for a succeeded queue row")
            _, body = _api_get(ctx.web_url, "/api/state")
            merge_target_run_id: Optional[str] = None
            if isinstance(body, dict):
                history = (body.get("history") or {}).get("items") or []
                for item in history:
                    if (
                        item.get("domain") == "queue"
                        and item.get("terminal_outcome") == "success"
                    ):
                        merge_target_run_id = item.get("run_id")
                        break
            if merge_target_run_id is None:
                failures.fail("no succeeded queue row found to merge")
            else:
                _log(f"  merging run_id={merge_target_run_id}")
                merge_clicked = _mc_click_run_action_from_ui(
                    page,
                    merge_target_run_id,
                    "merge",
                    failures=failures,
                    log_fn=_log,
                )
                # Wait briefly for merge live record
                if merge_clicked:
                    deadline = time.monotonic() + 60
                    saw_merge_history = False
                    while time.monotonic() < deadline:
                        _, body = _api_get(ctx.web_url, "/api/state")
                        if isinstance(body, dict):
                            history = (body.get("history") or {}).get("items") or []
                            for item in history:
                                if item.get("domain") == "merge" and item.get(
                                    "terminal_outcome"
                                ) == "success":
                                    saw_merge_history = True
                                    break
                        if saw_merge_history:
                            break
                        time.sleep(3)
                    failures.soft_assert(
                        saw_merge_history,
                        "merge action accepted but no merge history row appeared",
                    )

            # ---------- Step 13: post-merge git log check ----------
            _log("Step 13: git log on project")
            try:
                git_log = subprocess.run(
                    ["git", "log", "--oneline", "-n", "10"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=10,
                )
                (artifact_dir / "git-log.txt").write_text(
                    git_log.stdout, encoding="utf-8"
                )
                # Look for any merge-y / branch-y commits beyond initial
                merge_in_log = "Merge" in git_log.stdout or len(
                    [ln for ln in git_log.stdout.splitlines() if ln.strip()]
                ) > 1
                failures.soft_assert(
                    merge_in_log,
                    "git log shows no commits beyond initial — merge did not land",
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"git log failed: {exc}")

            # ---------- Step 14: final state has merged record ----------
            _, final_body = _api_get(ctx.web_url, "/api/state")
            if isinstance(final_body, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(final_body, indent=2, default=str), encoding="utf-8"
                )

            # ---------- Step 15: orphan checks ----------
            _log("Step 15: orphan process / worktree check")
            try:
                wt = subprocess.run(
                    ["git", "worktree", "list"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=5,
                )
                (artifact_dir / "worktrees.txt").write_text(
                    wt.stdout, encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"worktree list failed: {exc}")

            # ---------- Step 16: artifact-mine handled by harness wrapper ----------
            _log("Step 16: scenario complete; artifact_mine_pass runs in finally")
            _safe_screenshot(page, artifact_dir, "11-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    # Cleanup standalone cli build
    _terminate_process_group(cli_build_proc)
    cli_build_log_handle.close()

    # Console error gating
    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W11; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W11: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(
                f"unexpected 4xx/5xx during W11: {len(unexpected)}; see network-errors.json"
            )

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# Shared web-as-user helpers (used by W2/W12a/W12b/W13)
# ---------------------------------------------------------------------------


def _open_logger(debug_log: Path, scenario_id: str):
    """Return a (file_handle, log_fn) pair that prefixes messages with [scenario_id]."""
    debug = open(debug_log, "a", encoding="utf-8")

    def _log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S", time.gmtime())
        line = f"[{ts}] [{scenario_id}] {msg}\n"
        debug.write(line)
        debug.flush()
        print(line, end="", flush=True)

    return debug, _log


def _enqueue_via_dialog(
    page: Any,
    intent: str,
    *,
    failures: RunFailures,
    label: str,
    artifact_dir: Path,
    screenshot_idx: int,
) -> bool:
    """Open JobDialog, fill intent, submit. Returns True if submit click sent.

    Captures screenshot before/after; logs failures via soft_assert.
    """
    try:
        new_job = page.locator(
            '[data-testid="mission-new-job-button"], [data-testid="new-job-button"]'
        ).first
        if new_job.count() == 0:
            failures.fail(f"new-job button missing (enqueue {label})")
            return False
        new_job.click(timeout=5_000)
        page.wait_for_selector('[data-testid="job-dialog-intent"]', timeout=10_000)
        page.locator('[data-testid="job-dialog-intent"]').fill(intent, timeout=5_000)
        _safe_screenshot(page, artifact_dir, f"{screenshot_idx:02d}-dialog-{label}")
        page.locator('[data-testid="job-dialog-submit-button"]').click(timeout=5_000)
        try:
            page.wait_for_selector(
                '[data-testid="job-dialog-intent"]', state="detached", timeout=10_000
            )
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"enqueue {label} failed: {exc}")
        return False


def _click_start_watcher(
    page: Any,
    *,
    failures: RunFailures,
    label: str = "watcher",
    base_url: Optional[str] = None,
) -> None:
    if _web_watcher_running(base_url):
        return
    btn = page.locator(
        '[data-testid="mission-start-watcher-button"], '
        '[data-testid="start-watcher-button"], '
        '[data-testid="task-board-start-queue-runner"], '
        '[data-testid="run-detail-queued-start-watcher"]'
    ).first
    if btn.count() == 0:
        if _web_watcher_running(base_url):
            return
        failures.fail(f"start-watcher button missing ({label})")
        return
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if btn.is_enabled():
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    try:
        btn.click(timeout=5_000)
    except Exception as exc:  # noqa: BLE001
        if _web_watcher_running(base_url):
            return
        failures.fail(f"start watcher click failed ({label}): {exc}")


def _state(base_url: str) -> Optional[dict[str, Any]]:
    _, body = _api_get(base_url, "/api/state")
    return body if isinstance(body, dict) else None


def _queued_work_count(state: Optional[dict[str, Any]]) -> int:
    if not isinstance(state, dict):
        return 0
    watcher_counts = (state.get("watcher") or {}).get("counts") or {}
    queued_from_counts = sum(
        int(watcher_counts.get(key) or 0)
        for key in ("queued", "starting", "initializing", "running")
    )
    runtime = state.get("runtime") or {}
    backlog = (runtime.get("command_backlog") or {}).get("pending") or 0
    live_count = sum(
        1
        for item in _live_items(state)
        if _row_is_active(item)
    )
    landing_count = sum(
        1
        for item in (state.get("landing") or {}).get("items") or []
        if _row_is_active(item)
    )
    return max(
        queued_from_counts,
        int(runtime.get("queue_tasks") or 0),
        int(runtime.get("state_tasks") or 0),
        int(backlog or 0),
        live_count,
        landing_count,
    )


def _terminal_outcome_from_row(row: dict[str, Any]) -> str | None:
    outcome = str(row.get("terminal_outcome") or "").strip()
    if outcome:
        return outcome
    status = ""
    for key in ("status", "queue_status", "display_status", "state"):
        status = str(row.get(key) or "").strip().lower()
        if status:
            break
    if status in {"done", "success", "passed", "landed"}:
        return "success"
    if status in {"failed", "failure", "error", "blocked", "partial"}:
        return "failure"
    if status in {"cancelled", "canceled", "interrupted", "removed", "aborted"}:
        return status
    return None


def _row_is_active(row: dict[str, Any]) -> bool:
    if _terminal_outcome_from_row(row):
        return False
    status = ""
    for key in ("status", "queue_status", "display_status", "state"):
        status = str(row.get(key) or "").strip().lower()
        if status:
            break
    return status in {
        "queued",
        "pending",
        "waiting",
        "starting",
        "initializing",
        "running",
        "terminating",
    }


def _state_task_rows(state: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    rows: list[dict[str, Any]] = []
    for source, items in (
        ("live", (state.get("live") or {}).get("items") or []),
        ("history", (state.get("history") or {}).get("items") or []),
        ("landing", (state.get("landing") or {}).get("items") or []),
    ):
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["_source"] = source
            if source == "landing":
                row.setdefault("queue_task_id", row.get("task_id"))
            rows.append(row)
    return rows


def _task_row_keys(row: dict[str, Any]) -> set[str]:
    return {
        str(value).strip()
        for value in (
            row.get("queue_task_id"),
            row.get("task_id"),
            row.get("run_id"),
        )
        if str(value or "").strip()
    }


def _state_task_keys(state: Optional[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in _state_task_rows(state):
        keys.update(_task_row_keys(row))
    return keys


def _submitted_row_matches(
    row: dict[str, Any],
    *,
    submitted_task_id: str | None,
    submitted_run_id: str | None,
) -> bool:
    keys = _task_row_keys(row)
    if submitted_task_id and submitted_task_id in keys:
        return True
    if submitted_run_id and submitted_run_id in keys:
        return True
    return not submitted_task_id and not submitted_run_id


def _terminal_outcome_for_submitted_task(
    state: Optional[dict[str, Any]],
    *,
    submitted_task_id: str | None,
    submitted_run_id: str | None,
) -> tuple[str | None, str | None]:
    """Return a submitted run's terminal outcome from any Mission Control row.

    Mission Control can retain a completed queue run in the live section as
    ``status=done`` while also listing it in history. The true-web harness must
    stop when the user-visible run is terminal, regardless of which section
    currently contains the row.
    """
    for row in _state_task_rows(state):
        if not _submitted_row_matches(
            row,
            submitted_task_id=submitted_task_id,
            submitted_run_id=submitted_run_id,
        ):
            continue
        outcome = _terminal_outcome_from_row(row)
        if not outcome:
            continue
        run_id = str(row.get("run_id") or submitted_run_id or "").strip() or submitted_run_id
        return outcome, run_id
    return None, submitted_run_id


def _submitted_task_progress_signature(
    state: Optional[dict[str, Any]],
    *,
    submitted_task_id: str | None,
    submitted_run_id: str | None,
) -> str | None:
    for row in _state_task_rows(state):
        if not _submitted_row_matches(
            row,
            submitted_task_id=submitted_task_id,
            submitted_run_id=submitted_run_id,
        ):
            continue
        payload = {
            key: row.get(key)
            for key in (
                "_source",
                "status",
                "display_status",
                "terminal_outcome",
                "run_id",
                "queue_task_id",
                "task_id",
                "updated_at",
                "heartbeat_at",
                "finished_at",
                "elapsed_s",
                "duration_s",
                "progress",
                "last_event",
                "stories_passed",
                "stories_tested",
                "changed_file_count",
                "cost_usd",
            )
        }
        payload["token_usage"] = row.get("token_usage") or {}
        return json.dumps(payload, sort_keys=True, default=str)
    return None


def _row_meta_summary(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "_source",
        "domain",
        "status",
        "display_status",
        "terminal_outcome",
        "run_id",
        "queue_task_id",
        "task_id",
        "intent",
        "updated_at",
        "heartbeat_at",
        "finished_at",
        "elapsed_s",
        "duration_s",
        "progress",
        "last_event",
        "stories_passed",
        "stories_tested",
        "changed_file_count",
        "cost_usd",
    )
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "", [])}


def _state_meta_summary(
    state: Optional[dict[str, Any]],
    *,
    submitted_task_id: str | None,
    submitted_run_id: str | None,
) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {"available": False}
    rows = _state_task_rows(state)
    matched = [
        _row_meta_summary(row)
        for row in rows
        if _submitted_row_matches(
            row,
            submitted_task_id=submitted_task_id,
            submitted_run_id=submitted_run_id,
        )
    ]
    return {
        "available": True,
        "watcher": state.get("watcher"),
        "runtime": state.get("runtime"),
        "counts": {
            source: len((state.get(source) or {}).get("items") or [])
            for source in ("live", "history", "landing")
        },
        "submitted_rows": matched[:8],
        "visible_rows_sample": [_row_meta_summary(row) for row in rows[:8]],
    }


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _tail_jsonl(path: Path, *, max_lines: int = 40, max_bytes: int = 128_000) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()[-max_lines:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"raw": line[:1000]}
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _meta_debug_hints(packet: dict[str, Any]) -> list[str]:
    text = json.dumps(packet, default=str).lower()
    hints: list[str] = []
    if "merge wall budget exhausted" in text:
        hints.append("merge wall budget exhausted before a slice landed; inspect merge repair attempts and whether the agent was fixing the integrated state")
    if "package.json" in text and ("enoent" in text or "absent" in text or "missing" in text):
        hints.append("integrated project root is missing package.json; likely no app scaffold slice landed before audit")
    if "eperm" in text and ("5173" in text or "listen" in text or "bind" in text):
        hints.append("provider sandbox could not bind a local dev server; rely on Otto check-runner artifacts and avoid repeated browser-launch probing")
    if "strict mode violation" in text:
        hints.append("browser journey is failing on ambiguous accessible locators; repair should scope locators or UI names to user-visible containers")
    if "horizontal overflow" in text:
        hints.append("browser journey found responsive layout overflow; inspect saved screenshot/error context and widest DOM elements")
    if not hints:
        hints.append("inspect submitted_rows, recent spec events, and named check logs before waiting further")
    return hints


def _compact_meta_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "<max-depth>"
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:2000] + "...<truncated>"
    if isinstance(value, dict):
        return {
            str(key): _compact_meta_value(item, depth=depth + 1)
            for key, item in list(value.items())[:80]
        }
    if isinstance(value, list):
        return [_compact_meta_value(item, depth=depth + 1) for item in value[:20]]
    return value


def _write_meta_debug_packet(
    ctx: ScenarioContext,
    *,
    reason: str,
    state: Optional[dict[str, Any]],
    submitted_task_id: str | None,
    submitted_run_id: str | None,
    poll_count: int,
    progress_signature: str | None,
    terminal_outcome: str | None,
    log_fn: Callable[[str], None] | None = None,
) -> Path | None:
    """Write bounded diagnosis for the monitor; never bulk-read transcripts."""
    session_dir = _find_session_dir_for_run(
        ctx,
        submitted_run_id,
        queue_task_id=submitted_task_id,
    )
    packet: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenario": ctx.scenario.id,
        "reason": reason,
        "poll_count": poll_count,
        "submitted_task_id": submitted_task_id,
        "submitted_run_id": submitted_run_id,
        "terminal_outcome": terminal_outcome,
        "progress_signature": progress_signature,
        "state": _state_meta_summary(
            state,
            submitted_task_id=submitted_task_id,
            submitted_run_id=submitted_run_id,
        ),
        "session_dir": str(session_dir) if session_dir else None,
        "project_processes": _project_processes(ctx.project_dir)[:20],
        "notes": [
            "bounded meta-debug packet for the monitoring session",
            "raw messages.jsonl transcripts are intentionally not read here",
        ],
    }
    if session_dir is not None:
        summary = _read_json_dict(session_dir / "summary.json")
        if summary is not None:
            packet["summary"] = _compact_meta_value(summary)
        evidence = _read_json_dict(session_dir / "audit" / "attempt-00" / "evidence-packet.json")
        if evidence is not None:
            packet["audit_evidence"] = _compact_meta_value({
                key: evidence.get(key)
                for key in (
                    "build_summary",
                    "merge_summary",
                    "contract_test",
                    "cross_slice_evidence",
                    "verdict",
                    "summary",
                )
                if key in evidence
            })
        recent_events = _tail_jsonl(session_dir / "spec-state.jsonl", max_lines=50)
        if recent_events:
            packet["recent_spec_events"] = _compact_meta_value(recent_events)
    group_report = _read_json_dict(ctx.artifact_dir / "group-concurrency.json")
    if group_report is not None:
        packet["group_concurrency"] = group_report
    packet["hints"] = _meta_debug_hints(packet)

    path = ctx.artifact_dir / f"meta-debug-{_mc_slug(reason)}.json"
    try:
        ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(packet, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        if log_fn is not None:
            log_fn(f"meta-debug packet write failed: {exc}")
        return None
    _record_mc_user_action(
        ctx,
        phase="meta-debug",
        action=reason,
        packet=path.name,
        hints=packet["hints"],
    )
    if log_fn is not None:
        log_fn(f"meta-debug packet: {path.name}")
    return path


def _wait_for_submitted_task(
    base_url: str,
    *,
    before_keys: set[str],
    timeout_s: float,
    expected_task_id: str | None = None,
    expected_intent: str | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    intent_needle = " ".join(str(expected_intent or "").lower().split())[:120]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = _state(base_url)
        for row in _state_task_rows(state):
            keys = _task_row_keys(row)
            if expected_task_id and expected_task_id not in keys:
                continue
            if not expected_task_id and not keys.difference(before_keys):
                continue
            if intent_needle:
                haystack = " ".join(
                    str(value or "")
                    for value in (
                        row.get("summary"),
                        row.get("intent"),
                        row.get("display_name"),
                        row.get("row_label"),
                    )
                )
                haystack = " ".join(haystack.lower().split())
                if intent_needle not in haystack:
                    continue
            if log_fn is not None:
                label = row.get("queue_task_id") or row.get("task_id") or row.get("run_id")
                log_fn(f"submitted task visible after submit: {label} source={row.get('_source')}")
            return row
        time.sleep(0.5)
    if log_fn is not None:
        if expected_task_id:
            log_fn(f"submitted task {expected_task_id!r} not visible within {timeout_s:.0f}s")
        else:
            log_fn(f"no new task row became visible within {timeout_s:.0f}s")
    return None


def _wait_for_job_queued(
    base_url: str,
    *,
    timeout_s: float,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout_s
    last_count = 0
    while time.monotonic() < deadline:
        state = _state(base_url)
        last_count = _queued_work_count(state)
        if last_count > 0:
            if log_fn is not None:
                log_fn(f"queued work visible after submit: count={last_count}")
            return True
        time.sleep(0.5)
    if log_fn is not None:
        log_fn(f"queued work not visible within {timeout_s:.0f}s (last_count={last_count})")
    return False


def _live_items(state: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    return (state.get("live") or {}).get("items") or []


def _history_items(state: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    return (state.get("history") or {}).get("items") or []


def _spawn_otto_cli(
    project_dir: Path,
    argv: list[str],
    *,
    log_path: Path,
    extra_env: Optional[dict[str, str]] = None,
) -> Optional[subprocess.Popen[str]]:
    """Spawn an `otto` CLI subprocess in its own process group; tee output to log_path."""
    if not OTTO_BIN.exists():
        return None
    env = dict(os.environ)
    env.setdefault("OTTO_ALLOW_REAL_COST", "1")
    if extra_env:
        env.update(extra_env)
    log_handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(OTTO_BIN), *argv],
        cwd=project_dir,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        preexec_fn=os.setsid,
    )
    # We deliberately leak the file handle until cleanup; it'll be closed by
    # the OS or by the harness's _terminate_process_group caller path.
    return proc


# ---------------------------------------------------------------------------
# W2 — Multi-job operator
# ---------------------------------------------------------------------------


def _run_w2(ctx: ScenarioContext) -> ScenarioRunResult:
    """W2 — submit 3 jobs via dialog, watcher, drain, cancel one mid-run."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W2")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: open MC ----------
            _log("Step 1: page.goto + verify shell")
            try:
                page.goto(ctx.web_url, wait_until="networkidle", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            # ---------- Step 2: enqueue 3 builds ----------
            _log("Step 2: enqueue 3 build jobs")
            enqueued = 0
            for idx, (lab, intent) in enumerate(
                [("a", W2_INTENT_A), ("b", W2_INTENT_B), ("c", W2_INTENT_C)],
                start=1,
            ):
                if _enqueue_via_dialog(
                    page,
                    intent,
                    failures=failures,
                    label=lab,
                    artifact_dir=artifact_dir,
                    screenshot_idx=1 + idx,
                ):
                    enqueued += 1
                time.sleep(1.5)
            _log(f"  enqueued={enqueued}/3")
            failures.soft_assert(
                enqueued == 3,
                f"expected to enqueue 3 jobs, got {enqueued}",
            )

            # ---------- Step 3: verify queue reflects backlog ----------
            _log("Step 3: poll /api/state for 3 queue rows")
            queue_count = 0
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                queue_count = sum(
                    1 for it in items if it.get("domain") == "queue"
                )
                if queue_count >= 3:
                    break
                time.sleep(2)
            failures.soft_assert(
                queue_count >= 3,
                f"expected ≥3 queue rows in /api/state.live; got {queue_count}",
            )
            _safe_screenshot(page, artifact_dir, "05-queue-3rows")

            # ---------- Step 4: start watcher ----------
            _log("Step 4: start watcher")
            _click_start_watcher(page, failures=failures, label="W2", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "06-watcher")

            # ---------- Step 5: wait for one to actually start, then cancel another ----------
            _log("Step 5: wait for one running, cancel a queued one")
            running_run_id: Optional[str] = None
            cancelled_run_id: Optional[str] = None
            deadline = time.monotonic() + 5 * 60  # up to 5 min for first run to spin up
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                queue_items = [it for it in items if it.get("domain") == "queue"]
                running = [it for it in queue_items if it.get("status") == "running"]
                queued = [
                    it for it in queue_items
                    if it.get("status") in ("queued", "pending", "ready")
                ]
                if running and queued and running_run_id is None:
                    running_run_id = running[0].get("run_id")
                    cancelled_run_id = queued[-1].get("run_id")
                    break
                time.sleep(5)

            if cancelled_run_id is None:
                # Maybe all 3 already running concurrently (concurrent watcher),
                # or none have started. Try: cancel last queue item regardless.
                state = _state(ctx.web_url)
                items = _live_items(state)
                queue_items = [it for it in items if it.get("domain") == "queue"]
                if queue_items:
                    victim = queue_items[-1]
                    cancelled_run_id = victim.get("run_id")
                    _log(f"  fallback: cancelling last queue row {cancelled_run_id}")
                else:
                    failures.fail("no queue items to cancel")
            else:
                _log(f"  running_run_id={running_run_id} cancelling={cancelled_run_id}")

            if cancelled_run_id is not None:
                cancelled = _mc_click_run_action_from_ui(
                    page,
                    cancelled_run_id,
                    "cancel",
                    failures=failures,
                    log_fn=_log,
                )
                failures.soft_assert(cancelled, f"could not cancel run {cancelled_run_id} through visible UI")
            _safe_screenshot(page, artifact_dir, "07-after-cancel")

            # ---------- Step 6: wait for queue to drain ----------
            _log(f"Step 6: wait ≤{W2_BUILD_TIMEOUT_S}s for queue drain")
            deadline = time.monotonic() + W2_BUILD_TIMEOUT_S
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                non_terminal = [
                    it for it in items
                    if it.get("domain") == "queue"
                    and it.get("status")
                    not in ("done", "failed", "cancelled", "interrupted", "removed")
                ]
                history = _history_items(state)
                if not non_terminal:
                    _log(f"  drained (history rows={len(history)})")
                    break
                _log(
                    f"  non_terminal={len(non_terminal)} statuses="
                    f"{[it.get('status') for it in non_terminal]}"
                )
                time.sleep(15)
            else:
                failures.fail(
                    f"queue did not drain in {W2_BUILD_TIMEOUT_S}s; some jobs still non-terminal"
                )
            _safe_screenshot(page, artifact_dir, "08-drained")

            # ---------- Step 7: open history, verify all 3 appeared ----------
            _log("Step 7: verify history reflects all 3 jobs")
            try:
                history_btn = page.locator(
                    '[data-testid="open-history-button"], [data-testid="mission-history-button"]'
                ).first
                if history_btn.count() > 0 and history_btn.is_enabled():
                    history_btn.click(timeout=5_000)
                    time.sleep(1)
                    _safe_screenshot(page, artifact_dir, "09-history-tab")
            except Exception as exc:  # noqa: BLE001
                failures.note(f"history tab interaction: {exc}")

            state = _state(ctx.web_url)
            history = _history_items(state)
            queue_history = [
                it for it in history if it.get("domain") == "queue"
            ]
            _log(f"  history queue rows={len(queue_history)}")
            failures.soft_assert(
                len(queue_history) >= 3,
                f"expected ≥3 queue history rows, got {len(queue_history)}",
            )
            outcomes = [it.get("terminal_outcome") for it in queue_history[:3]]
            _log(f"  outcomes={outcomes}")
            cancelled_outcomes = [
                it for it in queue_history
                if it.get("terminal_outcome") in ("cancelled", "interrupted")
                or it.get("status") == "cancelled"
            ]
            failures.soft_assert(
                len(cancelled_outcomes) >= 1,
                "expected at least one cancelled outcome in history",
            )

            # final state snapshot
            state = _state(ctx.web_url)
            if isinstance(state, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(state, indent=2, default=str), encoding="utf-8"
                )

            # ---------- Step 8: subprocess inspection ----------
            _log("Step 8: ps inspection for orphans")
            try:
                ps = subprocess.run(
                    ["ps", "-o", "pid,command", "-A"],
                    capture_output=True, text=True, timeout=5,
                )
                # look for any otto processes referencing our project_dir
                relevant = [
                    line for line in ps.stdout.splitlines()
                    if str(ctx.project_dir) in line
                ]
                (artifact_dir / "ps-snapshot.txt").write_text(
                    "\n".join(relevant) or "(none)", encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"ps snapshot failed: {exc}")

            _safe_screenshot(page, artifact_dir, "10-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W2; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W2: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(
                f"unexpected 4xx/5xx during W2: {len(unexpected)}"
            )

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W12a — CLI atomic run → web inspect → cancel from UI
# ---------------------------------------------------------------------------


def _run_w12a(ctx: ScenarioContext) -> ScenarioRunResult:
    """W12a — CLI `otto build` (atomic), inspect from web, cancel via UI."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W12a")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    cli_proc: Optional[subprocess.Popen[str]] = None
    cli_log = artifact_dir / "cli-build.log"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: spawn CLI build ----------
            _log("Step 1: spawn `otto build` in project_dir")
            cli_proc = _spawn_otto_cli(
                ctx.project_dir,
                ["build", "--provider", ctx.provider, "--allow-dirty", W12A_INTENT],
                log_path=cli_log,
            )
            if cli_proc is None:
                failures.fail(f"otto bin missing at {OTTO_BIN}")
            else:
                _log(f"  cli pid={cli_proc.pid}")

            # ---------- Step 2: open MC ----------
            _log("Step 2: page.goto + verify shell")
            try:
                page.goto(ctx.web_url, wait_until="networkidle", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            # ---------- Step 3: poll for atomic-domain live row ----------
            _log("Step 3: poll for atomic-domain live row (≤90s)")
            atomic_run_id: Optional[str] = None
            atomic_domain_seen: Optional[str] = None
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                # W11-IMPORTANT-2 expected "atomic". Capture whatever we see.
                for it in items:
                    if it.get("domain") in ("atomic", "build"):
                        atomic_run_id = it.get("run_id")
                        atomic_domain_seen = it.get("domain")
                        break
                if atomic_run_id:
                    break
                time.sleep(2)

            failures.soft_assert(
                atomic_run_id is not None,
                "CLI atomic run never appeared in /api/state.live within 90s",
            )
            _log(f"  atomic_run_id={atomic_run_id} domain={atomic_domain_seen}")
            failures.soft_assert(
                atomic_domain_seen == "atomic",
                f"expected domain=='atomic' for CLI build, got {atomic_domain_seen!r} "
                "(see W11-IMPORTANT-2 — public naming consistency)",
            )
            _safe_screenshot(page, artifact_dir, "02-atomic-live")

            # Wait a few seconds for the run to actually be doing something
            time.sleep(8)
            _safe_screenshot(page, artifact_dir, "03-atomic-running")

            # ---------- Step 4: open inspector for the atomic row ----------
            _log("Step 4: click task card / inspect")
            try:
                # Try clicking the visible task card
                cards = page.locator(".task-card-main")
                if cards.count() > 0:
                    cards.first.click(timeout=5_000)
                    time.sleep(1)
                _safe_screenshot(page, artifact_dir, "04-inspector")
            except Exception as exc:  # noqa: BLE001
                failures.note(f"task-card click failed: {exc}")

            # ---------- Step 5: cancel from UI ----------
            _log("Step 5: cancel from visible UI")
            if atomic_run_id is not None:
                cancelled = _mc_click_run_action_from_ui(
                    page,
                    atomic_run_id,
                    "cancel",
                    failures=failures,
                    log_fn=_log,
                )
                failures.soft_assert(cancelled, f"could not cancel atomic run {atomic_run_id} through visible UI")
            _safe_screenshot(page, artifact_dir, "05-after-cancel")

            # ---------- Step 6: verify subprocess dies ----------
            _log("Step 6: verify CLI subprocess dies within 60s")
            died = False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if cli_proc is None or cli_proc.poll() is not None:
                    died = True
                    break
                time.sleep(2)
            failures.soft_assert(
                died,
                f"CLI subprocess pid={cli_proc.pid if cli_proc else '?'} did NOT die after UI cancel within 60s",
            )
            if cli_proc is not None and cli_proc.poll() is not None:
                _log(f"  cli exit code={cli_proc.poll()}")

            # ---------- Step 7: history reflects cancelled atomic ----------
            _log("Step 7: history reflects cancellation")
            deadline = time.monotonic() + 30
            saw_cancelled = False
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                history = _history_items(state)
                for it in history:
                    if it.get("run_id") == atomic_run_id:
                        outcome = it.get("terminal_outcome")
                        status = it.get("status")
                        _log(f"  history row outcome={outcome} status={status}")
                        if (
                            outcome in ("cancelled", "interrupted")
                            or status in ("cancelled", "interrupted")
                        ):
                            saw_cancelled = True
                        break
                if saw_cancelled:
                    break
                time.sleep(3)
            failures.soft_assert(
                saw_cancelled,
                "history did not show cancelled outcome for CLI atomic run within 30s of UI cancel",
            )

            # final snapshot
            state = _state(ctx.web_url)
            if isinstance(state, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(state, indent=2, default=str), encoding="utf-8"
                )
            _safe_screenshot(page, artifact_dir, "06-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    _terminate_process_group(cli_proc)

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W12a; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W12a: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(
                f"unexpected 4xx/5xx during W12a: {len(unexpected)}"
            )

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W12b — CLI-queued task → web → start watcher → run → merge from UI
# ---------------------------------------------------------------------------


def _run_w12b(ctx: ScenarioContext) -> ScenarioRunResult:
    """W12b — `otto queue build ... --as <task>` from terminal, merge from UI."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W12b")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    queue_log = artifact_dir / "cli-queue.log"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: enqueue from CLI ----------
            _log("Step 1: `otto queue build ... --as w12b-task`")
            queue_proc = _spawn_otto_cli(
                ctx.project_dir,
                [
                    "queue", "build", W12B_INTENT,
                    "--as", "w12b-task",
                    "--",
                    "--provider", ctx.provider,
                ],
                log_path=queue_log,
            )
            if queue_proc is None:
                failures.fail(f"otto bin missing at {OTTO_BIN}")
            else:
                # `otto queue build` returns quickly after enqueue; wait briefly.
                try:
                    rc = queue_proc.wait(timeout=30)
                    _log(f"  queue cli exited rc={rc}")
                    failures.soft_assert(
                        rc == 0,
                        f"`otto queue build` returned rc={rc} — see {queue_log}",
                    )
                except subprocess.TimeoutExpired:
                    failures.fail("`otto queue build` did not return within 30s")

            # ---------- Step 2: open MC ----------
            _log("Step 2: page.goto + verify shell")
            try:
                page.goto(ctx.web_url, wait_until="networkidle", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            # ---------- Step 3: queue row appears ----------
            _log("Step 3: poll for queue row in /api/state")
            queue_run_id: Optional[str] = None
            queue_task_id: Optional[str] = None
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                for it in items:
                    if it.get("domain") == "queue":
                        queue_run_id = it.get("run_id")
                        queue_task_id = it.get("queue_task_id")
                        break
                if queue_run_id:
                    break
                time.sleep(2)

            failures.soft_assert(
                queue_run_id is not None,
                "CLI-enqueued task never appeared as queue domain row in /api/state.live within 60s",
            )
            _log(f"  queue_run_id={queue_run_id} task_id={queue_task_id}")
            _safe_screenshot(page, artifact_dir, "02-queue-row")

            # ---------- Step 4: start watcher ----------
            _log("Step 4: start watcher")
            _click_start_watcher(page, failures=failures, label="W12b", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "03-watcher")

            # ---------- Step 5: wait for queue task to complete ----------
            _log(f"Step 5: wait ≤{W12B_BUILD_TIMEOUT_S}s for queue task terminal")
            terminal_outcome: Optional[str] = None
            history_run_id: Optional[str] = None
            deadline = time.monotonic() + W12B_BUILD_TIMEOUT_S
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                history = _history_items(state)
                for it in history:
                    if (
                        it.get("domain") == "queue"
                        and it.get("queue_task_id") == queue_task_id
                    ):
                        terminal_outcome = it.get("terminal_outcome")
                        history_run_id = it.get("run_id")
                        break
                if not history_run_id:
                    # fallback: match by run_id
                    for it in history:
                        if (
                            it.get("domain") == "queue"
                            and it.get("run_id") == queue_run_id
                        ):
                            terminal_outcome = it.get("terminal_outcome")
                            history_run_id = it.get("run_id")
                            break
                if terminal_outcome:
                    break
                time.sleep(15)
            _log(f"  terminal_outcome={terminal_outcome} history_run_id={history_run_id}")
            failures.soft_assert(
                terminal_outcome is not None,
                f"queue task did not reach terminal in {W12B_BUILD_TIMEOUT_S}s",
            )
            failures.soft_assert(
                terminal_outcome == "success",
                f"queue task terminal_outcome={terminal_outcome!r} (expected success)",
            )
            _safe_screenshot(page, artifact_dir, "04-terminal")

            # ---------- Step 6: merge from UI ----------
            _log("Step 6: merge from visible UI")
            merge_run_id = history_run_id or queue_run_id
            merge_clicked = False
            if merge_run_id and terminal_outcome == "success":
                merge_clicked = _mc_click_run_action_from_ui(
                    page,
                    merge_run_id,
                    "merge",
                    failures=failures,
                    log_fn=_log,
                )
                failures.soft_assert(merge_clicked, f"could not merge run {merge_run_id} through visible UI")

            # ---------- Step 7: verify merge live → terminal ----------
            if merge_clicked:
                _log("Step 7: poll for merge history row")
                deadline = time.monotonic() + 90
                saw_merge_history = False
                while time.monotonic() < deadline:
                    state = _state(ctx.web_url)
                    history = _history_items(state)
                    for it in history:
                        if (
                            it.get("domain") == "merge"
                            and it.get("terminal_outcome") == "success"
                        ):
                            saw_merge_history = True
                            break
                    if saw_merge_history:
                        break
                    time.sleep(3)
                failures.soft_assert(
                    saw_merge_history,
                    "merge accepted but no merge history row appeared within 90s",
                )

            # ---------- Step 8: verify branch landed ----------
            _log("Step 8: git log on project main")
            try:
                git_log = subprocess.run(
                    ["git", "log", "--oneline", "-n", "10"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=10,
                )
                (artifact_dir / "git-log.txt").write_text(
                    git_log.stdout, encoding="utf-8"
                )
                non_initial_commits = [
                    ln for ln in git_log.stdout.splitlines()
                    if ln.strip() and "initial" not in ln.lower()
                ]
                failures.soft_assert(
                    len(non_initial_commits) >= 1,
                    "git log shows no commits beyond initial — merge did not land",
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"git log failed: {exc}")

            # ---------- Step 9: archived in history ----------
            state = _state(ctx.web_url)
            if isinstance(state, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(state, indent=2, default=str), encoding="utf-8"
                )
            _safe_screenshot(page, artifact_dir, "05-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W12b; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W12b: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(
                f"unexpected 4xx/5xx during W12b: {len(unexpected)}"
            )

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W13 — Outage recovery
# ---------------------------------------------------------------------------


def _run_w13(ctx: ScenarioContext) -> ScenarioRunResult:
    """W13 — kill `otto web` mid-run, restart, verify recovery.

    Special: this scenario tears down the harness-provided in-process backend
    and restarts a NEW one against the same project_dir. The harness will
    call ``backend.stop()`` again in the outer finally — that's safe (the
    handle's stop is idempotent).
    """
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W13")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"initial web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    # We need to manage two backend instances: ctx-provided and a restart.
    backend_b: Any = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: open MC + start a real build via dialog ----------
            _log("Step 1: page.goto + start a real build via JobDialog")
            try:
                page.goto(ctx.web_url, wait_until="networkidle", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell-pre")

            submitted = _enqueue_via_dialog(
                page, W13_INTENT,
                failures=failures, label="w13-build",
                artifact_dir=artifact_dir, screenshot_idx=2,
            )
            failures.soft_assert(submitted, "could not submit W13 build")
            time.sleep(1.5)
            _click_start_watcher(page, failures=failures, label="W13-pre", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "03-pre-outage")

            # ---------- Step 2: wait until build is actually in-flight ----------
            _log("Step 2: wait for queue/atomic row to be running")
            running_run_id: Optional[str] = None
            deadline = time.monotonic() + 5 * 60
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                running = [
                    it for it in items
                    if it.get("status") == "running"
                ]
                if running:
                    running_run_id = running[0].get("run_id")
                    break
                time.sleep(5)
            failures.soft_assert(
                running_run_id is not None,
                "no running build observed within 5min before outage",
            )
            _log(f"  running_run_id={running_run_id}")

            # Capture pre-outage state for comparison
            pre_state = _state(ctx.web_url)
            (artifact_dir / "pre-outage-state.json").write_text(
                json.dumps(pre_state, indent=2, default=str), encoding="utf-8"
            )

            # ---------- Step 3: kill the backend mid-run ----------
            _log("Step 3: kill in-process backend (simulate outage)")
            outage_at = time.monotonic()
            if ctx.backend is None or not hasattr(ctx.backend, "stop"):
                failures.fail("W13 cannot simulate outage because ScenarioContext.backend is missing")
            else:
                try:
                    ctx.backend.stop()
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"failed to stop in-process backend for outage simulation: {exc}")
            time.sleep(2)  # let the running build accrue some progress

            _log("Step 3b: start backend_b on a new port against same project")
            try:
                backend_b = _start_otto_web_in_process(ctx.project_dir, artifact_dir / "backend_b")
                _log(f"  backend_b url={backend_b.url}")
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"second backend start failed: {exc}")

            # ---------- Step 4: reopen browser to backend_b URL ----------
            if backend_b is not None:
                _log("Step 4: reopen browser to backend_b")
                try:
                    page.goto(backend_b.url, wait_until="networkidle", timeout=30_000)
                    _wait_for_mc_ready(page)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"shell load on backend_b failed: {exc}")
                _safe_screenshot(page, artifact_dir, "04-post-restart")

                # ---------- Step 5: live tab shows the run still in progress (or terminal) ----------
                _log("Step 5: verify run survives across restart")
                state_b = _state(backend_b.url)
                (artifact_dir / "post-restart-state.json").write_text(
                    json.dumps(state_b, indent=2, default=str), encoding="utf-8"
                )
                items_b = _live_items(state_b)
                history_b = _history_items(state_b)
                # Look for our running_run_id in either live or history
                in_live = any(
                    it.get("run_id") == running_run_id for it in items_b
                )
                in_history = any(
                    it.get("run_id") == running_run_id for it in history_b
                )
                failures.soft_assert(
                    in_live or in_history,
                    f"after restart, run {running_run_id!r} not visible in live or history "
                    f"(live={len(items_b)} history={len(history_b)})",
                )

                # ---------- Step 6: click into the run and walk drawers ----------
                _log("Step 6: walk Logs/Diff/Proof drawers post-restart")
                try:
                    cards = page.locator(".task-card-main")
                    if cards.count() > 0:
                        cards.first.click(timeout=5_000)
                        time.sleep(1)
                    for label in ["logs", "diff", "proof"]:
                        clicked = _mc_click_first(
                            page,
                            MC_EVIDENCE_BUTTON_SELECTORS[label],
                            log_fn=_log,
                            timeout_ms=5_000,
                        )
                        if not clicked:
                            failures.fail(f"drawer {label} button missing or disabled post-restart")
                            continue
                        time.sleep(1)
                        _safe_screenshot(page, artifact_dir, f"05-drawer-{label}")
                        _mc_assert_evidence_panel(
                            page,
                            ctx,
                            phase=f"post-restart-{label}",
                            kind=label,
                            log_fn=_log,
                            failures=failures,
                            hard=True,
                            clicked_selector=clicked,
                        )
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"walking drawers post-restart failed: {exc}")

                # ---------- Step 7: wait for the build to settle ----------
                _log(f"Step 7: wait ≤{W13_BUILD_TIMEOUT_S}s for build settle on backend_b")
                deadline = time.monotonic() + W13_BUILD_TIMEOUT_S
                final_outcome: Optional[str] = None
                while time.monotonic() < deadline:
                    state_b = _state(backend_b.url)
                    history_b = _history_items(state_b)
                    for it in history_b:
                        if it.get("run_id") == running_run_id:
                            final_outcome = it.get("terminal_outcome")
                            break
                    if final_outcome:
                        break
                    time.sleep(15)
                _log(f"  final_outcome={final_outcome}")
                failures.soft_assert(
                    final_outcome is not None,
                    f"run {running_run_id!r} never reached terminal post-restart in {W13_BUILD_TIMEOUT_S}s",
                )
                if final_outcome and final_outcome != "success":
                    failures.fail(f"run {running_run_id!r} terminal_outcome={final_outcome!r} post-restart (expected success)")

            # ---------- Step 8: actions not lost (event log preserved) ----------
            url_for_events = (
                backend_b.url if backend_b is not None else ctx.web_url
            )
            _, ev_body = _api_get(url_for_events, "/api/events?limit=200")
            if isinstance(ev_body, dict):
                (artifact_dir / "post-restart-events.json").write_text(
                    json.dumps(ev_body, indent=2, default=str), encoding="utf-8"
                )
                events_count = len(ev_body.get("events") or [])
                _log(f"  post-restart events={events_count}")
                failures.soft_assert(
                    events_count > 0,
                    "no events visible after restart — event log lost across restart",
                )

            outage_duration = time.monotonic() - outage_at
            _log(f"  outage-to-end wall: {outage_duration:.1f}s")
            _safe_screenshot(page, artifact_dir, "06-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
            if backend_b is not None:
                try:
                    backend_b.stop()
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"backend_b.stop raised: {exc}")

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W13; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W13: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(
                f"unexpected 4xx/5xx during W13: {len(unexpected)}"
            )

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# Helpers used by W3-W7
# ---------------------------------------------------------------------------


def _set_dialog_command(page: Any, command: str, *, failures: RunFailures) -> bool:
    """Set the JobDialog command (build|improve|certify) via the select."""
    try:
        sel = page.locator('[data-testid="job-command-select"]')
        if sel.count() == 0:
            failures.fail("job-command-select missing")
            return False
        sel.select_option(value=command, timeout=5_000)
        return True
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"could not set command={command}: {exc}")
        return False


def _set_improve_subcommand(page: Any, sub: str, *, failures: RunFailures) -> bool:
    try:
        sel = page.locator('[data-testid="job-improve-mode-select"]')
        if sel.count() == 0:
            failures.fail("job-improve-mode-select missing (after switching to improve)")
            return False
        sel.select_option(value=sub, timeout=5_000)
        return True
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"could not set improve subcommand={sub}: {exc}")
        return False


def _maybe_confirm_dirty_target(page: Any, *, failures: RunFailures) -> None:
    """Tick the dirty-target confirm if it's visible (W11-CRITICAL-1 workaround)."""
    try:
        cb = page.locator('[data-testid="target-project-confirm"]')
        if cb.count() > 0 and cb.is_visible():
            cb.check(timeout=3_000)
    except Exception as exc:  # noqa: BLE001
        failures.note(f"target-project-confirm tick failed (non-fatal): {exc}")


def _enqueue_via_dialog_full(
    page: Any,
    *,
    intent: str,
    command: str,
    subcommand: Optional[str],
    provider: Optional[str],
    failures: RunFailures,
    label: str,
    artifact_dir: Path,
    screenshot_idx: int,
) -> bool:
    """Open JobDialog, optionally switch command/subcommand, fill intent, submit.

    Returns True on success, False on any failure. Records a single
    diagnostic ``failures.note`` describing the specific reason; the
    caller is expected to wrap this with one ``failures.soft_assert(ok,
    ...)``. (W4-IMPORTANT-1: previously we recorded a hard ``fail``
    here and the caller added a duplicate ``soft_assert`` — surfacing
    the same root cause as two separate findings.)
    """
    try:
        new_job = page.locator(
            '[data-testid="mission-new-job-button"], [data-testid="new-job-button"]'
        ).first
        if new_job.count() == 0:
            failures.note(f"enqueue {label}: new-job button not present")
            return False
        new_job.click(timeout=5_000)
        page.wait_for_selector('[data-testid="job-dialog-intent"]', timeout=10_000)
        if command != "build":
            if not _set_dialog_command(page, command, failures=failures):
                return False
            time.sleep(0.5)
        if command == "improve" and subcommand:
            if not _set_improve_subcommand(page, subcommand, failures=failures):
                return False
        if provider:
            provider_select = page.locator('[data-testid="job-provider-select"]').first
            if provider_select.count() == 0:
                failures.note(f"enqueue {label}: provider selector not present")
                return False
            try:
                provider_select.select_option(value=provider, timeout=5_000)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"enqueue {label}: could not select provider {provider!r} — {exc}")
                return False
        page.locator('[data-testid="job-dialog-intent"]').fill(intent, timeout=5_000)
        _maybe_confirm_dirty_target(page, failures=failures)
        _safe_screenshot(page, artifact_dir, f"{screenshot_idx:02d}-dialog-{label}")
        submit = page.locator('[data-testid="job-dialog-submit-button"]')
        if submit.count() == 0:
            failures.note(f"enqueue {label}: submit button missing")
            return False
        submit.click(timeout=5_000)
        try:
            page.wait_for_selector(
                '[data-testid="job-dialog-intent"]', state="detached", timeout=10_000
            )
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        failures.note(f"enqueue {label}: exception — {exc}")
        return False


def _wait_for_terminal(
    base_url: str,
    *,
    timeout_s: float,
    log_fn: Callable[[str], None],
    domain_filter: Optional[set[str]] = None,
    queue_task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    on_poll: Optional[Callable[[int], None]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Poll /api/state until a matching Mission Control row is terminal.

    Returns (terminal_outcome, run_id) or (None, None) on timeout.
    """
    deadline = time.monotonic() + timeout_s
    poll = 0
    while time.monotonic() < deadline:
        poll += 1
        state = _state(base_url)
        rows = _state_task_rows(state)
        for it in rows:
            if domain_filter and it.get("domain") not in domain_filter:
                continue
            if queue_task_id and it.get("queue_task_id") != queue_task_id:
                continue
            if run_id and it.get("run_id") != run_id:
                continue
            outcome = _terminal_outcome_from_row(it)
            if outcome:
                return outcome, it.get("run_id")
        if poll % 12 == 1:
            live = _live_items(state)
            history = _history_items(state)
            log_fn(
                f"  poll#{poll}: history={len(history)} live={len(live)} "
                f"live_statuses={[it.get('status') for it in live[:3]]}"
            )
            if on_poll:
                on_poll(poll)
        time.sleep(5)
    return None, None


# ---------------------------------------------------------------------------
# W3 — Iterative improve loop
# ---------------------------------------------------------------------------


def _run_w3(ctx: ScenarioContext) -> ScenarioRunResult:
    """W3 — submit a build, then submit Improve referencing the prior run."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W3")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: open MC ----------
            _log("Step 1: open MC")
            try:
                page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            # ---------- Step 2: enqueue prior build ----------
            _log("Step 2: enqueue prior build via JobDialog")
            ok = _enqueue_via_dialog_full(
                page,
                intent=W3_BUILD_INTENT,
                command="build",
                subcommand=None,
                provider=ctx.provider,
                failures=failures,
                label="prior-build",
                artifact_dir=artifact_dir,
                screenshot_idx=2,
            )
            failures.soft_assert(ok, "could not enqueue prior build")
            time.sleep(1.5)

            # ---------- Step 3: start watcher, wait for build terminal ----------
            _log("Step 3: start watcher; wait for prior-build terminal")
            _click_start_watcher(page, failures=failures, label="W3-build", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "03-watcher-build")

            outcome, build_run_id = _wait_for_terminal(
                ctx.web_url,
                timeout_s=W3_BUILD_TIMEOUT_S,
                log_fn=_log,
                domain_filter={"queue"},
            )
            _log(f"  prior-build outcome={outcome} run_id={build_run_id}")
            failures.soft_assert(
                outcome is not None,
                f"prior build never reached terminal in {W3_BUILD_TIMEOUT_S}s",
            )
            if outcome and outcome != "success":
                failures.fail(f"prior build outcome={outcome!r} (need success for improve)")
            _safe_screenshot(page, artifact_dir, "04-build-terminal")

            # ---------- Step 4: enqueue improve job ----------
            _log("Step 4: enqueue Improve(bugs) via JobDialog")
            try:
                page.reload(wait_until="domcontentloaded", timeout=15_000)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"reload before improve failed: {exc}")
            time.sleep(1.5)

            ok = _enqueue_via_dialog_full(
                page,
                intent=W3_IMPROVE_FOCUS,
                command="improve",
                subcommand="bugs",
                provider=ctx.provider,
                failures=failures,
                label="improve",
                artifact_dir=artifact_dir,
                screenshot_idx=5,
            )
            failures.soft_assert(ok, "could not enqueue improve job")
            time.sleep(2)

            # Refresh / restart watcher so improve picks up
            _click_start_watcher(page, failures=failures, label="W3-improve", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "06-watcher-improve")

            # ---------- Step 5: poll for build-journal updates ----------
            _log("Step 5: tail build-journal.md round-by-round")
            journal_observed: list[str] = []
            improve_outcome: Optional[str] = None
            improve_run_id: Optional[str] = None
            deadline = time.monotonic() + W3_IMPROVE_TIMEOUT_S
            tick = 0
            while time.monotonic() < deadline:
                tick += 1
                # Look for build-journal.md across all sessions in worktree dirs
                journals = []
                try:
                    for path in ctx.project_dir.glob(".worktrees/*/otto_logs/sessions/*/improve/build-journal.md"):
                        if path.is_file():
                            journals.append(path)
                    for path in ctx.project_dir.glob("otto_logs/sessions/*/improve/build-journal.md"):
                        if path.is_file():
                            journals.append(path)
                except Exception:
                    pass
                if journals:
                    journals.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    latest = journals[0]
                    try:
                        snapshot = latest.read_text(encoding="utf-8")
                    except Exception:  # noqa: BLE001
                        snapshot = ""
                    if snapshot and snapshot not in journal_observed:
                        journal_observed.append(snapshot)
                        _log(f"  build-journal updated (len={len(snapshot)}) @ {latest}")
                # Check terminal
                state = _state(ctx.web_url)
                history = _history_items(state)
                for it in history:
                    if (
                        it.get("domain") == "queue"
                        and it.get("run_id") != build_run_id
                        and it.get("terminal_outcome")
                    ):
                        # Try to disambiguate as improve vs build
                        improve_outcome = it.get("terminal_outcome")
                        improve_run_id = it.get("run_id")
                        break
                if improve_outcome:
                    break
                if tick % 12 == 1:
                    _log(
                        f"  tick#{tick}: live={len(_live_items(state))} history={len(history)} "
                        f"journal_versions={len(journal_observed)}"
                    )
                time.sleep(10)

            _log(f"  improve outcome={improve_outcome} run_id={improve_run_id} "
                 f"journal_versions={len(journal_observed)}")
            failures.soft_assert(
                improve_outcome is not None,
                f"improve never reached terminal in {W3_IMPROVE_TIMEOUT_S}s",
            )
            failures.soft_assert(
                len(journal_observed) >= 1,
                "no build-journal.md observed during improve loop",
            )
            _safe_screenshot(page, artifact_dir, "07-improve-terminal")

            # Save the journals captured
            (artifact_dir / "build-journal-versions.txt").write_text(
                "\n\n=== version boundary ===\n\n".join(journal_observed) or "(none)",
                encoding="utf-8",
            )

            # ---------- Step 6: improvement-report.md visible ----------
            _log("Step 6: locate improvement-report.md")
            reports = []
            try:
                for path in ctx.project_dir.glob(
                    ".worktrees/*/otto_logs/sessions/*/improve/improvement-report.md"
                ):
                    if path.is_file():
                        reports.append(path)
                for path in ctx.project_dir.glob(
                    "otto_logs/sessions/*/improve/improvement-report.md"
                ):
                    if path.is_file():
                        reports.append(path)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"glob improvement-report failed: {exc}")
            failures.soft_assert(
                len(reports) >= 1,
                "no improvement-report.md found after improve terminated",
            )
            if reports:
                reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                latest = reports[0]
                _log(f"  improvement-report.md @ {latest}")
                try:
                    shutil.copy2(latest, artifact_dir / "improvement-report.md")
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"copy improvement-report failed: {exc}")

            # ---------- Step 7: product verification — pytest passes ----------
            _log("Step 7: product verification — pytest greet")
            # Look for greet.py / test_greet.py somewhere under the project
            greet_files = []
            try:
                greet_files = [
                    str(p.relative_to(ctx.project_dir))
                    for p in ctx.project_dir.rglob("greet.py")
                    if ".worktrees" not in p.parts and "otto_logs" not in p.parts
                    and "node_modules" not in p.parts
                ]
            except Exception:
                pass
            _log(f"  greet.py files in project root: {greet_files}")
            # Run pytest in the latest improved worktree if one exists
            test_target = None
            try:
                wts = sorted(
                    ctx.project_dir.glob(".worktrees/*/test_greet.py"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                if wts:
                    test_target = wts[0].parent
            except Exception:
                pass
            if test_target is None:
                # Fallback: project root
                if (ctx.project_dir / "test_greet.py").exists():
                    test_target = ctx.project_dir
            if test_target is None:
                failures.note("no test_greet.py found — skipping pytest product check")
            else:
                _log(f"  running pytest in {test_target}")
                try:
                    res = subprocess.run(
                        ["python", "-m", "pytest", "-x", "-q", "test_greet.py"],
                        cwd=test_target,
                        capture_output=True, text=True, timeout=60,
                    )
                    (artifact_dir / "pytest.log").write_text(
                        f"rc={res.returncode}\n--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}",
                        encoding="utf-8",
                    )
                    failures.soft_assert(
                        res.returncode == 0,
                        f"pytest after improve failed rc={res.returncode}: {res.stdout[:200]}",
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"pytest invocation failed: {exc}")

            # final state
            state = _state(ctx.web_url)
            if isinstance(state, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(state, indent=2, default=str), encoding="utf-8"
                )
            _safe_screenshot(page, artifact_dir, "08-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W3; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W3: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx during W3: {len(unexpected)}")

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W4 — Merge happy path
# ---------------------------------------------------------------------------


def _run_w4(ctx: ScenarioContext) -> ScenarioRunResult:
    """W4 — submit a tiny build, watcher, wait success, merge from UI, verify branch landed."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W4")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: open MC ----------
            _log("Step 1: open MC")
            try:
                page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            # ---------- Step 2: enqueue tiny build ----------
            _log("Step 2: enqueue tiny build")
            ok = _enqueue_via_dialog_full(
                page,
                intent=W4_INTENT,
                command="build",
                subcommand=None,
                provider=ctx.provider,
                failures=failures,
                label="w4-build",
                artifact_dir=artifact_dir,
                screenshot_idx=2,
            )
            failures.soft_assert(ok, "could not enqueue W4 build")
            time.sleep(1.5)

            # ---------- Step 3: start watcher, wait terminal ----------
            _log("Step 3: start watcher; wait for build terminal")
            _click_start_watcher(page, failures=failures, label="W4", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "03-watcher")

            outcome, build_run_id = _wait_for_terminal(
                ctx.web_url, timeout_s=W4_BUILD_TIMEOUT_S, log_fn=_log,
                domain_filter={"queue"},
            )
            _log(f"  build outcome={outcome} run_id={build_run_id}")
            failures.soft_assert(
                outcome == "success",
                f"build outcome={outcome!r} (need success to merge)",
            )
            _safe_screenshot(page, artifact_dir, "04-build-terminal")

            # ---------- Step 4: snapshot pre-merge git log ----------
            _log("Step 4: pre-merge git log on main")
            try:
                pre = subprocess.run(
                    ["git", "log", "--oneline", "-n", "10", "main"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=10,
                )
                (artifact_dir / "pre-merge-git-log.txt").write_text(
                    pre.stdout, encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"pre-merge git log failed: {exc}")

            # ---------- Step 5: merge from UI ----------
            _log("Step 5: merge through visible UI")
            if build_run_id:
                state = _state(ctx.web_url)
                landing = state.get("landing") if isinstance(state, dict) else {}
                ready_items = [
                    item for item in (landing.get("items") or [])
                    if isinstance(item, dict) and item.get("landing_state") == "ready"
                ] if isinstance(landing, dict) else []
                _log(f"  landing ready items={len(ready_items)}")
                failures.soft_assert(
                    any(item.get("run_id") == build_run_id for item in ready_items),
                    f"run {build_run_id} was not listed as ready to land",
                )
                merge_clicked = _mc_click_land_ready_from_ui(
                    page, failures=failures, log_fn=_log
                )
                if not merge_clicked:
                    merge_clicked = _mc_click_run_action_from_ui(
                        page,
                        build_run_id,
                        "merge",
                        failures=failures,
                        log_fn=_log,
                    )
                failures.soft_assert(merge_clicked, f"could not land run {build_run_id} through visible UI")

            # ---------- Step 6: wait merge history row ----------
            _log("Step 6: poll for merge history row")
            saw_merge = False
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                history = _history_items(state)
                for it in history:
                    if it.get("domain") == "merge" and it.get("terminal_outcome") == "success":
                        saw_merge = True
                        break
                if saw_merge:
                    break
                time.sleep(3)
            failures.soft_assert(saw_merge, "no merge history row appeared in 90s")
            _safe_screenshot(page, artifact_dir, "05-after-merge")

            # ---------- Step 7: verify branch landed in main ----------
            _log("Step 7: post-merge git log + verify hello.py exists on main")
            try:
                post = subprocess.run(
                    ["git", "log", "--oneline", "-n", "10", "main"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=10,
                )
                (artifact_dir / "post-merge-git-log.txt").write_text(
                    post.stdout, encoding="utf-8"
                )
                lines_pre = (
                    (artifact_dir / "pre-merge-git-log.txt").read_text(encoding="utf-8")
                    if (artifact_dir / "pre-merge-git-log.txt").exists() else ""
                ).splitlines()
                lines_post = post.stdout.splitlines()
                failures.soft_assert(
                    len(lines_post) > len(lines_pre),
                    f"main commit count did not grow: pre={len(lines_pre)} post={len(lines_post)}",
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"post-merge git log failed: {exc}")

            # Verify hello.py is in main
            try:
                show = subprocess.run(
                    ["git", "show", "main:hello.py"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=10,
                )
                (artifact_dir / "hello-py-in-main.txt").write_text(
                    f"rc={show.returncode}\n{show.stdout}",
                    encoding="utf-8",
                )
                failures.soft_assert(
                    show.returncode == 0,
                    f"hello.py not present on main after merge (rc={show.returncode})",
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"git show hello.py failed: {exc}")

            # final state
            state = _state(ctx.web_url)
            if isinstance(state, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(state, indent=2, default=str), encoding="utf-8"
                )
            _safe_screenshot(page, artifact_dir, "06-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W4; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W4: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx during W4: {len(unexpected)}")

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W5 — Merge blocking
# ---------------------------------------------------------------------------


def _run_w5(ctx: ScenarioContext) -> ScenarioRunResult:
    """W5 — successful build but merge is blocked because target is dirty."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W5")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: open MC, enqueue build ----------
            _log("Step 1: open MC + enqueue build")
            try:
                page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            ok = _enqueue_via_dialog_full(
                page,
                intent=W5_INTENT,
                command="build",
                subcommand=None,
                provider=ctx.provider,
                failures=failures,
                label="w5-build",
                artifact_dir=artifact_dir,
                screenshot_idx=2,
            )
            failures.soft_assert(ok, "could not enqueue W5 build")
            time.sleep(1.5)
            _click_start_watcher(page, failures=failures, label="W5", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "03-watcher")

            # ---------- Step 2: wait for build success ----------
            _log("Step 2: wait for build terminal")
            outcome, build_run_id = _wait_for_terminal(
                ctx.web_url, timeout_s=W5_BUILD_TIMEOUT_S, log_fn=_log,
                domain_filter={"queue"},
            )
            _log(f"  outcome={outcome} run_id={build_run_id}")
            failures.soft_assert(
                outcome == "success",
                f"W5 build outcome={outcome!r} (need success to attempt merge)",
            )
            _safe_screenshot(page, artifact_dir, "04-build-terminal")

            # ---------- Step 3: dirty the project root so merge is blocked ----------
            _log("Step 3: write a dirty file in project root to block merge")
            dirty_path = ctx.project_dir / "DIRTY_FILE.txt"
            try:
                dirty_path.write_text(
                    "intentional dirt — W5 wants this to block the merge\n",
                    encoding="utf-8",
                )
                _log(f"  wrote {dirty_path}")
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"could not seed dirty file: {exc}")

            # Capture pre-merge git status for evidence
            try:
                gs = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=10,
                )
                (artifact_dir / "pre-merge-git-status.txt").write_text(
                    gs.stdout, encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"git status failed: {exc}")

            # ---------- Step 4: refresh UI — expect visible blocked reason ----------
            _log("Step 4: refresh Mission Control — expect landing blocked")
            merge_body = ""
            try:
                refresh = page.locator('[data-testid="project-workspace-refresh"]').first
                if refresh.count() > 0 and refresh.is_enabled():
                    refresh.click(timeout=5_000)
                    time.sleep(1.0)
                else:
                    page.reload(wait_until="domcontentloaded", timeout=15_000)
                    _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"W5 refresh after dirtying project failed: {exc}")
            state = _state(ctx.web_url)
            landing = state.get("landing") if isinstance(state, dict) else {}
            blocked = bool(landing.get("merge_blocked")) if isinstance(landing, dict) else False
            merge_body = _mc_visible_text(page)
            (artifact_dir / "merge-blocked-visible-text.txt").write_text(merge_body, encoding="utf-8")
            failures.soft_assert(
                blocked or page.locator('[data-testid="merge-blocked-banner"]').count() > 0,
                "Mission Control did not show landing as blocked after project became dirty",
            )
            # Reason should mention dirty / blocked / repository / merge-ready
            body_lower = merge_body.lower()
            failures.soft_assert(
                any(kw in body_lower for kw in ("dirty", "block", "merge", "uncommit", "repo")),
                f"merge-blocked reason did not mention dirty/blocked/merge/repo: {merge_body[:200]}",
            )
            _safe_screenshot(page, artifact_dir, "05-after-merge-attempt")

            # ---------- Step 5: verify nothing landed on main ----------
            _log("Step 5: verify hello.py NOT in main (since merge was blocked)")
            try:
                subprocess.run(
                    ["git", "show", f"main:{Path(W5_INTENT).name}"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=10,
                )
                # rc != 0 means the file isn't there — that's what we want
            except Exception:
                pass
            try:
                show2 = subprocess.run(
                    ["git", "show", "main:ping.py"],
                    cwd=ctx.project_dir, capture_output=True, text=True, timeout=10,
                )
                (artifact_dir / "main-ping-show.txt").write_text(
                    f"rc={show2.returncode}\n{show2.stdout}\n{show2.stderr}",
                    encoding="utf-8",
                )
                failures.soft_assert(
                    show2.returncode != 0,
                    f"ping.py SHOULD NOT be on main after blocked merge (rc={show2.returncode})",
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"git show ping.py failed: {exc}")

            # ---------- Step 6: clean up dirty file & verify merge would now work ----------
            # NB: we don't actually re-attempt merge here — the scenario is about
            # the BLOCK with clear reason. Just record state.
            try:
                if dirty_path.exists():
                    dirty_path.unlink()
            except Exception:
                pass

            state = _state(ctx.web_url)
            if isinstance(state, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(state, indent=2, default=str), encoding="utf-8"
                )
            _safe_screenshot(page, artifact_dir, "06-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W5; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W5: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        # 409 from merge action is EXPECTED — filter it out
        unexpected = [
            n for n in captured["network_errors"]
            if n.get("status") not in (404, 409)
        ]
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx during W5: {len(unexpected)}")

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W6 — Deterministic failure + retry
# ---------------------------------------------------------------------------


def _run_w6(ctx: ScenarioContext) -> ScenarioRunResult:
    """W6 — seed malformed otto.yaml so build fails; harness fixes it; retry succeeds."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W6")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    # ---------- Step 0: pre-seed malformed otto.yaml ----------
    yaml_path = ctx.project_dir / "otto.yaml"
    bad_yaml = "default_branch: main\nqueue:\n  bookkeeping_files: not-a-list-but-a-string\n[unparseable yaml line"
    good_yaml = "default_branch: main\nqueue:\n  bookkeeping_files: []\n"
    try:
        yaml_path.write_text(bad_yaml, encoding="utf-8")
        # Commit so the project is clean before the build
        subprocess.run(["git", "add", "otto.yaml"], cwd=ctx.project_dir, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed malformed otto.yaml"],
            cwd=ctx.project_dir, check=True,
        )
        _log(f"  seeded malformed otto.yaml at {yaml_path}")
    except Exception as exc:  # noqa: BLE001
        failures.fail(f"could not seed malformed otto.yaml: {exc}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: open MC ----------
            _log("Step 1: open MC")
            try:
                page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=30_000)
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            # ---------- Step 2: enqueue build (will fail) ----------
            _log("Step 2: enqueue build with broken yaml")
            ok = _enqueue_via_dialog_full(
                page,
                intent=W6_INTENT,
                command="build",
                subcommand=None,
                provider=ctx.provider,
                failures=failures,
                label="w6-build-fail",
                artifact_dir=artifact_dir,
                screenshot_idx=2,
            )
            # Note: enqueue itself may succeed even with broken yaml; the
            # failure happens when the watcher reads otto.yaml.
            failures.soft_assert(ok, "could not enqueue W6 broken build")
            time.sleep(1.5)
            _click_start_watcher(page, failures=failures, label="W6-fail", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "03-watcher-fail")

            # ---------- Step 3: wait for failure ----------
            _log("Step 3: wait for build to fail")
            outcome, fail_run_id = _wait_for_terminal(
                ctx.web_url, timeout_s=W6_BUILD_TIMEOUT_S, log_fn=_log,
                domain_filter={"queue"},
            )
            _log(f"  build outcome={outcome} run_id={fail_run_id}")
            # Allowed terminal outcomes that satisfy "deterministic failure":
            # failed | interrupted | error
            failures.soft_assert(
                outcome in ("failed", "interrupted", "error", "cancelled", "removed")
                or (outcome and outcome != "success"),
                f"expected non-success deterministic failure; got outcome={outcome!r}",
            )
            _safe_screenshot(page, artifact_dir, "04-build-failed")

            # Capture detail/legal_actions for the failed run so we can validate
            # the UI exposes Retry as legal.
            if fail_run_id:
                _, detail_body = _api_get(ctx.web_url, f"/api/runs/{fail_run_id}")
                if isinstance(detail_body, dict):
                    (artifact_dir / "failed-run-detail.json").write_text(
                        json.dumps(detail_body, indent=2, default=str),
                        encoding="utf-8",
                    )
                    legal = [a.get("key") for a in (detail_body.get("legal_actions") or [])]
                    _log(f"  failed-run legal_actions keys={legal}")
                    failures.soft_assert(
                        "R" in legal or "retry" in legal or "requeue" in legal,
                        f"failed run does not expose Retry in legal_actions: {legal}",
                    )

            # ---------- Step 4: harness fixes the yaml ----------
            _log("Step 4: harness fixes otto.yaml")
            try:
                yaml_path.write_text(good_yaml, encoding="utf-8")
                subprocess.run(["git", "add", "otto.yaml"], cwd=ctx.project_dir, check=True)
                subprocess.run(
                    ["git", "commit", "-q", "-m", "fix: repair otto.yaml"],
                    cwd=ctx.project_dir, check=True,
                )
                _log("  yaml repaired and committed")
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"could not repair otto.yaml: {exc}")

            # ---------- Step 5: click Retry from UI ----------
            _log("Step 5: retry from visible UI")
            if fail_run_id:
                retry_clicked = _mc_click_run_action_from_ui(
                    page,
                    fail_run_id,
                    "retry",
                    failures=failures,
                    log_fn=_log,
                )
                failures.soft_assert(
                    retry_clicked,
                    f"could not retry run {fail_run_id} through visible UI",
                )

            # Restart watcher (it may have exited after empty queue)
            time.sleep(2)
            try:
                page.reload(wait_until="domcontentloaded", timeout=15_000)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"reload after retry failed: {exc}")
            time.sleep(1)
            _click_start_watcher(page, failures=failures, label="W6-retry", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "05-after-retry")

            # ---------- Step 6: wait for success ----------
            _log("Step 6: wait for retry success")
            # Use a fresh deadline; a different run_id may be created.
            retry_outcome: Optional[str] = None
            retry_run_id: Optional[str] = None
            deadline = time.monotonic() + W6_BUILD_TIMEOUT_S
            tick = 0
            while time.monotonic() < deadline:
                tick += 1
                state = _state(ctx.web_url)
                history = _history_items(state)
                # Look for ANY success outcome in queue domain that isn't the failed run
                for it in history:
                    if (
                        it.get("domain") == "queue"
                        and it.get("terminal_outcome") == "success"
                    ):
                        retry_outcome = "success"
                        retry_run_id = it.get("run_id")
                        break
                if retry_outcome:
                    break
                if tick % 12 == 1:
                    live = _live_items(state)
                    _log(f"  tick#{tick}: live_statuses={[it.get('status') for it in live[:5]]}")
                time.sleep(10)

            _log(f"  retry outcome={retry_outcome} run_id={retry_run_id}")
            failures.soft_assert(
                retry_outcome == "success",
                f"retry did not reach success in {W6_BUILD_TIMEOUT_S}s; got {retry_outcome!r}",
            )
            _safe_screenshot(page, artifact_dir, "06-retry-success")

            # ---------- Step 7: product verification — mod.py exists ----------
            _log("Step 7: product verification — mod.py / one()")
            mod_files = list(ctx.project_dir.glob(".worktrees/*/mod.py"))
            if not mod_files and (ctx.project_dir / "mod.py").exists():
                mod_files = [ctx.project_dir / "mod.py"]
            failures.soft_assert(
                len(mod_files) >= 1,
                "no mod.py created after retry success",
            )
            (artifact_dir / "mod-py-locations.txt").write_text(
                "\n".join(str(p) for p in mod_files) or "(none)",
                encoding="utf-8",
            )

            state = _state(ctx.web_url)
            if isinstance(state, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(state, indent=2, default=str), encoding="utf-8"
                )

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W6; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W6: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx during W6: {len(unexpected)}")

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W7 — iPhone live (W1 flow on devices['iPhone 14'] + webkit)
# ---------------------------------------------------------------------------


def _run_w7(ctx: ScenarioContext) -> ScenarioRunResult:
    """W7 — same flow as W1 but using webkit + iPhone 14 device emulation."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W7")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")
    if ctx.user_behavior != "off":
        _log(f"mc_user_behavior={ctx.user_behavior} seed={_mc_user_seed(ctx)}")

    final_state: Optional[dict[str, Any]] = None

    with sync_playwright() as pw:
        # iPhone 14 emulation (webkit). Fall back to chromium if webkit missing.
        try:
            iphone = pw.devices["iPhone 14"]
        except Exception as exc:  # noqa: BLE001
            failures.note(f"iPhone 14 device descriptor unavailable: {exc}")
            iphone = {}
        browser = None
        try:
            browser = pw.webkit.launch(headless=True)
            _log("  using webkit")
        except Exception as exc:  # noqa: BLE001
            failures.note(f"webkit launch failed; falling back to chromium: {exc}")
            browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            **iphone,
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: page loads on mobile ----------
            _log("Step 1: page.goto on mobile viewport")
            try:
                page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"page.goto failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-mobile-loaded")

            try:
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"react never hydrated on mobile: {exc}")
            _mc_select_project_from_launcher_if_needed(
                page,
                ctx,
                log_fn=_log,
                failures=failures,
            )
            if ctx.user_behavior != "off":
                _mc_check_expectation(
                    page,
                    ctx,
                    phase="mobile-shell-loaded",
                    action="open Mission Control on mobile",
                    expectation="Mission Control loads into an actionable mobile launcher or workspace.",
                    any_visible=[
                        '[data-mc-shell="ready"]',
                        '[data-testid="launcher-subhead"]',
                        '[data-testid="project-workspace"]',
                        '[data-testid="task-board"]',
                    ],
                    log_fn=_log,
                    failures=failures,
                    hard=True,
                )

            # Inspect viewport metrics for evidence
            try:
                vw = page.evaluate("({w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio})")
                _log(f"  viewport={vw}")
                (artifact_dir / "viewport.json").write_text(
                    json.dumps(vw, indent=2), encoding="utf-8",
                )
                # iPhone 14 viewport is 390x664 (CSS pixels). Don't hard-fail on
                # exact match (Playwright versions vary), just verify mobile-ish.
                w = int(vw.get("w", 0))
                failures.soft_assert(
                    100 <= w <= 500,
                    f"viewport width {w} not in mobile range — emulation didn't apply",
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"viewport inspect failed: {exc}")

            # ---------- Step 2: shell present ----------
            has_task_board = page.locator('[data-testid="task-board"]').count() > 0
            has_launcher = page.locator('[data-testid="launcher-subhead"]').count() > 0
            failures.soft_assert(
                has_task_board or has_launcher,
                f"shell not present on mobile: tb={has_task_board} l={has_launcher}",
            )
            _safe_screenshot(page, artifact_dir, "02-mobile-shell")
            _mc_user_probe(
                page,
                ctx,
                phase="mobile-shell-loaded",
                log_fn=_log,
                failures=failures,
            )

            # ---------- Step 3: open JobDialog (touch tap) ----------
            _log("Step 3: tap new-job button")
            new_job = page.locator(
                '[data-testid="mission-new-job-button"], [data-testid="new-job-button"]'
            ).first
            if new_job.count() == 0:
                failures.fail("new-job button not found on mobile")
            else:
                # Touch-target check: button bounding box ≥44x44pt per Apple HIG
                try:
                    box = new_job.bounding_box()
                    if box:
                        _log(f"  new-job button box={box}")
                        (artifact_dir / "touch-target-new-job.json").write_text(
                            json.dumps(box, indent=2), encoding="utf-8",
                        )
                        failures.soft_assert(
                            box["width"] >= 32 and box["height"] >= 32,
                            f"new-job button is too small for touch: {box['width']}x{box['height']} (HIG: 44pt)",
                        )
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"bounding_box failed: {exc}")

                try:
                    # Use tap() instead of click() to test mobile gesture
                    new_job.tap(timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"tap failed; falling back to click: {exc}")
                    try:
                        new_job.click(timeout=5_000)
                    except Exception as exc2:  # noqa: BLE001
                        failures.fail(f"new-job click also failed on mobile: {exc2}")

            try:
                page.wait_for_selector('[data-testid="job-dialog-intent"]', timeout=10_000)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"job dialog did not open on mobile: {exc}")
            _safe_screenshot(page, artifact_dir, "03-mobile-dialog")
            if ctx.user_behavior != "off":
                _mc_check_expectation(
                    page,
                    ctx,
                    phase="mobile-job-dialog-open",
                    action="tap new job",
                    expectation="The mobile job dialog opens with an intent field and submit button.",
                    all_visible=[
                        '[data-testid="job-dialog-intent"]',
                        '[data-testid="job-dialog-submit-button"]',
                    ],
                    log_fn=_log,
                    failures=failures,
                    hard=True,
                )

            # ---------- Step 4: fill intent + submit ----------
            _log("Step 4: fill + submit on mobile")
            intent_field = page.locator('[data-testid="job-dialog-intent"]')
            if intent_field.count() > 0:
                try:
                    intent_field.fill(_scenario_intent(ctx, W7_INTENT), timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"intent fill failed on mobile: {exc}")

            submit = page.locator('[data-testid="job-dialog-submit-button"]')
            if submit.count() == 0:
                failures.fail("submit button missing on mobile")
            else:
                try:
                    box = submit.bounding_box()
                    if box:
                        (artifact_dir / "touch-target-submit.json").write_text(
                            json.dumps(box, indent=2), encoding="utf-8",
                        )
                        failures.soft_assert(
                            box["width"] >= 32 and box["height"] >= 32,
                            f"submit button too small for touch: {box['width']}x{box['height']}",
                        )
                except Exception:
                    pass
                try:
                    submit.tap(timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"submit tap failed; trying click: {exc}")
                    try:
                        submit.click(timeout=5_000)
                    except Exception as exc2:  # noqa: BLE001
                        failures.fail(f"submit click also failed on mobile: {exc2}")
            _safe_screenshot(page, artifact_dir, "04-mobile-submitted")
            if ctx.user_behavior != "off":
                _mc_check_expectation(
                    page,
                    ctx,
                    phase="mobile-job-submitted",
                    action="submit mobile build intent",
                    expectation="After mobile submit, Mission Control returns to visible queue/workspace feedback.",
                    any_visible=[
                        '[data-testid="mission-start-watcher-button"]',
                        '[data-testid="start-watcher-button"]',
                        '[data-testid="project-workspace"]',
                        '[data-testid="task-board"]',
                    ],
                    log_fn=_log,
                    failures=failures,
                    hard=True,
                )

            # ---------- Step 5: start watcher ----------
            _log("Step 5: start watcher")
            time.sleep(1.5)
            _click_start_watcher(page, failures=failures, label="W7", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "05-mobile-watcher")
            if ctx.user_behavior != "off":
                _mc_check_expectation(
                    page,
                    ctx,
                    phase="mobile-watcher-started",
                    action="start queue watcher on mobile",
                    expectation="Mobile Mission Control keeps the queue/workspace visible while work starts.",
                    any_visible=[
                        '[data-testid="project-workspace"]',
                        '[data-testid="task-board"]',
                        ".task-card-main",
                    ],
                    log_fn=_log,
                    failures=failures,
                )
            _mc_user_probe(
                page,
                ctx,
                phase="mobile-running-start",
                log_fn=_log,
                failures=failures,
            )

            # ---------- Step 6: wait for terminal ----------
            build_timeout_s = ctx.build_timeout_s or W7_BUILD_TIMEOUT_S
            _log(f"Step 6: wait ≤{build_timeout_s}s for terminal")
            outcome, run_id = _wait_for_terminal(
                ctx.web_url, timeout_s=build_timeout_s, log_fn=_log,
                domain_filter={"queue"},
                on_poll=lambda poll: _mc_user_probe(
                    page,
                    ctx,
                    phase=f"mobile-running-poll-{poll}",
                    log_fn=_log,
                    failures=failures,
                ),
            )
            _log(f"  outcome={outcome} run_id={run_id}")
            failures.soft_assert(
                outcome is not None,
                f"build did not reach terminal in {build_timeout_s}s",
            )
            if outcome and outcome != "success":
                failures.fail(f"build outcome={outcome!r} (expected success)")
            _safe_screenshot(page, artifact_dir, "06-mobile-terminal")
            _mc_user_probe(
                page,
                ctx,
                phase="mobile-terminal-state",
                log_fn=_log,
                failures=failures,
            )

            # ---------- Step 7: refresh + verify mobile layout still works ----------
            try:
                page.reload(wait_until="domcontentloaded", timeout=15_000)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"mobile reload failed: {exc}")
            _safe_screenshot(page, artifact_dir, "07-mobile-after-reload")

            # ---------- Step 8: product verification ----------
            _log("Step 8: product verification — file scan")
            try:
                file_list = []
                for path in sorted(ctx.project_dir.rglob("*"))[:200]:
                    if path.is_file() and ".git" not in path.parts and "otto_logs" not in path.parts:
                        rel = path.relative_to(ctx.project_dir)
                        file_list.append(str(rel))
                (artifact_dir / "project-files.txt").write_text(
                    "\n".join(file_list), encoding="utf-8"
                )
                created = [
                    f for f in file_list
                    if f.endswith((".html", ".js", ".jsx", ".tsx", ".css", ".py"))
                ]
                failures.soft_assert(
                    len(created) > 0 or outcome != "success",
                    "build reported success but no source files appeared",
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"file scan raised: {exc}")

            # final state
            try:
                _, body = _api_get(ctx.web_url, "/api/state")
                final_state = body if isinstance(body, dict) else None
                if final_state is not None:
                    (artifact_dir / "final-state.json").write_text(
                        json.dumps(final_state, indent=2, default=str), encoding="utf-8"
                    )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"final state snapshot failed: {exc}")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W7; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W7: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx during W7: {len(unexpected)}")

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W8 — Power-user keyboard-only
# ---------------------------------------------------------------------------


def _kbd_focus_testid(page: Any, testid: str, *, max_tabs: int = 60) -> bool:
    """Press Tab repeatedly until the focused element matches ``[data-testid=testid]``.

    Returns True if focus landed on the target. Records observed focus chain
    (only useful for debugging) on the page object via attribute side-channel.
    """
    chain: list[str] = []
    for i in range(max_tabs):
        try:
            descriptor = page.evaluate(
                "() => { const el = document.activeElement; if (!el) return null;"
                " return { tag: el.tagName, testid: el.getAttribute('data-testid'),"
                " text: (el.textContent||'').trim().slice(0,40),"
                " role: el.getAttribute('role'), aria: el.getAttribute('aria-label') }; }"
            )
        except Exception:  # noqa: BLE001
            descriptor = None
        if descriptor:
            chain.append(
                f"{descriptor.get('tag')}#{descriptor.get('testid') or '-'}|"
                f"{descriptor.get('aria') or descriptor.get('text') or ''}"
            )
            if descriptor.get("testid") == testid:
                # Stash for caller diagnostics.
                setattr(page, "_w8_focus_chain", chain)
                return True
        page.keyboard.press("Tab")
        time.sleep(0.05)
    setattr(page, "_w8_focus_chain", chain)
    return False


def _kbd_focused_testid(page: Any) -> Optional[str]:
    try:
        return page.evaluate(
            "() => document.activeElement?.getAttribute('data-testid')"
        )
    except Exception:  # noqa: BLE001
        return None


def _run_w8(ctx: ScenarioContext) -> ScenarioRunResult:
    """W8 — multi-job operator driven entirely from the keyboard.

    Mirrors W2 (3 builds, watcher, cancel one) but never uses the mouse.
    Findings catalogue any flow that REQUIRES mouse to complete.
    """
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W8")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    # Per-step focus chains — written out as artifacts.
    focus_chains: dict[str, list[str]] = {}

    def _enqueue_via_keyboard(label: str, intent: str, idx: int) -> bool:
        """Open dialog (Tab→Enter or Cmd-K), fill intent, submit — all via keyboard."""
        # Step a: focus body so Tab order is deterministic
        try:
            page.evaluate("() => document.body.focus()")
        except Exception:  # noqa: BLE001
            pass

        # Try Cmd-K palette first (canonical power-user entry).
        opened_via_palette = False
        try:
            page.keyboard.press("Meta+k")
            time.sleep(0.4)
            palette = page.locator(
                '[data-testid="command-palette"], [role="dialog"][aria-label*="Command"]'
            )
            if palette.count() > 0:
                # Type "new job" or pick first item with Enter
                _log(f"  palette opened via Cmd-K ({label})")
                page.keyboard.type("new job", delay=20)
                time.sleep(0.3)
                page.keyboard.press("Enter")
                time.sleep(0.5)
                opened_via_palette = page.locator('[data-testid="job-dialog-intent"]').count() > 0
                if opened_via_palette:
                    _log("  Cmd-K palette → JobDialog OK")
                else:
                    failures.note(
                        f"Cmd-K palette opened but Enter on first match did not open JobDialog ({label})"
                    )
                    # Dismiss palette if still open
                    page.keyboard.press("Escape")
                    time.sleep(0.2)
            else:
                _log(f"  Cmd-K did not open a command palette ({label}) — fall back to Tab")
                # press Escape just in case something opened invisibly
                page.keyboard.press("Escape")
                time.sleep(0.1)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"Cmd-K attempt raised: {exc}")

        if not opened_via_palette:
            # Fallback: Tab through to mission-new-job-button
            page.evaluate("() => document.body.focus()")
            time.sleep(0.1)
            landed = _kbd_focus_testid(
                page,
                "mission-new-job-button",
                max_tabs=60,
            )
            chain = getattr(page, "_w8_focus_chain", [])
            focus_chains[f"{label}-tab-to-newjob"] = list(chain)
            if not landed:
                # try the alternate id
                page.evaluate("() => document.body.focus()")
                landed = _kbd_focus_testid(page, "new-job-button", max_tabs=60)
                chain = getattr(page, "_w8_focus_chain", [])
                focus_chains[f"{label}-tab-to-newjob-alt"] = list(chain)
            if not landed:
                failures.fail(
                    f"could not Tab to new-job button via keyboard ({label}); "
                    f"chain length={len(chain)}"
                )
                return False
            page.keyboard.press("Enter")
            try:
                page.wait_for_selector('[data-testid="job-dialog-intent"]', timeout=10_000)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"job dialog did not open after Enter on new-job ({label}): {exc}")
                return False

        _safe_screenshot(page, artifact_dir, f"{idx:02d}-dialog-{label}")

        # Dialog open. The intent textarea SHOULD be auto-focused; if not, that's a finding.
        cur = _kbd_focused_testid(page)
        _log(f"  on dialog open, activeElement testid={cur!r}")
        if cur != "job-dialog-intent":
            failures.note(
                f"JobDialog intent textarea is NOT auto-focused on open ({label}); "
                f"focused testid={cur!r}"
            )
            # Force focus into intent via Tab loop
            landed = _kbd_focus_testid(page, "job-dialog-intent", max_tabs=20)
            if not landed:
                failures.fail(f"could not Tab to job-dialog-intent ({label})")
                return False

        # Type intent (no mouse).
        page.keyboard.type(intent, delay=2)
        time.sleep(0.2)

        # Submit via keyboard. Two paths to test:
        #   1. Cmd+Enter shortcut from inside textarea
        #   2. Tab to submit, press Enter
        # We test path 1 for the first, path 2 for subsequent — gives signal on both.
        if idx % 2 == 0:
            _log("  submitting via Cmd+Enter from textarea")
            page.keyboard.press("Meta+Enter")
        else:
            _log("  submitting via Tab → Enter on submit button")
            landed = _kbd_focus_testid(page, "job-dialog-submit-button", max_tabs=20)
            chain = getattr(page, "_w8_focus_chain", [])
            focus_chains[f"{label}-tab-to-submit"] = list(chain)
            if not landed:
                failures.fail(f"could not Tab to submit button ({label})")
                return False
            page.keyboard.press("Enter")

        # Wait for dialog to close
        try:
            page.wait_for_selector(
                '[data-testid="job-dialog-intent"]',
                state="detached",
                timeout=10_000,
            )
        except Exception:  # noqa: BLE001
            # If submit didn't close it, that's a real bug — but record and try Escape.
            still_open = page.locator('[data-testid="job-dialog-intent"]').count() > 0
            if still_open:
                failures.fail(
                    f"JobDialog still open after keyboard submit ({label}) — Cmd+Enter / Enter ignored"
                )
                # Try clicking submit via .focus().press(Enter) once more
                try:
                    page.locator('[data-testid="job-dialog-submit-button"]').focus()
                    page.keyboard.press("Enter")
                    page.wait_for_selector(
                        '[data-testid="job-dialog-intent"]',
                        state="detached",
                        timeout=5_000,
                    )
                except Exception:  # noqa: BLE001
                    failures.fail(f"JobDialog refused all keyboard submit attempts ({label})")
                    # Last-resort: press Escape so we don't blockade the next iteration
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                    return False
        return True

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")
        page = context.new_page()
        captured = _capture_console_and_network(page, artifact_dir)

        try:
            # ---------- Step 1: open MC, verify shell ready ----------
            _log("Step 1: open MC, verify shell ready marker")
            try:
                page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=30_000)
                _wait_for_mc_ready(page)
                _log("  mc shell ready (data-mc-shell=ready or launcher)")
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"shell load failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-shell")

            # ---------- Step 2: enqueue 3 builds via keyboard ----------
            _log("Step 2: enqueue 3 jobs via keyboard")
            enqueued = 0
            for idx, (lab, intent) in enumerate(
                [("a", W8_INTENT_A), ("b", W8_INTENT_B), ("c", W8_INTENT_C)],
                start=1,
            ):
                if _enqueue_via_keyboard(lab, intent, idx + 1):
                    enqueued += 1
                time.sleep(1.5)
            _log(f"  enqueued={enqueued}/3")
            failures.soft_assert(
                enqueued == 3,
                f"expected to enqueue 3 jobs via keyboard, got {enqueued}",
            )

            # ---------- Step 3: verify queue reflects backlog ----------
            _log("Step 3: poll /api/state for ≥3 queue rows")
            queue_count = 0
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                queue_count = sum(1 for it in items if it.get("domain") == "queue")
                if queue_count >= 3:
                    break
                time.sleep(2)
            failures.soft_assert(
                queue_count >= 3,
                f"expected ≥3 queue rows, got {queue_count}",
            )
            _safe_screenshot(page, artifact_dir, "05-queue-3rows")

            # ---------- Step 4: start watcher via keyboard ----------
            _log("Step 4: Tab to start watcher and Enter")
            page.evaluate("() => document.body.focus()")
            time.sleep(0.1)
            landed = _kbd_focus_testid(
                page, "mission-start-watcher-button", max_tabs=80,
            )
            chain = getattr(page, "_w8_focus_chain", [])
            focus_chains["tab-to-start-watcher"] = list(chain)
            if not landed:
                # alternate id
                page.evaluate("() => document.body.focus()")
                landed = _kbd_focus_testid(page, "start-watcher-button", max_tabs=80)
                chain = getattr(page, "_w8_focus_chain", [])
                focus_chains["tab-to-start-watcher-alt"] = list(chain)
            if not landed:
                failures.fail("could not Tab to start-watcher-button")
            else:
                page.keyboard.press("Enter")
                _log("  Enter pressed on start-watcher button")
            _safe_screenshot(page, artifact_dir, "06-watcher")
            time.sleep(2)

            # ---------- Step 5: cancel one queued task via keyboard ----------
            # Tab order in queue is unpredictable; try keyboard tabs first, but
            # fall back to API POST so the rest of the scenario still runs.
            _log("Step 5: try cancel via keyboard (Tab to a cancel/'C' button on a queue row)")

            # Wait a bit so something is running and one is queued
            running_run_id: Optional[str] = None
            cancelled_run_id: Optional[str] = None
            cancel_via: str = "none"
            kb_cancel_ok: bool = False
            kdeadline = time.monotonic() + 5 * 60
            while time.monotonic() < kdeadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                queue_items = [it for it in items if it.get("domain") == "queue"]
                running = [it for it in queue_items if it.get("status") == "running"]
                queued = [
                    it for it in queue_items
                    if it.get("status") in ("queued", "pending", "ready")
                ]
                if running and queued:
                    running_run_id = running[0].get("run_id")
                    cancelled_run_id = queued[-1].get("run_id")
                    break
                time.sleep(5)
            _log(f"  candidates: running={running_run_id} cancel={cancelled_run_id}")

            kb_cancel_ok = False
            if cancelled_run_id is not None:
                # Try keyboard. Look for a cancel/dismiss row affordance with
                # data-testid containing 'cancel' or aria-label 'Cancel'.
                page.evaluate("() => document.body.focus()")
                # Walk Tab order, log every focused element with aria-label/text
                # containing 'cancel'.
                cancel_candidates: list[str] = []
                for _ in range(120):
                    try:
                        d = page.evaluate(
                            "() => { const e = document.activeElement; if (!e) return null;"
                            " return {testid: e.getAttribute('data-testid'),"
                            " aria: e.getAttribute('aria-label'),"
                            " text: (e.textContent||'').trim().slice(0,30)}; }"
                        )
                    except Exception:  # noqa: BLE001
                        d = None
                    if d:
                        cancel_candidates.append(
                            f"{d.get('testid') or '-'}|{d.get('aria') or ''}|{d.get('text') or ''}"
                        )
                        text = (d.get("aria") or "").lower() + " " + (d.get("text") or "").lower()
                        testid = (d.get("testid") or "").lower()
                        if (
                            "cancel" in text
                            or "cancel" in testid
                            or testid.endswith("-cancel")
                        ):
                            page.keyboard.press("Enter")
                            time.sleep(0.6)
                            kb_cancel_ok = True
                            cancel_via = "keyboard"
                            _log(f"  pressed Enter on focused cancel candidate testid={testid!r}")
                            break
                    page.keyboard.press("Tab")
                    time.sleep(0.03)
                focus_chains["tab-cancel-candidates"] = cancel_candidates

                if not kb_cancel_ok:
                    failures.fail(
                        "no cancel affordance reachable via keyboard Tab — "
                        "queue rows lack a per-row cancel button in tab order"
                    )
            else:
                failures.fail("no cancellation candidate appeared within 5 min — cannot test cancel via keyboard")

            _safe_screenshot(page, artifact_dir, "07-after-cancel")

            # ---------- Step 6: wait for queue to drain ----------
            _log(f"Step 6: wait ≤{W8_BUILD_TIMEOUT_S}s for drain")
            ddeadline = time.monotonic() + W8_BUILD_TIMEOUT_S
            drained = False
            while time.monotonic() < ddeadline:
                state = _state(ctx.web_url)
                items = _live_items(state)
                non_terminal = [
                    it for it in items
                    if it.get("domain") == "queue"
                    and it.get("status")
                    not in ("done", "failed", "cancelled", "interrupted", "removed")
                ]
                if not non_terminal:
                    drained = True
                    break
                _log(
                    f"  non_terminal={len(non_terminal)} statuses="
                    f"{[it.get('status') for it in non_terminal]}"
                )
                time.sleep(15)
            failures.soft_assert(
                drained, f"queue did not drain in {W8_BUILD_TIMEOUT_S}s",
            )
            _safe_screenshot(page, artifact_dir, "08-drained")

            # ---------- Step 7: focus management probe — Cmd-K palette + tablist arrows ----------
            _log("Step 7: Cmd-K palette + tablist arrow nav probe")
            try:
                page.keyboard.press("Meta+k")
                time.sleep(0.5)
                pal_present = page.locator(
                    '[data-testid="command-palette"], [role="dialog"][aria-label*="Command"]'
                ).count() > 0
                _log(f"  Cmd-K palette present (post-drain) = {pal_present}")
                if pal_present:
                    page.keyboard.press("Escape")
                    time.sleep(0.2)
                else:
                    failures.note(
                        "Cmd-K does NOT open a command palette — power-user keyboard entry missing"
                    )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"Cmd-K probe raised: {exc}")

            # Tab to tablist (if present) and press ArrowRight
            try:
                tablist_count = page.evaluate(
                    "() => document.querySelectorAll('[role=\"tablist\"]').length"
                )
                _log(f"  tablists in DOM = {tablist_count}")
                if tablist_count and tablist_count > 0:
                    # Find first tablist and click first tab via JS focus, then ArrowRight
                    page.evaluate(
                        "() => {const tl=document.querySelector('[role=\"tablist\"]');"
                        " if(!tl) return null; const t=tl.querySelector('[role=\"tab\"]');"
                        " if(t) t.focus(); return t?.getAttribute('aria-label')||t?.textContent;}"
                    )
                    before_aria = _kbd_focused_testid(page) or "(unknown)"
                    page.keyboard.press("ArrowRight")
                    time.sleep(0.2)
                    after_aria = _kbd_focused_testid(page) or "(unknown)"
                    _log(f"  arrow-right on tablist: {before_aria} -> {after_aria}")
                    if before_aria == after_aria:
                        failures.note(
                            "Tablist ArrowRight did not move focus — WAI-ARIA tablist key support missing"
                        )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tablist arrow probe raised: {exc}")

            # ---------- Step 8: snapshot artifacts ----------
            state = _state(ctx.web_url)
            if isinstance(state, dict):
                (artifact_dir / "final-state.json").write_text(
                    json.dumps(state, indent=2, default=str), encoding="utf-8"
                )
            (artifact_dir / "focus-chains.json").write_text(
                json.dumps(focus_chains, indent=2), encoding="utf-8",
            )
            (artifact_dir / "cancel-summary.json").write_text(
                json.dumps(
                    {
                        "running_run_id": running_run_id,
                        "cancelled_run_id": cancelled_run_id,
                        "kb_cancel_ok": kb_cancel_ok,
                        "cancel_via": cancel_via,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            _safe_screenshot(page, artifact_dir, "09-final")

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W8; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W8: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx during W8: {len(unexpected)}")

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W9 — Backgrounded tab + return
# ---------------------------------------------------------------------------


def _install_notification_spy(page: Any) -> None:
    """Replace window.Notification with a recording spy BEFORE the SPA boots.

    The MC client (App.tsx ~6543, useTrackCompletions hook) fires
    `new Notification("Otto: build completed", ...)` when (a) a live run
    transitions out and (b) document.visibilityState === "hidden". We can't
    rely on the OS to actually display the notification in headless tests, so
    we install a recorder and check window.__otto_notifs__ later.

    Permission is also forced to "granted" so the gate that checks
    `Notification.permission === "granted"` succeeds.
    """

    page.add_init_script(
        """
        (() => {
            const records = [];
            const Spy = function (title, opts) {
                records.push({
                    title: String(title),
                    body: opts && opts.body ? String(opts.body) : null,
                    visibility: document.visibilityState,
                    when: Date.now(),
                });
                return { close: () => {} };
            };
            Spy.permission = "granted";
            Spy.requestPermission = () => Promise.resolve("granted");
            // Some code reads the static .permission via `window.Notification`
            // identity — preserve that.
            try {
                Object.defineProperty(window, "Notification", {
                    configurable: true,
                    writable: true,
                    value: Spy,
                });
            } catch (e) {
                window.Notification = Spy;
            }
            window.__otto_notifs__ = records;
        })();
        """
    )


def _set_visibility(page: Any, *, hidden: bool) -> None:
    """Force document.visibilityState by overriding the property + dispatching event.

    Playwright doesn't expose a direct "background this tab" hook for
    headless. We install descriptor overrides + fire visibilitychange. The
    React hook listens for visibilitychange events.
    """

    state = "hidden" if hidden else "visible"
    is_hidden = "true" if hidden else "false"
    page.evaluate(
        f"""
        (() => {{
            try {{
                Object.defineProperty(document, 'visibilityState', {{
                    configurable: true, get: () => "{state}",
                }});
                Object.defineProperty(document, 'hidden', {{
                    configurable: true, get: () => {is_hidden},
                }});
            }} catch (e) {{}}
            document.dispatchEvent(new Event('visibilitychange'));
            window.__otto_visibility_log = window.__otto_visibility_log || [];
            window.__otto_visibility_log.push({{state: "{state}", t: Date.now()}});
        }})();
        """
    )


def _run_w9(ctx: ScenarioContext) -> ScenarioRunResult:
    """W9 — submit a build, hide the tab for ~2 min, return, verify state coherence."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W9")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(artifact_dir),
        )
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")

        page = context.new_page()
        # Install the Notification spy BEFORE goto so it lands before SPA boots.
        try:
            _install_notification_spy(page)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"notification spy install failed: {exc}")

        captured = _capture_console_and_network(page, artifact_dir)
        # Track raw poll calls so we can detect "did polling pause" / "did it
        # resume". /api/state is the canonical poll endpoint.
        poll_log: list[dict[str, Any]] = []

        def _on_request(req: Any) -> None:
            try:
                url = req.url
                if "/api/state" in url:
                    poll_log.append({
                        "t": time.monotonic() - started,
                        "url": url,
                        "method": req.method,
                    })
            except Exception:  # noqa: BLE001
                pass

        page.on("request", _on_request)

        try:
            # ---------- Step 1: load + shell ready ----------
            _log("Step 1: page.goto")
            try:
                page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"page.goto failed: {exc}")
            _safe_screenshot(page, artifact_dir, "01-loaded")
            try:
                _wait_for_mc_ready(page)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"react never hydrated: {exc}")

            # ---------- Step 2: enqueue one build via JobDialog ----------
            _log("Step 2: enqueue 1 build")
            ok = _enqueue_via_dialog(
                page, W9_INTENT,
                failures=failures, label="w9", artifact_dir=artifact_dir,
                screenshot_idx=2,
            )
            if not ok:
                failures.fail("could not enqueue W9 build")

            # ---------- Step 3: start watcher ----------
            _log("Step 3: start watcher")
            time.sleep(1.5)
            _click_start_watcher(page, failures=failures, label="W9", base_url=ctx.web_url)
            _safe_screenshot(page, artifact_dir, "03-watcher-started")

            # ---------- Step 4: wait for the build to actually start ----------
            _log("Step 4: wait for live row to appear (running)")
            running_run_id: Optional[str] = None
            t0 = time.monotonic()
            while time.monotonic() - t0 < 90:
                state = _state(ctx.web_url)
                items = _live_items(state)
                running = [it for it in items if it.get("status") == "running"]
                if running:
                    running_run_id = running[0].get("run_id")
                    break
                time.sleep(3)
            _log(f"  running_run_id={running_run_id}")
            failures.soft_assert(
                running_run_id is not None,
                "no running row appeared after starting watcher",
            )

            # snapshot poll-count BEFORE hide, then hide.
            polls_before_hide = len(poll_log)
            _log(f"  polls before hide={polls_before_hide}")

            # ---------- Step 5: hide the tab ----------
            _log(f"Step 5: hide tab for {W9_HIDE_S}s")
            try:
                _set_visibility(page, hidden=True)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"could not set visibilityState=hidden: {exc}")
            _safe_screenshot(page, artifact_dir, "05-hidden")

            # While "hidden": just sleep on the python side. The page is still
            # alive, JS continues to run; the SPA may throttle its polling
            # under hidden, but should NOT die.
            hide_started = time.monotonic()
            # Sleep in chunks so we can record poll arrivals.
            chunk = 10
            while time.monotonic() - hide_started < W9_HIDE_S:
                time.sleep(chunk)
                _log(
                    f"  hide-tick t+{int(time.monotonic() - hide_started)}s "
                    f"polls={len(poll_log)}"
                )

            polls_during_hide = len(poll_log) - polls_before_hide
            _log(f"  polls during hide ({W9_HIDE_S}s)={polls_during_hide}")
            (artifact_dir / "poll-stats.json").write_text(
                json.dumps({
                    "polls_before_hide": polls_before_hide,
                    "polls_during_hide_window": polls_during_hide,
                    "hide_window_s": W9_HIDE_S,
                }, indent=2),
                encoding="utf-8",
            )

            # ---------- Step 6: return — restore visibility ----------
            _log("Step 6: restore visibility")
            try:
                _set_visibility(page, hidden=False)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"visibility restore failed: {exc}")
            time.sleep(2)
            _safe_screenshot(page, artifact_dir, "06-restored")

            # The SPA should resume / catch up immediately on visibilitychange.
            # Snapshot poll count after the restore so we can verify resume.
            polls_at_restore = len(poll_log)
            time.sleep(15)
            polls_after_restore = len(poll_log) - polls_at_restore
            _log(f"  polls in 15s after restore={polls_after_restore}")
            failures.soft_assert(
                polls_after_restore >= 1,
                f"only {polls_after_restore} polls in 15s after returning — polling may not have resumed",
            )

            # ---------- Step 7: wait for terminal ----------
            _log(f"Step 7: wait ≤{W9_BUILD_TIMEOUT_S}s for terminal")
            outcome, run_id = _wait_for_terminal(
                ctx.web_url, timeout_s=W9_BUILD_TIMEOUT_S, log_fn=_log,
                domain_filter={"queue"},
            )
            _log(f"  outcome={outcome} run_id={run_id}")
            failures.soft_assert(
                outcome is not None,
                f"build did not reach terminal in {W9_BUILD_TIMEOUT_S}s",
            )
            _safe_screenshot(page, artifact_dir, "07-terminal")

            # ---------- Step 8: state coherence — no duplicate live rows ----------
            _log("Step 8: state coherence — no stale duplicates")
            state = _state(ctx.web_url)
            live = _live_items(state)
            history = _history_items(state)
            # When a run completes, it should leave live and appear in history.
            if run_id is not None:
                live_dupes = [it for it in live if it.get("run_id") == run_id]
                hist_match = [it for it in history if it.get("run_id") == run_id]
                _log(f"  live_dupes={len(live_dupes)} hist_match={len(hist_match)}")
                failures.soft_assert(
                    len(live_dupes) == 0,
                    f"completed run {run_id} still in live[] after terminal — stale state",
                )
                failures.soft_assert(
                    len(hist_match) >= 1,
                    f"completed run {run_id} not in history[] — handoff missing",
                )
            (artifact_dir / "final-state.json").write_text(
                json.dumps(state or {}, indent=2, default=str), encoding="utf-8"
            )

            # ---------- Step 9: notification spy check ----------
            _log("Step 9: check Notification API spy")
            try:
                notifs = page.evaluate("() => window.__otto_notifs__ || []")
            except Exception as exc:  # noqa: BLE001
                notifs = []
                failures.note(f"could not read notification spy: {exc}")
            _log(f"  notifications fired={len(notifs)}")
            (artifact_dir / "notifications.json").write_text(
                json.dumps(notifs, indent=2), encoding="utf-8"
            )
            # The hook only fires when a live run drops out WHILE hidden. If
            # the build completed AFTER we restored visibility, no notif will
            # fire — which is the correct semantic. Capture the timing and
            # assert only when we're confident the completion landed during
            # the hide window.
            try:
                vis_log = page.evaluate("() => window.__otto_visibility_log || []")
            except Exception:  # noqa: BLE001
                vis_log = []
            (artifact_dir / "visibility-log.json").write_text(
                json.dumps(vis_log, indent=2), encoding="utf-8"
            )
            # Soft-only: it is informational. We log a NOTE if the obvious
            # heavy-user notification path didn't fire.
            if outcome and not notifs:
                failures.note(
                    "background-tab notification did not fire (run may have "
                    "completed after restore — see visibility-log.json + "
                    "poll-stats.json)"
                )

            # ---------- Step 10: no double-fire — request log dedup ----------
            (artifact_dir / "poll-log.json").write_text(
                json.dumps(poll_log, indent=2), encoding="utf-8"
            )

        finally:
            try:
                context.tracing.stop(path=str(artifact_dir / "trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop failed: {exc}")
            _flush_captured(captured, artifact_dir)
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if any(c.get("type") == "error" for c in captured["console"]):
        failures.fail("console errors during W9; see console.json")
    if captured["page_errors"]:
        failures.fail(f"page errors during W9: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx during W9: {len(unexpected)}")

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# W10 — Two-tab consistency
# ---------------------------------------------------------------------------


def _run_w10(ctx: ScenarioContext) -> ScenarioRunResult:
    """W10 — open two browser contexts on same backend; mutation in A → visible in B."""
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    failures = ctx.failures
    artifact_dir = ctx.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug, _log = _open_logger(ctx.debug_log, "W10")

    if not failures.soft_assert(ctx.web_url is not None, "no web_url"):
        debug.close()
        return ScenarioRunResult(
            outcome="FAIL", note="no web_url",
            duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )

    _log(f"web_url={ctx.web_url}")
    _log(f"project_dir={ctx.project_dir}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx_a = browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(artifact_dir / "tab-a"),
        )
        ctx_b = browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(artifact_dir / "tab-b"),
        )
        try:
            ctx_a.tracing.start(screenshots=True, snapshots=True)
            ctx_b.tracing.start(screenshots=True, snapshots=True)
        except Exception as exc:  # noqa: BLE001
            failures.note(f"tracing.start failed: {exc}")

        page_a = ctx_a.new_page()
        page_b = ctx_b.new_page()
        captured_a = _capture_console_and_network(page_a, artifact_dir / "tab-a")
        captured_b = _capture_console_and_network(page_b, artifact_dir / "tab-b")

        try:
            # ---------- Step 1: both tabs goto + ready ----------
            _log("Step 1: load both tabs")
            for label, page in [("A", page_a), ("B", page_b)]:
                try:
                    page.goto(ctx.web_url, wait_until="domcontentloaded", timeout=30_000)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"page.goto failed (tab {label}): {exc}")
                try:
                    _wait_for_mc_ready(page)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"hydration failed (tab {label}): {exc}")
            _safe_screenshot(page_a, artifact_dir / "tab-a", "01-loaded")
            _safe_screenshot(page_b, artifact_dir / "tab-b", "01-loaded")

            # ---------- Step 2: enqueue from tab A via dialog ----------
            _log("Step 2: enqueue from tab A")
            ok = _enqueue_via_dialog(
                page_a, W10_INTENT,
                failures=failures, label="w10-A", artifact_dir=artifact_dir / "tab-a",
                screenshot_idx=2,
            )
            failures.soft_assert(ok, "tab A enqueue failed")
            t_submit = time.monotonic()
            _safe_screenshot(page_a, artifact_dir / "tab-a", "02-after-submit")

            # ---------- Step 2b: start watcher from tab A so the queued task
            # actually progresses to "running" — that is the realistic path
            # users will exercise before cancelling.
            _log("Step 2b: start watcher from tab A")
            time.sleep(1.5)
            _click_start_watcher(page_a, failures=failures, label="W10-A", base_url=ctx.web_url)
            _safe_screenshot(page_a, artifact_dir / "tab-a", "02b-watcher-started")

            # ---------- Step 3: tab B should pick up the new task within poll window ----------
            _log(f"Step 3: poll tab B for new task (≤{W10_PROPAGATION_TIMEOUT_S}s)")
            target_run_id: Optional[str] = None
            queue_task_id: Optional[str] = None
            propagation_s: Optional[float] = None

            # Use the API to get the run_id deterministically (the dialog
            # closing alone doesn't expose run_id), then poll tab B's DOM for
            # a row matching that id. Wait until the run reaches "running" so
            # the row gets a stable run_id (queue-compat:* sentinel ids may
            # not match what the task board actually renders).
            t0 = time.monotonic()
            running_seen = False
            while time.monotonic() - t0 < W10_PROPAGATION_TIMEOUT_S * 4:  # up to ~2 min
                state = _state(ctx.web_url)
                items = _live_items(state)
                queue_items = [it for it in items if it.get("domain") == "queue"]
                if queue_items:
                    # Prefer a "running" row if available (its run_id is real).
                    running = [it for it in queue_items if it.get("status") == "running"]
                    chosen = running[0] if running else queue_items[0]
                    target_run_id = chosen.get("run_id")
                    queue_task_id = chosen.get("queue_task_id")
                    if running:
                        running_seen = True
                        break
                time.sleep(1)
            _log(
                f"  target_run_id={target_run_id} task_id={queue_task_id} "
                f"running_seen={running_seen}"
            )
            failures.soft_assert(
                target_run_id is not None,
                f"backend never showed the enqueued job within {W10_PROPAGATION_TIMEOUT_S}s",
            )
            failures.soft_assert(
                running_seen,
                "queued row never transitioned to running (watcher may not be picking it up)",
            )

            # Now poll tab B for that row in DOM.
            if target_run_id is not None:
                deadline_b = time.monotonic() + W10_PROPAGATION_TIMEOUT_S
                appeared = False
                while time.monotonic() < deadline_b:
                    try:
                        # Look for ANY DOM evidence of the run id. Two paths:
                        # (1) a [data-run-id="X"] attribute on a task card.
                        # (2) the visible run id in any task-board cell.
                        count = page_b.evaluate(
                            "(rid) => {"
                            "  if (!rid) return 0;"
                            "  let n = document.querySelectorAll('[data-run-id=\"' + rid + '\"]').length;"
                            "  if (n) return n;"
                            "  const board = document.querySelector('[data-testid=\"task-board\"]');"
                            "  if (!board) return 0;"
                            "  return board.textContent.includes(rid) ? 1 : 0;"
                            "}",
                            target_run_id,
                        )
                        if count and count > 0:
                            appeared = True
                            propagation_s = time.monotonic() - t_submit
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(1)
                _log(f"  propagation_to_B s={propagation_s} appeared={appeared}")
                failures.soft_assert(
                    appeared,
                    f"tab B did not show run {target_run_id} within {W10_PROPAGATION_TIMEOUT_S}s",
                )
            _safe_screenshot(page_b, artifact_dir / "tab-b", "03-after-propagation")

            # ---------- Step 4: cancel from tab B through visible UI ----------
            _log("Step 4: cancel from tab B through visible UI")
            cancel_clicked = False
            if target_run_id is not None:
                cancel_clicked = _mc_click_run_action_from_ui(
                    page_b,
                    target_run_id,
                    "cancel",
                    failures=failures,
                    log_fn=_log,
                )
                failures.soft_assert(
                    cancel_clicked,
                    f"could not cancel run {target_run_id} from tab B through visible UI",
                )
            _safe_screenshot(page_b, artifact_dir / "tab-b", "04-after-cancel")

            # ---------- Step 5: tab A should reflect the cancellation ----------
            _log(f"Step 5: poll tab A for cancellation reflection (≤{W10_PROPAGATION_TIMEOUT_S}s)")
            cancel_seen_in_a = False
            t_cancel = time.monotonic()
            cancel_propagation_s: Optional[float] = None
            if target_run_id is not None:
                deadline_a = time.monotonic() + W10_PROPAGATION_TIMEOUT_S
                while time.monotonic() < deadline_a:
                    # 1) Backend truth via /api/state
                    state = _state(ctx.web_url)
                    history = _history_items(state)
                    live = _live_items(state)
                    matching_history = [
                        it for it in history if it.get("run_id") == target_run_id
                    ]
                    matching_live = [
                        it for it in live if it.get("run_id") == target_run_id
                    ]
                    backend_cancelled = bool(matching_history) and any(
                        it.get("terminal_outcome") in ("cancelled", "interrupted")
                        or it.get("status") == "cancelled"
                        for it in matching_history
                    )
                    # 2) Tab A DOM truth: row should either be gone from board
                    #    or carry a "cancelled" affordance.
                    try:
                        a_state = page_a.evaluate(
                            "(rid) => {"
                            "  const board = document.querySelector('[data-testid=\"task-board\"]');"
                            "  const row = board ? Array.from(board.querySelectorAll('*')).find("
                            "    el => (el.getAttribute && el.getAttribute('data-run-id') === rid)"
                            "          || (el.textContent && el.textContent.includes(rid))"
                            "  ) : null;"
                            "  if (!row) return {present: false, text: ''};"
                            "  return {present: true, text: (row.textContent || '').slice(0, 200)};"
                            "}",
                            target_run_id,
                        )
                    except Exception:  # noqa: BLE001
                        a_state = {"present": False, "text": ""}
                    a_says_cancelled = (
                        not a_state.get("present", False)
                        or "cancel" in (a_state.get("text") or "").lower()
                        or "interrupt" in (a_state.get("text") or "").lower()
                    )
                    if backend_cancelled and a_says_cancelled and not matching_live:
                        cancel_seen_in_a = True
                        cancel_propagation_s = time.monotonic() - t_cancel
                        break
                    time.sleep(1)
                _log(
                    f"  cancel_seen_in_a={cancel_seen_in_a} "
                    f"propagation_s={cancel_propagation_s}"
                )
                failures.soft_assert(
                    cancel_seen_in_a,
                    f"tab A did not reflect cancellation within "
                    f"{W10_PROPAGATION_TIMEOUT_S}s",
                )
            _safe_screenshot(page_a, artifact_dir / "tab-a", "05-after-cancel-reflected")

            # ---------- Step 6: wait for terminal (best-effort, may be quick) ----------
            _log(f"Step 6: wait ≤{W10_TERMINAL_TIMEOUT_S}s for terminal")
            terminal_outcome, terminal_run = _wait_for_terminal(
                ctx.web_url, timeout_s=W10_TERMINAL_TIMEOUT_S, log_fn=_log,
                domain_filter={"queue"},
            )
            _log(f"  terminal_outcome={terminal_outcome} run_id={terminal_run}")
            # The cancel may convert outcome to cancelled/interrupted, which
            # is the success path here. We don't fail on outcome.
            (artifact_dir / "metrics.json").write_text(
                json.dumps({
                    "target_run_id": target_run_id,
                    "queue_task_id": queue_task_id,
                    "propagation_to_B_s": propagation_s,
                    "cancel_clicked": cancel_clicked,
                    "cancel_propagation_to_A_s": cancel_propagation_s,
                    "terminal_outcome": terminal_outcome,
                }, indent=2),
                encoding="utf-8",
            )

            # final state snapshot
            state = _state(ctx.web_url)
            (artifact_dir / "final-state.json").write_text(
                json.dumps(state or {}, indent=2, default=str),
                encoding="utf-8",
            )

        finally:
            try:
                ctx_a.tracing.stop(path=str(artifact_dir / "tab-a-trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop A failed: {exc}")
            try:
                ctx_b.tracing.stop(path=str(artifact_dir / "tab-b-trace.zip"))
            except Exception as exc:  # noqa: BLE001
                failures.note(f"tracing.stop B failed: {exc}")
            _flush_captured(captured_a, artifact_dir / "tab-a")
            _flush_captured(captured_b, artifact_dir / "tab-b")
            for c in (ctx_a, ctx_b):
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    # Console errors gated separately per tab.
    for label, captured in [("A", captured_a), ("B", captured_b)]:
        if any(c.get("type") == "error" for c in captured["console"]):
            failures.fail(f"console errors during W10 (tab {label})")
        if captured["page_errors"]:
            failures.fail(f"page errors during W10 (tab {label}): {len(captured['page_errors'])}")
        if captured["network_errors"]:
            unexpected = [n for n in captured["network_errors"] if n.get("status") not in (404,)]
            if unexpected:
                failures.fail(
                    f"unexpected 4xx/5xx during W10 (tab {label}): {len(unexpected)}"
                )

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
        note=failures.summary(),
        duration_s=time.monotonic() - started,
        failures=list(failures.failures),
    )


# ---------------------------------------------------------------------------
# Scenario registry — every W1..W13 (W12 split into W12a, W12b) has an entry
# ---------------------------------------------------------------------------


SCENARIOS: dict[str, Scenario] = {
    "W1": Scenario(
        id="W1",
        description="First-time user — create project, submit kanban build, walk inspector tabs",
        tier="nightly",
        estimated_cost=1.0,
        estimated_seconds=8 * 60,
        needs_product_verification=True,
        target_recordings=["R1"],
        run_fn=_run_w1,
    ),
    "W2": Scenario(
        id="W2",
        description="Multi-job operator — submit 3 jobs, watcher, queue drain, cancel one",
        tier="weekly",
        estimated_cost=3.0,
        estimated_seconds=15 * 60,
        needs_product_verification=True,
        target_recordings=["R9", "R5"],
        run_fn=_run_w2,
    ),
    "W3": Scenario(
        id="W3",
        description="Improve loop via JobDialog — build-journal updates, improvement-report visible",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R4"],
        run_fn=_run_w3,
    ),
    "W4": Scenario(
        id="W4",
        description="Merge happy path — successful run → Merge → audit branch lands",
        tier="weekly",
        estimated_cost=1.0,
        estimated_seconds=6 * 60,
        needs_product_verification=True,
        target_recordings=["R5"],
        run_fn=_run_w4,
    ),
    "W5": Scenario(
        id="W5",
        description="Merge blocking — Merge blocked with clear reason",
        tier="weekly",
        estimated_cost=1.0,
        estimated_seconds=6 * 60,
        needs_product_verification=False,
        target_recordings=["R12"],
        run_fn=_run_w5,
    ),
    "W6": Scenario(
        id="W6",
        description="Deterministic failure + retry (after harness-side fixture correction)",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R2"],
        run_fn=_run_w6,
    ),
    "W7": Scenario(
        id="W7",
        description="iPhone live — W1 flow on devices['iPhone 14'] + webkit",
        tier="nightly",
        estimated_cost=1.0,
        estimated_seconds=8 * 60,
        needs_product_verification=True,
        target_recordings=["R1"],
        run_fn=_run_w7,
    ),
    "W8": Scenario(
        id="W8",
        description="Power-user keyboard-only — full W2 session via keyboard",
        tier="weekly",
        estimated_cost=3.0,
        estimated_seconds=15 * 60,
        needs_product_verification=True,
        target_recordings=["R9", "R5"],
        run_fn=_run_w8,
    ),
    "W9": Scenario(
        id="W9",
        description="Backgrounded tab — long build, switch tab, return, polling caught up",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=10 * 60,
        needs_product_verification=True,
        target_recordings=["R1.mid"],
        run_fn=_run_w9,
    ),
    "W10": Scenario(
        id="W10",
        description="Two-tab consistency — mutation in tab A visible in tab B within poll window",
        tier="weekly",
        estimated_cost=1.0,
        estimated_seconds=6 * 60,
        needs_product_verification=True,
        target_recordings=["R1.mid"],
        run_fn=_run_w10,
    ),
    "W11": Scenario(
        id="W11",
        description="Operator day (nightly core, N9-style) — CLI/web interop, queue, watcher, cancel, merge",
        tier="nightly",
        estimated_cost=5.0,
        estimated_seconds=25 * 60,
        needs_product_verification=True,
        target_recordings=["R5", "R9", "R10", "R14"],
        run_fn=_run_w11,
    ),
    "W12a": Scenario(
        id="W12a",
        description="CLI atomic run → web inspect → cancel/retry from UI (no merge — atomic doesn't expose it)",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R3"],
        run_fn=_run_w12a,
    ),
    "W12b": Scenario(
        id="W12b",
        description="CLI-queued task → web → start watcher → run → merge from UI",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R5", "R9"],
        run_fn=_run_w12b,
    ),
    "W13": Scenario(
        id="W13",
        description="Outage recovery — restart `otto web` mid-build, reopen browser, verify recovery",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R1.mid", "R1.post"],
        run_fn=_run_w13,
    ),
}


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def parse_csv_ids(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def select_scenarios(
    *,
    mode: Optional[str],
    scenario_csv: Optional[str],
    tier: Optional[str],
) -> list[Scenario]:
    selected_ids: Optional[list[str]] = None

    if scenario_csv:
        selected_ids = parse_csv_ids(scenario_csv)
    elif tier:
        if tier == "nightly":
            selected_ids = list(TIER_NIGHTLY)
        elif tier == "weekly":
            selected_ids = list(TIER_WEEKLY)
        else:
            raise SystemExit(f"unknown tier: {tier!r}")
    elif mode == "quick":
        selected_ids = list(QUICK_SCENARIOS)
    elif mode == "full":
        selected_ids = list(SCENARIOS)
    else:
        # default: quick
        selected_ids = list(QUICK_SCENARIOS)

    missing = [sid for sid in selected_ids if sid not in SCENARIOS]
    if missing:
        raise SystemExit(
            f"unknown scenario(s): {', '.join(missing)}. "
            f"Available: {', '.join(SCENARIOS)}"
        )
    ordered = [SCENARIOS[sid] for sid in SCENARIOS if sid in selected_ids]
    if not ordered:
        raise SystemExit("no scenarios selected")
    return ordered


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def print_scenario_list() -> None:
    print("Mission Control web-as-user scenarios (Phase 5)")
    print()
    print("ID    Tier    Cost    Secs   ProdVerify  Target R           Description")
    for scenario in SCENARIOS.values():
        impl = "STUB" if scenario.run_fn is _stub_scenario else "IMPL"
        targets = ",".join(scenario.target_recordings)
        print(
            f"{scenario.id:5} {scenario.tier:7} ${scenario.estimated_cost:>4.2f} "
            f"{scenario.estimated_seconds:>5}s  {('YES' if scenario.needs_product_verification else 'no'):>3}        "
            f"{targets:18} [{impl}] {scenario.description}"
        )
    print()
    print("Tiers:")
    print(f"  nightly = {' + '.join(TIER_NIGHTLY)}")
    print(f"  weekly  = {' + '.join(TIER_WEEKLY)}")
    print(f"  quick (--mode quick) = {' + '.join(QUICK_SCENARIOS)}")


# ---------------------------------------------------------------------------
# Per-scenario execution
# ---------------------------------------------------------------------------


@contextmanager
def _throwaway_project() -> Iterator[Path]:
    """Yield a fresh git-init'd project dir; clean up unless OTTO_KEEP_TMP=1."""
    project = Path(tempfile.mkdtemp(prefix="otto-mc-web-"))
    try:
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=project, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "web-as-user@example.com"],
            cwd=project, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Web As User"],
            cwd=project, check=True,
        )
        (project / "README.md").write_text("# web-as-user\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=project, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=project, check=True,
        )
        yield project
    finally:
        if os.environ.get("OTTO_KEEP_TMP") != "1":
            shutil.rmtree(project, ignore_errors=True)


def _queue_timeout_for_build_timeout(build_timeout_s: int) -> int:
    """Return a queue hard-kill guard that cannot preempt the harness timeout."""
    return max(build_timeout_s + 1200, int(build_timeout_s * 1.2))


def _terminal_wait_for_build_timeout(build_timeout_s: int) -> int:
    """Return the outer true-web wait for a run with this Otto build budget."""
    return _queue_timeout_for_build_timeout(build_timeout_s)


def _configure_throwaway_project(
    project_dir: Path,
    *,
    group_concurrent: int | None,
    build_timeout_s: int | None = None,
) -> None:
    if group_concurrent is None and build_timeout_s is None:
        return
    config_path = project_dir / "otto.yaml"
    lines: list[str] = []
    if build_timeout_s is not None:
        lines.extend(
            [
                f"run_budget_seconds: {build_timeout_s}",
                "queue:",
                f"  task_timeout_s: {_queue_timeout_for_build_timeout(build_timeout_s)}",
            ]
        )
    if group_concurrent is not None:
        lines.extend(
            [
                "build:",
                f"  group_concurrent: {group_concurrent}",
            ]
        )
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "otto.yaml"], cwd=project_dir, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: configure Otto web-as-user run"],
        cwd=project_dir,
        check=True,
    )


def _project_processes(project_dir: Path) -> list[dict[str, Any]]:
    """Return processes whose command line still references this scenario project."""
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid,ppid,pgid,command"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    needle = str(project_dir)
    current_pid = os.getpid()
    rows: list[dict[str, Any]] = []
    for raw in (proc.stdout or "").splitlines()[1:]:
        parts = raw.strip().split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
        except ValueError:
            continue
        command = parts[3]
        if pid == current_pid or needle not in command:
            continue
        rows.append({"pid": pid, "ppid": ppid, "pgid": pgid, "command": command})
    return rows


def _terminate_project_processes(project_dir: Path, *, grace_s: float = 3.0) -> dict[str, Any]:
    """Best-effort cleanup for watcher/provider children before temp deletion."""
    report: dict[str, Any] = {"matched": []}
    children = _project_processes(project_dir)
    report["matched"] = children
    if not children:
        return report

    pgids = sorted({int(child["pgid"]) for child in children if int(child["pgid"]) > 0})
    report["signalled_pgids"] = pgids
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            report.setdefault("term_errors", []).append({"pgid": pgid, "error": str(exc)})

    deadline = time.monotonic() + max(0.0, grace_s)
    live = _project_processes(project_dir)
    while live and time.monotonic() < deadline:
        time.sleep(0.1)
        live = _project_processes(project_dir)
    report["after_sigterm"] = live
    if not live:
        return report

    live_pgids = sorted({int(child["pgid"]) for child in live if int(child["pgid"]) > 0})
    report["killed_pgids"] = live_pgids
    for pgid in live_pgids:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            report.setdefault("kill_errors", []).append({"pgid": pgid, "error": str(exc)})
    time.sleep(0.2)
    report["after_sigkill"] = _project_processes(project_dir)
    return report


def _preserve_failed_project_snapshot(project_dir: Path, artifact_dir: Path) -> dict[str, Any]:
    """Copy useful failure evidence before deleting the throwaway project."""
    snapshot = artifact_dir / "project-snapshot"
    report: dict[str, Any] = {"snapshot": str(snapshot), "copied": False}
    if not project_dir.exists():
        report["error"] = "project_dir missing"
        return report
    if snapshot.exists():
        shutil.rmtree(snapshot, ignore_errors=True)

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = {
            ".git",
            "node_modules",
            "dist",
            "build",
            "coverage",
            ".pytest_cache",
            "__pycache__",
        }
        return {name for name in names if name in ignored}

    try:
        shutil.copytree(project_dir, snapshot, ignore=_ignore)
        report["copied"] = True
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


def _teardown_scenario_runtime(
    *,
    project_dir: Path,
    artifact_dir: Path,
    web_url: str | None,
    keep_snapshot: bool,
) -> dict[str, Any]:
    """Stop live Otto work before backend/tempdir teardown."""
    report: dict[str, Any] = {}
    if keep_snapshot:
        report["snapshot"] = _preserve_failed_project_snapshot(project_dir, artifact_dir)
    if web_url:
        status, body = _api_post(web_url, "/api/watcher/stop", {}, timeout=10.0)
        report["watcher_stop_api"] = {"status": status, "body": body}
    try:
        from otto.mission_control.service import terminate_watcher_blocking

        report["terminate_watcher_blocking"] = terminate_watcher_blocking(
            project_dir,
            grace=5.0,
            reason="web-as-user teardown",
        )
    except Exception as exc:  # noqa: BLE001
        report["terminate_watcher_blocking_error"] = str(exc)
    report["project_process_cleanup"] = _terminate_project_processes(project_dir)
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "teardown.json").write_text(
            json.dumps(report, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass
    return report


def classify_failure(failures: RunFailures) -> FailureClassification:
    """Classify whether a run's failures are INFRA or genuine FAIL."""
    text = "\n".join(failures.failures).lower()
    if _text_is_provider_infra_failure(text):
        return "INFRA"
    return "FAIL"


def run_one_scenario(
    scenario: Scenario,
    *,
    run_id: str,
    provider: str,
    dry_run: bool,
    user_behavior: UserBehaviorMode,
    user_seed: Optional[int],
    intent_override: Optional[str] = None,
    build_timeout_s: Optional[int] = None,
    group_concurrent: Optional[int] = None,
    keep_heavy_artifacts: bool = False,
) -> ScenarioOutcome:
    """Drive one W-scenario end-to-end.

    The orchestrator wires up:
      - `otto web` server start (atomic free-port bind via
        `tests/browser/_helpers/server.start_backend` if importable, or
        an `otto web --port 0` subprocess).
      - Playwright trace start: `tracing.start(screenshots=True,
        snapshots=True, sources=True)`.
      - Process-group teardown so no orphan processes survive.
    """
    started = time.monotonic()
    artifact_dir = DEFAULT_ARTIFACT_ROOT / run_id / scenario.id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    debug_log = artifact_dir / "debug.log"

    if dry_run:
        # Dry-run: skip browser + LLM. Verify wiring (Playwright importable
        # if expected, fixture dir creatable, scenario callable shape).
        outcome = "PASS"
        note = "dry-run: skipped real LLM and Playwright invocation"
        return ScenarioOutcome(
            scenario_id=scenario.id,
            description=scenario.description,
            outcome=outcome,
            note=note,
            artifact_dir=artifact_dir,
            wall_duration_s=time.monotonic() - started,
        )

    failures = RunFailures()
    with _throwaway_project() as project_dir:
        _configure_throwaway_project(
            project_dir,
            group_concurrent=group_concurrent,
            build_timeout_s=build_timeout_s,
        )
        # Start the in-process FastAPI backend for the duration of the scenario.
        backend = None
        try:
            backend = _start_otto_web_in_process(
                project_dir,
                artifact_dir,
                project_launcher=scenario.id in {"W1", "W7"},
            )
        except Exception as exc:  # noqa: BLE001
            failures.fail(f"failed to start in-process otto web backend: {exc}")

        ctx = ScenarioContext(
            scenario=scenario,
            project_dir=project_dir,
            artifact_dir=artifact_dir,
            provider=provider,
            failures=failures,
            debug_log=debug_log,
            run_id=run_id,
            user_behavior=user_behavior,
            user_seed=user_seed,
            web_port=getattr(backend, "port", None),
            web_url=getattr(backend, "url", None),
            backend=backend,
            intent_override=intent_override,
            build_timeout_s=build_timeout_s,
            group_concurrent=group_concurrent,
        )
        _record_mc_user_action(ctx, phase="scenario-start", action="config")
        result: Optional[ScenarioRunResult] = None
        note = "scenario did not run"
        try:
            if backend is not None:
                result = scenario.run_fn(ctx)
                note = result.note or "scenario phases completed"
                for failure in result.failures:
                    if failure not in failures.failures:
                        failures.fail(failure)
                if (
                    scenario.needs_product_verification
                    and not (artifact_dir / "product-verification").exists()
                ):
                    failures.fail(
                        "scenario is marked needs_product_verification but never ran "
                        "the generated-product browser verification step"
                    )
            else:
                note = "backend unavailable; scenario not run"
        except NotImplementedError as exc:
            note = f"stub: {exc}"
            failures.fail(f"NotImplementedError: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.fail(f"unhandled exception in scenario body: {exc}")
            note = "scenario body raised — collected via failures + auto-mine"
        finally:
            # Phase 5H: artifact-mine pass runs regardless of outcome.
            try:
                artifact_mine_pass(project_dir, failures)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"artifact_mine_pass crashed: {exc}")
            teardown_report = _teardown_scenario_runtime(
                project_dir=project_dir,
                artifact_dir=artifact_dir,
                web_url=getattr(backend, "url", None),
                keep_snapshot=(
                    bool(failures.failures)
                    or (
                        result is not None
                        and result.outcome in {"FAIL", "INFRA"}
                    )
                ),
            )
            cleanup_report = teardown_report.get("project_process_cleanup")
            if isinstance(cleanup_report, dict) and cleanup_report.get("after_sigkill"):
                failures.fail(
                    "web-as-user teardown left live project processes after SIGKILL; "
                    "see teardown.json"
                )
            # Stop the in-process backend.
            if backend is not None:
                try:
                    backend.stop()
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"backend.stop raised: {exc}")

    explicit_outcome = result.outcome if result is not None else None
    cleanup_heavy_browser_artifacts(artifact_dir, keep=keep_heavy_artifacts)

    if failures.failures or (explicit_outcome is not None and explicit_outcome != "PASS"):
        classification = (
            explicit_outcome
            if explicit_outcome in ("FAIL", "INFRA")
            else classify_failure(failures)
        )
        return ScenarioOutcome(
            scenario_id=scenario.id,
            description=scenario.description,
            outcome=classification,
            note=failures.summary() if failures.failures else note,
            artifact_dir=artifact_dir,
            wall_duration_s=time.monotonic() - started,
            failures=list(failures.failures),
        )
    return ScenarioOutcome(
        scenario_id=scenario.id,
        description=scenario.description,
        outcome="PASS",
        note=note,
        artifact_dir=artifact_dir,
        wall_duration_s=time.monotonic() - started,
    )


def maybe_prune_artifacts(
    artifact_dir: Path, *, keep_failed_only: bool, passed: bool
) -> None:
    if not keep_failed_only or not passed:
        return
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir, ignore_errors=True)


def _is_heavy_browser_artifact(path: Path) -> bool:
    name = path.name.lower()
    if name in HEAVY_BROWSER_TRACE_NAMES or name.endswith("-trace.zip"):
        return True
    return path.suffix.lower() in HEAVY_BROWSER_ARTIFACT_SUFFIXES


def cleanup_heavy_browser_artifacts(artifact_dir: Path, *, keep: bool = False) -> dict[str, Any]:
    """Prune bulky trace/HAR files after evidence JSON/screenshots/video are saved."""
    report: dict[str, Any] = {
        "kept": bool(keep),
        "deleted": [],
        "bytes_deleted": 0,
    }
    if keep or not artifact_dir.exists():
        return report
    for path in sorted(artifact_dir.rglob("*")):
        if not path.is_file() or not _is_heavy_browser_artifact(path):
            continue
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as exc:
            report.setdefault("errors", []).append({"path": str(path), "error": str(exc)})
            continue
        report["bytes_deleted"] += size
        report["deleted"].append(
            {
                "path": str(path.relative_to(artifact_dir)),
                "bytes": size,
            }
        )
    try:
        (artifact_dir / "artifact-cleanup.json").write_text(
            json.dumps(report, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass
    return report


def utc_run_id() -> str:
    return time.strftime("%Y-%m-%d-%H%M%S", time.gmtime()) + "-" + os.urandom(3).hex()


def print_summary(run_id: str, outcomes: list[ScenarioOutcome]) -> int:
    print()
    print(f"web-as-user run {run_id}")
    print(f"scenarios: {len(outcomes)}")
    print()
    print("ID    Outcome  Duration  Note")
    for outcome in outcomes:
        print(
            f"{outcome.scenario_id:5} {outcome.outcome:7} "
            f"{int(outcome.wall_duration_s):>4}s     {outcome.note}"
        )
    failed = [o for o in outcomes if o.outcome == "FAIL"]
    not_passed = [o for o in outcomes if o.outcome != "PASS"]
    print()
    print(
        f"Totals: {len(outcomes)} scenarios, "
        f"{sum(1 for o in outcomes if o.outcome == 'PASS')} PASS, "
        f"{len(failed)} FAIL, "
        f"{sum(1 for o in outcomes if o.outcome == 'INFRA')} INFRA"
    )
    return 1 if not_passed else 0


# ---------------------------------------------------------------------------
# Process-group cleanup
# ---------------------------------------------------------------------------


def _install_signal_handlers() -> None:
    """Best-effort process-group cleanup on SIGTERM/SIGINT (Phase 5E)."""
    handling_signal = False

    def _handle(signum: int, frame: Any) -> None:  # noqa: ANN401
        nonlocal handling_signal
        if handling_signal:
            raise SystemExit(130 if signum == signal.SIGINT else 143)
        handling_signal = True
        try:
            signal.signal(signum, signal.SIG_IGN)
        except (ValueError, OSError):  # not on main thread / unsupported
            pass
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        raise SystemExit(130 if signum == signal.SIGINT else 143)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):  # not on main thread / unsupported
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Mission Control true-web as-user scenarios with real browser automation + real LLM."
    )
    parser.add_argument("--list", action="store_true", help="enumerate scenarios + tiers and exit")
    parser.add_argument(
        "--mode",
        choices=["quick", "full"],
        help="quick = W1+W11; full = all 14 scenarios",
    )
    parser.add_argument(
        "--scenario", help="comma-separated scenario IDs, e.g. W1,W7,W11"
    )
    parser.add_argument(
        "--tier",
        choices=["nightly", "weekly"],
        help="nightly = W11+W1+W7; weekly = all 14",
    )
    parser.add_argument(
        "--provider",
        choices=["codex-app-server", "codex", "claude"],
        default="codex-app-server",
    )
    parser.add_argument(
        "--user-behavior",
        choices=["off", "mc-realistic", "mc-stress"],
        default="mc-realistic",
        help=(
            "seeded Mission Control user exploration; defaults to the true-web "
            "mc-realistic path for normal user confidence (or use mc-stress/off)"
        ),
    )
    parser.add_argument(
        "--user-seed",
        type=int,
        help="seed for replaying --user-behavior choices",
    )
    parser.add_argument(
        "--intent",
        help=(
            "override the primary build intent for single-project scenarios "
            "such as W1/W7; useful for pressure-testing varied projects"
        ),
    )
    parser.add_argument(
        "--build-timeout-s",
        type=int,
        help=(
            "override the Otto build budget for scenarios that support it; "
            "throwaway true-web projects also inherit this as the visible run "
            "budget and get larger queue hard-kill / harness terminal guards"
        ),
    )
    parser.add_argument(
        "--group-concurrent",
        type=int,
        help="write build.group_concurrent into the throwaway project before starting Otto",
    )
    parser.add_argument(
        "--scenario-delay",
        type=float,
        default=DEFAULT_SCENARIO_DELAY_S,
        help=f"seconds to sleep between scenarios (default {DEFAULT_SCENARIO_DELAY_S})",
    )
    parser.add_argument(
        "--bail-fast",
        action="store_true",
        help="stop on first FAIL (INFRA failures don't trigger bail)",
    )
    parser.add_argument(
        "--keep-failed-only",
        action="store_true",
        help="only keep artifacts for FAIL/INFRA scenarios",
    )
    parser.add_argument(
        "--keep-heavy-browser-artifacts",
        action="store_true",
        help="keep Playwright trace/HAR bundles instead of pruning them after the run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="skip Playwright + LLM; verify harness wiring only",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.list:
        print_scenario_list()
        return 0

    if args.scenario_delay < 0:
        raise SystemExit("--scenario-delay must be >= 0")
    if args.user_seed is not None and args.user_seed < 0:
        raise SystemExit("--user-seed must be >= 0")
    if args.build_timeout_s is not None and args.build_timeout_s <= 0:
        raise SystemExit("--build-timeout-s must be > 0")
    if args.group_concurrent is not None and args.group_concurrent <= 0:
        raise SystemExit("--group-concurrent must be > 0")

    if not args.dry_run:
        try:
            require_real_cost_opt_in("web-as-user live scenario")
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 2

    _install_signal_handlers()

    scenarios = select_scenarios(
        mode=args.mode, scenario_csv=args.scenario, tier=args.tier
    )
    run_id = utc_run_id()
    outcomes: list[ScenarioOutcome] = []
    for index, scenario in enumerate(scenarios):
        print(f"\n=== {scenario.id} {scenario.description} ===", flush=True)
        try:
            outcome = run_one_scenario(
                scenario,
                run_id=run_id,
                provider=args.provider,
                dry_run=args.dry_run,
                user_behavior=args.user_behavior,
                user_seed=args.user_seed,
                intent_override=args.intent,
                build_timeout_s=args.build_timeout_s,
                group_concurrent=args.group_concurrent,
                keep_heavy_artifacts=args.keep_heavy_browser_artifacts,
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            outcome = ScenarioOutcome(
                scenario_id=scenario.id,
                description=scenario.description,
                outcome="FAIL",
                note=f"unhandled exception: {exc}",
                artifact_dir=DEFAULT_ARTIFACT_ROOT / run_id / scenario.id,
                wall_duration_s=0.0,
            )
        outcomes.append(outcome)
        maybe_prune_artifacts(
            outcome.artifact_dir,
            keep_failed_only=args.keep_failed_only,
            passed=outcome.outcome == "PASS",
        )
        print(f"[{scenario.id}] {outcome.outcome}: {outcome.note}", flush=True)
        if args.bail_fast and outcome.outcome != "PASS":
            break
        if index < len(scenarios) - 1 and args.scenario_delay > 0:
            print(
                f"[scenario-delay] sleeping {args.scenario_delay:.1f}s before next scenario",
                flush=True,
            )
            time.sleep(args.scenario_delay)

    return print_summary(run_id, outcomes)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
