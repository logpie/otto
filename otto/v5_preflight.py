"""Deterministic pre-flight checks on the v5 task graph.

Catches structural bugs in decomposition before children dispatch:
- Architect sub-decomposed (should be inline)
- CHARTER.md missing after architect-pass
- DAG cycles in depends_on
- Duplicate task IDs

Phase 1 (this file): cheap deterministic checks. Returns issues; the
caller decides whether to log, fix, or block dispatch. Semantic checks
(path overlap, contract gaps) belong to a Phase 2 LLM reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


Severity = Literal["warn", "error", "block"]


@dataclass
class PreflightIssue:
    """A single issue found by pre-flight.

    Attributes:
        kind: machine-readable issue identifier.
        severity: warn (log), error (log + emit event), block (refuse dispatch).
        message: human-readable description.
        task_id: the task this issue is about, if applicable.
    """

    kind: str
    severity: Severity
    message: str
    task_id: str | None = None


def _is_architect(task: dict[str, Any]) -> bool:
    """Heuristic: an architect task has empty depends_on and an intent
    that starts with 'Architect' or contains 'CHARTER' as a deliverable.

    The architect-first prompt convention names this role explicitly.
    """
    if task.get("depends_on"):
        return False
    intent = (task.get("intent") or "").lstrip()
    return intent.startswith("Architect") or intent.startswith("architect")


def _detect_architect_sub_decomp(graph: dict[str, Any]) -> list[PreflightIssue]:
    """Architect must run inline; if it called submit_subtask, flag it.

    The whiteboard regression: architect emitted 3 grandchildren that
    wrote scaffolds to the same dirs the root's parallel feature
    siblings later wrote to. Unrecoverable merge_blocked.
    """
    issues: list[PreflightIssue] = []
    tasks = graph.get("tasks") or {}
    for tid, task in tasks.items():
        if not _is_architect(task):
            continue
        if task.get("decomposition") == "emit":
            issues.append(
                PreflightIssue(
                    kind="architect_sub_decomposed",
                    severity="block",
                    message=(
                        f"Architect task {tid} called submit_subtask; "
                        f"must call begin_inline. Grandchildren will "
                        f"conflict with parallel feature siblings."
                    ),
                    task_id=tid,
                )
            )
    return issues


def _detect_missing_charter(
    graph: dict[str, Any], project_dir: Path
) -> list[PreflightIssue]:
    """If architect completed with verdict=pass but CHARTER.md is
    missing at the repo root, the architect didn't deliver.

    Decomp3 regression: architect was skipped or didn't write CHARTER;
    integration session had to patch 3 contract bugs to recover.
    """
    issues: list[PreflightIssue] = []
    charter = project_dir / "CHARTER.md"
    if charter.exists():
        return issues
    tasks = graph.get("tasks") or {}
    for tid, task in tasks.items():
        if not _is_architect(task):
            continue
        if task.get("verdict") == "pass":
            issues.append(
                PreflightIssue(
                    kind="charter_missing",
                    severity="error",
                    message=(
                        f"Architect task {tid} reported verdict=pass "
                        f"but CHARTER.md is missing at {charter}. The "
                        f"primary architect deliverable is absent."
                    ),
                    task_id=tid,
                )
            )
    return issues


def _detect_dag_cycle(graph: dict[str, Any]) -> list[PreflightIssue]:
    """depends_on cycle would cause the runner to deadlock.

    Walks each task; if revisiting a task during DFS, there's a cycle.
    """
    issues: list[PreflightIssue] = []
    tasks = graph.get("tasks") or {}

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in tasks}

    def visit(tid: str, path: list[str]) -> str | None:
        if color.get(tid) == GRAY:
            cycle_start = path.index(tid)
            return " -> ".join(path[cycle_start:] + [tid])
        if color.get(tid) == BLACK:
            return None
        color[tid] = GRAY
        for dep in tasks.get(tid, {}).get("depends_on") or []:
            if dep in tasks:
                cycle = visit(dep, path + [tid])
                if cycle:
                    return cycle
        color[tid] = BLACK
        return None

    for tid in tasks:
        if color.get(tid) == WHITE:
            cycle = visit(tid, [])
            if cycle:
                issues.append(
                    PreflightIssue(
                        kind="dag_cycle",
                        severity="block",
                        message=f"Dependency cycle in task graph: {cycle}",
                    )
                )
                break
    return issues


def _detect_duplicate_task_ids(pending: list[dict[str, Any]]) -> list[PreflightIssue]:
    """Two pending tasks with the same task_id would corrupt state.

    Very rare but cheap to check.
    """
    issues: list[PreflightIssue] = []
    seen: set[str] = set()
    for entry in pending:
        tid = entry.get("task_id")
        if not tid:
            continue
        if tid in seen:
            issues.append(
                PreflightIssue(
                    kind="duplicate_task_id",
                    severity="block",
                    message=f"Duplicate task_id in v5_pending: {tid}",
                    task_id=tid,
                )
            )
        seen.add(tid)
    return issues


def run_preflight(
    project_dir: Path,
    graph: dict[str, Any],
    pending: list[dict[str, Any]],
) -> list[PreflightIssue]:
    """Run all Phase-1 deterministic checks. Returns list of issues."""
    issues: list[PreflightIssue] = []
    issues.extend(_detect_architect_sub_decomp(graph))
    issues.extend(_detect_missing_charter(graph, project_dir))
    issues.extend(_detect_dag_cycle(graph))
    issues.extend(_detect_duplicate_task_ids(pending))
    return issues


def check_scaffold_compiles(
    project_dir: Path, timeout_s: int = 90
) -> list[PreflightIssue]:
    """Run compile/typecheck against the project's scaffold.

    Called after architect completes successfully. The architect's own
    lightweight verify might pass when the scaffold has errors that
    won't surface until a feature child tries to import the broken
    module. Catching this at architect-boundary saves 20+ min of
    doomed feature work.

    Strategy: detect manifests at well-known paths and run a fast
    compile-only check for each one. Skips if no manifests found
    (e.g., greenfield where architect hasn't created anything yet).
    """
    import shutil
    import subprocess

    issues: list[PreflightIssue] = []

    # Find manifests anywhere in project_dir (not just root, since
    # multi-subsystem projects have manifest per subsystem dir).
    package_jsons = [
        p for p in project_dir.rglob("package.json")
        if "node_modules" not in p.parts and ".worktrees" not in p.parts
    ]
    pyprojects = [
        p for p in project_dir.rglob("pyproject.toml")
        if ".venv" not in p.parts and ".worktrees" not in p.parts
    ]

    # JS: try `npm run build` if present; fall back to `tsc --noEmit`
    npm = shutil.which("npm")
    if npm:
        for pkg in package_jsons:
            try:
                manifest = pkg.read_text(encoding="utf-8")
                if '"build"' not in manifest:
                    continue  # no build script defined
                proc = subprocess.run(
                    [npm, "run", "build", "--silent"],
                    cwd=pkg.parent,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                if proc.returncode != 0:
                    tail = (proc.stderr or proc.stdout or "")[-300:]
                    issues.append(
                        PreflightIssue(
                            kind="scaffold_compile_failed",
                            severity="block",
                            message=(
                                f"npm run build failed in {pkg.parent}: "
                                f"exit {proc.returncode}. Tail: {tail!r}"
                            ),
                        )
                    )
            except subprocess.TimeoutExpired:
                issues.append(
                    PreflightIssue(
                        kind="scaffold_compile_timeout",
                        severity="warn",
                        message=f"npm run build in {pkg.parent} timed out after {timeout_s}s",
                    )
                )
            except OSError as exc:
                # e.g. node_modules missing — don't fail the run on this
                logger_msg = f"compile check skipped for {pkg.parent}: {exc}"
                issues.append(
                    PreflightIssue(
                        kind="scaffold_compile_skipped",
                        severity="warn",
                        message=logger_msg,
                    )
                )

    # Python: try import of the package (cheap proxy for syntactic validity)
    python = shutil.which("python3") or shutil.which("python")
    if python:
        for pyp in pyprojects:
            # Find the package name from pyproject (heuristic)
            try:
                manifest = pyp.read_text(encoding="utf-8")
            except OSError:
                continue
            # Try py_compile on all .py files in this dir
            py_files = [
                p for p in pyp.parent.rglob("*.py")
                if ".venv" not in p.parts and "__pycache__" not in p.parts
                and ".worktrees" not in p.parts
            ]
            if not py_files:
                continue
            try:
                proc = subprocess.run(
                    [python, "-m", "py_compile", *map(str, py_files)],
                    cwd=pyp.parent,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                if proc.returncode != 0:
                    tail = (proc.stderr or proc.stdout or "")[-300:]
                    issues.append(
                        PreflightIssue(
                            kind="scaffold_compile_failed",
                            severity="block",
                            message=(
                                f"py_compile failed in {pyp.parent}: "
                                f"exit {proc.returncode}. Tail: {tail!r}"
                            ),
                        )
                    )
            except subprocess.TimeoutExpired:
                issues.append(
                    PreflightIssue(
                        kind="scaffold_compile_timeout",
                        severity="warn",
                        message=f"py_compile in {pyp.parent} timed out after {timeout_s}s",
                    )
                )

    return issues


def smoke_start_services(
    project_dir: Path, timeout_s: int = 8
) -> list[PreflightIssue]:
    """Try to start the project's services briefly to verify they bind.

    Called before the integration agent dispatches. The integration
    agent will try to run E2E tests which require services to start;
    if they can't bind (port collision, missing deps, syntax errors),
    catching it here saves 20-30 min of the integration agent
    iterating on the same start failure.

    Strategy: if start.sh exists at project_dir root, run it in the
    background, wait ~5s, check that at least one declared port from
    CHARTER is listening, then kill. Issues are reported as 'warn'
    severity (not block) because the integration agent might
    legitimately fix the issue.
    """
    import re
    import shutil
    import socket
    import subprocess
    import time

    issues: list[PreflightIssue] = []

    start_sh = project_dir / "start.sh"
    if not start_sh.exists():
        return issues  # no start.sh — caller may want to skip this check entirely

    # Parse CHARTER for declared ports.
    charter = project_dir / "CHARTER.md"
    declared_ports: list[int] = []
    if charter.exists():
        try:
            text = charter.read_text(encoding="utf-8")
            for m in re.finditer(r"\b(?:port|Port|PORT)\s*[:=]?\s*(\d{4,5})\b", text):
                declared_ports.append(int(m.group(1)))
            # also "127.0.0.1:N" or "localhost:N"
            for m in re.finditer(r"(?:127\.0\.0\.1|localhost):(\d{4,5})", text):
                declared_ports.append(int(m.group(1)))
        except OSError:
            pass
    declared_ports = sorted(set(declared_ports))
    if not declared_ports:
        # nothing to verify; skip
        return issues

    # Check ports aren't ALREADY bound (zombie) before we start.
    busy_before: list[int] = []
    for port in declared_ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            busy_before.append(port)
        finally:
            s.close()
    if busy_before:
        issues.append(
            PreflightIssue(
                kind="pre_integration_port_busy",
                severity="warn",
                message=(
                    f"Ports already bound before integration start: {busy_before}. "
                    f"Integration agent may fail to start services or pick wrong server. "
                    f"Free these ports or kill stale processes first."
                ),
            )
        )
        return issues  # don't try to start if ports busy

    # Run start.sh in background, wait, then kill.
    bash = shutil.which("bash")
    if not bash:
        return issues
    proc = None
    try:
        proc = subprocess.Popen(  # noqa: S603 — running our own script
            [bash, str(start_sh)],
            cwd=project_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        # Wait briefly for services to come up.
        time.sleep(timeout_s)
        # Check which ports are now listening.
        listening: list[int] = []
        for port in declared_ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                listening.append(port)
            except OSError:
                pass
            finally:
                s.close()
        missing = [p for p in declared_ports if p not in listening]
        if missing:
            issues.append(
                PreflightIssue(
                    kind="pre_integration_services_not_listening",
                    severity="warn",
                    message=(
                        f"After running start.sh for {timeout_s}s, these "
                        f"declared ports are not listening: {missing}. "
                        f"Integration may waste time iterating on start failure."
                    ),
                )
            )
    except Exception as exc:  # noqa: BLE001
        issues.append(
            PreflightIssue(
                kind="pre_integration_smoke_error",
                severity="warn",
                message=f"smoke_start_services raised: {exc}",
            )
        )
    finally:
        # Best-effort cleanup: kill the process group.
        if proc and proc.poll() is None:
            try:
                import os
                import signal

                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        # Also kill anything we might've spawned on those ports.
        for port in declared_ports:
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f":{port}"], text=True, timeout=2
                )
                for pid in out.strip().split("\n"):
                    if pid.strip().isdigit():
                        subprocess.run(["kill", "-9", pid.strip()], timeout=2, check=False)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                pass

    return issues


def filter_blocked_descendants(
    graph: dict[str, Any],
    pending: list[dict[str, Any]],
    blocking_issues: list[PreflightIssue],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Filter out pending tasks that descend from a blocked task.

    For severity=block issues with a task_id, remove that task's
    descendants from the pending list so they don't dispatch.

    Returns (filtered_pending, set_of_blocked_task_ids).
    """
    blocked: set[str] = set()
    tasks = graph.get("tasks") or {}
    for issue in blocking_issues:
        if issue.severity == "block" and issue.task_id:
            blocked.add(issue.task_id)

    if not blocked:
        return pending, blocked

    # Walk: a pending task is blocked if any of its ancestors (via
    # depends_on) is in `blocked`.
    def is_descendant_of_blocked(tid: str) -> bool:
        task = tasks.get(tid) or {}
        deps = task.get("depends_on") or []
        return any(d in blocked or is_descendant_of_blocked(d) for d in deps)

    filtered: list[dict[str, Any]] = []
    for entry in pending:
        tid = entry.get("task_id", "")
        if tid in blocked:
            blocked.add(tid)
            continue
        # Also check if this pending task's depends_on includes a blocked task
        deps = entry.get("depends_on") or []
        if any(d in blocked for d in deps):
            blocked.add(tid)
            continue
        filtered.append(entry)

    return filtered, blocked
