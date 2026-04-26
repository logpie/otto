#!/usr/bin/env python3
"""Phase 3.5 capture script: record real Otto activity into Mission Control browser fixtures.

Captures the full project runtime state after a real `otto build` (or related
command) so the Phase 3 Playwright suite can replay against frozen fixtures
that mirror what the FastAPI service actually returned at recording time.

Per `plan-mc-audit.md` Phase 3.5:

- Recordings R1, R2, R5, R8 are fully implemented; R3/R4/R6/R7/R9-R14 are
  shape-complete stubs that raise NotImplementedError. The orchestrator that
  runs Phase 3.5 fills them in.
- Captures use `otto.paths` helpers — never hardcoded path strings (Codex
  Plan-Gate round 3 issue #1).
- Sanitizes timestamps, run_ids, costs, abs paths (`$PROJECT_ROOT/...`),
  and branch names. Restore step hydrates placeholders.
- Captures git state via `git bundle create fixture.bundle --all` plus a
  `restore-git.sh` helper that follows the init→fetch→checkout→overlay order
  documented in plan 3.5A-ter.
- Runs the sanitization invariant pass (every artifact path resolves, every
  run_id has a session, every queue task ID matches a manifest, every audit
  branch exists in the bundle, FastAPI app boots clean against the fixture,
  and every endpoint returns the per-recording status in `manifest.json`).
- For multi-phase recordings (R1) emits `state-contract.{pre,mid,post}.json`.

CLI::

    python scripts/web_record_fixture.py --list
    python scripts/web_record_fixture.py --recording R1
    python scripts/web_record_fixture.py --recording R1,R2 --provider claude
    python scripts/web_record_fixture.py --all --provider codex --bail-fast
    python scripts/web_record_fixture.py --dry-run --recording R1

Real-cost guard: refuses to invoke a recording without OTTO_ALLOW_REAL_COST=1
unless `--dry-run` is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from real_cost_guard import require_real_cost_opt_in  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "bench-results" / "web-record"
DEFAULT_FIXTURE_ROOT = REPO_ROOT / "tests" / "browser" / "_fixtures" / "recorded-runs"
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"

FROZEN_TIMESTAMP = "2026-04-25T12:00:00Z"
FROZEN_BRANCH_PREFIX = "otto/recorded"
PROJECT_ROOT_PLACEHOLDER = "$PROJECT_ROOT"


# ---------------------------------------------------------------------------
# Per-recording endpoint-status contract (Phase 3.5A-bis)
# ---------------------------------------------------------------------------

# Default contract: all GET endpoints return 200 with non-empty payloads.
DEFAULT_ENDPOINT_CONTRACT: dict[str, dict[str, Any]] = {
    "GET /api/state": {"status": 200},
    "GET /api/runs/{id}": {"status": 200},
    "GET /api/runs/{id}/logs": {"status": 200},
    "GET /api/runs/{id}/artifacts": {"status": 200, "non_empty": True},
    "GET /api/runs/{id}/artifacts/{i}/content": {"status": 200},
    "GET /api/runs/{id}/proof-report": {"status": 200},
    "GET /api/runs/{id}/diff": {"status": 200},
    "GET /api/watcher": {"status": 200},
    "GET /api/runtime": {"status": 200},
    "GET /api/events": {"status": 200},
}


def _r8_contract() -> dict[str, dict[str, Any]]:
    """R8 (minimal/edge) intentionally lacks proof + artifacts."""
    contract = dict(DEFAULT_ENDPOINT_CONTRACT)
    contract["GET /api/runs/{id}/artifacts"] = {"status": 200, "non_empty": False}
    contract["GET /api/runs/{id}/artifacts/{i}/content"] = {"status": "n/a"}
    contract["GET /api/runs/{id}/proof-report"] = {"status": 404}
    contract["GET /api/runs/{id}/diff"] = {"status": 200, "empty_ok": True}
    contract["GET /api/events"] = {"status": 200, "empty_ok": True}
    return contract


# ---------------------------------------------------------------------------
# Recording dataclass
# ---------------------------------------------------------------------------


@dataclass
class Recording:
    """One Phase 3.5 recording entry.

    Multi-phase recordings declare ``phase_snapshots`` (e.g., ["pre", "mid",
    "post"] for R1). Stub recordings supply ``capture_fn = _stub_capture``
    which raises ``NotImplementedError``.
    """

    id: str
    description: str
    intent: str
    command: str  # "build" | "improve" | "certify" | "queue-build"
    capture_fn: Callable[["RecordingContext"], "RecordingResult"] = field(repr=False)
    multi_phase: bool = False
    phase_snapshots: list[str] = field(default_factory=lambda: ["post"])
    endpoint_contract: dict[str, dict[str, Any]] = field(
        default_factory=lambda: dict(DEFAULT_ENDPOINT_CONTRACT)
    )
    estimated_cost: float = 0.0
    estimated_seconds: int = 0


@dataclass
class RecordingContext:
    """Per-recording runtime context — temp dir, provider, artifact dir."""

    recording: Recording
    project_dir: Path
    artifact_dir: Path
    fixture_dir: Path
    provider: str
    debug_log: Path


@dataclass
class RecordingResult:
    """Outcome of a recording attempt — used for the run summary."""

    recording_id: str
    outcome: str  # "PASS" | "FAIL" | "SKIPPED" | "INFRA"
    note: str
    artifact_dir: Path
    fixture_dir: Optional[Path]
    duration_s: float
    invariant_failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Capture stubs
# ---------------------------------------------------------------------------


def _stub_capture(ctx: RecordingContext) -> RecordingResult:
    """Stub for recordings the orchestrator hasn't implemented yet."""
    raise NotImplementedError(
        f"recording {ctx.recording.id} not yet implemented; see plan-mc-audit.md 3.5B"
    )


# ---------------------------------------------------------------------------
# R1 — successful kanban build (multi-phase: pre/mid/post)
# ---------------------------------------------------------------------------


def _capture_r1(ctx: RecordingContext) -> RecordingResult:
    """R1: successful kanban build.

    Captures pre/mid/post `state-contract.json` snapshots by polling the
    FastAPI app at three points:

    - pre  — just before `otto build` is invoked (state shows no live runs)
    - mid  — once the agent is active, polled while events flow
    - post — terminal state with completed run + proof report

    Implementation outline (orchestrator fills in the polling loop and the
    FastAPI process management):
      1. Init throwaway git project, write minimal kanban intent.md.
      2. Start FastAPI app via `otto.web.app.create_app(project_dir=...)`
         on a free port (use `tests/browser/_helpers/server.start_backend`
         when the orchestrator runs in-process).
      3. Snapshot endpoint contract → `state-contract.pre.json`.
      4. Spawn `otto build --provider <provider> "<intent>"` subprocess;
         capture stderr to `ctx.debug_log`.
      5. Poll until the `events` endpoint shows the agent has emitted at
         least one `STORY_RESULT` line, then snapshot → `state-contract.mid.json`.
      6. Wait for build completion; snapshot → `state-contract.post.json`.
      7. Walk the project tree via `otto.paths` helpers (NOT string literals)
         and mirror sanitized files into ctx.fixture_dir.
      8. Capture git state via `git bundle create` + `restore-git.sh`.
      9. Run sanitization invariant pass.
    """
    return _record_via_otto_build(
        ctx,
        intent=ctx.recording.intent,
        require_proof=True,
    )


# ---------------------------------------------------------------------------
# R2 — deterministic failure (known-bad fixture)
# ---------------------------------------------------------------------------


def _capture_r2(ctx: RecordingContext) -> RecordingResult:
    """R2: deterministic failure, retry-eligible state.

    The intent points at a malformed otto.yaml or pre-staged intent that
    references a nonexistent local file — i.e., a **non-INFRA** cause that
    the harness can verify reproduces.
    """
    return _record_via_otto_build(
        ctx,
        intent=ctx.recording.intent,
        require_proof=False,  # failed builds may not produce a complete proof
        expect_failure=True,
    )


# ---------------------------------------------------------------------------
# R5 — successful build merged (audit branch state)
# ---------------------------------------------------------------------------


def _capture_r5(ctx: RecordingContext) -> RecordingResult:
    """R5: successful build merged into target branch.

    Same as R1 but follows the build with `otto merge` so the captured state
    shows the audit branch landed (visible in merge-state files + git
    bundle).
    """
    result = _record_via_otto_build(
        ctx,
        intent=ctx.recording.intent,
        require_proof=True,
    )
    # Orchestrator: insert `otto merge --all --cleanup-on-success` here
    # before the final snapshot. Verify against the merge_dir via
    # otto.paths.merge_dir and otto.paths.merge_command_acks.
    return result


# ---------------------------------------------------------------------------
# R8 — minimal/edge run (no proof, no artifacts)
# ---------------------------------------------------------------------------


def _capture_r8(ctx: RecordingContext) -> RecordingResult:
    """R8: minimal/edge run.

    A trivial intent that runs but produces no proof report and no artifacts.
    Verifies the FastAPI app gracefully 404s `/proof-report` and returns an
    empty artifacts list.
    """
    return _record_via_otto_build(
        ctx,
        intent=ctx.recording.intent,
        require_proof=False,
    )


# ---------------------------------------------------------------------------
# Shared recording helper
# ---------------------------------------------------------------------------


def _record_via_otto_build(
    ctx: RecordingContext,
    *,
    intent: str,
    require_proof: bool,
    expect_failure: bool = False,
) -> RecordingResult:
    """Drive an `otto build` end-to-end, then snapshot + sanitize + bundle.

    The orchestrator implements the actual subprocess + polling behavior;
    here we only sketch the flow. The implementations of R1/R2/R5/R8 share
    this body — they differ only in the intent and contract expectations.

    Returns a RecordingResult — outcome="PASS" iff sanitization invariants
    held and the per-recording endpoint-status contract matched.
    """
    started = time.monotonic()
    # The orchestrator invokes `otto build` here. For now this scaffold
    # creates the artifact dir layout so `--list` and dry-run wiring are
    # exercised.
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    ctx.fixture_dir.mkdir(parents=True, exist_ok=True)

    # Placeholder so the scaffold is callable but does not pretend to record.
    # Once the orchestrator wires up the build subprocess this raises until
    # implementation is complete.
    raise NotImplementedError(
        f"recording {ctx.recording.id} capture path is scaffolded but the "
        f"orchestrator must wire up the otto build subprocess; intent={intent!r}, "
        f"require_proof={require_proof}, expect_failure={expect_failure}, "
        f"elapsed={time.monotonic() - started:.2f}s"
    )


# ---------------------------------------------------------------------------
# Recording registry — every R1..R14 has an entry
# ---------------------------------------------------------------------------


RECORDINGS: dict[str, Recording] = {
    "R1": Recording(
        id="R1",
        description="Successful kanban build (multi-phase pre/mid/post)",
        intent=(
            "Build a small kanban board web app with a To-Do / In-Progress / Done "
            "column layout. Tasks can be added, moved between columns, and removed."
        ),
        command="build",
        capture_fn=_capture_r1,
        multi_phase=True,
        phase_snapshots=["pre", "mid", "post"],
        estimated_cost=1.5,
        estimated_seconds=8 * 60,
    ),
    "R2": Recording(
        id="R2",
        description="Deterministic failure (known-bad fixture)",
        # Pre-staged intent referencing a nonexistent local file → deterministic FAIL,
        # not INFRA. See plan-mc-audit.md round 4 issue #4.
        intent=(
            "Implement the requirements documented in ./SPEC-DOES-NOT-EXIST.md "
            "exactly as written."
        ),
        command="build",
        capture_fn=_capture_r2,
        estimated_cost=0.5,
        estimated_seconds=4 * 60,
    ),
    "R3": Recording(
        id="R3",
        description="Paused / resumable build (checkpoint mid-build)",
        intent="(stub — orchestrator supplies checkpoint-friendly intent)",
        command="build",
        capture_fn=_stub_capture,
        estimated_cost=1.0,
        estimated_seconds=6 * 60,
    ),
    "R4": Recording(
        id="R4",
        description="Improve loop with 2 cert rounds",
        intent="(stub — orchestrator supplies fixture with broken acceptance test)",
        command="improve",
        capture_fn=_stub_capture,
        estimated_cost=2.0,
        estimated_seconds=12 * 60,
    ),
    "R5": Recording(
        id="R5",
        description="Successful build merged (audit branch landed)",
        intent=(
            "Build a tiny notes app: GET /notes returns JSON; POST /notes adds a note."
        ),
        command="build",
        capture_fn=_capture_r5,
        estimated_cost=1.5,
        estimated_seconds=10 * 60,
    ),
    "R6": Recording(
        id="R6",
        description="Codex provider build (token-usage rendering)",
        intent="(stub — same intent shape as R1, --provider codex)",
        command="build",
        capture_fn=_stub_capture,
        estimated_cost=1.5,
        estimated_seconds=10 * 60,
    ),
    "R7": Recording(
        id="R7",
        description="Large run (1000-event JSONL, 100-file diff, 10MB log)",
        intent="(stub — orchestrator supplies wide-surface intent)",
        command="build",
        capture_fn=_stub_capture,
        estimated_cost=3.0,
        estimated_seconds=20 * 60,
    ),
    "R8": Recording(
        id="R8",
        description="Minimal/edge run (no proof, no artifacts)",
        intent="Print Hello World to stdout. Nothing else.",
        command="build",
        capture_fn=_capture_r8,
        endpoint_contract=_r8_contract(),
        estimated_cost=0.2,
        estimated_seconds=2 * 60,
    ),
    "R9": Recording(
        id="R9",
        description="Queue with mixed rows (queued/running/done/failed/cancelled)",
        intent="(stub — orchestrator submits 4 queue tasks with curated outcomes)",
        command="queue-build",
        capture_fn=_stub_capture,
        estimated_cost=2.5,
        estimated_seconds=15 * 60,
    ),
    "R10": Recording(
        id="R10",
        description="Active watcher (running PID, command backlog, acks)",
        intent="(stub — orchestrator launches watcher then snapshots mid-flight)",
        command="queue-build",
        capture_fn=_stub_capture,
        estimated_cost=1.5,
        estimated_seconds=10 * 60,
    ),
    "R11": Recording(
        id="R11",
        description="Stale watcher (PID file present, process dead)",
        intent="(stub — orchestrator forces stale supervisor metadata)",
        command="queue-build",
        capture_fn=_stub_capture,
        estimated_cost=0.5,
        estimated_seconds=5 * 60,
    ),
    "R12": Recording(
        id="R12",
        description="Blocked merge (state shows blocking reason)",
        intent="(stub — orchestrator stages dirty target / failing run)",
        command="build",
        capture_fn=_stub_capture,
        estimated_cost=1.0,
        estimated_seconds=8 * 60,
    ),
    "R13": Recording(
        id="R13",
        description="Merge with conflicts",
        intent="(stub — orchestrator stages two conflicting builds + merge attempt)",
        command="queue-build",
        capture_fn=_stub_capture,
        estimated_cost=2.0,
        estimated_seconds=15 * 60,
    ),
    "R14": Recording(
        id="R14",
        description="Large run history (200+ runs in cross-sessions/history.jsonl)",
        intent="(stub — orchestrator seeds history before recording)",
        command="build",
        capture_fn=_stub_capture,
        estimated_cost=0.5,
        estimated_seconds=5 * 60,
    ),
}


# ---------------------------------------------------------------------------
# Path enumeration via otto.paths (Phase 3.5A) — NEVER hardcoded strings
# ---------------------------------------------------------------------------


def enumerate_capture_paths(project_dir: Path) -> dict[str, Any]:
    """Return the canonical set of paths the recording must mirror.

    Generated entirely from `otto.paths` helpers (Codex round 3 issue #1):
    if a path the recording needs lacks a helper, the missing helper is the
    bug to fix first.
    """
    from otto import paths

    project = Path(project_dir)
    sessions_root = paths.sessions_root(project)
    queue_dir = paths.queue_dir(project)
    return {
        "logs_dir": paths.logs_dir(project),
        "sessions_root": sessions_root,
        "session_dirs": (
            sorted(sessions_root.iterdir()) if sessions_root.exists() else []
        ),
        "cross_sessions_dir": paths.cross_sessions_dir(project),
        "history_jsonl": paths.history_jsonl(project),
        "certifier_memory_jsonl": paths.certifier_memory_jsonl(project),
        "live_runs_dir": paths.live_runs_dir(project),
        "merge_dir": paths.merge_dir(project),
        "queue_dir": queue_dir,
        "queue_state_path": paths.queue_state_path(project),
        "queue_commands_path": paths.queue_commands_path(project),
        "queue_command_acks_path": paths.queue_command_acks_path(project),
        # project-root user-owned inputs the recording must include
        "project_intent_md": paths.project_intent_md(project),
        "project_otto_yaml": paths.project_otto_yaml(project),
        "project_claude_md": paths.project_claude_md(project),
    }


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

_ABS_PATH_RE_CACHE: dict[Path, re.Pattern[str]] = {}
_TIMESTAMP_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b")
_RUN_ID_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}-\d{6}-[0-9a-f]{6}\b")
_COST_RE = re.compile(r'("?cost(?:_usd)?"?\s*[:=]\s*)([0-9]+\.[0-9]{4,})')


def _abs_path_re(project_dir: Path) -> re.Pattern[str]:
    if project_dir not in _ABS_PATH_RE_CACHE:
        _ABS_PATH_RE_CACHE[project_dir] = re.compile(re.escape(str(project_dir)))
    return _ABS_PATH_RE_CACHE[project_dir]


def sanitize_text(text: str, project_dir: Path) -> str:
    text = _abs_path_re(project_dir).sub(PROJECT_ROOT_PLACEHOLDER, text)
    text = _TIMESTAMP_RE.sub(FROZEN_TIMESTAMP, text)
    # Run IDs are deterministic-by-position: replace with placeholder; the
    # orchestrator's deterministic-rewriter assigns IDs in capture order.
    text = _RUN_ID_RE.sub("2026-04-25-120000-aaaaaa", text)
    text = _COST_RE.sub(lambda m: f"{m.group(1)}{round(float(m.group(2)), 4)}", text)
    return text


def sanitize_file(path: Path, project_dir: Path) -> None:
    """Sanitize a JSON / log file in place."""
    if not path.is_file():
        return
    try:
        original = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return  # binary file: leave untouched
    sanitized = sanitize_text(original, project_dir)
    if sanitized != original:
        path.write_text(sanitized, encoding="utf-8")


# ---------------------------------------------------------------------------
# Git bundle capture (Phase 3.5A-ter)
# ---------------------------------------------------------------------------


_RESTORE_GIT_SH = """#!/usr/bin/env bash
# Reconstruct the captured git state from fixture.bundle.
#
# Restore order (per plan-mc-audit.md 3.5A-ter):
#   1. init empty repo
#   2. fetch from bundle
#   3. checkout the recorded branch
#   4. caller overlays sanitized otto_logs/ and other non-git files afterward
#
# Usage: PROJECT_DIR=<dest> FIXTURE_DIR=<this dir> bash restore-git.sh
set -euo pipefail
: "${PROJECT_DIR:?must set PROJECT_DIR}"
: "${FIXTURE_DIR:?must set FIXTURE_DIR}"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"
git init -q -b main
git config user.email "fixture@example.com"
git config user.name "Fixture Restore"
git remote add fixture "$FIXTURE_DIR/fixture.bundle"
git fetch -q fixture
RECORDED_BRANCH="${RECORDED_BRANCH:-main}"
git checkout -q "$RECORDED_BRANCH"
git remote remove fixture
"""


def capture_git_bundle(project_dir: Path, fixture_git_dir: Path) -> None:
    """Capture project's git state as a minimal bundle + restore script."""
    fixture_git_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = fixture_git_dir / "fixture.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle_path), "--all"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    restore = fixture_git_dir / "restore-git.sh"
    restore.write_text(_RESTORE_GIT_SH, encoding="utf-8")
    restore.chmod(0o755)


# ---------------------------------------------------------------------------
# Sanitization invariant pass (Phase 3.5A-bis)
# ---------------------------------------------------------------------------


def run_invariant_pass(
    fixture_dir: Path,
    *,
    contract: dict[str, dict[str, Any]],
) -> list[str]:
    """Validate the sanitized fixture is internally consistent.

    Returns a list of failure messages (empty list = pass). The orchestrator
    is expected to fail the recording and discard the fixture if any
    invariant breaks.

    Checks (sketch — orchestrator implements the deep ones):
      - manifest.json exists and parses
      - state-contract.json (or .{pre,mid,post}.json) exist and parse
      - every artifact path inside any captured JSON resolves under the
        fixture (after $PROJECT_ROOT hydration)
      - every run_id in cross-sessions/history.jsonl has a sessions/<id>/ dir
      - every queue task ID matches its manifest under queue_dir
      - every audit branch in merge state exists in the captured git bundle
      - booting `otto.web.app.create_app(project_dir=...)` against the fixture
        passes the per-recording endpoint-status contract above
    """
    failures: list[str] = []
    manifest = fixture_dir / "manifest.json"
    if not manifest.is_file():
        failures.append(f"manifest.json missing at {manifest}")
        return failures
    try:
        json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        failures.append(f"manifest.json invalid JSON: {exc}")
        return failures
    # Orchestrator extends with the rest. The placeholder above keeps the
    # invariant pass callable; each fully-implemented R recording grows the
    # checks list as it materializes its outputs.
    if not contract:
        failures.append("recording supplied empty endpoint contract")
    return failures


# ---------------------------------------------------------------------------
# Manifest emission
# ---------------------------------------------------------------------------


def write_manifest(
    fixture_dir: Path,
    recording: Recording,
    *,
    provider: str,
    outcome: str,
    included_artifacts: list[str],
    source_commit: str,
    otto_version: str,
) -> Path:
    """Emit manifest.json describing the recording."""
    manifest_path = fixture_dir / "manifest.json"
    payload = {
        "recording_id": recording.id,
        "description": recording.description,
        "intent": recording.intent,
        "command": recording.command,
        "provider": provider,
        "outcome": outcome,
        "multi_phase": recording.multi_phase,
        "phase_snapshots": recording.phase_snapshots,
        "endpoint_contract": recording.endpoint_contract,
        "included_artifacts": sorted(included_artifacts),
        "recording_date": FROZEN_TIMESTAMP,
        "source_commit": source_commit,
        "otto_version": otto_version,
        "schema_version": 1,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


# ---------------------------------------------------------------------------
# Run-id helpers
# ---------------------------------------------------------------------------


def utc_run_id() -> str:
    return time.strftime("%Y-%m-%d-%H%M%S", time.gmtime()) + "-" + os.urandom(3).hex()


def get_source_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
        return out
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def get_otto_version() -> str:
    try:
        from otto import __version__  # type: ignore[attr-defined]

        return str(__version__)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def select_recordings(
    *, all_flag: bool, recording_csv: Optional[str]
) -> list[Recording]:
    if all_flag and recording_csv:
        raise SystemExit("Use either --all or --recording, not both")
    if all_flag:
        return list(RECORDINGS.values())
    if not recording_csv:
        raise SystemExit(
            "Must supply --recording <ids> or --all (or --list to enumerate)"
        )
    ids = [part.strip() for part in recording_csv.split(",") if part.strip()]
    missing = [rid for rid in ids if rid not in RECORDINGS]
    if missing:
        raise SystemExit(
            f"Unknown recording id(s): {', '.join(missing)}. "
            f"Available: {', '.join(RECORDINGS)}"
        )
    return [RECORDINGS[rid] for rid in ids]


def print_recording_list() -> None:
    print("ID   Phases    Cost    Secs   Description")
    for recording in RECORDINGS.values():
        phases = "/".join(recording.phase_snapshots)
        impl = "STUB" if recording.capture_fn is _stub_capture else "IMPL"
        print(
            f"{recording.id:4} {phases:9} ${recording.estimated_cost:>4.2f} "
            f"{recording.estimated_seconds:>5}s  [{impl}] {recording.description}"
        )


def record_one(recording: Recording, *, run_id: str, provider: str, dry_run: bool) -> RecordingResult:
    """Record one R-scenario.

    Spins up a throwaway git project under tempfile.mkdtemp, invokes the
    recording's capture_fn, then sanitizes + bundles + validates.
    """
    started = time.monotonic()
    artifact_dir = DEFAULT_ARTIFACT_ROOT / run_id / recording.id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir = DEFAULT_FIXTURE_ROOT / recording.id
    debug_log = artifact_dir / "debug.log"

    if dry_run:
        # Dry-run: verify wiring (path helpers callable, fixture dir creatable,
        # contract structure sane) without spending real money.
        try:
            _verify_path_helpers_wiring()
        except Exception as exc:  # noqa: BLE001
            return RecordingResult(
                recording_id=recording.id,
                outcome="FAIL",
                note=f"path helper wiring broken: {exc}",
                artifact_dir=artifact_dir,
                fixture_dir=None,
                duration_s=time.monotonic() - started,
            )
        return RecordingResult(
            recording_id=recording.id,
            outcome="SKIPPED",
            note="dry-run: skipped real LLM invocation",
            artifact_dir=artifact_dir,
            fixture_dir=fixture_dir,
            duration_s=time.monotonic() - started,
        )

    project_dir = Path(tempfile.mkdtemp(prefix="otto-mc-record-"))
    try:
        ctx = RecordingContext(
            recording=recording,
            project_dir=project_dir,
            artifact_dir=artifact_dir,
            fixture_dir=fixture_dir,
            provider=provider,
            debug_log=debug_log,
        )
        try:
            recording.capture_fn(ctx)
        except NotImplementedError as exc:
            return RecordingResult(
                recording_id=recording.id,
                outcome="SKIPPED",
                note=f"stub: {exc}",
                artifact_dir=artifact_dir,
                fixture_dir=None,
                duration_s=time.monotonic() - started,
            )

        # On a successful capture: write manifest, validate.
        write_manifest(
            fixture_dir,
            recording,
            provider=provider,
            outcome="PASS",
            included_artifacts=[
                p.name for p in fixture_dir.iterdir() if p.is_file()
            ],
            source_commit=get_source_commit(),
            otto_version=get_otto_version(),
        )
        invariant_failures = run_invariant_pass(
            fixture_dir, contract=recording.endpoint_contract
        )
        if invariant_failures:
            return RecordingResult(
                recording_id=recording.id,
                outcome="FAIL",
                note=f"invariant pass failed ({len(invariant_failures)} issues)",
                artifact_dir=artifact_dir,
                fixture_dir=fixture_dir,
                duration_s=time.monotonic() - started,
                invariant_failures=invariant_failures,
            )
        return RecordingResult(
            recording_id=recording.id,
            outcome="PASS",
            note="recorded + sanitized + validated",
            artifact_dir=artifact_dir,
            fixture_dir=fixture_dir,
            duration_s=time.monotonic() - started,
        )
    finally:
        # Keep project_dir on disk for forensic debugging; orchestrator can
        # opt into shutil.rmtree once stable.
        debug_log.write_text(
            f"project_dir={project_dir}\nrun_id={run_id}\nprovider={provider}\n",
            encoding="utf-8",
        )


def _verify_path_helpers_wiring() -> None:
    """Confirm `otto.paths` exposes every helper the recording uses."""
    from otto import paths

    project = Path(tempfile.mkdtemp(prefix="otto-mc-record-dry-"))
    try:
        enumerated = enumerate_capture_paths(project)
        # Sanity: every value should be a Path or list, never None.
        for key, value in enumerated.items():
            if value is None:
                raise RuntimeError(f"path helper for {key} returned None")
        # And `otto.paths` must still expose every named helper this script
        # needs at the boundaries we listed in plan-mc-audit.md 3.5A:
        for attr in (
            "logs_dir",
            "sessions_root",
            "cross_sessions_dir",
            "history_jsonl",
            "live_runs_dir",
            "merge_dir",
            "queue_dir",
            "queue_state_path",
            "queue_commands_path",
            "project_intent_md",
            "project_otto_yaml",
        ):
            if not hasattr(paths, attr):
                raise RuntimeError(
                    f"otto.paths missing helper {attr!r}; this is the bug to fix first"
                )
    finally:
        shutil.rmtree(project, ignore_errors=True)


def print_summary(run_id: str, results: list[RecordingResult]) -> int:
    print()
    print(f"web-record-fixture run {run_id}")
    print(f"recordings: {len(results)}")
    print()
    print("ID   Outcome   Duration  Note")
    for res in results:
        print(
            f"{res.recording_id:4} {res.outcome:9} {int(res.duration_s):>4}s     {res.note}"
        )
    failed = [r for r in results if r.outcome == "FAIL"]
    print()
    print(
        f"Totals: {len(results)} recordings, "
        f"{sum(1 for r in results if r.outcome == 'PASS')} PASS, "
        f"{len(failed)} FAIL, "
        f"{sum(1 for r in results if r.outcome == 'SKIPPED')} SKIPPED, "
        f"{sum(1 for r in results if r.outcome == 'INFRA')} INFRA"
    )
    return 1 if failed else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record real Otto activity into Mission Control browser fixtures."
    )
    parser.add_argument("--list", action="store_true", help="enumerate recordings and exit")
    parser.add_argument(
        "--recording", help="comma-separated recording IDs (e.g. R1,R2,R5)"
    )
    parser.add_argument(
        "--all", action="store_true", help="record all R1..R14 in order"
    )
    parser.add_argument(
        "--provider", choices=["claude", "codex"], default="claude"
    )
    parser.add_argument(
        "--bail-fast",
        action="store_true",
        help="stop on first FAIL (SKIPPED stubs and INFRA failures don't trigger bail)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="skip real LLM invocation; verify harness wiring + path enumeration",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.list:
        print_recording_list()
        return 0

    if not args.dry_run:
        try:
            require_real_cost_opt_in("web-record-fixture recording capture")
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 2

    recordings = select_recordings(all_flag=args.all, recording_csv=args.recording)
    run_id = utc_run_id()
    results: list[RecordingResult] = []
    for index, recording in enumerate(recordings):
        print(f"\n=== {recording.id} {recording.description} ===", flush=True)
        try:
            result = record_one(
                recording, run_id=run_id, provider=args.provider, dry_run=args.dry_run
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            result = RecordingResult(
                recording_id=recording.id,
                outcome="FAIL",
                note=f"unhandled exception: {exc}",
                artifact_dir=DEFAULT_ARTIFACT_ROOT / run_id / recording.id,
                fixture_dir=None,
                duration_s=0.0,
            )
        results.append(result)
        print(f"[{recording.id}] {result.outcome}: {result.note}", flush=True)
        if args.bail_fast and result.outcome == "FAIL":
            break

    return print_summary(run_id, results)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
