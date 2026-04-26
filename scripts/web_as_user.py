#!/usr/bin/env python3
"""Phase 5 live web-as-user harness for Mission Control.

Drives a real browser (Playwright) against a real `otto web` server backed by
a throwaway git project running real LLM builds. Mirrors the patterns in
`scripts/otto_as_user.py` (TUI harness) and `scripts/otto_as_user_nightly.py`
(N9 operator-day pattern that W11 emulates).

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
    python scripts/web_as_user.py --mode full --provider claude
    python scripts/web_as_user.py --scenario W11
    python scripts/web_as_user.py --scenario W1,W7,W11 --scenario-delay 10
    python scripts/web_as_user.py --tier nightly  # W11 + W1 + W7
    python scripts/web_as_user.py --tier weekly   # all W1..W13
    python scripts/web_as_user.py --dry-run --scenario W1
    python scripts/web_as_user.py --bail-fast --scenario W11
    python scripts/web_as_user.py --keep-failed-only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from real_cost_guard import real_cost_allowed, require_real_cost_opt_in  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "bench-results" / "web-as-user"
DEFAULT_SCENARIO_DELAY_S = 5.0
INFRA_RETRY_DELAY_S = 30.0
INFRA_RETRY_ATTEMPTS = 2
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"

ScenarioStatus = Literal["PASS", "FAIL", "INFRA"]
FailureClassification = Literal["INFRA", "FAIL"]


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
      - assert `.gitignore` (if present) excludes `otto_logs/`
    """
    from otto import paths

    queue_state = paths.queue_state_path(project_dir)
    if queue_state.exists():
        try:
            state = json.loads(queue_state.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.fail(f"queue state JSON malformed at {queue_state}: {exc}")
            state = {}
        for task_id in (state.get("tasks") or {}):
            try:
                manifest = paths.queue_manifest_path(project_dir, task_id)
            except ValueError as exc:
                failures.fail(f"queue task ID {task_id!r} invalid: {exc}")
                continue
            if not manifest.is_file():
                failures.fail(
                    f"queue task {task_id!r} listed in state but has no manifest at {manifest}"
                )

    live_runs = paths.live_runs_dir(project_dir)
    if live_runs.is_dir():
        for live_path in live_runs.glob("*.json"):
            run_id = live_path.stem
            sess = paths.session_dir(project_dir, run_id)
            if not sess.exists():
                failures.fail(
                    f"live run {run_id!r} has no session dir at {sess}"
                )

    # Orchestrator extends with worktree + gitignore + leakage checks.


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
    web_port: Optional[int] = None
    web_url: Optional[str] = None


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

# How long the harness will wait for the LLM build to reach a terminal state
# before bailing. Per scenario plan: W1 ~8 min, W11 ~25 min. Add a margin so
# slow-but-completing runs aren't forced into FAIL.
W1_BUILD_TIMEOUT_S = 12 * 60
W11_BUILD_TIMEOUT_S = 25 * 60


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


def _safe_screenshot(page: Any, artifact_dir: Path, name: str) -> Optional[Path]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:  # noqa: BLE001
        return None


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


def _start_otto_web_in_process(project_dir: Path, artifact_dir: Path) -> Any:
    """Bring up otto's FastAPI in a thread on a free port; return the handle."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests.browser._helpers.build_bundle import ensure_bundle_built  # type: ignore

    ensure_bundle_built()
    start_backend = _import_start_backend()
    projects_root = artifact_dir / "managed-projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    return start_backend(project_dir, projects_root=projects_root, project_launcher=False)


def _provider_extra_args(provider: str) -> list[str]:
    return ["--provider", provider]


def _stub_scenario(ctx: ScenarioContext) -> ScenarioRunResult:
    raise NotImplementedError(
        f"scenario {ctx.scenario.id} not yet implemented; see plan-mc-audit.md 5B"
    )


def _run_w1(ctx: ScenarioContext) -> ScenarioRunResult:
    """W1 — first-time user end-to-end via Playwright + real LLM build."""
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

    final_state: Optional[dict[str, Any]] = None

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

            # Wait for hydration
            try:
                page.wait_for_function(
                    "document.querySelector('#root')?.children.length > 0", timeout=15_000
                )
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"react never hydrated: {exc}")

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

            # ---------- Step 6: submit intent ----------
            _log("Step 6: fill intent + submit")
            intent_field = page.locator('[data-testid="job-dialog-intent"]')
            if intent_field.count() > 0:
                try:
                    intent_field.fill(W1_INTENT, timeout=5_000)
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

            submit = page.locator('[data-testid="job-dialog-submit-button"]')
            if not failures.soft_assert(
                submit.count() > 0, "submit button missing in JobDialog"
            ):
                _safe_screenshot(page, artifact_dir, "05-no-submit")
            else:
                try:
                    submit.click(timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"submit click failed: {exc}")

            _safe_screenshot(page, artifact_dir, "05-submitted")

            # ---------- Step 7: start watcher (queue won't run otherwise) ----------
            _log("Step 7: start watcher")
            time.sleep(1.5)  # let dialog close + state refresh
            start_watcher = page.locator(
                '[data-testid="mission-start-watcher-button"], [data-testid="start-watcher-button"]'
            ).first
            if start_watcher.count() == 0:
                failures.fail("no start-watcher button after submitting first job")
            else:
                # Watch out: button may be disabled until queue has items
                start_attempt_deadline = time.monotonic() + 30
                while time.monotonic() < start_attempt_deadline:
                    try:
                        if start_watcher.is_enabled():
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.5)
                try:
                    start_watcher.click(timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"start watcher click failed: {exc}")
            _safe_screenshot(page, artifact_dir, "06-watcher-started")

            # ---------- Step 8: wait for terminal state ----------
            _log("Step 8: poll /api/state for terminal status")
            deadline = time.monotonic() + W1_BUILD_TIMEOUT_S
            terminal_outcome: Optional[str] = None
            poll_count = 0
            while time.monotonic() < deadline:
                poll_count += 1
                status, body = _api_get(ctx.web_url, "/api/state")
                if status != 200 or not isinstance(body, dict):
                    if poll_count % 6 == 0:
                        _log(f"/api/state returned status={status}")
                    time.sleep(5)
                    continue
                # /api/state schema: live.items[]/history.items[] flat dicts.
                history = (body.get("history") or {}).get("items") or []
                live = (body.get("live") or {}).get("items") or []
                live_statuses = [item.get("status") for item in live]
                history_outcomes = [item.get("terminal_outcome") for item in history]
                if poll_count % 12 == 1:  # ~every minute
                    _log(
                        f"poll#{poll_count}: live={live_statuses[:3]} history_outcomes={history_outcomes[:3]}"
                    )
                if history and any(o for o in history_outcomes):
                    terminal_outcome = next(
                        (o for o in history_outcomes if o), None
                    )
                    break
                time.sleep(5)
            _log(f"terminal_outcome={terminal_outcome}")
            failures.soft_assert(
                terminal_outcome is not None,
                f"build did not reach terminal in {W1_BUILD_TIMEOUT_S}s",
            )
            if terminal_outcome and terminal_outcome != "success":
                failures.fail(f"build terminal_outcome={terminal_outcome!r} (expected success)")
            _safe_screenshot(page, artifact_dir, "07-build-terminal")

            # ---------- Step 9: refresh page + final state ----------
            try:
                page.reload(wait_until="networkidle", timeout=15_000)
            except Exception as exc:  # noqa: BLE001
                failures.note(f"reload after build failed: {exc}")
            _safe_screenshot(page, artifact_dir, "08-after-reload")

            # ---------- Step 10: walk inspector tabs ----------
            _log("Step 10: walk inspector tabs")
            try:
                # Click first task card if present
                task_cards = page.locator(".task-card-main").first
                if task_cards.count() > 0 and task_cards.is_enabled():
                    try:
                        task_cards.click(timeout=5_000)
                    except Exception as exc:  # noqa: BLE001
                        failures.note(f"task-card click failed: {exc}")
                    time.sleep(1)
                # Walk tabs
                for tab_name, testid in [
                    ("Logs", "open-logs-button"),
                    ("Diff", "open-diff-button"),
                    ("Proof", "open-proof-button"),
                    ("Artifacts", "open-artifacts-button"),
                ]:
                    btn = page.locator(f'[data-testid="{testid}"]')
                    if btn.count() == 0:
                        failures.note(f"tab button {tab_name} ({testid}) not present")
                        continue
                    try:
                        if not btn.is_enabled():
                            failures.note(f"tab button {tab_name} disabled — skipped")
                            continue
                        btn.click(timeout=5_000)
                        time.sleep(1)
                        _safe_screenshot(page, artifact_dir, f"09-tab-{tab_name.lower()}")
                    except Exception as exc:  # noqa: BLE001
                        failures.fail(f"tab {tab_name} interaction failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"walking inspector tabs raised: {exc}")

            # ---------- Step 11: product verification (best-effort) ----------
            # The kanban built into ctx.project_dir's queue worktree — we
            # don't auto-launch the SPA. Just record what files were created.
            _log("Step 11: product verification (best-effort)")
            try:
                file_list = []
                for path in sorted(ctx.project_dir.rglob("*"))[:200]:
                    if path.is_file() and ".git" not in path.parts and "otto_logs" not in path.parts:
                        rel = path.relative_to(ctx.project_dir)
                        file_list.append(str(rel))
                (artifact_dir / "project-files.txt").write_text(
                    "\n".join(file_list), encoding="utf-8"
                )
                # Check for any html/js/jsx/tsx/py files as a sign that something was built
                created = [
                    f for f in file_list
                    if f.endswith((".html", ".js", ".jsx", ".tsx", ".css", ".py"))
                ]
                failures.soft_assert(
                    len(created) > 0 or terminal_outcome != "success",
                    "build reported success but no source files appeared in project_dir",
                )
            except Exception as exc:  # noqa: BLE001
                failures.note(f"file scan raised: {exc}")

            # Final state snapshot
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
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    # Treat console errors as soft failures (user-facing UX bugs)
    if captured["console"]:
        errors = [
            c for c in captured["console"] if c.get("type") in ("error",)
        ]
        if errors:
            failures.fail(
                f"console errors during W1 ({len(errors)}); see console.json"
            )
    if captured["page_errors"]:
        failures.fail(f"page errors during W1: {len(captured['page_errors'])}")
    if captured["network_errors"]:
        # Filter out expected 404s (e.g. before run exists)
        unexpected = [
            n for n in captured["network_errors"]
            if n.get("status") not in (404,)
        ]
        if unexpected:
            failures.fail(f"unexpected 4xx/5xx responses: {len(unexpected)}; see network-errors.json")

    debug.close()
    return ScenarioRunResult(
        outcome="PASS" if not failures.failures else "FAIL",
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
                page.wait_for_function(
                    "document.querySelector('#root')?.children.length > 0", timeout=15_000
                )
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
            start_watcher = page.locator(
                '[data-testid="mission-start-watcher-button"], [data-testid="start-watcher-button"]'
            ).first
            if start_watcher.count() == 0:
                failures.fail("start-watcher button missing")
            else:
                # Wait for it to enable
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        if start_watcher.is_enabled():
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.5)
                try:
                    start_watcher.click(timeout=5_000)
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"start watcher click failed: {exc}")
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
            _log("Step 8: cancel one queue task via API actions")
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
                # POST /api/runs/<run_id>/actions/cancel
                import urllib.request

                req = urllib.request.Request(
                    ctx.web_url.rstrip("/") + f"/api/runs/{cancelled_run_id}/actions/cancel",
                    data=b"{}", method="POST",
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        body = resp.read().decode("utf-8")
                        _log(f"  cancel response status={resp.status}: {body[:200]}")
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"cancel POST failed: {exc}")

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
                import urllib.request

                # First check legal_actions includes merge
                _, detail_body = _api_get(
                    ctx.web_url, f"/api/runs/{merge_target_run_id}"
                )
                detail = detail_body if isinstance(detail_body, dict) else {}
                req = urllib.request.Request(
                    ctx.web_url.rstrip("/") + f"/api/runs/{merge_target_run_id}/actions/merge",
                    data=b"{}", method="POST",
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        body = resp.read().decode("utf-8")
                        _log(f"  merge response status={resp.status}: {body[:200]}")
                        merge_status = resp.status
                except Exception as exc:  # noqa: BLE001
                    failures.fail(f"merge POST failed: {exc}")
                    merge_status = 0
                # Wait briefly for merge live record
                if merge_status == 200:
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
        run_fn=_stub_scenario,
    ),
    "W3": Scenario(
        id="W3",
        description="Improve loop via JobDialog — build-journal updates, improvement-report visible",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R4"],
        run_fn=_stub_scenario,
    ),
    "W4": Scenario(
        id="W4",
        description="Merge happy path — successful run → Merge → audit branch lands",
        tier="weekly",
        estimated_cost=1.0,
        estimated_seconds=6 * 60,
        needs_product_verification=True,
        target_recordings=["R5"],
        run_fn=_stub_scenario,
    ),
    "W5": Scenario(
        id="W5",
        description="Merge blocking — Merge blocked with clear reason",
        tier="weekly",
        estimated_cost=1.0,
        estimated_seconds=6 * 60,
        needs_product_verification=False,
        target_recordings=["R12"],
        run_fn=_stub_scenario,
    ),
    "W6": Scenario(
        id="W6",
        description="Deterministic failure + retry (after harness-side fixture correction)",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R2"],
        run_fn=_stub_scenario,
    ),
    "W7": Scenario(
        id="W7",
        description="iPhone live — W1 flow on devices['iPhone 14'] + webkit",
        tier="nightly",
        estimated_cost=1.0,
        estimated_seconds=8 * 60,
        needs_product_verification=True,
        target_recordings=["R1"],
        run_fn=_stub_scenario,
    ),
    "W8": Scenario(
        id="W8",
        description="Power-user keyboard-only — full W2 session via keyboard",
        tier="weekly",
        estimated_cost=3.0,
        estimated_seconds=15 * 60,
        needs_product_verification=True,
        target_recordings=["R9", "R5"],
        run_fn=_stub_scenario,
    ),
    "W9": Scenario(
        id="W9",
        description="Backgrounded tab — long build, switch tab, return, polling caught up",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=10 * 60,
        needs_product_verification=True,
        target_recordings=["R1.mid"],
        run_fn=_stub_scenario,
    ),
    "W10": Scenario(
        id="W10",
        description="Two-tab consistency — mutation in tab A visible in tab B within poll window",
        tier="weekly",
        estimated_cost=1.0,
        estimated_seconds=6 * 60,
        needs_product_verification=True,
        target_recordings=["R1.mid"],
        run_fn=_stub_scenario,
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
        run_fn=_stub_scenario,
    ),
    "W12b": Scenario(
        id="W12b",
        description="CLI-queued task → web → start watcher → run → merge from UI",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R5", "R9"],
        run_fn=_stub_scenario,
    ),
    "W13": Scenario(
        id="W13",
        description="Outage recovery — restart `otto web` mid-build, reopen browser, verify recovery",
        tier="weekly",
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
        needs_product_verification=True,
        target_recordings=["R1.mid", "R1.post"],
        run_fn=_stub_scenario,
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


def classify_failure(failures: RunFailures) -> FailureClassification:
    """Classify whether a run's failures are INFRA or genuine FAIL."""
    text = "\n".join(failures.failures).lower()
    if "rate limit" in text or "429" in text or "throttle" in text:
        return "INFRA"
    if "not logged in" in text or "please run /login" in text:
        return "INFRA"
    return "FAIL"


def run_one_scenario(
    scenario: Scenario,
    *,
    run_id: str,
    provider: str,
    dry_run: bool,
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
        # Start the in-process FastAPI backend for the duration of the scenario.
        backend = None
        try:
            backend = _start_otto_web_in_process(project_dir, artifact_dir)
        except Exception as exc:  # noqa: BLE001
            failures.fail(f"failed to start in-process otto web backend: {exc}")

        ctx = ScenarioContext(
            scenario=scenario,
            project_dir=project_dir,
            artifact_dir=artifact_dir,
            provider=provider,
            failures=failures,
            debug_log=debug_log,
            web_port=getattr(backend, "port", None),
            web_url=getattr(backend, "url", None),
        )
        try:
            if backend is not None:
                scenario.run_fn(ctx)
            note = "scenario phases completed"
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
            # Stop the in-process backend.
            if backend is not None:
                try:
                    backend.stop()
                except Exception as exc:  # noqa: BLE001
                    failures.note(f"backend.stop raised: {exc}")

    if failures.failures:
        classification = classify_failure(failures)
        return ScenarioOutcome(
            scenario_id=scenario.id,
            description=scenario.description,
            outcome=classification,
            note=failures.summary(),
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
    print()
    print(
        f"Totals: {len(outcomes)} scenarios, "
        f"{sum(1 for o in outcomes if o.outcome == 'PASS')} PASS, "
        f"{len(failed)} FAIL, "
        f"{sum(1 for o in outcomes if o.outcome == 'INFRA')} INFRA"
    )
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Process-group cleanup
# ---------------------------------------------------------------------------


def _install_signal_handlers() -> None:
    """Best-effort process-group cleanup on SIGTERM/SIGINT (Phase 5E)."""

    def _handle(signum: int, frame: Any) -> None:  # noqa: ANN401
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
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
        description="Run Mission Control web-as-user scenarios with Playwright + real LLM."
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
        "--provider", choices=["claude", "codex"], default="claude"
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
        if args.bail_fast and outcome.outcome == "FAIL":
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
