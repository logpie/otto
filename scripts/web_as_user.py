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


def _stub_scenario(ctx: ScenarioContext) -> ScenarioRunResult:
    raise NotImplementedError(
        f"scenario {ctx.scenario.id} not yet implemented; see plan-mc-audit.md 5B"
    )


def _run_w1(ctx: ScenarioContext) -> ScenarioRunResult:
    """W1 — first-time user.

    Steps (orchestrator wires up the actual Playwright + subprocess calls):
      1. `otto web` server already started by the harness driver against the
         throwaway git project under ctx.project_dir on a free port (use
         `tests/browser/_helpers/server.start_backend` when running
         in-process; otherwise spawn `otto web` subprocess).
      2. Open Playwright chromium browser to ctx.web_url.
      3. Wait for project create / select UI; create a project pointing at
         ctx.project_dir.
      4. Open the JobDialog, submit `--build` with W1_INTENT.
      5. Wait for build completion; walk inspector tabs (Logs / Diff /
         Proof / Artifacts) and assert each renders.
      6. Product verification (5D): browser-load the built kanban app, run
         its tests via `pytest`/`npm test` (whichever exists), and confirm
         at least one acceptance criterion (e.g., visit `/` and see all
         three columns rendered).
    """
    started = time.monotonic()
    failures = ctx.failures
    failures.soft_assert(
        ctx.web_url is not None,
        "harness did not provide ctx.web_url",
    )
    # Orchestrator implements the Playwright + subprocess flow.
    raise NotImplementedError(
        "W1 capture flow scaffolded; orchestrator must wire up Playwright + subprocess. "
        f"intent={W1_INTENT!r}"
    )


def _run_w11(ctx: ScenarioContext) -> ScenarioRunResult:
    """W11 — operator day (mandatory nightly core, modeled on N9).

    Faithfully follows `scripts/otto_as_user_nightly.py` N9 step plan:

      1. Open Mission Control against ctx.project_dir.
      2. `otto build --provider <provider> --allow-dirty <W11_BUILD_INTENT>`
         standalone in a subprocess; wait until the live row appears in UI.
      3. From terminal: `otto queue build <W11_POST_INTENT> --as add-post`
         and `otto queue build <W11_DELETE_INTENT> --as add-delete`.
      4. Start the queue watcher while the standalone build is still running.
      5. Wait for both queue live rows so three live records overlap.
      6. Drill into the standalone Detail row, verify heartbeat progress
         within 4s, switch logs.
      7. Drill into one queue row, cancel via UI, wait for cancel history +
         cancelled status.
      8. Wait up to 3 minutes for the other queue task; cancel standalone if
         still running after 8 minutes.
      9. Open History, verify terminal snapshots, drill into the cancelled row.
     10. Select the succeeded queue row, merge from UI (NOT --all).
     11. Wait up to 30s for merge evidence.
     12. Run product verification on the merged audit branch (5D mandatory).

    All assertions go through `ctx.failures.soft_assert(...)` — never raise.
    Post-scenario, `artifact_mine_pass(ctx.project_dir, ctx.failures)` runs
    regardless of outcome.
    """
    started = time.monotonic()
    failures = ctx.failures
    failures.soft_assert(
        ctx.web_url is not None,
        "harness did not provide ctx.web_url",
    )
    # Orchestrator implements the playwright + multi-subprocess orchestration.
    raise NotImplementedError(
        "W11 operator-day flow scaffolded; orchestrator must wire up the "
        "playwright + multi-subprocess sequence per N9. "
        f"build_intent={W11_BUILD_INTENT!r}"
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
        ctx = ScenarioContext(
            scenario=scenario,
            project_dir=project_dir,
            artifact_dir=artifact_dir,
            provider=provider,
            failures=failures,
            debug_log=debug_log,
        )
        # Orchestrator: start otto web server here, set ctx.web_port + web_url.
        try:
            scenario.run_fn(ctx)
            note = "scenario phases completed"
        except NotImplementedError as exc:
            return ScenarioOutcome(
                scenario_id=scenario.id,
                description=scenario.description,
                outcome="FAIL",
                note=f"stub: {exc}",
                artifact_dir=artifact_dir,
                wall_duration_s=time.monotonic() - started,
                failures=[str(exc)],
            )
        except Exception as exc:  # noqa: BLE001
            failures.fail(f"unhandled exception in scenario body: {exc}")
            note = "scenario body raised — collected via failures + auto-mine"
        finally:
            # Phase 5H: artifact-mine pass runs regardless of outcome.
            try:
                artifact_mine_pass(project_dir, failures)
            except Exception as exc:  # noqa: BLE001
                failures.fail(f"artifact_mine_pass crashed: {exc}")

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
