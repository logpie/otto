"""TODO-CLI benchmark for the unified i2p pipeline.

A third project shape distinct from Microfeed (Flask + DB + API) and
SSG (Python CLI + filesystem rendering): a Python TODO CLI that
persists tasks to a JSON file. Different from SSG because it has
NO web UI and the CLI IS the primary user surface (not a build tool).

Validates:
  - The walkthrough's "not-applicable" branch fires correctly for
    non-webapp projects.
  - The CLI baseline applies (--help complete, friendly errors,
    conventional exit codes, sensible default behavior).
  - The audit-final-quality rubric grades CLI projects honestly
    (CLI tools shouldn't be penalized for "no responsive design" —
    that's not their dimension).

Composes scripts/bench_evaluator.py:
  - contract_test       — tests/run_acceptance.py exits 0
  - code_health         — AST parses, no leftover TODOs, no huge files
  - edge_cases (CLI)    — --help, no-args, invalid flag handling
  - user_journey        — full add → list → complete → list flow

Usage:
  OTTO_ALLOW_REAL_COST=1 uv run python scripts/bench_todo_cli_i2p.py \\
      --timeout-s 1800 --provider claude
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from real_cost_guard import require_real_cost_opt_in  # noqa: E402

import bench_evaluator as be  # noqa: E402

PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python3"
if not PYTHON_BIN.exists():
    PYTHON_BIN = Path(sys.executable)
RESULTS_DIR = REPO_ROOT / "bench-results"


TODO_INTENT = (
    "Build a Python TODO CLI tool from this greenfield repo. Provide a "
    "single-file CLI entry-point at `todo.py` (also discoverable as "
    "`python -m todo` if you ship a package). Persist tasks as a JSON "
    "list at `tasks.json` next to the CLI. Required subcommands: "
    "`add <text>` (creates a new task with auto-incrementing integer id, "
    "default status 'pending'), `list` (prints all tasks one per line "
    "as `<id>. [<status>] <text>`, sorted by id ascending; "
    "`--status pending` and `--status done` filter), `complete <id>` "
    "(marks task done; prints `completed: <text>`; exit 1 if id "
    "missing), `delete <id>` (removes; exit 1 if missing), `clear` "
    "(removes all tasks; prints count removed). Top-level `--help` "
    "lists all subcommands; `<subcommand> --help` documents flags. "
    "Exit codes: 0 success, 1 user error (missing id, invalid input), "
    "2 unexpected error. Running `todo` with no args should print help. "
    "Tasks must survive across CLI invocations (persisted to disk). "
    "Wrap internal exceptions with friendly error messages — raw "
    "Python tracebacks are NOT acceptable for missing-task / "
    "invalid-input cases."
)


# Acceptance test that exercises the full CLI lifecycle.
ACCEPTANCE_SCRIPT = r'''
"""Acceptance test for the TODO-CLI bench."""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _entry_argv() -> list[str]:
    """Find the CLI entry-point. Try `python todo.py` first, then `python -m todo`."""
    if (ROOT / "todo.py").exists():
        return [sys.executable, str(ROOT / "todo.py")]
    if (ROOT / "todo" / "__main__.py").exists() or (ROOT / "todo" / "__init__.py").exists():
        return [sys.executable, "-m", "todo"]
    print("acceptance:entry-point:FAIL (no todo.py and no todo/ package)")
    sys.exit(1)


def run(argv: list[str], expect_code: int = 0) -> tuple[str, str]:
    """Run a CLI command; assert exit code; return (stdout, stderr)."""
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=30)
    if proc.returncode != expect_code:
        print(f"acceptance:run:FAIL ({argv} exited {proc.returncode}, expected {expect_code})")
        print(f"  stdout: {proc.stdout!r}")
        print(f"  stderr: {proc.stderr!r}")
        sys.exit(2)
    return proc.stdout, proc.stderr


def main() -> int:
    entry = _entry_argv()
    print(f"acceptance:entry-point:PASS ({' '.join(entry[1:])})")

    # Start clean.
    tasks_path = ROOT / "tasks.json"
    if tasks_path.exists():
        tasks_path.unlink()

    # 1. `--help` works.
    out, _ = run([*entry, "--help"], expect_code=0)
    if not out.strip():
        print("acceptance:help:FAIL (empty output)")
        return 1
    print("acceptance:help:PASS")

    # 2. List on empty store: 0 tasks.
    out, _ = run([*entry, "list"], expect_code=0)
    if any(line.strip() for line in out.splitlines()):
        # may print an empty hint, but no task lines
        meaningful = [l for l in out.splitlines() if l and not l.lower().startswith(("no ", "empty"))]
        if meaningful:
            print(f"acceptance:list-empty:FAIL (got: {out!r})")
            return 1
    print("acceptance:list-empty:PASS")

    # 3. Add three tasks.
    run([*entry, "add", "Buy groceries"])
    run([*entry, "add", "Write tests"])
    run([*entry, "add", "Ship feature"])
    print("acceptance:add:PASS (3 tasks added)")

    # 4. List shows all 3 in order, with ids 1/2/3.
    out, _ = run([*entry, "list"], expect_code=0)
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) != 3:
        print(f"acceptance:list-three:FAIL (got {len(lines)} lines, expected 3)")
        print(f"  output: {out!r}")
        return 1
    if "Buy groceries" not in lines[0] or "Write tests" not in lines[1]:
        print(f"acceptance:list-order:FAIL (got: {lines!r})")
        return 1
    print("acceptance:list-three:PASS")

    # 5. Complete task 2.
    out, _ = run([*entry, "complete", "2"], expect_code=0)
    if "Write tests" not in out:
        print(f"acceptance:complete-output:FAIL (got: {out!r})")
        return 1
    print("acceptance:complete:PASS")

    # 6. Filter by status.
    out, _ = run([*entry, "list", "--status", "pending"], expect_code=0)
    pending_lines = [l for l in out.splitlines() if l.strip()]
    if len(pending_lines) != 2:
        print(f"acceptance:filter-pending:FAIL (got {len(pending_lines)}, expected 2)")
        return 1
    out, _ = run([*entry, "list", "--status", "done"], expect_code=0)
    done_lines = [l for l in out.splitlines() if l.strip()]
    if len(done_lines) != 1 or "Write tests" not in done_lines[0]:
        print(f"acceptance:filter-done:FAIL (got: {done_lines!r})")
        return 1
    print("acceptance:filter-status:PASS")

    # 7. Persistence: tasks survive across invocations (already validated by 4-6).
    # Verify tasks.json exists.
    if not tasks_path.exists():
        print("acceptance:persistence-file:FAIL (tasks.json not present)")
        return 1
    try:
        json.loads(tasks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"acceptance:persistence-format:FAIL (tasks.json not valid JSON: {exc})")
        return 1
    print("acceptance:persistence:PASS")

    # 8. Complete missing id → exit 1, friendly error (no traceback).
    proc = subprocess.run(
        [*entry, "complete", "999"],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    if proc.returncode == 0:
        print("acceptance:complete-missing-exit:FAIL (exited 0; expected 1)")
        return 1
    if "Traceback" in proc.stderr or "Traceback" in proc.stdout:
        print("acceptance:complete-missing-friendly:FAIL (Python traceback shown)")
        return 1
    print("acceptance:complete-missing:PASS")

    # 9. Delete missing id → exit 1, friendly error.
    proc = subprocess.run(
        [*entry, "delete", "999"],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    if proc.returncode == 0:
        print("acceptance:delete-missing-exit:FAIL (exited 0; expected 1)")
        return 1
    if "Traceback" in proc.stderr:
        print("acceptance:delete-missing-friendly:FAIL (Python traceback shown)")
        return 1
    print("acceptance:delete-missing:PASS")

    # 10. Clear.
    out, _ = run([*entry, "clear"], expect_code=0)
    if "3" not in out and "removed" not in out.lower():
        print(f"acceptance:clear-output:FAIL (no count in output: {out!r})")
        return 1
    out, _ = run([*entry, "list"], expect_code=0)
    if any(l.strip() and not l.lower().startswith(("no ", "empty")) for l in out.splitlines()):
        meaningful = [l for l in out.splitlines() if l and not l.lower().startswith(("no ", "empty"))]
        if meaningful:
            print(f"acceptance:clear-empty:FAIL (got: {out!r})")
            return 1
    print("acceptance:clear:PASS")

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# User-journey script run by the bench's user_journey evaluator.
# Mimics what a real user would actually do, including stumbles.
USER_JOURNEY_SCRIPT = r'''
"""Stumbling user journey for the TODO-CLI bench.

Mimics what a real first-time user does: try wrong invocations,
forget about empty state, hit edge cases.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _entry() -> list[str]:
    if (ROOT / "todo.py").exists():
        return [sys.executable, str(ROOT / "todo.py")]
    return [sys.executable, "-m", "todo"]


def run(argv: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def assert_friendly_error(proc: subprocess.CompletedProcess, label: str) -> None:
    """Friendly = non-zero exit, output not a Python traceback."""
    if proc.returncode == 0:
        print(f"FAIL:{label}:expected non-zero exit, got 0")
        sys.exit(1)
    combined = (proc.stdout or "") + (proc.stderr or "")
    if "Traceback" in combined:
        print(f"FAIL:{label}:Python traceback in output")
        print(f"  {combined[:500]!r}")
        sys.exit(1)
    if not combined.strip():
        print(f"FAIL:{label}:no error message printed")
        sys.exit(1)


def main() -> int:
    entry = _entry()

    # Stumble 1: user runs `todo` with no args
    proc = run(entry)
    if not proc.stdout.strip() and not proc.stderr.strip():
        print("FAIL:no-args:produced no output (should print help)")
        return 1

    # Stumble 2: typos a subcommand
    proc = run([*entry, "ad", "buy milk"])
    assert_friendly_error(proc, "typo-subcommand")

    # Stumble 3: completes a task that doesn't exist (clean store)
    run([*entry, "clear"])
    proc = run([*entry, "complete", "1"])
    assert_friendly_error(proc, "complete-missing")

    # Real flow: add, list, complete, delete
    if run([*entry, "add", "task-a"]).returncode != 0:
        print("FAIL:add:returned non-zero")
        return 1
    if run([*entry, "add", "task-b"]).returncode != 0:
        print("FAIL:add-second:returned non-zero")
        return 1

    proc = run([*entry, "list"])
    if "task-a" not in proc.stdout or "task-b" not in proc.stdout:
        print(f"FAIL:list:tasks not present (got: {proc.stdout!r})")
        return 1

    if run([*entry, "complete", "1"]).returncode != 0:
        print("FAIL:complete:returned non-zero")
        return 1

    # Stumble 4: complete the same task twice (already done)
    # Should NOT crash; should either succeed idempotently or
    # fail with a friendly message. Tracebacks are unacceptable.
    proc = run([*entry, "complete", "1"])
    if "Traceback" in (proc.stdout + proc.stderr):
        print("FAIL:double-complete:traceback")
        return 1

    # Edge: unicode in task text
    if run([*entry, "add", "café 🎉 résumé"]).returncode != 0:
        print("FAIL:unicode:add returned non-zero")
        return 1
    proc = run([*entry, "list"])
    if "café" not in proc.stdout:
        print(f"FAIL:unicode-list:character lost (got: {proc.stdout!r})")
        return 1

    print("PASS:user_journey")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


OTTO_YAML = '''\
test_command: "python tests/run_acceptance.py"
project_kind: cli
'''


@dataclass
class BenchResult:
    schema_version: int = 1
    run_id: str = ""
    run_root: str = ""
    started_at: str = ""
    seed_intent: str = ""
    cli_exit_code: int | None = None
    cli_timeout: bool = False
    wall_s: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)
    evaluator_results: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = "unknown"


def _setup_repo(run_root: Path) -> Path:
    project_dir = run_root / "i2p"
    project_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "otto.yaml").write_text(OTTO_YAML)

    tests_dir = project_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "run_acceptance.py").write_text(ACCEPTANCE_SCRIPT)
    (tests_dir / "user_journey.py").write_text(USER_JOURNEY_SCRIPT)

    (project_dir / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\notto_logs/\notto_artifacts/\n"
        "tasks.json\n.otto/\n"
    )

    subprocess.run(["git", "init", "-b", "main"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.com",
         "commit", "-m", "seed"],
        cwd=project_dir, check=True, capture_output=True,
    )
    return project_dir


def _drive_otto(
    project_dir: Path,
    artifacts_dir: Path,
    timeout_s: int,
    provider: str,
) -> tuple[int, bool, float]:
    log_path = artifacts_dir / "todo-cli-otto-run.log"
    cmd = [
        str(PYTHON_BIN), "-m", "otto.cli", "run",
        "--project-kind", "cli",
        TODO_INTENT,
    ]
    print(f"[todo-cli] $ {shlex.join(cmd)}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        with log_path.open("wb") as fh:
            proc = subprocess.run(
                cmd, cwd=project_dir, stdout=fh, stderr=subprocess.STDOUT,
                timeout=timeout_s, check=False,
            )
        return proc.returncode, False, time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return -1, True, time.monotonic() - t0


def _read_journal(session_dir: Path) -> list[dict[str, Any]]:
    target = session_dir / "spec-state.jsonl"
    if not target.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _latest_session_dir(project_dir: Path) -> Path | None:
    sessions = project_dir / "otto_logs" / "sessions"
    if not sessions.exists():
        return None
    children = [c for c in sessions.iterdir() if c.is_dir()]
    if not children:
        return None
    return max(children, key=lambda p: p.stat().st_mtime)


def _entry_argv(project_dir: Path) -> list[str]:
    """Match the acceptance test's discovery."""
    if (project_dir / "todo.py").exists():
        return [str(PYTHON_BIN), str(project_dir / "todo.py")]
    return [str(PYTHON_BIN), "-m", "todo"]


def _run_evaluators(project_dir: Path) -> list[be.EvalResult]:
    """Run the deep evaluator suite for this project shape."""
    ctx = be.EvaluatorContext(
        project_dir=project_dir,
        python=PYTHON_BIN,
        project_kind="cli",
        timeout_s=120,
    )
    journey_script = project_dir / "tests" / "user_journey.py"
    return be.run_evaluators(ctx, [
        be.eval_contract_test,
        be.eval_code_health,
        lambda c: be.eval_edge_cases_cli(c, _entry_argv(project_dir)),
        lambda c: be.eval_user_journey_webapp(c, journey_script),  # subprocess flavor works for CLI too
    ])


def _summarize(
    project_dir: Path,
    cli_exit: int,
    cli_timeout: bool,
    wall_s: float,
) -> tuple[dict[str, Any], list[be.EvalResult]]:
    session_dir = _latest_session_dir(project_dir)
    events = _read_journal(session_dir) if session_dir else []
    landed = [e.get("slice_id") for e in events if e.get("kind") == "slice.merge.landed"]
    blocked = [e.get("slice_id") for e in events if e.get("kind") == "slice.blocked"]
    run_finished = [e for e in events if e.get("kind") == "run.finished"]
    audit_verdict = ""
    if run_finished:
        audit_verdict = run_finished[-1].get("extra", {}).get("verdict", "")

    quality_score = 0
    quality_findings: list[str] = []
    # A0.4: read the canonical `feature_audits` key.
    feature_audits: list[dict[str, Any]] = []
    packet_path = session_dir / "proof-packet.json" if session_dir else None
    if packet_path and packet_path.exists():
        try:
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            quality_score = int(packet.get("quality_score") or 0)
            quality_findings = [str(f) for f in (packet.get("quality_findings") or [])[:20]]
            feature_audits = list(packet.get("feature_audits") or [])
        except (OSError, json.JSONDecodeError):
            pass

    eval_results = _run_evaluators(project_dir)

    return {
        "cli_exit_code": cli_exit,
        "cli_timeout": cli_timeout,
        "wall_s": round(wall_s, 1),
        "session_dir": str(session_dir) if session_dir else None,
        "slices_landed": landed,
        "slices_blocked": blocked,
        "audit_verdict": audit_verdict,
        "quality_score": quality_score,
        "quality_findings": quality_findings,
        "feature_audits": feature_audits,
        "evaluator_aggregate": be.aggregate_status(eval_results),
        "evaluator_summary": {r.name: r.status for r in eval_results},
    }, eval_results


def _verdict(summary: dict[str, Any]) -> str:
    if summary["cli_timeout"]:
        return "timeout"
    if not summary.get("session_dir"):
        return "no_session_produced"
    if summary.get("slices_blocked"):
        return "slices_did_not_land"
    if not summary.get("slices_landed"):
        return "no_slices_landed"
    if summary.get("audit_verdict") not in ("passed", "partial"):
        return f"unexpected_audit_verdict_{summary.get('audit_verdict','')}"
    qs = summary.get("quality_score") or 0
    if 0 < qs < 3:
        return "low_quality"
    agg = summary.get("evaluator_aggregate", "skipped")
    if agg == "blocked":
        return "evaluators_blocked"
    if agg == "partial":
        return "evaluators_partial"
    return "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--provider", default="claude")
    args = parser.parse_args()

    require_real_cost_opt_in()

    started_at = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"todo-cli-i2p-{started_at}"
    artifacts_dir = args.output_dir / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if args.run_root is not None:
        run_root = args.run_root
        run_root.mkdir(parents=True, exist_ok=True)
    else:
        run_root = Path(tempfile.mkdtemp(prefix=f"{run_id}-"))

    (artifacts_dir / "paths.env").write_text(
        f"RUN_ROOT={run_root}\n"
        f"ART={artifacts_dir}\n"
        f"REPO_ROOT={REPO_ROOT}\n"
        f"PROVIDER={args.provider}\n"
    )

    print(f"[todo-cli] run_root={run_root}")
    project_dir = _setup_repo(run_root)
    cli_exit, cli_timeout, wall_s = _drive_otto(
        project_dir, artifacts_dir, args.timeout_s, args.provider,
    )
    summary, eval_results = _summarize(project_dir, cli_exit, cli_timeout, wall_s)
    verdict = _verdict(summary)

    result = BenchResult(
        run_id=run_id,
        run_root=str(run_root),
        started_at=started_at,
        seed_intent=TODO_INTENT,
        cli_exit_code=cli_exit,
        cli_timeout=cli_timeout,
        wall_s=wall_s,
        summary=summary,
        evaluator_results=[r.to_dict() for r in eval_results],
        verdict=verdict,
    )
    (artifacts_dir / "result.json").write_text(json.dumps(asdict(result), indent=2) + "\n")

    lines = [
        f"# TODO-CLI i2p bench — {run_id}",
        f"\n**Verdict:** `{verdict}`",
        "",
        f"- wall_s: {wall_s:.0f}",
        f"- cli_exit_code: {cli_exit}",
        f"- slices landed: {summary.get('slices_landed')}",
        f"- slices blocked: {summary.get('slices_blocked')}",
        f"- audit verdict: {summary.get('audit_verdict')}",
        f"- quality_score: {summary.get('quality_score', 0)}/5",
        f"- evaluator_aggregate: {summary.get('evaluator_aggregate')}",
        "",
        "## Evaluator results",
    ]
    for r in eval_results:
        lines.append(f"- **{r.name}**: {r.status} — {r.summary} ({r.duration_s:.1f}s)")
    if summary.get("quality_findings"):
        lines.append("")
        lines.append("## Quality findings")
        for f in summary["quality_findings"]:
            lines.append(f"- {f}")
    feature_audit_list = summary.get("feature_audits")
    if feature_audit_list:
        lines.append("")
        lines.append("## Feature audits")
        for fa in feature_audit_list:
            lines.append(f"- **{fa.get('name')}** [{fa.get('status')}]: {fa.get('detail','')[:200]}")
    (artifacts_dir / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"\nwrote {artifacts_dir}/REPORT.md")
    print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
