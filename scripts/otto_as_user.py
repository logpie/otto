#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from cast_utils import (
    CURSOR_HIDE,
    CURSOR_SHOW,
    cast_output,
    find_last_frame,
    mouse_disable_codes,
    mouse_enable_codes,
)
from real_cost_guard import require_real_cost_opt_in


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "bench-results" / "as-user"
DEFAULT_ROWS = 30
DEFAULT_COLS = 120
DEFAULT_SCENARIO_DELAY_S = 5.0
INFRA_RETRY_DELAY_S = 30.0
INFRA_RETRY_ATTEMPTS = 2
INFRA_SMOKING_GUN_DURATION_S = 2.0
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)")
QUICK_SCENARIOS = ["A1", "A2", "B1", "B3", "C1", "D2", "U2"]
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"
FAKE_OTTO_BIN = REPO_ROOT / "scripts" / "fake-otto.sh"
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
LOCAL_ASCIINEMA = REPO_ROOT / ".venv" / "bin" / "asciinema"
ASCIINEMA_SHIM = REPO_ROOT / "scripts" / "asciinema_shim.py"
INFRA_COLOR = "\033[2;33m"
ANSI_RESET = "\033[0m"
REAL_OTTO_HELP_MARKER = "build and certify software products"
OTTO_SHADOWED_ERROR = (
    "ERROR: cc-autonomous Otto CLI is shadowed in venv. Reinstall with: "
    "`uv pip install -e . --python .venv/bin/python --reinstall-package otto`"
)
PACKAGING_INTENT_RE = re.compile(
    r"\b("
    r"pyproject(?:\.toml)?|setup\.py|setup\.cfg|editable install|install -e|pip install|"
    r"python package|publish(?:able)? package|console[_ -]?script|entry point|wheel|sdist|"
    r"package manager|poetry|hatch(?:ling)?|setuptools|build backend"
    r")\b",
    re.IGNORECASE,
)

FailureClassification = Literal["INFRA", "FAIL"]
ScenarioStatus = Literal["PASS", "FAIL", "INFRA"]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_run_id() -> str:
    return time.strftime("%Y-%m-%d-%H%M%S", time.gmtime()) + "-" + os.urandom(3).hex()


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in argv)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def terminal_history_rows(repo: Path) -> list[dict[str, Any]]:
    history_path = repo / "otto_logs" / "cross-sessions" / "history.jsonl"
    latest_by_dedupe: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(history_path):
        if row.get("history_kind", "terminal_snapshot") != "terminal_snapshot":
            continue
        dedupe_key = str(row.get("dedupe_key") or row.get("run_id") or "").strip()
        if dedupe_key:
            latest_by_dedupe[dedupe_key] = row
    return list(latest_by_dedupe.values())


def queue_terminal_snapshot(repo: Path, task_id: str) -> dict[str, Any] | None:
    for row in reversed(terminal_history_rows(repo)):
        if str(row.get("queue_task_id") or "").strip() == task_id:
            return row
    return None


VERIFY_STATUS_BY_TERMINAL_OUTCOME = {
    "success": "done",
    "failure": "failed",
    "cancelled": "cancelled",
    "removed": "removed",
    "interrupted": "interrupted",
}


def verifier_status_from_terminal_snapshot(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    terminal_outcome = str(row.get("terminal_outcome") or "").strip().lower()
    mapped = VERIFY_STATUS_BY_TERMINAL_OUTCOME.get(terminal_outcome)
    if mapped:
        return mapped
    status = str(row.get("status") or "").strip().lower()
    return status or None


def verifier_status_from_queue_task(task: dict[str, Any] | None) -> str | None:
    if not isinstance(task, dict):
        return None
    status = str(task.get("status") or "").strip().lower()
    terminal_status = str(task.get("terminal_status") or "").strip().lower()
    if status and status != "terminating":
        return status
    if terminal_status:
        return terminal_status
    return status or None


def resolve_queue_task_verify_status(
    repo: Path,
    task_id: str,
    *,
    state: dict[str, Any] | None = None,
    history_snapshot: dict[str, Any] | None = None,
    history_timeout_s: float = 0.0,
    interval_s: float = 0.2,
) -> str | None:
    deadline = time.monotonic() + max(history_timeout_s, 0.0)
    snapshot = history_snapshot
    task = {}
    if isinstance(state, dict):
        task = state.get("tasks", {}).get(task_id, {})
    state_status = verifier_status_from_queue_task(task)
    while True:
        status = verifier_status_from_terminal_snapshot(snapshot)
        if status is not None:
            return status
        if state_status in {"done", "failed", "cancelled", "removed", "interrupted"}:
            return state_status
        snapshot = queue_terminal_snapshot(repo, task_id)
        status = verifier_status_from_terminal_snapshot(snapshot)
        if status is not None:
            return status
        if time.monotonic() >= deadline:
            break
        time.sleep(interval_s)

    if state_status is not None:
        return state_status
    live_task = load_queue_state(repo).get("tasks", {}).get(task_id, {})
    return verifier_status_from_queue_task(live_task)


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def format_seconds_for_log(seconds: float) -> str:
    if float(seconds).is_integer():
        return f"{int(seconds)}s"
    return f"{seconds:g}s"


def attempt_suffix(attempt_index: int) -> str:
    if attempt_index <= 1:
        return ""
    if attempt_index == 2:
        return "-retry"
    return f"-retry{attempt_index - 1}"


def attempt_filename(filename: str, attempt_index: int) -> str:
    suffix = attempt_suffix(attempt_index)
    if not suffix:
        return filename
    path = Path(filename)
    return f"{path.stem}{suffix}{path.suffix}"


def read_text_if_exists(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def find_asciinema() -> Path | None:
    found = shutil.which("asciinema")
    if found:
        return Path(found)
    if LOCAL_ASCIINEMA.exists():
        return LOCAL_ASCIINEMA
    return None


def install_asciinema() -> Path:
    existing = find_asciinema()
    if existing is not None:
        return existing

    attempts: list[tuple[str, list[str]]] = []
    uv_bin = shutil.which("uv")
    brew_bin = shutil.which("brew")
    if uv_bin and PYTHON_BIN.exists():
        attempts.append(
            (
                "uv",
                [uv_bin, "pip", "install", "--python", str(PYTHON_BIN), "asciinema"],
            )
        )
    if PYTHON_BIN.exists():
        attempts.append(
            (
                "pip",
                [str(PYTHON_BIN), "-m", "pip", "install", "asciinema"],
            )
        )
    if brew_bin:
        attempts.append(("brew", [brew_bin, "install", "asciinema"]))

    failures: list[str] = []
    for label, argv in attempts:
        print(f"[otto-as-user] installing asciinema via {label}: {shell_join(argv)}", file=sys.stderr)
        env = dict(os.environ)
        env.setdefault("UV_CACHE_DIR", str(Path(tempfile.gettempdir()) / "otto-as-user-uv-cache"))
        env.setdefault("PIP_CACHE_DIR", str(Path(tempfile.gettempdir()) / "otto-as-user-pip-cache"))
        result = subprocess.run(argv, cwd=REPO_ROOT, env=env, text=True, capture_output=True)
        if result.returncode == 0:
            installed = find_asciinema()
            if installed is not None:
                return installed
            failures.append(f"{label}: install reported success but binary not found")
            continue
        failures.append(
            f"{label}: rc={result.returncode}\nstdout:\n{result.stdout[-1000:]}\nstderr:\n{result.stderr[-1000:]}"
        )

    failure_text = "\n\n".join(failures) if failures else "no installer available"
    if ASCIINEMA_SHIM.exists():
        print(
            "[otto-as-user] falling back to bundled asciinema shim because external install is unavailable",
            file=sys.stderr,
        )
        print(failure_text, file=sys.stderr)
        return ASCIINEMA_SHIM
    raise RuntimeError(
        "asciinema is required but was not found on PATH or in .venv/bin.\n"
        f"Install failure details:\n{failure_text}"
    )


def harness_python_bin() -> Path:
    return PYTHON_BIN if PYTHON_BIN.exists() else Path(sys.executable)


def python_site_packages(python_bin: Path) -> Path:
    if python_bin.resolve(strict=False) == Path(sys.executable).resolve(strict=False):
        return Path(sysconfig.get_path("purelib"))
    result = subprocess.run(
        [str(python_bin), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to resolve site-packages for {python_bin}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return Path(result.stdout.strip())


def otto_shadow_preview() -> str:
    try:
        result = subprocess.run(
            [str(OTTO_BIN), "--help"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    combined = "\n".join(
        line for line in ((result.stdout or "") + (result.stderr or "")).splitlines()[:5] if line.strip()
    )
    return strip_ansi(combined)


def ensure_real_otto_cli() -> None:
    preview = otto_shadow_preview()
    if REAL_OTTO_HELP_MARKER not in preview:
        raise SystemExit(OTTO_SHADOWED_ERROR)


def run_checked(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed (rc={result.returncode}): {shell_join(argv)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def init_repo(repo: Path, *, readme: str = "# Otto as user\n", intent: str | None = None) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    run_checked(["git", "init", "-q", "-b", "main"], cwd=repo)
    run_checked(["git", "config", "user.email", "otto-as-user@example.com"], cwd=repo)
    run_checked(["git", "config", "user.name", "Otto As User"], cwd=repo)
    (repo / "README.md").write_text(readme)
    if intent is not None:
        (repo / "intent.md").write_text(intent)
    run_checked(["git", "add", "."], cwd=repo)
    run_checked(["git", "commit", "-q", "-m", "initial"], cwd=repo)
    return repo


def commit_all(repo: Path, message: str) -> None:
    run_checked(["git", "add", "."], cwd=repo)
    status = git(repo, "status", "--porcelain")
    if status.strip():
        run_checked(["git", "commit", "-q", "-m", message], cwd=repo)


def should_warn_packaging_intent(intent: str) -> bool:
    normalized = intent.lower()
    if PACKAGING_INTENT_RE.search(intent):
        return True
    return "build a python cli" in normalized and "script" not in normalized


def maybe_warn_packaging_intent(intent: str) -> None:
    if not should_warn_packaging_intent(intent):
        return
    ctx = current_ctx()
    venv_path = ctx.isolated_venv or (ctx.artifact_dir / ".scenario-venv")
    log_line(
        "[isolation] packaging-like Python intent detected; "
        f"editable installs are isolated to {venv_path}"
    )


def write_otto_yaml(
    repo: Path,
    *,
    provider: str,
    certifier_mode: str | None = "fast",
    memory: bool = False,
    extra_lines: list[str] | None = None,
) -> None:
    lines = [
        "default_branch: main",
        "test_command: pytest -q",
        f"provider: {provider}",
    ]
    if certifier_mode:
        lines.append(f"certifier_mode: {certifier_mode}")
    if memory:
        lines.append("memory: true")
    if extra_lines:
        lines.extend(extra_lines)
    (repo / "otto.yaml").write_text("\n".join(lines) + "\n")


def tiny_cli_intent(name: str, expression: str, expected: str) -> str:
    return textwrap.dedent(
        f"""
        Build a Python CLI script `{name}.py`.
        When run with `python {name}.py`, it should print `{expected}`.
        Keep it minimal. Include a pytest test file `test_{name}.py` that asserts the exact stdout.
        """
    ).strip()


def add_mul_intent(name: str, op: str, expected: str) -> str:
    return textwrap.dedent(
        f"""
        Build a Python CLI script `{name}.py` with argparse.
        Running `python {name}.py 2 3` should print `{expected}` using {op}.
        Include `test_{name}.py` with a passing pytest assertion for that example.
        """
    ).strip()


def write_existing_greeter(repo: Path) -> None:
    (repo / "hello.py").write_text(
        "def main():\n"
        "    print('hello from existing project')\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    (repo / "test_hello.py").write_text(
        "import subprocess\n"
        "import sys\n\n"
        "def test_hello():\n"
        "    out = subprocess.check_output([sys.executable, 'hello.py'], text=True)\n"
        "    assert out == 'hello from existing project\\n'\n"
    )
    (repo / "intent.md").write_text("A tiny Python CLI that prints hello from existing project.\n")
    commit_all(repo, "add existing greeter project")


def write_buggy_calculator(repo: Path) -> None:
    (repo / "calculator.py").write_text(
        "import argparse\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('op', choices=['add', 'sub'])\n"
        "    parser.add_argument('a', type=int)\n"
        "    parser.add_argument('b', type=int)\n"
        "    args = parser.parse_args()\n"
        "    if args.op == 'add':\n"
        "        print(args.a + args.b)\n"
        "    else:\n"
        "        print(args.a + args.b)\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    (repo / "test_calculator.py").write_text(
        "import subprocess\n"
        "import sys\n\n"
        "def run_cli(*args):\n"
        "    return subprocess.check_output([sys.executable, 'calculator.py', *args], text=True)\n\n"
        "def test_add():\n"
        "    assert run_cli('add', '2', '3') == '5\\n'\n"
    )
    (repo / "intent.md").write_text(
        "A calculator CLI supporting add and sub. `sub` must subtract the second integer from the first.\n"
    )
    commit_all(repo, "add buggy calculator")


def write_conflict_base(repo: Path) -> None:
    (repo / "tools.py").write_text(
        "def render(value: int) -> str:\n"
        "    return f'value={value}'\n"
    )
    (repo / "test_tools.py").write_text(
        "from tools import render\n\n"
        "def test_render():\n"
        "    assert render(3) == 'value=3'\n"
    )
    (repo / "intent.md").write_text("A tiny Python helper library around tools.py.\n")
    commit_all(repo, "add tools base")


UX_PRIMARY_TASK_ID = "build-add-ux-check"
UX_SECONDARY_TASK_ID = "build-mul-ux-check"
UX_QUEUED_TASK_ID = "build-sub-ux-check"
UX_PRIMARY_BRANCH = "build/add-2026-04-21"
UX_SECONDARY_BRANCH = "build/mul-2026-04-21"
UX_PRIMARY_SESSION_ID = "2026-04-21-200030-aaa111"
UX_SECONDARY_SESSION_ID = "2026-04-21-200100-bbb222"


def scenario_env(**overrides: str) -> dict[str, str]:
    env = dict(current_ctx().env)
    for key, value in overrides.items():
        env[key] = value
    return env


def prepend_path(env: dict[str, str], *entries: Path) -> dict[str, str]:
    path_bits = [str(entry) for entry in entries if str(entry)]
    path_bits.append(env.get("PATH", ""))
    updated = dict(env)
    updated["PATH"] = os.pathsep.join(path_bits)
    return updated


def write_queue_state_file(repo: Path, tasks: dict[str, Any], *, watcher: dict[str, Any] | None = None) -> None:
    payload = {"schema_version": 1, "watcher": watcher, "tasks": tasks}
    (repo / ".otto-queue-state.json").write_text(json.dumps(payload, indent=2) + "\n")


def append_dashboard_task(
    repo: Path,
    *,
    task_id: str,
    branch: str,
    intent: str,
) -> None:
    from otto.queue.schema import QueueTask, append_task

    append_task(
        repo,
        QueueTask(
            id=task_id,
            command_argv=["build", "--fast", intent],
            resumable=True,
            added_at="2026-04-21T20:00:00Z",
            resolved_intent=intent,
            branch=branch,
            worktree=f".worktrees/{task_id}",
        ),
    )


def write_dashboard_task_artifacts(
    repo: Path,
    *,
    task_id: str,
    branch: str,
    session_id: str,
    narrative_text: str,
) -> dict[str, str]:
    worktree = repo / ".worktrees" / task_id
    session_root = worktree / "otto_logs" / "sessions" / session_id
    build_dir = session_root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    narrative_path = build_dir / "narrative.log"
    narrative_path.write_text(narrative_text)

    manifest_path = repo / "otto_logs" / "queue" / task_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "command": "build",
                "argv": ["build", "--fast", f"Build {task_id}.py CLI"],
                "queue_task_id": task_id,
                "run_id": session_id,
                "branch": branch,
                "checkpoint_path": str(session_root / "checkpoint.json"),
                "proof_of_work_path": str(session_root / "certify" / "proof-of-work.json"),
                "cost_usd": 0.0,
                "duration_s": 0.0,
                "started_at": "2026-04-21T20:00:30Z",
                "finished_at": "2026-04-21T20:00:35Z",
                "head_sha": None,
                "resolved_intent": f"Build {task_id}.py CLI",
                "focus": None,
                "target": None,
                "exit_status": "success",
                "schema_version": 1,
                "extra": {},
                "mirror_of": str(session_root / "manifest.json"),
            },
            indent=2,
        )
        + "\n"
    )
    return {
        "narrative_path": str(narrative_path.resolve(strict=False)),
        "manifest_path": str(manifest_path.resolve(strict=False)),
    }


def setup_u_realistic(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 2"])
    append_dashboard_task(
        repo,
        task_id=UX_PRIMARY_TASK_ID,
        branch=UX_PRIMARY_BRANCH,
        intent="Build add.py CLI",
    )
    append_dashboard_task(
        repo,
        task_id=UX_SECONDARY_TASK_ID,
        branch=UX_SECONDARY_BRANCH,
        intent="Build mul.py CLI",
    )
    append_dashboard_task(
        repo,
        task_id=UX_QUEUED_TASK_ID,
        branch="build/sub-2026-04-21",
        intent="Build sub.py CLI",
    )
    write_dashboard_task_artifacts(
        repo,
        task_id=UX_PRIMARY_TASK_ID,
        branch=UX_PRIMARY_BRANCH,
        session_id=UX_PRIMARY_SESSION_ID,
        narrative_text=(
            "[+0:00] BUILD starting for add\n"
            "[+0:01] reading project layout\n"
            "[+0:02] writing add.py with argparse\n"
            "[+0:05] STORY_RESULT: smoke | PASS | python add.py works\n"
        ),
    )
    write_dashboard_task_artifacts(
        repo,
        task_id=UX_SECONDARY_TASK_ID,
        branch=UX_SECONDARY_BRANCH,
        session_id=UX_SECONDARY_SESSION_ID,
        narrative_text=(
            "[+0:00] BUILD starting for mul\n"
            "[+0:01] reading project layout\n"
            "[+0:02] writing mul.py with argparse\n"
            "[+0:05] STORY_RESULT: smoke | PASS | python mul.py works\n"
        ),
    )
    write_dashboard_task_artifacts(
        repo,
        task_id=UX_QUEUED_TASK_ID,
        branch="build/sub-2026-04-21",
        session_id="2026-04-21-200130-ccc333",
        narrative_text=(
            "[+0:00] BUILD starting for sub\n"
            "[+0:01] reading project layout\n"
            "[+0:02] writing sub.py with argparse\n"
            "[+0:05] STORY_RESULT: smoke | PASS | python sub.py works\n"
        ),
    )
    write_queue_state_file(
        repo,
        {
            UX_PRIMARY_TASK_ID: {
                "status": "done",
                "started_at": "2026-04-21T20:00:30Z",
                "finished_at": "2026-04-21T20:00:35Z",
                "child": None,
                "manifest_path": str((repo / "otto_logs" / "queue" / UX_PRIMARY_TASK_ID / "manifest.json").resolve(strict=False)),
                "cost_usd": 0.0,
                "duration_s": 5.0,
                "failure_reason": None,
            },
            UX_SECONDARY_TASK_ID: {
                "status": "done",
                "started_at": "2026-04-21T20:01:00Z",
                "finished_at": "2026-04-21T20:01:05Z",
                "child": None,
                "manifest_path": str((repo / "otto_logs" / "queue" / UX_SECONDARY_TASK_ID / "manifest.json").resolve(strict=False)),
                "cost_usd": 0.0,
                "duration_s": 5.0,
                "failure_reason": None,
            },
            UX_QUEUED_TASK_ID: {
                "status": "done",
                "started_at": "2026-04-21T20:01:30Z",
                "finished_at": "2026-04-21T20:01:35Z",
                "child": None,
                "manifest_path": str((repo / "otto_logs" / "queue" / UX_QUEUED_TASK_ID / "manifest.json").resolve(strict=False)),
                "cost_usd": 0.0,
                "duration_s": 5.0,
                "failure_reason": None,
            },
        },
    )


def install_clipboard_capture(root: Path, capture_path: Path) -> Path:
    bindir = root / "mockbin"
    bindir.mkdir(parents=True, exist_ok=True)
    target = shlex.quote(str(capture_path))
    wrappers = {
        "pbcopy": f"#!/usr/bin/env bash\nset -euo pipefail\ncat > {target}\n",
        "xclip": f"#!/usr/bin/env bash\nset -euo pipefail\ncat > {target}\n",
        "wl-copy": f"#!/usr/bin/env bash\nset -euo pipefail\ncat > {target}\n",
    }
    for name, body in wrappers.items():
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)
    return bindir


def spawn_placeholder_child(cwd: Path, *, sleep_s: int) -> tuple[subprocess.Popen[Any], dict[str, Any]]:
    argv = ["/bin/sh", "-lc", f"sleep {sleep_s}"]
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        preexec_fn=os.setsid,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import psutil

        start_time_ns = int(psutil.Process(proc.pid).create_time() * 1_000_000_000)
    except Exception:
        start_time_ns = int(time.time() * 1_000_000_000)
    return proc, {
        "pid": proc.pid,
        "pgid": proc.pid,
        "start_time_ns": start_time_ns,
        "argv": argv,
        "cwd": str(cwd),
    }


def normalize_wrapped_text(text: str) -> str:
    return "".join(line.strip() for line in text.splitlines())


def path_fragments_visible(text: str, path: str) -> bool:
    candidate = Path(path)
    parts = [part for part in candidate.parts if part not in {candidate.anchor, ""}]
    required = [candidate.name]
    for marker in (".worktrees", "otto_logs"):
        if marker in parts:
            index = parts.index(marker)
            required.extend(parts[index:min(len(parts), index + 4)])
    if "otto_logs" in parts:
        required.extend(parts[-3:])
    else:
        required.extend(parts[-2:])
    required = [fragment for fragment in dict.fromkeys(required) if fragment]
    return all(fragment in text for fragment in required)


@dataclass
class CommandResult:
    argv: list[str]
    rc: int
    duration_s: float
    output: str


@dataclass
class RunResult:
    scenario_id: str
    returncode: int
    started_at: str
    finished_at: str
    duration_s: float
    recording_path: str
    repo_path: str
    debug_log: str
    output: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _extract_cost_values(payload: Any) -> list[float]:
    costs: list[float] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(nested, (int, float)) and "cost" in key.lower():
                    costs.append(float(nested))
                else:
                    walk(nested)
            return
        if isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    return costs


def classify_failure(
    narrative_log_path: Path | None,
    debug_log_path: Path | None,
    run_result: RunResult,
) -> FailureClassification:
    narrative_text = read_text_if_exists(narrative_log_path)
    debug_text = read_text_if_exists(debug_log_path)
    text = "\n".join(
        part for part in [narrative_text, debug_text, run_result.output] if part
    )
    if "Not logged in" in text:
        return "INFRA"
    if "Please run /login" in text:
        return "INFRA"
    costs = _extract_cost_values(run_result.details)
    zero_cost = any(abs(cost) <= 1e-9 for cost in costs)
    provider_context = re.search(
        r"(?:anthropic|claude|codex|openai|provider|subscription|quota)",
        text,
        re.IGNORECASE,
    )
    provider_rate_limit = re.search(
        r"(?:rate limit|too many requests|429|throttle)",
        text,
        re.IGNORECASE,
    )
    if provider_context and provider_rate_limit:
        return "INFRA"
    generic_transient_429 = re.search(
        r"(?:429.{0,80}(?:throttle|rate|too many requests)|(?:throttle|rate|too many requests).{0,80}429)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if generic_transient_429 and zero_cost:
        return "INFRA"
    smoking_gun = (
        "Command failed with exit code 1" in text
        and "Check stderr output for details" in text
        and run_result.duration_s < INFRA_SMOKING_GUN_DURATION_S
        and (zero_cost or not costs)
    )
    if smoking_gun:
        return "INFRA"
    return "FAIL"


@dataclass
class VerifyResult:
    passed: bool
    note: str
    details: list[str] = field(default_factory=list)


@dataclass
class ScenarioOutcome:
    scenario: "Scenario"
    outcome: ScenarioStatus
    run_result: RunResult
    verify_result: VerifyResult
    artifact_dir: Path
    recording_name: str = "recording.cast"
    attempt_count: int = 1
    wall_duration_s: float = 0.0
    retried_after_infra: bool = False


@dataclass
class Scenario:
    name: str
    group: str
    description: str
    quick: bool
    estimated_cost: float
    estimated_seconds: int
    requires_pty: bool
    setup_fn: Callable[[Path, str], None] = field(repr=False)
    run_fn: Callable[[Path, str], RunResult] = field(repr=False)
    verify_fn: Callable[[Path, RunResult], VerifyResult] = field(repr=False)

    def setup(self, tmp_repo: Path, provider: str) -> None:
        self.setup_fn(tmp_repo, provider)

    def run(self, tmp_repo: Path, provider: str) -> RunResult:
        return self.run_fn(tmp_repo, provider)

    def verify(self, tmp_repo: Path, run_result: RunResult) -> VerifyResult:
        return self.verify_fn(tmp_repo, run_result)


@dataclass
class ExecutionContext:
    scenario: Scenario
    artifact_dir: Path
    repo: Path
    provider: str
    debug_log: Path
    recording_path: Path
    isolated_venv: Path | None = field(default=None, repr=False)
    prepended_path_entries: list[Path] = field(default_factory=list, repr=False)

    @property
    def env(self) -> dict[str, str]:
        path_parts = [str(entry) for entry in self.prepended_path_entries if entry.exists()]
        if OTTO_BIN.parent.exists():
            path_parts.append(str(OTTO_BIN.parent))
        path_parts.append(os.environ.get("PATH", ""))
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(path_parts)
        env["TERM"] = env.get("TERM", "xterm-256color")
        if self.isolated_venv is not None:
            env["VIRTUAL_ENV"] = str(self.isolated_venv)
            env["OTTO_AS_USER_SCENARIO_VENV"] = str(self.isolated_venv)
        env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        return env


EXECUTION_CONTEXT: ExecutionContext | None = None


def current_ctx() -> ExecutionContext:
    if EXECUTION_CONTEXT is None:
        raise RuntimeError("execution context not initialized")
    return EXECUTION_CONTEXT


def is_otto_site_entry(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == "otto"
        or name.startswith("otto-")
        or name.startswith("otto.")
        or name.startswith("__editable__.otto")
    )


def mirror_non_otto_site_packages(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if is_otto_site_entry(entry):
            continue
        link_path = target / entry.name
        if link_path.exists() or link_path.is_symlink():
            continue
        link_path.symlink_to(entry)


def write_pytest_launcher(bindir: Path, python_bin: Path) -> None:
    for launcher_name in ("pytest", "py.test"):
        launcher = bindir / launcher_name
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"exec {shlex.quote(str(python_bin))} -m pytest \"$@\"\n"
        )
        launcher.chmod(0o755)


def prepare_scenario_isolation(ctx: ExecutionContext) -> None:
    python_bin = harness_python_bin()
    venv_dir = ctx.artifact_dir / ".scenario-venv"
    scenario_python = venv_dir / "bin" / "python"
    if not scenario_python.exists():
        run_checked([str(python_bin), "-m", "venv", str(venv_dir)], cwd=REPO_ROOT)
    host_site = python_site_packages(python_bin)
    scenario_site = python_site_packages(scenario_python)
    mirror_non_otto_site_packages(host_site, scenario_site)
    write_pytest_launcher(venv_dir / "bin", scenario_python)
    install_env = dict(os.environ)
    install_env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    run_checked(
        [
            str(scenario_python),
            "-m",
            "pip",
            "install",
            "-e",
            str(REPO_ROOT),
            "--no-deps",
            "--force-reinstall",
        ],
        cwd=REPO_ROOT,
        env=install_env,
    )
    ctx.isolated_venv = venv_dir
    ctx.prepended_path_entries = [venv_dir / "bin"]


def log_line(text: str) -> None:
    ctx = current_ctx()
    ensure_parent(ctx.debug_log)
    with ctx.debug_log.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip("\n") + "\n")
    print(text, flush=True)


def run_streaming(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> CommandResult:
    cmd_text = shell_join(argv)
    log_line(f"$ {cmd_text}")
    started = time.time()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    chunks: list[str] = []
    deadline = started + timeout_s if timeout_s is not None else None
    assert proc.stdout is not None
    while True:
        if deadline is not None and time.time() > deadline and proc.poll() is None:
            proc.terminate()
            time.sleep(1.0)
            if proc.poll() is None:
                proc.kill()
            chunks.append(f"\n[otto-as-user] timed out after {timeout_s:.0f}s\n")
            break
        line = proc.stdout.readline()
        if line:
            chunks.append(line)
            print(line, end="", flush=True)
            with current_ctx().debug_log.open("a", encoding="utf-8") as handle:
                handle.write(line)
            continue
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    remainder = proc.stdout.read()
    if remainder:
        chunks.append(remainder)
        print(remainder, end="", flush=True)
        with current_ctx().debug_log.open("a", encoding="utf-8") as handle:
            handle.write(remainder)
    rc = proc.wait()
    return CommandResult(argv=argv, rc=rc, duration_s=time.time() - started, output="".join(chunks))


def latest_session_dir(repo: Path) -> Path:
    latest = repo / "otto_logs" / "latest"
    if not latest.exists():
        raise AssertionError("otto_logs/latest not found")
    return latest.resolve()


def latest_narrative_log(repo: Path) -> Path | None:
    candidates: list[Path] = []
    direct_latest = repo / "otto_logs" / "latest" / "build" / "narrative.log"
    if direct_latest.exists():
        candidates.append(direct_latest)
    candidates.extend(repo.glob("otto_logs/sessions/*/build/narrative.log"))
    candidates.extend(repo.glob(".worktrees/*/otto_logs/latest/build/narrative.log"))
    candidates.extend(repo.glob(".worktrees/*/otto_logs/sessions/*/build/narrative.log"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_summary(repo: Path) -> dict[str, Any]:
    return read_json(latest_session_dir(repo) / "summary.json")


def find_worktree(repo: Path, *, prefix: str) -> Path:
    matches = sorted(
        path for path in (repo / ".worktrees").glob(f"{prefix}*") if path.is_dir()
    )
    if not matches:
        raise AssertionError(f"no worktree found matching {prefix!r}")
    if len(matches) == 1:
        return matches[0]
    with_latest = [path for path in matches if (path / "otto_logs" / "latest").exists()]
    if len(with_latest) == 1:
        return with_latest[0]
    raise AssertionError(
        f"expected one {prefix!r} worktree, found {[path.name for path in matches]!r}"
    )


def assert_exists(path: Path, message: str) -> None:
    if not path.exists():
        raise AssertionError(f"{message}: missing {path}")


def queue_build(repo: Path, task_id: str, provider: str, intent: str, *extra_inner: str) -> None:
    maybe_warn_packaging_intent(intent)
    argv = [
        str(OTTO_BIN),
        "queue",
        "build",
        intent,
        "--as",
        task_id,
        "--",
        "--provider",
        provider,
        *extra_inner,
    ]
    run_checked(argv, cwd=repo, env=current_ctx().env)


def load_queue_state(repo: Path) -> dict[str, Any]:
    state_path = repo / ".otto-queue-state.json"
    if not state_path.exists():
        return {"tasks": {}, "watcher": None}
    return read_json(state_path)


def wait_for(predicate: Callable[[], bool], *, timeout_s: float, label: str, interval_s: float = 0.2) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s:.1f}s waiting for {label}")


def append_cancel_envelope_and_wait_for_ack(
    repo: Path,
    run_id: str,
    *,
    proc: subprocess.Popen[str] | None = None,
    run_exited_message: str,
    timeout_message: str,
    poll_interval_s: float = 0.05,
) -> dict[str, Any]:
    from otto import paths
    from otto.runs.registry import (
        HEARTBEAT_INTERVAL_S,
        append_command_request,
        load_command_ack_ids,
        load_live_record,
        utc_now_iso,
    )

    request_path = paths.session_command_requests(repo, run_id)
    ack_path = paths.session_command_acks(repo, run_id)
    live_record = load_live_record(repo, run_id)
    heartbeat_s = max(float(live_record.timing.get("heartbeat_interval_s") or HEARTBEAT_INTERVAL_S), 0.1)
    command_id = f"{int(time.time() * 1000)}-{os.getpid()}-1"
    append_command_request(
        request_path,
        {
            "schema_version": 1,
            "command_id": command_id,
            "run_id": run_id,
            "domain": live_record.domain,
            "kind": "cancel",
            "requested_at": utc_now_iso(),
            "requested_by": {
                "source": "harness",
                "pid": os.getpid(),
            },
            "args": {},
        },
    )

    ack_started = time.monotonic()
    ack_deadline_s = max(4.0, 2.0 * heartbeat_s)
    ack_deadline = ack_started + ack_deadline_s
    while time.monotonic() < ack_deadline:
        if command_id in load_command_ack_ids(ack_path):
            ack_latency_ms = int((time.monotonic() - ack_started) * 1000)
            return {
                "command_id": command_id,
                "request_path": str(request_path),
                "ack_path": str(ack_path),
                "heartbeat_interval_s": heartbeat_s,
                "ack_latency_ms": ack_latency_ms,
                "ack_deadline_ms": int(ack_deadline_s * 1000),
            }
        if proc is not None and proc.poll() is not None:
            raise AssertionError(run_exited_message)
        time.sleep(poll_interval_s)
    raise AssertionError(f"{timeout_message} within {ack_deadline_s:.1f}s")


def interrupt_build_after_checkpoint(
    repo: Path,
    provider: str,
    intent: str,
    *,
    post_checkpoint_sleep_s: float = 0.0,
    shutdown_timeout_s: float = 180.0,
) -> str:
    from otto.checkpoint import load_checkpoint

    proc = subprocess.Popen(
        [str(OTTO_BIN), "build", "--provider", provider, intent],
        cwd=repo,
        env=current_ctx().env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wait_for(
        lambda: bool((load_checkpoint(repo) or {}).get("status") == "in_progress"),
        timeout_s=30,
        label="build checkpoint",
    )
    if post_checkpoint_sleep_s > 0:
        time.sleep(post_checkpoint_sleep_s)
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.communicate(timeout=shutdown_timeout_s)[0] or ""
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        proc.kill()
        try:
            tail = proc.communicate(timeout=20)[0] or ""
        except subprocess.TimeoutExpired:
            tail = ""
        return partial + tail


@dataclass
class PaneSnapshot:
    name: str
    screen_text: str
    raw_tail: str
    timestamp: float


class AnsiScreen:
    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self._lines = [[" "] * cols for _ in range(rows)]
        self.row = 0
        self.col = 0
        self.saved = (0, 0)
        self.state = "normal"
        self.csi = ""
        self.osc = ""

    def resize(self, rows: int, cols: int) -> None:
        rendered = self.render().splitlines()
        self.rows = rows
        self.cols = cols
        self._lines = [[" "] * cols for _ in range(rows)]
        for r, line in enumerate(rendered[:rows]):
            for c, ch in enumerate(line[:cols]):
                self._lines[r][c] = ch
        self.row = min(self.row, rows - 1)
        self.col = min(self.col, cols - 1)

    def feed(self, text: str) -> None:
        i = 0
        while i < len(text):
            ch = text[i]
            if self.state == "normal":
                if ch == "\x1b":
                    self.state = "esc"
                elif ch == "\r":
                    self.col = 0
                elif ch == "\n":
                    self.row = min(self.rows - 1, self.row + 1)
                elif ch == "\b":
                    self.col = max(0, self.col - 1)
                elif ch == "\t":
                    self.col = min(self.cols - 1, ((self.col // 8) + 1) * 8)
                elif ch >= " ":
                    self._put(ch)
            elif self.state == "esc":
                if ch == "[":
                    self.state = "csi"
                    self.csi = ""
                elif ch == "]":
                    self.state = "osc"
                    self.osc = ""
                elif ch == "7":
                    self.saved = (self.row, self.col)
                    self.state = "normal"
                elif ch == "8":
                    self.row, self.col = self.saved
                    self.state = "normal"
                else:
                    self.state = "normal"
            elif self.state == "osc":
                self.osc += ch
                if ch == "\x07":
                    self.state = "normal"
                elif ch == "\\" and self.osc.endswith("\x1b\\"):
                    self.state = "normal"
            else:
                self.csi += ch
                if "@" <= ch <= "~":
                    self._handle_csi(self.csi)
                    self.state = "normal"
            i += 1

    def _put(self, ch: str) -> None:
        if 0 <= self.row < self.rows and 0 <= self.col < self.cols:
            self._lines[self.row][self.col] = ch
        if self.col < self.cols - 1:
            self.col += 1

    def _handle_csi(self, seq: str) -> None:
        final = seq[-1]
        params_text = seq[:-1]
        private = params_text.startswith("?")
        if private:
            params_text = params_text[1:]
        params = self._parse_params(params_text)
        if final in {"H", "f"}:
            row = (params[0] if len(params) >= 1 and params[0] else 1) - 1
            col = (params[1] if len(params) >= 2 and params[1] else 1) - 1
            self.row = max(0, min(self.rows - 1, row))
            self.col = max(0, min(self.cols - 1, col))
            return
        if final == "A":
            self.row = max(0, self.row - (params[0] if params else 1))
            return
        if final == "B":
            self.row = min(self.rows - 1, self.row + (params[0] if params else 1))
            return
        if final == "C":
            self.col = min(self.cols - 1, self.col + (params[0] if params else 1))
            return
        if final == "D":
            self.col = max(0, self.col - (params[0] if params else 1))
            return
        if final == "J":
            self._clear()
            return
        if final == "K":
            mode = params[0] if params else 0
            if mode == 2:
                start, stop = 0, self.cols
            elif mode == 1:
                start, stop = 0, self.col + 1
            else:
                start, stop = self.col, self.cols
            for c in range(start, stop):
                self._lines[self.row][c] = " "
            return
        if final == "m":
            return
        if private and final in {"h", "l"} and params_text == "1049":
            self._clear()
            return

    @staticmethod
    def _parse_params(params_text: str) -> list[int]:
        if not params_text:
            return []
        out: list[int] = []
        for part in params_text.split(";"):
            if not part:
                out.append(0)
                continue
            match = re.match(r"\d+", part)
            out.append(int(match.group(0)) if match else 0)
        return out

    def _clear(self) -> None:
        self._lines = [[" "] * self.cols for _ in range(self.rows)]
        self.row = 0
        self.col = 0

    def render(self) -> str:
        return "\n".join("".join(line).rstrip() for line in self._lines).rstrip()


class PtySession:
    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str], rows: int = DEFAULT_ROWS, cols: int = DEFAULT_COLS) -> None:
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.rows = rows
        self.cols = cols
        self.master_fd, slave_fd = pty.openpty()
        self.screen = AnsiScreen(rows, cols)
        self.raw_chunks: list[str] = []
        self._set_winsize(slave_fd, rows, cols)
        self.pid = os.fork()
        if self.pid == 0:
            try:
                os.chdir(cwd)
                os.environ.clear()
                os.environ.update(env)
                os.login_tty(slave_fd)
                os.execvpe(argv[0], argv, os.environ)
            finally:
                os._exit(127)
        os.close(slave_fd)
        os.set_blocking(self.master_fd, False)

    def _set_winsize(self, fd: int, rows: int, cols: int) -> None:
        import fcntl
        import termios

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self._set_winsize(self.master_fd, rows, cols)
        self.screen.resize(rows, cols)
        try:
            os.kill(self.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass
        self.drain(0.4)

    def send(self, text: str) -> None:
        os.write(self.master_fd, text.encode())

    def drain(self, timeout: float = 0.2) -> str:
        deadline = time.time() + timeout
        pieces: list[str] = []
        while True:
            try:
                chunk = os.read(self.master_fd, 65536)
            except BlockingIOError:
                if time.time() >= deadline:
                    break
                time.sleep(0.02)
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            self.raw_chunks.append(text)
            self.raw_chunks = self.raw_chunks[-256:]
            pieces.append(text)
            self.screen.feed(text)
            print(text, end="", flush=True)
            with current_ctx().debug_log.open("a", encoding="utf-8") as handle:
                handle.write(text)
        return "".join(pieces)

    def snapshot(self, name: str) -> PaneSnapshot:
        self.drain(0.3)
        raw_tail = "".join(self.raw_chunks)[-64000:]
        snap = PaneSnapshot(
            name=name,
            screen_text=self.screen.render(),
            raw_tail=raw_tail,
            timestamp=time.time(),
        )
        base = current_ctx().artifact_dir / f"{name}"
        base.with_suffix(".screen.txt").write_text(snap.screen_text)
        base.with_suffix(".raw.txt").write_text(snap.raw_tail)
        return snap

    def wait(self, timeout: float) -> int | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                return os.waitstatus_to_exitcode(status)
            self.drain(0.05)
            time.sleep(0.05)
        return None

    def terminate(self) -> int:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return 0
        rc = self.wait(3.0)
        if rc is not None:
            return rc
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return 0
        final = self.wait(2.0)
        return 1 if final is None else final


def wait_for_screen_text(session: PtySession, needle: str, *, timeout_s: float, label: str) -> PaneSnapshot:
    deadline = time.time() + timeout_s
    last = session.snapshot(label)
    while time.time() < deadline:
        last = session.snapshot(label)
        if needle in strip_ansi(last.screen_text):
            return last
        time.sleep(0.1)
    raise AssertionError(f"screen never showed {needle!r} while waiting for {label}")


def wait_for_empty_queue_screen(session: PtySession, *, timeout_s: float, label: str) -> PaneSnapshot:
    deadline = time.time() + timeout_s
    last = session.snapshot(f"{label}-initial")
    while time.time() < deadline:
        last = session.snapshot(label)
        screen = strip_ansi(last.screen_text)
        if "No runs yet." in screen or "No tasks queued." in screen or "rows=0 live, 0 history" in screen:
            return last
        time.sleep(0.1)
    raise AssertionError(f"empty queue screen never appeared while waiting for {label}")


def focus_queue_task(session: PtySession, task_id: str, *, max_moves: int = 8) -> PaneSnapshot:
    for attempt in range(max_moves):
        snap = session.snapshot(f"focus-{task_id}-{attempt}")
        screen = strip_ansi(snap.screen_text)
        if f"queue: {task_id}" in screen or f"task: {task_id}" in screen:
            return snap
        session.send("j")
        time.sleep(0.2)
    raise AssertionError(f"could not focus queue task {task_id!r}")


def run_dashboard_session(
    repo: Path,
    *,
    concurrent: int,
    actions: Callable[[PtySession], dict[str, Any]],
    no_dashboard: bool = False,
    extra_flags: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    argv = [str(OTTO_BIN), "queue", "run", "--concurrent", str(concurrent)]
    if no_dashboard:
        argv.append("--no-dashboard")
    if extra_flags:
        argv.extend(extra_flags)
    session = PtySession(argv, cwd=repo, env=env or current_ctx().env)
    try:
        details = actions(session)
    finally:
        session.terminate()
    return details


def _base_result(returncode: int, output: str = "", **details: Any) -> RunResult:
    ctx = current_ctx()
    return RunResult(
        scenario_id=ctx.scenario.name,
        returncode=returncode,
        started_at=details.pop("started_at", now_iso()),
        finished_at=details.pop("finished_at", now_iso()),
        duration_s=float(details.pop("duration_s", 0.0)),
        recording_path=str(ctx.recording_path),
        repo_path=str(ctx.repo),
        debug_log=str(ctx.debug_log),
        output=output,
        details=details,
    )


def run_build(repo: Path, provider: str, *args: str, timeout_s: float = 1200) -> CommandResult:
    for arg in args:
        maybe_warn_packaging_intent(arg)
    return run_streaming(
        [str(OTTO_BIN), "build", "--provider", provider, *args],
        cwd=repo,
        env=current_ctx().env,
        timeout_s=timeout_s,
    )


def run_certify(repo: Path, provider: str, *args: str, timeout_s: float = 900) -> CommandResult:
    return run_streaming(
        [str(OTTO_BIN), "certify", "--provider", provider, *args],
        cwd=repo,
        env=current_ctx().env,
        timeout_s=timeout_s,
    )


def run_improve(repo: Path, provider: str, subcommand: str, *args: str, timeout_s: float = 1200) -> CommandResult:
    return run_streaming(
        [str(OTTO_BIN), "improve", subcommand, "--provider", provider, *args],
        cwd=repo,
        env=current_ctx().env,
        timeout_s=timeout_s,
    )


def run_queue(repo: Path, *args: str, timeout_s: float = 1200) -> CommandResult:
    return run_streaming(
        [str(OTTO_BIN), "queue", *args],
        cwd=repo,
        env=current_ctx().env,
        timeout_s=timeout_s,
    )


def run_merge(repo: Path, *args: str, timeout_s: float = 1800) -> CommandResult:
    return run_streaming(
        [str(OTTO_BIN), "merge", *args],
        cwd=repo,
        env=current_ctx().env,
        timeout_s=timeout_s,
    )


def run_setup(repo: Path, timeout_s: float = 1200) -> CommandResult:
    return run_streaming(
        ["/bin/sh", "-lc", f"printf '\\n' | {shell_join([str(OTTO_BIN), 'setup'])}"],
        cwd=repo,
        env=current_ctx().env,
        timeout_s=timeout_s,
    )


def verify_summary_passed(repo: Path, run_result: RunResult, *, message: str) -> VerifyResult:
    summary = load_summary(repo)
    if summary.get("verdict") != "passed":
        return VerifyResult(False, f"{message}: expected passed verdict, got {summary.get('verdict')!r}")
    return VerifyResult(True, message, [f"session={summary.get('run_id', '?')}"])


def setup_a1(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_a1(repo: Path, provider: str) -> RunResult:
    intent = tiny_cli_intent("hello", "print hello", "hello")
    started = now_iso()
    result = run_build(repo, provider, intent)
    summary = load_summary(repo)
    return _base_result(
        result.rc,
        output=result.output,
        started_at=started,
        finished_at=now_iso(),
        duration_s=result.duration_s,
        session_id=summary.get("run_id"),
        summary=summary,
    )


def verify_a1(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"A1 build failed with rc={run_result.returncode}")
    assert_exists(repo / "hello.py", "A1 expected hello.py")
    assert_exists(repo / "test_hello.py", "A1 expected test_hello.py")
    return verify_summary_passed(repo, run_result, message="atomic build happy path passed")


def setup_a2(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_a2(repo: Path, provider: str) -> RunResult:
    intent = tiny_cli_intent("spec_hello", "print spec hello", "spec hello")
    started = now_iso()
    result = run_build(repo, provider, "--spec", "--yes", intent)
    summary = load_summary(repo)
    spec_dir = latest_session_dir(repo) / "spec"
    return _base_result(
        result.rc,
        output=result.output,
        started_at=started,
        finished_at=now_iso(),
        duration_s=result.duration_s,
        session_id=summary.get("run_id"),
        spec_dir=str(spec_dir),
        summary=summary,
    )


def verify_a2(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"A2 build --spec failed with rc={run_result.returncode}")
    spec_dir = Path(str(run_result.details.get("spec_dir", "")))
    assert_exists(spec_dir / "spec.md", "A2 expected spec.md")
    return verify_summary_passed(repo, run_result, message="spec gate auto-approve passed")


def setup_a3(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_a3(repo: Path, provider: str) -> RunResult:
    intent = tiny_cli_intent("worktree_hello", "print worktree hello", "worktree hello")
    started = now_iso()
    result = run_build(repo, provider, "--in-worktree", intent)
    worktree = find_worktree(repo, prefix="build-")
    log_line(f"[A3] build worktree: {worktree}")
    summary = load_summary(worktree) if result.rc == 0 else {}
    worktrees = sorted((repo / ".worktrees").glob("*"))
    return _base_result(
        result.rc,
        output=result.output,
        started_at=started,
        finished_at=now_iso(),
        duration_s=result.duration_s,
        summary=summary,
        worktree=str(worktree),
        branch=git(worktree, "branch", "--show-current"),
        worktrees=[str(path) for path in worktrees],
    )


def verify_a3(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"A3 build --in-worktree failed with rc={run_result.returncode}")
    worktrees = [Path(path) for path in run_result.details.get("worktrees", [])]
    if not worktrees:
        return VerifyResult(False, "A3 expected at least one worktree")
    worktree = Path(str(run_result.details.get("worktree", "")))
    assert_exists(worktree / "otto_logs" / "latest", "A3 expected otto_logs/latest inside worktree")
    branch = str(run_result.details.get("branch", ""))
    if not branch.startswith("build/"):
        return VerifyResult(False, f"A3 expected build/* branch inside worktree, got {branch!r}")
    return verify_summary_passed(worktree, run_result, message="build --in-worktree passed")


def setup_a4(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")
    write_existing_greeter(repo)


def run_a4(repo: Path, provider: str) -> RunResult:
    started = now_iso()
    result = run_certify(repo, provider, "--fast")
    summary = load_summary(repo)
    return _base_result(
        result.rc,
        output=result.output,
        started_at=started,
        finished_at=now_iso(),
        duration_s=result.duration_s,
        summary=summary,
    )


def verify_a4(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"A4 certify failed with rc={run_result.returncode}")
    assert_exists(latest_session_dir(repo) / "certify" / "proof-of-work.json", "A4 expected proof-of-work.json")
    return verify_summary_passed(repo, run_result, message="standalone certify passed")


def setup_a5(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")
    write_buggy_calculator(repo)


def run_a5(repo: Path, provider: str) -> RunResult:
    started = now_iso()
    result = run_improve(repo, provider, "bugs", "subtraction bug", timeout_s=1500)
    branch = git(repo, "branch", "--show-current")
    check = subprocess.run(
        [sys.executable, "calculator.py", "sub", "7", "2"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    return _base_result(
        result.rc,
        output=result.output,
        started_at=started,
        finished_at=now_iso(),
        duration_s=result.duration_s,
        branch=branch,
        subtraction_stdout=check.stdout,
    )


def verify_a5(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"A5 improve bugs failed with rc={run_result.returncode}")
    if run_result.details.get("subtraction_stdout") != "5\n":
        return VerifyResult(False, "A5 expected subtraction bug to be fixed")
    branch = str(run_result.details.get("branch", ""))
    if not branch.startswith("improve/"):
        return VerifyResult(False, f"A5 expected improve/* branch, got {branch!r}")
    return VerifyResult(True, "improve bugs fixed known bug", [branch])


def setup_b1(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 2"])


def run_b1(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "add", provider, add_mul_intent("add", "integer addition", "5"), "--fast")
    queue_build(repo, "mul", provider, add_mul_intent("mul", "integer multiplication", "6"), "--fast")

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for(
            lambda: sum(
                1
                for task in load_queue_state(repo).get("tasks", {}).values()
                if task.get("status") == "running"
            ) == 2,
            timeout_s=20,
            label="both tasks running",
        )
        # Linger so quick-mode recordings capture the dashboard with live work
        # before the drill-in / exit sequence starts.
        time.sleep(2.5)
        session.send("\r")
        detail = wait_for_screen_text(session, "focus=detail", timeout_s=10, label="detail open")
        time.sleep(2.0)
        session.send("\x1b")
        wait_for_screen_text(session, "focus=live", timeout_s=10, label="overview return")
        time.sleep(2.0)
        notice_text = ""
        try:
            session.send("q")
            notice = wait_for_screen_text(session, "Dashboard closed.", timeout_s=10, label="post quit")
            notice_text = notice.screen_text
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            log_line(f"[B1] dashboard PTY closed before quit key was delivered: {exc}")
            notice_text = session.snapshot("post quit closed").screen_text
        log_line("[B1] dashboard hidden; waiting for watcher exit after queue drain")
        rc = session.wait(420.0)
        state = load_queue_state(repo)
        log_line(f"[B1] final queue state: {json.dumps(state, indent=2)}")
        return {
            "detail_text": detail.screen_text,
            "notice_text": notice_text,
            "watcher_rc": rc,
            "state": state,
        }

    started = now_iso()
    details = run_dashboard_session(repo, concurrent=2, actions=actions, extra_flags=["--exit-when-empty"])
    return _base_result(0, started_at=started, finished_at=now_iso(), duration_s=0.0, **details)


def verify_b1(repo: Path, run_result: RunResult) -> VerifyResult:
    state = run_result.details.get("state", {})
    statuses = {
        task_id: resolve_queue_task_verify_status(repo, task_id, state=state, history_timeout_s=5.0) or ""
        for task_id in ("add", "mul")
    }
    if statuses != {"add": "done", "mul": "done"}:
        return VerifyResult(
            False,
            f"B1 expected both tasks done, got {statuses} (watcher_rc={run_result.details.get('watcher_rc')!r})",
        )
    notice = strip_ansi(str(run_result.details.get("notice_text", "")))
    if notice and "Dashboard closed." not in notice:
        return VerifyResult(False, "B1 expected dashboard closed notice")
    watcher_rc = run_result.details.get("watcher_rc")
    if watcher_rc not in (0, None):
        return VerifyResult(False, f"B1 expected watcher rc 0/None, got {watcher_rc!r}")
    return VerifyResult(True, "dashboard nav + drill + watcher drain passed")


def setup_b2(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 3"])


def run_b2(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "alpha", provider, add_mul_intent("alpha", "integer addition", "5"), "--fast")
    queue_build(repo, "beta", provider, add_mul_intent("beta", "integer multiplication", "6"), "--fast")
    queue_build(repo, "gamma", provider, add_mul_intent("gamma", "integer subtraction", "-1"), "--fast")

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for(lambda: load_queue_state(repo).get("tasks", {}).get("alpha", {}).get("status") == "running", timeout_s=20, label="alpha running")
        # updated for durable cancel: Mission Control live rows are recency-sorted, so select alpha explicitly before sending `c`.
        focus_queue_task(session, "alpha")
        session.send("c")
        try:
            wait_for(
                lambda: (
                    load_queue_state(repo).get("tasks", {}).get("alpha", {}).get("status") in {"terminating", "cancelled"}
                    or str(load_queue_state(repo).get("tasks", {}).get("alpha", {}).get("terminal_status") or "") == "cancelled"
                    or str((queue_terminal_snapshot(repo, "alpha") or {}).get("terminal_outcome") or "") == "cancelled"
                ),
                timeout_s=4,
                label="alpha cancel requested",
            )
        except AssertionError:
            focus_queue_task(session, "alpha")
            session.send("c")
        wait_for(
            lambda: (
                load_queue_state(repo).get("tasks", {}).get("alpha", {}).get("status") == "cancelled"
                or str((queue_terminal_snapshot(repo, "alpha") or {}).get("terminal_outcome") or "") == "cancelled"
            ),
            timeout_s=90,
            label="alpha fully cancelled",
        )
        time.sleep(0.5)
        return {
            "state": load_queue_state(repo),
            "history_alpha": queue_terminal_snapshot(repo, "alpha"),
        }

    details = run_dashboard_session(repo, concurrent=3, actions=actions, extra_flags=["--exit-when-empty"])
    return _base_result(0, **details)


def verify_b2(repo: Path, run_result: RunResult) -> VerifyResult:
    # updated for durable cancel: terminal history is authoritative once the watcher finalizes the cancelled task.
    status = resolve_queue_task_verify_status(
        repo,
        "alpha",
        state=run_result.details.get("state"),
        history_snapshot=run_result.details.get("history_alpha"),
        history_timeout_s=10.0,
    )
    if status != "cancelled":
        return VerifyResult(False, f"B2 expected alpha cancelled, got {status!r}")
    return VerifyResult(True, "dashboard cancel path reached cancelled state")


def setup_b3(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_b3(repo: Path, provider: str) -> RunResult:
    def actions(session: PtySession) -> dict[str, Any]:
        # updated for Mission Control: accept the current zero-row footer as the empty-queue hint.
        snap = wait_for_empty_queue_screen(session, timeout_s=10, label="empty state")
        # Linger so the dashboard frame is visible in playback (recording would
        # otherwise flash for <1s and be useless for human review).
        time.sleep(3.0)
        session.send("q")
        rc = session.wait(10.0)
        if rc is None:
            raise AssertionError("empty queue dashboard did not exit")
        return {"screen": snap.screen_text, "watcher_rc": rc}

    details = run_dashboard_session(repo, concurrent=1, actions=actions)
    return _base_result(0, **details)


def verify_b3(repo: Path, run_result: RunResult) -> VerifyResult:
    screen = strip_ansi(str(run_result.details.get("screen", "")))
    if "No tasks queued." not in screen and "rows=0 live, 0 history" not in screen:
        return VerifyResult(False, "B3 expected empty queue hint")
    if run_result.details.get("watcher_rc") != 0:
        return VerifyResult(False, f"B3 expected watcher rc 0, got {run_result.details.get('watcher_rc')!r}")
    return VerifyResult(True, "empty queue dashboard exits cleanly")


def setup_b4(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 1"])


def run_b4(repo: Path, provider: str) -> RunResult:
    task_id = "tail"
    if load_queue_state(repo).get("tasks", {}).get(task_id):
        task_id = f"tail-{int(time.time())}"
    queue_build(repo, task_id, provider, add_mul_intent("tail", "integer addition", "5"), "--fast")

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for(
            lambda: load_queue_state(repo).get("tasks", {}).get(task_id, {}).get("status") == "running",
            timeout_s=20,
            label="tail running",
        )
        session.send("\r")
        wait_for_screen_text(session, "focus=detail", timeout_s=10, label="detail")
        narrative = next((repo / ".worktrees").glob(f"{task_id}*/otto_logs/sessions/*/build/narrative.log"))
        before_lines = narrative.read_text().splitlines()
        wait_for(lambda: len(narrative.read_text().splitlines()) > len(before_lines), timeout_s=60, label="tail growth")
        session.send("q")
        rc = session.wait(180.0)
        return {"watcher_rc": rc, "narrative_path": str(narrative), "task_id": task_id}

    details = run_dashboard_session(repo, concurrent=1, actions=actions, extra_flags=["--exit-when-empty"])
    return _base_result(0, **details)


def verify_b4(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    narrative_path = str(run_result.details.get("narrative_path", "")).strip()
    if not narrative_path:
        return VerifyResult(False, "B4 expected narrative_path in run_result details")
    narrative = Path(narrative_path)
    assert_exists(narrative, "B4 expected narrative path")
    if len(narrative.read_text().splitlines()) < 4:
        return VerifyResult(False, "B4 expected streamed narrative lines")
    raw = strip_ansi(cast_output(Path(run_result.recording_path)))
    if not all(token in raw for token in ("1. Live Runs", "2. History", "3. Detail + Logs")):
        return VerifyResult(False, "B4 cast did not show the 3-pane Mission Control layout")
    return VerifyResult(True, "detail tail streamed in real time inside the 3-pane Mission Control layout")


def setup_b5(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_b5(repo: Path, provider: str) -> RunResult:
    run_checked([str(OTTO_BIN), "queue", "build", "first queued task", "--as", "first"], cwd=repo, env=current_ctx().env)
    run_checked([str(OTTO_BIN), "queue", "build", "second queued task", "--as", "second"], cwd=repo, env=current_ctx().env)
    rm_result = run_queue(repo, "rm", "first")
    cancel_result = run_queue(repo, "cancel", "second")
    ls_result = run_queue(repo, "ls", "--all")
    return _base_result(
        0,
        output=rm_result.output + cancel_result.output + ls_result.output,
        rm_output=rm_result.output,
        cancel_output=cancel_result.output,
        ls_output=ls_result.output,
    )


def verify_b5(repo: Path, run_result: RunResult) -> VerifyResult:
    queue_text = (repo / ".otto-queue.yml").read_text() if (repo / ".otto-queue.yml").exists() else ""
    if "first:" in queue_text:
        return VerifyResult(False, "B5 expected queue rm to remove first task offline")
    if "second:" in queue_text:
        return VerifyResult(False, "B5 expected queue cancel to remove queued task offline")
    return VerifyResult(True, "offline queue rm/cancel paths worked")


def setup_b6(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 1"])


def run_b6(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "stdout", provider, add_mul_intent("stdout_task", "integer addition", "5"), "--fast")
    result = run_queue(repo, "run", "--concurrent", "1", "--no-dashboard", "--exit-when-empty", timeout_s=600)
    return _base_result(result.rc, output=result.output)


def verify_b6(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(run_result.output)
    if "Queue worker" not in text:
        return VerifyResult(False, "B6 expected prefixed-stdout worker banner")
    if load_queue_state(repo).get("tasks", {}).get("stdout", {}).get("status") != "done":
        return VerifyResult(False, "B6 expected queued task to finish in --no-dashboard mode")
    return VerifyResult(True, "--no-dashboard watcher drained queued work")


def setup_b7(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_b7(repo: Path, provider: str) -> RunResult:
    watcher = subprocess.Popen(
        [str(OTTO_BIN), "queue", "run", "--concurrent", "1", "--no-dashboard"],
        cwd=repo,
        env=current_ctx().env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(2.0)
        second = subprocess.run(
            [str(OTTO_BIN), "queue", "run", "--concurrent", "1", "--no-dashboard"],
            cwd=repo,
            env=current_ctx().env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        output = (second.stdout or "") + (second.stderr or "")
        return _base_result(second.returncode, output=output)
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)


def verify_b7(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(run_result.output)
    if "another otto queue runner is holding" not in text:
        return VerifyResult(False, "B7 expected lock contention error from second watcher")
    return VerifyResult(True, "second queue watcher refused on lock contention")


def setup_c1(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 2"])
    commit_all(repo, "add otto config")


def run_c1(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "add", provider, add_mul_intent("add", "integer addition", "5"), "--fast")
    queue_build(repo, "mul", provider, add_mul_intent("mul", "integer multiplication", "6"), "--fast")
    watcher = run_queue(repo, "run", "--concurrent", "2", "--no-dashboard", "--exit-when-empty", timeout_s=900)
    if watcher.rc != 0:
        return _base_result(watcher.rc, output=watcher.output)
    queue_state = load_queue_state(repo)
    queue_statuses = {
        task_id: task.get("status")
        for task_id, task in queue_state.get("tasks", {}).items()
    }
    log_line(f"[C1] queue state before merge: {json.dumps(queue_statuses, indent=2)}")
    pre_merge_status = git(repo, "status", "--short")
    log_line(f"[C1] pre-merge git status:\n{pre_merge_status or '(clean)'}")
    if any(status != "done" for status in queue_statuses.values()):
        return _base_result(
            1,
            output=watcher.output,
            pre_merge_status=pre_merge_status,
            queue_state=queue_state,
            sessions=[],
        )
    merge = run_merge(repo, "--all", "--cleanup-on-success", timeout_s=1800)
    sessions = sorted((repo / "otto_logs" / "sessions").glob("*"))
    return _base_result(
        merge.rc,
        output=watcher.output + merge.output,
        sessions=[str(path) for path in sessions],
        pre_merge_status=pre_merge_status,
        queue_state=queue_state,
    )


def verify_c1(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        queue_state = run_result.details.get("queue_state", {})
        statuses = {
            task_id: task.get("status")
            for task_id, task in queue_state.get("tasks", {}).items()
        }
        if statuses and any(status != "done" for status in statuses.values()):
            return VerifyResult(False, f"C1 queued builds did not finish successfully before merge: {statuses}")
        status = str(run_result.details.get("pre_merge_status", "")).strip() or "(clean)"
        return VerifyResult(False, f"C1 merge failed with rc={run_result.returncode}; pre-merge status: {status}")
    sessions = [Path(path) for path in run_result.details.get("sessions", [])]
    if len(sessions) < 2:
        return VerifyResult(False, f"C1 expected graduated sessions in main repo, got {len(sessions)}")
    for session in sessions[:2]:
        summary = read_json(session / "summary.json")
        if not summary.get("merge_commit_sha"):
            return VerifyResult(False, f"C1 expected merge_commit_sha in {session / 'summary.json'}")
    return VerifyResult(True, "merge --all cleanup-on-success graduated sessions")


def setup_c2(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 2"])
    write_conflict_base(repo)


def run_c2(repo: Path, provider: str) -> RunResult:
    suffix = str(int(time.time() * 1000))[-6:]
    render_json_id = f"render-json-{suffix}"
    render_angle_id = f"render-angle-{suffix}"
    queue_build(
        repo,
        render_json_id,
        provider,
        "Modify tools.py so render returns JSON-like text `{\"value\": <n>}` and update tests accordingly.",
        "--fast",
    )
    queue_build(
        repo,
        render_angle_id,
        provider,
        "Modify tools.py so render returns angle-bracket text `<value=<n>>` and update tests accordingly.",
        "--fast",
    )
    watcher = run_queue(repo, "run", "--concurrent", "2", "--no-dashboard", "--exit-when-empty", timeout_s=1200)
    merge = run_merge(repo, "--all", timeout_s=2400)
    return _base_result(merge.rc, output=watcher.output + merge.output)


def verify_c2(repo: Path, run_result: RunResult) -> VerifyResult:
    from otto.merge.state import find_latest_merge_id, load_state as load_merge_state

    if run_result.returncode != 0:
        return VerifyResult(False, f"C2 merge failed with rc={run_result.returncode}")
    merge_id = find_latest_merge_id(repo)
    if not merge_id:
        return VerifyResult(False, "C2 expected a persisted merge state")
    state = load_merge_state(repo, merge_id)
    statuses = {outcome.status for outcome in state.outcomes}
    if "conflict_resolved" not in statuses:
        return VerifyResult(False, f"C2 expected consolidated conflict resolution, got {sorted(statuses)!r}")
    return VerifyResult(True, "conflicting branches triggered merge conflict path")


def setup_c3(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 2"])
    commit_all(repo, "add otto config")


def run_c3(repo: Path, provider: str) -> RunResult:
    suffix = str(int(time.time() * 1000))[-6:]
    add_task_id = f"add-{suffix}"
    mul_task_id = f"mul-{suffix}"
    queue_build(repo, add_task_id, provider, add_mul_intent("add_no_cert", "integer addition", "5"), "--fast")
    queue_build(repo, mul_task_id, provider, add_mul_intent("mul_no_cert", "integer multiplication", "6"), "--fast")
    watcher = run_queue(repo, "run", "--concurrent", "2", "--no-dashboard", "--exit-when-empty", timeout_s=900)
    merge = run_merge(repo, "--all", "--no-certify", "--cleanup-on-success", timeout_s=1200)
    remaining_worktrees = sorted(path.name for path in (repo / ".worktrees").glob("*") if path.is_dir())
    return _base_result(
        merge.rc,
        output=watcher.output + merge.output,
        watcher_output=watcher.output,
        merge_output=merge.output,
        task_ids=[add_task_id, mul_task_id],
        remaining_worktrees=remaining_worktrees,
    )


def verify_c3(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(str(run_result.details.get("merge_output", ""))).lower()
    if "certify starting" in text or "verification:" in text or "post-merge:" in text:
        return VerifyResult(False, "C3 expected merge --no-certify to skip post-merge verification")
    task_ids = [str(task_id) for task_id in run_result.details.get("task_ids", [])]
    remaining_worktrees = [str(name) for name in run_result.details.get("remaining_worktrees", [])]
    if any(any(name.startswith(task_id) for name in remaining_worktrees) for task_id in task_ids):
        return VerifyResult(False, "C3 expected cleanup-on-success to remove merged task worktrees")
    return VerifyResult(True, "merge --no-certify still graduated sessions")


def setup_c4(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")
    (repo / "feature.txt").write_text("random feature\n")
    commit_all(repo, "base feature file")
    run_checked(["git", "checkout", "-q", "-b", "feature/random"], cwd=repo)
    (repo / "feature.txt").write_text("random feature branch change\n")
    commit_all(repo, "feature branch change")
    run_checked(["git", "checkout", "-q", "main"], cwd=repo)


def run_c4(repo: Path, provider: str) -> RunResult:
    refused = run_merge(repo, "feature/random", "--no-certify", timeout_s=120)
    allowed_args = ["feature/random", "--allow-any-branch", "--no-certify"]
    if provider == "codex":
        allowed_args.append("--fast")
    allowed = run_merge(repo, *allowed_args, timeout_s=120)
    return _base_result(allowed.rc, output=refused.output + allowed.output, refused_output=refused.output, allowed_output=allowed.output)


def verify_c4(repo: Path, run_result: RunResult) -> VerifyResult:
    refused = strip_ansi(str(run_result.details.get("refused_output", ""))).lower()
    # Real refusal message: "branch '<x>' is not a queue task or atomic-mode
    # branch; ... otto-managed ... Use plain `git merge` ... --allow-any-branch ..."
    refusal_signals = (
        "not a queue task",
        "not an otto-managed",
        "otto merge only works",
        "allow-any-branch",
    )
    if not any(signal in refused for signal in refusal_signals):
        return VerifyResult(False, f"C4 expected non-otto branch refusal message; got: {refused[:300]!r}")
    if run_result.returncode != 0:
        return VerifyResult(False, "C4 expected --allow-any-branch merge to succeed")
    return VerifyResult(True, "merge refusal and --allow-any-branch override both verified")


def setup_c5(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")
    (repo / "base.txt").write_text("base\n")
    commit_all(repo, "base text")
    run_checked(["git", "checkout", "-q", "-b", "feature/a"], cwd=repo)
    (repo / "a.txt").write_text("a\n")
    commit_all(repo, "branch a")
    run_checked(["git", "checkout", "-q", "main"], cwd=repo)
    run_checked(["git", "checkout", "-q", "-b", "feature/b"], cwd=repo)
    (repo / "b.txt").write_text("b\n")
    commit_all(repo, "branch b")
    run_checked(["git", "checkout", "-q", "main"], cwd=repo)


def run_c5(repo: Path, provider: str) -> RunResult:
    lock_holder = subprocess.Popen(
        [
            "python3",
            "-c",
            (
                "import fcntl, json, os, signal, time; "
                "from pathlib import Path; "
                "path = Path('otto_logs/.merge.lock'); "
                "path.parent.mkdir(parents=True, exist_ok=True); "
                "handle = open(path, 'a+', encoding='utf-8'); "
                "fcntl.flock(handle.fileno(), fcntl.LOCK_EX); "
                "handle.seek(0); handle.truncate(); "
                "handle.write(json.dumps({'pid': os.getpid(), 'started_at': 'scenario-c5'})); "
                "handle.flush(); os.fsync(handle.fileno()); "
                "signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0))); "
                "time.sleep(300)"
            ),
        ],
        cwd=repo,
    )
    try:
        wait_for(lambda: (repo / "otto_logs" / ".merge.lock").exists(), timeout_s=10, label="merge lock holder")
        second = subprocess.run(
            [str(OTTO_BIN), "merge", "feature/b", "--allow-any-branch", "--no-certify"],
            cwd=repo,
            env=current_ctx().env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        output = (second.stdout or "") + (second.stderr or "")
        return _base_result(second.returncode, output=output)
    finally:
        try:
            lock_holder.terminate()
            lock_holder.wait(timeout=20)
        except Exception:
            lock_holder.kill()


def verify_c5(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(run_result.output)
    if "another otto merge is in progress" not in text:
        return VerifyResult(False, "C5 expected second merge to hit merge lock")
    return VerifyResult(True, "merge lock contention surfaced clearly")


def setup_d1(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_d1(repo: Path, provider: str) -> RunResult:
    intent = tiny_cli_intent("resume_hello", "print resume hello", "resume hello")
    partial_output = interrupt_build_after_checkpoint(repo, provider, intent)
    # updated for fingerprint check: resume now needs --force when the interrupted run already changed the repo state.
    resumed = run_build(repo, provider, "--resume", "--force", timeout_s=1200)
    summary: dict[str, Any] = {}
    if resumed.rc == 0:
        try:
            summary = load_summary(repo)
        except (AssertionError, FileNotFoundError, json.JSONDecodeError):
            summary = {}
    return _base_result(
        resumed.rc,
        output=partial_output + resumed.output,
        summary=summary,
    )


def verify_d1(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"D1 resume failed with rc={run_result.returncode}")
    return verify_summary_passed(repo, run_result, message="SIGTERM then --resume completed successfully")


def setup_d2(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_d2(repo: Path, provider: str) -> RunResult:
    intent = tiny_cli_intent("resume_done", "print resume done", "resume done")
    build = run_build(repo, provider, intent)
    resume = run_build(repo, provider, "--resume", timeout_s=120)
    return _base_result(resume.rc, output=build.output + resume.output, resume_output=resume.output)


def verify_d2(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(str(run_result.details.get("resume_output", "")))
    if "Last run completed" not in text or "Nothing to resume" not in text:
        return VerifyResult(False, "D2 expected completed-run resume message")
    return VerifyResult(True, "completed build reports nothing to resume")


def setup_d3(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_d3(repo: Path, provider: str) -> RunResult:
    from otto.checkpoint import load_checkpoint

    intent = tiny_cli_intent("resume_mismatch", "print old", "old")
    partial_output = interrupt_build_after_checkpoint(repo, provider, intent)
    checkpoint = load_checkpoint(repo) or {}
    log_line(
        "[D3] checkpoint before resume: "
        f"status={checkpoint.get('status')!r} "
        f"phase={checkpoint.get('phase')!r} "
        f"intent={checkpoint.get('intent')!r}"
    )
    mismatch = run_build(repo, provider, "different intent", "--resume", timeout_s=120)
    return _base_result(
        mismatch.rc,
        output=partial_output + mismatch.output,
        checkpoint_before_resume=checkpoint,
    )


def verify_d3(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(run_result.output).lower()
    if "intent mismatch on resume" not in text and "checkpoint fingerprint does not match" not in text:
        return VerifyResult(False, "D3 expected intent mismatch rejection")
    return VerifyResult(True, "resume with different intent was rejected")


def setup_d4(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="thorough", extra_lines=["run_budget_seconds: 90"])


def run_d4(repo: Path, provider: str) -> RunResult:
    intent = "Build a production-ready web crawler with full browser automation, persistent auth, and screenshots under a 90 second total budget."
    result = run_build(repo, provider, intent, timeout_s=300)
    return _base_result(result.rc, output=result.output)


def verify_d4(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(run_result.output).lower()
    if "run budget exhausted" not in text and "verification failed" not in text and "paused" not in text and "run timed out after" not in text:
        return VerifyResult(False, "D4 expected a graceful give-up or pause signal")
    return VerifyResult(True, "impossible-or-budget-constrained run gave up gracefully")


def setup_d5(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_d5(repo: Path, provider: str) -> RunResult:
    intent = tiny_cli_intent("cross_resume", "print cross resume", "cross resume")
    partial_output = interrupt_build_after_checkpoint(repo, provider, intent)
    # updated for fingerprint check: force past the fingerprint gate so this scenario still exercises the cross-command gate.
    rejected = run_improve(repo, provider, "bugs", "--resume", "--force", timeout_s=120)
    forced = run_improve(repo, provider, "bugs", "--resume", "--force", "--force-cross-command-resume", timeout_s=300)
    return _base_result(
        forced.rc,
        output=partial_output + rejected.output + forced.output,
        rejected_output=rejected.output,
        forced_output=forced.output,
    )


def verify_d5(repo: Path, run_result: RunResult) -> VerifyResult:
    rejected = strip_ansi(str(run_result.details.get("rejected_output", "")))
    forced = strip_ansi(str(run_result.details.get("forced_output", "")))
    if "Checkpoint command mismatch" not in rejected:
        return VerifyResult(False, "D5 expected cross-command resume rejection without force")
    if "Checkpoint is from" not in forced or run_result.returncode != 0:
        return VerifyResult(False, "D5 expected forced cross-command resume to get past the mismatch gate")
    return VerifyResult(True, "cross-command resume gate and override both exercised")


def setup_e1(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")
    (repo / "app.py").write_text("print('setup target')\n")
    commit_all(repo, "seed setup target")


def run_e1(repo: Path, provider: str) -> RunResult:
    result = run_setup(repo, timeout_s=1200)
    return _base_result(result.rc, output=result.output)


def verify_e1(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"E1 setup failed with rc={run_result.returncode}")
    assert_exists(repo / "CLAUDE.md", "E1 expected CLAUDE.md")
    return VerifyResult(True, "otto setup generated CLAUDE.md")


def setup_e2(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_e2(repo: Path, provider: str) -> RunResult:
    intent = tiny_cli_intent("history_demo", "print history demo", "history demo")
    build = run_build(repo, provider, intent)
    session_id = str(load_summary(repo).get("run_id"))
    history = run_streaming([str(OTTO_BIN), "history"], cwd=repo, env=current_ctx().env, timeout_s=120)
    replay = run_streaming([str(OTTO_BIN), "replay", session_id], cwd=repo, env=current_ctx().env, timeout_s=180)
    return _base_result(
        replay.rc,
        output=build.output + history.output + replay.output,
        session_id=session_id,
        history_output=history.output,
    )


def verify_e2(repo: Path, run_result: RunResult) -> VerifyResult:
    history_output = strip_ansi(str(run_result.details.get("history_output", "")))
    if "history_demo" not in history_output and "PASS" not in history_output:
        return VerifyResult(False, "E2 expected history output to include the recent run")
    regen = latest_session_dir(repo) / "build" / "narrative.regenerated.log"
    assert_exists(regen, "E2 expected replay to regenerate narrative log")
    return VerifyResult(True, "history and replay both worked")


def setup_e3(repo: Path, provider: str) -> None:
    init_repo(repo)


def run_e3(repo: Path, provider: str) -> RunResult:
    result = run_streaming([str(OTTO_BIN), "--version"], cwd=repo, env=current_ctx().env, timeout_s=30)
    return _base_result(result.rc, output=result.output)


def verify_e3(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(run_result.output)
    if "source:" not in text or "otto " not in text:
        return VerifyResult(False, "E3 expected version output with source path")
    return VerifyResult(True, "--version reports branch/source metadata")


def setup_e4(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", memory=True)
    write_existing_greeter(repo)


def run_e4(repo: Path, provider: str) -> RunResult:
    first = run_certify(repo, provider, "--fast", timeout_s=900)
    first_session = str(load_summary(repo).get("run_id"))
    second = run_certify(repo, provider, "--fast", timeout_s=900)
    second_session = str(load_summary(repo).get("run_id"))
    memory_path = repo / "otto_logs" / "cross-sessions" / "certifier-memory.jsonl"
    injected_marker = False
    messages = latest_session_dir(repo) / "certify" / "messages.jsonl"
    if messages.exists() and "Previous Certification History" in messages.read_text():
        injected_marker = True
    return _base_result(
        second.rc,
        output=first.output + second.output,
        first_session=first_session,
        second_session=second_session,
        memory_path=str(memory_path),
        injected_marker=injected_marker,
    )


def verify_e4(repo: Path, run_result: RunResult) -> VerifyResult:
    memory_path = Path(str(run_result.details.get("memory_path", "")))
    assert_exists(memory_path, "E4 expected certifier-memory.jsonl")
    lines = [line for line in memory_path.read_text().splitlines() if line.strip()]
    if len(lines) < 2:
        return VerifyResult(False, f"E4 expected >=2 memory entries, got {len(lines)}")
    if not run_result.details.get("injected_marker"):
        return VerifyResult(False, "E4 expected prior certification history in the second certifier prompt")
    return VerifyResult(True, "cross-run memory recorded and re-injected")


def setup_e5(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="thorough")
    write_existing_greeter(repo)


def run_e5(repo: Path, provider: str) -> RunResult:
    result = run_certify(repo, provider, "--thorough", timeout_s=1800)
    return _base_result(result.rc, output=result.output)


def verify_e5(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"E5 thorough certify failed with rc={run_result.returncode}")
    return verify_summary_passed(repo, run_result, message="thorough standalone certify passed")


def setup_e6(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_e6(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "one", provider, add_mul_intent("one", "integer addition", "5"), "--fast")
    queue_build(repo, "two", provider, add_mul_intent("two", "integer multiplication", "6"), "--fast")
    queue_build(repo, "three", provider, add_mul_intent("three", "integer subtraction", "-1"), "--fast")
    serial = run_queue(repo, "run", "--concurrent", "1", "--no-dashboard", "--exit-when-empty", timeout_s=1200)

    repo2 = current_ctx().artifact_dir / "concurrency-10-repo"
    init_repo(repo2)
    write_otto_yaml(repo2, provider=provider, certifier_mode="fast")
    queue_build(repo2, "one", provider, add_mul_intent("one", "integer addition", "5"), "--fast")
    queue_build(repo2, "two", provider, add_mul_intent("two", "integer multiplication", "6"), "--fast")
    queue_build(repo2, "three", provider, add_mul_intent("three", "integer subtraction", "-1"), "--fast")
    wide = run_queue(repo2, "run", "--concurrent", "10", "--no-dashboard", "--exit-when-empty", timeout_s=1200)
    return _base_result(
        wide.rc if serial.rc == 0 else serial.rc,
        output=serial.output + wide.output,
        serial_state=load_queue_state(repo),
        wide_state=load_queue_state(repo2),
    )


def verify_e6(repo: Path, run_result: RunResult) -> VerifyResult:
    serial_tasks = run_result.details.get("serial_state", {}).get("tasks", {})
    wide_tasks = run_result.details.get("wide_state", {}).get("tasks", {})
    if not all(task.get("status") == "done" for task in serial_tasks.values()):
        return VerifyResult(False, "E6 expected serial queue run to finish all tasks")
    if not all(task.get("status") == "done" for task in wide_tasks.values()):
        return VerifyResult(False, "E6 expected wide queue run to finish all tasks")
    return VerifyResult(True, "queue concurrency extremes both drained successfully")


def setup_u1(repo: Path, provider: str) -> None:
    setup_b3(repo, provider)


def run_u1(repo: Path, provider: str) -> RunResult:
    del provider

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for_empty_queue_screen(session, timeout_s=10, label="u1-empty")
        time.sleep(1.0)
        session.send("q")
        rc = session.wait(10.0)
        if rc is None:
            raise AssertionError("U1 empty queue dashboard did not exit")
        return {"watcher_rc": rc}

    details = run_dashboard_session(repo, concurrent=1, actions=actions)
    return _base_result(0, **details)


def verify_u1(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    raw = cast_output(Path(run_result.recording_path))
    matches = mouse_enable_codes(raw)
    if matches:
        return VerifyResult(False, f"U1 expected mouse capture OFF by default, saw {matches}")
    return VerifyResult(True, "default dashboard session did not enable mouse capture")


def setup_u2(repo: Path, provider: str) -> None:
    setup_a1(repo, provider)


def run_u2(repo: Path, provider: str) -> RunResult:
    from otto import paths
    from otto.runs.registry import allocate_run_id, read_live_records

    intent = textwrap.dedent(
        """
        Build a tiny TODO CLI in Python.
        Store tasks in tasks.json in the repo root.
        Support add, list, and done commands with argparse.
        Keep the implementation minimal and readable.
        Include pytest coverage for one add/list/done flow.
        """
    ).strip()
    build_run_id = allocate_run_id(repo)
    build_env = scenario_env(OTTO_RUN_ID=build_run_id)
    build_argv = [str(OTTO_BIN), "build", "--provider", provider, intent]
    build_log_path = current_ctx().artifact_dir / "u2-build.log"
    dashboard_session: PtySession | None = None
    build_proc: subprocess.Popen[str] | None = None
    build_handle = None
    build_rc: int | None = None

    try:
        build_handle = build_log_path.open("w", encoding="utf-8")
        build_proc = subprocess.Popen(
            build_argv,
            cwd=repo,
            env=build_env,
            stdout=build_handle,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )
        dashboard_session = PtySession([str(OTTO_BIN), "dashboard"], cwd=repo, env=current_ctx().env)
        wait_for(
            lambda: (paths.session_dir(repo, build_run_id) / "commands").exists(),
            timeout_s=15,
            label="u2 command channel",
            interval_s=0.05,
        )
        wait_for(
            lambda: any(record.run_id == build_run_id for record in read_live_records(repo)),
            timeout_s=15,
            label="u2 live record",
            interval_s=0.05,
        )
        live = wait_for_screen_text(dashboard_session, build_run_id, timeout_s=15, label="u2-live")
        time.sleep(3.0)
        cancel = append_cancel_envelope_and_wait_for_ack(
            repo,
            build_run_id,
            proc=build_proc,
            run_exited_message="U2 build exited before cancel ack arrived",
            timeout_message="U2 cancel ack did not arrive",
        )
        assert build_proc is not None
        build_rc = build_proc.wait(timeout=30.0)
        wait_for(
            lambda: any(
                row.get("run_id") == build_run_id
                and row.get("history_kind", "terminal_snapshot") == "terminal_snapshot"
                and row.get("terminal_outcome") == "cancelled"
                for row in read_jsonl(paths.history_jsonl(repo))
            ),
            timeout_s=15,
            label="u2 cancelled terminal snapshot",
            interval_s=0.1,
        )
        dashboard_session.send("q")
        dashboard_rc = dashboard_session.wait(10.0)
        if dashboard_rc is None:
            raise AssertionError("U2 dashboard did not exit")
        live_records = read_live_records(repo)
        if build_handle is not None:
            build_handle.flush()
        return _base_result(
            0,
            output=build_log_path.read_text(encoding="utf-8"),
            build_run_id=build_run_id,
            build_returncode=build_rc,
            live_record_count=len(live_records),
            live_screen=live.screen_text,
            dashboard_rc=dashboard_rc,
            **cancel,
        )
    finally:
        if dashboard_session is not None:
            dashboard_session.terminate()
        if build_handle is not None:
            build_handle.flush()
            build_handle.close()
        if build_proc is not None and build_proc.poll() is None:
            try:
                os.killpg(build_proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                build_proc.terminate()
            try:
                build_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(build_proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    build_proc.kill()
                build_proc.wait(timeout=5.0)


def verify_u2(repo: Path, run_result: RunResult) -> VerifyResult:
    from otto import paths

    ack_latency_ms = run_result.details.get("ack_latency_ms")
    ack_deadline_ms = run_result.details.get("ack_deadline_ms")
    if not isinstance(ack_latency_ms, int) or not isinstance(ack_deadline_ms, int) or ack_latency_ms > ack_deadline_ms:
        return VerifyResult(False, "U2 expected a timely cancel ack")
    history_rows = [
        row
        for row in read_jsonl(paths.history_jsonl(repo))
        if row.get("history_kind", "terminal_snapshot") == "terminal_snapshot"
    ]
    if len(history_rows) != 1:
        return VerifyResult(False, f"U2 expected exactly one terminal snapshot row, got {len(history_rows)}")
    row = history_rows[0]
    if row.get("terminal_outcome") != "cancelled":
        return VerifyResult(False, f"U2 expected terminal_outcome='cancelled', got {row.get('terminal_outcome')!r}")
    cast_path = Path(run_result.recording_path)
    if not cast_path.exists() or cast_path.stat().st_size == 0:
        return VerifyResult(False, "U2 expected a non-empty recording.cast")
    return VerifyResult(True, "Mission Control showed the live build, cancel ack landed in time, and history recorded cancelled")


def setup_u3(repo: Path, provider: str) -> None:
    setup_u_realistic(repo, provider)


def run_u3(repo: Path, provider: str) -> RunResult:
    del provider
    placeholder_proc, placeholder_child = spawn_placeholder_child(repo, sleep_s=4)
    capture_path = current_ctx().artifact_dir / "clipboard.txt"
    bindir = install_clipboard_capture(current_ctx().artifact_dir, capture_path)
    env = prepend_path(scenario_env(), bindir)
    write_queue_state_file(
        repo,
        {
            UX_PRIMARY_TASK_ID: {
                "status": "running",
                "started_at": "2026-04-21T20:00:30Z",
                "finished_at": None,
                "child": placeholder_child,
                "manifest_path": str((repo / "otto_logs" / "queue" / UX_PRIMARY_TASK_ID / "manifest.json").resolve(strict=False)),
                "cost_usd": None,
                "duration_s": None,
                "failure_reason": None,
            },
            UX_SECONDARY_TASK_ID: {
                "status": "done",
                "started_at": "2026-04-21T20:01:00Z",
                "finished_at": "2026-04-21T20:01:05Z",
                "child": None,
                "manifest_path": str((repo / "otto_logs" / "queue" / UX_SECONDARY_TASK_ID / "manifest.json").resolve(strict=False)),
                "cost_usd": 0.0,
                "duration_s": 5.0,
                "failure_reason": None,
            },
            UX_QUEUED_TASK_ID: {
                "status": "done",
                "started_at": "2026-04-21T20:01:30Z",
                "finished_at": "2026-04-21T20:01:35Z",
                "child": None,
                "manifest_path": str((repo / "otto_logs" / "queue" / UX_QUEUED_TASK_ID / "manifest.json").resolve(strict=False)),
                "cost_usd": 0.0,
                "duration_s": 5.0,
                "failure_reason": None,
            },
        },
    )
    try:
        def actions(session: PtySession) -> dict[str, Any]:
            wait_for_screen_text(session, UX_PRIMARY_TASK_ID, timeout_s=10, label="u3-overview")
            session.send("y")
            wait_for(lambda: capture_path.exists(), timeout_s=10, label="clipboard capture")
            time.sleep(0.5)
            session.send("q")
            rc = session.wait(15.0)
            if rc is None:
                raise AssertionError("U3 dashboard did not exit after clipboard yank")
            return {"watcher_rc": rc}

        details = run_dashboard_session(repo, concurrent=2, actions=actions, env=env)
    finally:
        if placeholder_proc.poll() is None:
            placeholder_proc.terminate()
            placeholder_proc.wait(timeout=10)
    return _base_result(
        0,
        clipboard_path=str(capture_path),
        expected_task_id=UX_PRIMARY_TASK_ID,
        expected_branch=UX_PRIMARY_BRANCH,
        expected_session_id=UX_PRIMARY_SESSION_ID,
        expected_manifest_path=str((repo / "otto_logs" / "queue" / UX_PRIMARY_TASK_ID / "manifest.json").resolve(strict=False)),
        **details,
    )


def verify_u3(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    clipboard_path = Path(str(run_result.details.get("clipboard_path", "")))
    if not clipboard_path.exists():
        return VerifyResult(False, "U3 expected clipboard capture file to exist")
    payload = clipboard_path.read_text()
    for label in ("expected_task_id", "expected_branch", "expected_session_id", "expected_manifest_path"):
        expected = str(run_result.details.get(label, ""))
        if not expected or expected not in payload:
            return VerifyResult(False, f"U3 clipboard payload missing {label.replace('expected_', '')}: {expected!r}")
    if "…" in payload:
        return VerifyResult(False, "U3 clipboard payload unexpectedly contained an ellipsis")
    if "/otto_logs/" not in payload:
        return VerifyResult(False, "U3 clipboard payload did not include an absolute otto_logs path")
    return VerifyResult(True, "overview yank payload preserved full IDs, branch, session, and manifest path")


def setup_u4(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 2"])
    append_dashboard_task(repo, task_id="ad", branch=UX_PRIMARY_BRANCH, intent="Build add.py CLI")
    append_dashboard_task(repo, task_id="mu", branch=UX_SECONDARY_BRANCH, intent="Build mul.py CLI")
    write_dashboard_task_artifacts(
        repo,
        task_id="ad",
        branch=UX_PRIMARY_BRANCH,
        session_id="s",
        narrative_text=(
            "[+0:00] BUILD starting for add\n"
            "[+0:01] reading project layout\n"
            "[+0:02] writing add.py with argparse\n"
            "[+0:05] STORY_RESULT: smoke | PASS | python add.py works\n"
        ),
    )
    write_dashboard_task_artifacts(
        repo,
        task_id="mu",
        branch=UX_SECONDARY_BRANCH,
        session_id="t",
        narrative_text=(
            "[+0:00] BUILD starting for mul\n"
            "[+0:01] reading project layout\n"
            "[+0:02] writing mul.py with argparse\n"
            "[+0:05] STORY_RESULT: smoke | PASS | python mul.py works\n"
        ),
    )
    write_queue_state_file(
        repo,
        {
            "ad": {
                "status": "done",
                "started_at": "2026-04-21T20:00:30Z",
                "finished_at": "2026-04-21T20:00:35Z",
                "child": None,
                "manifest_path": None,
                "cost_usd": 0.0,
                "duration_s": 5.0,
                "failure_reason": None,
            },
            "mu": {
                "status": "done",
                "started_at": "2026-04-21T20:01:00Z",
                "finished_at": "2026-04-21T20:01:05Z",
                "child": None,
                "manifest_path": None,
                "cost_usd": 0.0,
                "duration_s": 5.0,
                "failure_reason": None,
            },
        },
    )


def run_u4(repo: Path, provider: str) -> RunResult:
    del provider
    narrative_path = repo / ".worktrees" / "ad" / "otto_logs" / "sessions" / "s" / "build" / "narrative.log"
    manifest_path = repo / "otto_logs" / "queue" / "ad" / "manifest.json"

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for_screen_text(session, "ad", timeout_s=10, label="u4-overview")
        session.send("\r")
        wait_for_screen_text(session, "queue: ad", timeout_s=10, label="u4-detail")
        time.sleep(2.5)
        session.send("\x1b")
        wait_for_screen_text(session, "ad", timeout_s=10, label="u4-overview-return")
        session.send("q")
        rc = session.wait(10.0)
        if rc is None:
            raise AssertionError("U4 dashboard did not exit")
        return {"watcher_rc": rc}

    details = run_dashboard_session(repo, concurrent=2, actions=actions)
    return _base_result(
        0,
        expected_branch=UX_PRIMARY_BRANCH,
        expected_narrative_path=str(narrative_path.resolve(strict=False)),
        expected_manifest_path=str(manifest_path.resolve(strict=False)),
        **details,
    )


def verify_u4(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    cast_path = Path(run_result.recording_path)
    frame = find_last_frame(cast_path, lambda text: "queue: ad" in text)
    if frame is None:
        return VerifyResult(False, "U4 did not find a detail-screen frame in the cast")
    normalized = normalize_wrapped_text(frame.screen_text)
    raw = cast_output(cast_path)
    expected_branch = str(run_result.details.get("expected_branch", ""))
    if expected_branch not in normalized and expected_branch not in raw:
        return VerifyResult(False, f"U4 detail frame missing branch: {expected_branch!r}")
    for label in ("expected_narrative_path", "expected_manifest_path"):
        expected = str(run_result.details.get(label, ""))
        if expected not in raw and not path_fragments_visible(raw, expected):
            return VerifyResult(False, f"U4 detail output missing visible {label.replace('expected_', '')} fragments: {expected!r}")
    if "…" in normalized and str(run_result.details.get("expected_branch", "")) not in normalized:
        return VerifyResult(False, "U4 branch looked truncated in the detail frame")
    return VerifyResult(True, "detail screen exposed full branch, narrative path, and manifest path")


def setup_u5(repo: Path, provider: str) -> None:
    setup_u_realistic(repo, provider)


def run_u5(repo: Path, provider: str) -> RunResult:
    del provider
    narrative_path = repo / ".worktrees" / UX_PRIMARY_TASK_ID / "otto_logs" / "sessions" / UX_PRIMARY_SESSION_ID / "build" / "narrative.log"
    narrative_path.write_text(
        "\n".join(f"[+0:{index:02d}] scrolling line {index}" for index in range(200)) + "\n"
    )

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for_screen_text(session, UX_PRIMARY_TASK_ID, timeout_s=10, label="u5-overview")
        session.send("\r")
        wait_for_screen_text(session, f"queue: {UX_PRIMARY_TASK_ID}", timeout_s=10, label="u5-detail")
        time.sleep(1.0)
        session.send("j" * 50)
        time.sleep(0.5)
        session.send("\x1b[F")
        time.sleep(0.2)
        session.send("\x1b[H")
        time.sleep(0.2)
        session.send("\x1b")
        wait_for_screen_text(session, UX_PRIMARY_TASK_ID, timeout_s=10, label="u5-overview-return")
        session.send("q")
        rc = session.wait(10.0)
        if rc is None:
            raise AssertionError("U5 dashboard did not exit")
        return {"watcher_rc": rc}

    details = run_dashboard_session(repo, concurrent=2, actions=actions)
    return _base_result(0, **details)


def verify_u5(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    raw = cast_output(Path(run_result.recording_path))
    matches = mouse_enable_codes(raw)
    if matches:
        return VerifyResult(False, f"U5 expected keyboard-only detail scrolling; saw mouse enable codes {matches}")
    return VerifyResult(True, "detail scrolling stayed keyboard-only without enabling mouse capture")


def setup_u6(repo: Path, provider: str) -> None:
    setup_b3(repo, provider)


def run_u6(repo: Path, provider: str) -> RunResult:
    del provider

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for_empty_queue_screen(session, timeout_s=10, label="u6-empty")
        session.send("?")
        wait_for_screen_text(session, "Mission Control Help", timeout_s=10, label="u6-help")
        time.sleep(2.0)
        session.send("\x1b")
        wait_for_empty_queue_screen(session, timeout_s=10, label="u6-empty-return")
        session.send("q")
        rc = session.wait(10.0)
        if rc is None:
            raise AssertionError("U6 dashboard did not exit")
        return {"watcher_rc": rc}

    details = run_dashboard_session(repo, concurrent=1, actions=actions)
    return _base_result(0, **details)


def verify_u6(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    frame = find_last_frame(Path(run_result.recording_path), lambda text: "Mission Control Help" in text)
    if frame is None:
        return VerifyResult(False, "U6 help overlay was not found in the cast")
    text = frame.screen_text
    required = ["j", "k", "Enter", "Esc", "c", "y", "q", "?", "Up", "Down"]
    missing = [token for token in required if token not in text]
    if missing:
        return VerifyResult(False, f"U6 help overlay missing bindings: {', '.join(missing)}")
    return VerifyResult(True, "help overlay listed discoverable overview bindings and arrows")


def setup_u7(repo: Path, provider: str) -> None:
    setup_b3(repo, provider)


def run_u7(repo: Path, provider: str) -> RunResult:
    del provider

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for_empty_queue_screen(session, timeout_s=10, label="u7-empty")
        time.sleep(1.0)
        session.send("q")
        rc = session.wait(10.0)
        if rc is None:
            raise AssertionError("U7 dashboard did not exit")
        return {"watcher_rc": rc}

    details = run_dashboard_session(repo, concurrent=1, actions=actions)
    return _base_result(0, **details)


def verify_u7(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    frame = find_last_frame(
        Path(run_result.recording_path),
        lambda text: "No runs yet." in text or "No tasks queued." in text,
    )
    if frame is None:
        return VerifyResult(False, "U7 empty-state frame was not found in the cast")
    text = " ".join(frame.screen_text.lower().split())
    for needle in ("build", "improve", "certify", "otto queue"):
        if needle not in text:
            return VerifyResult(False, f"U7 empty-state hint missing {needle!r}")
    return VerifyResult(True, "empty-state hint mentioned all enqueue commands and otto queue")


def setup_u8(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 2"])


def run_u8(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "task1", provider, "Build task1 CLI", "--fast")
    queue_build(repo, "task2", provider, "Build task2 CLI", "--fast")
    env = scenario_env(
        OTTO_BIN=str(FAKE_OTTO_BIN),
        FAKE_OTTO_SLEEP="2",
        FAKE_OTTO_PRINT="synthetic child stdout",
        FAKE_OTTO_COST="0.00",
        FAKE_OTTO_DURATION="2.0",
    )
    result = run_streaming(
        [str(OTTO_BIN), "queue", "run", "--no-dashboard", "--concurrent", "2", "--exit-when-empty"],
        cwd=repo,
        env=env,
        timeout_s=120,
    )
    return _base_result(result.rc, output=result.output, state=load_queue_state(repo))


def verify_u8(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    lines = [strip_ansi(line).rstrip() for line in run_result.output.splitlines() if strip_ansi(line).strip()]
    if not any(line.startswith("[task1] ") for line in lines):
        return VerifyResult(False, "U8 expected at least one [task1] stdout line")
    if not any(line.startswith("[task2] ") for line in lines):
        return VerifyResult(False, "U8 expected at least one [task2] stdout line")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[task1] ") or stripped.startswith("[task2] "):
            continue
        if stripped.startswith("[watcher] "):
            continue
        if stripped.startswith("[") and "]" in stripped[:12]:
            continue
        if stripped.startswith("Queue worker"):
            continue
        return VerifyResult(False, f"U8 found non-grep-friendly stdout line: {line!r}")
    state = run_result.details.get("state", {}).get("tasks", {})
    if state.get("task1", {}).get("status") != "done" or state.get("task2", {}).get("status") != "done":
        return VerifyResult(False, "U8 expected both fake tasks to finish")
    return VerifyResult(True, "no-dashboard stdout stayed grep-friendly with per-task prefixes")


def setup_u9(repo: Path, provider: str) -> None:
    setup_b3(repo, provider)


def run_u9(repo: Path, provider: str) -> RunResult:
    del provider

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for_empty_queue_screen(session, timeout_s=10, label="u9-empty")
        time.sleep(1.0)
        session.send("q")
        rc = session.wait(10.0)
        if rc is None:
            raise AssertionError("U9 dashboard did not exit")
        return {"watcher_rc": rc}

    details = run_dashboard_session(repo, concurrent=1, actions=actions, extra_flags=["--dashboard-mouse"])
    return _base_result(0, **details)


def verify_u9(repo: Path, run_result: RunResult) -> VerifyResult:
    del repo
    raw = cast_output(Path(run_result.recording_path))
    tail = raw[-4096:]
    enable_matches = mouse_enable_codes(raw)
    if not enable_matches:
        return VerifyResult(False, "U9 expected mouse enable codes when launched with --dashboard-mouse")
    disable_matches = mouse_disable_codes(tail)
    if not disable_matches:
        return VerifyResult(False, "U9 expected mouse disable codes near the end of the cast")
    hide_index = raw.find(CURSOR_HIDE)
    show_index = raw.rfind(CURSOR_SHOW)
    if hide_index != -1 and show_index <= hide_index:
        return VerifyResult(False, "U9 saw cursor hide without a matching cursor show near exit")
    return VerifyResult(True, "terminal teardown restored mouse/cursor mode on quit")


SCENARIOS: dict[str, Scenario] = {
    "A1": Scenario("A1", "A", "atomic build happy path", True, 0.35, 90, False, setup_a1, run_a1, verify_a1),
    "A2": Scenario("A2", "A", "build --spec --yes auto-approves spec gate", True, 0.50, 120, False, setup_a2, run_a2, verify_a2),
    "A3": Scenario("A3", "A", "build --in-worktree uses isolated git worktree", False, 0.40, 100, False, setup_a3, run_a3, verify_a3),
    "A4": Scenario("A4", "A", "standalone certify on an existing project", False, 0.25, 60, False, setup_a4, run_a4, verify_a4),
    "A5": Scenario("A5", "A", "improve bugs fixes a known defect on an existing project", False, 0.55, 150, False, setup_a5, run_a5, verify_a5),
    "B1": Scenario("B1", "B", "dashboard nav, drill-in, quit notice, and drain with 2 non-conflicting builds", False, 0.90, 220, True, setup_b1, run_b1, verify_b1),
    "B2": Scenario("B2", "B", "dashboard cancel via c transitions a running task to cancelled", False, 1.10, 260, True, setup_b2, run_b2, verify_b2),
    "B3": Scenario("B3", "B", "empty queue dashboard shows hint and q exits cleanly", True, 0.00, 20, True, setup_b3, run_b3, verify_b3),
    "B4": Scenario("B4", "B", "detail view streams new narrative lines in real time", False, 0.35, 120, True, setup_b4, run_b4, verify_b4),
    "B5": Scenario("B5", "B", "offline queue rm/cancel mutate the queue without a watcher", False, 0.00, 20, False, setup_b5, run_b5, verify_b5),
    "B6": Scenario("B6", "B", "--no-dashboard watcher prints prefixed stdout and drains work", False, 0.35, 90, False, setup_b6, run_b6, verify_b6),
    "B7": Scenario("B7", "B", "second queue run is refused while watcher lock is held", False, 0.00, 25, False, setup_b7, run_b7, verify_b7),
    "C1": Scenario("C1", "C", "merge --all --cleanup-on-success graduates 2 non-conflicting builds", True, 0.85, 220, False, setup_c1, run_c1, verify_c1),
    "C2": Scenario("C2", "C", "conflicting queued builds trigger merge conflict resolution path", False, 1.20, 320, False, setup_c2, run_c2, verify_c2),
    "C3": Scenario("C3", "C", "merge --no-certify still graduates sessions", False, 0.80, 180, False, setup_c3, run_c3, verify_c3),
    "C4": Scenario("C4", "C", "merge refuses non-otto branch unless --allow-any-branch is set", False, 0.00, 20, False, setup_c4, run_c4, verify_c4),
    "C5": Scenario("C5", "C", "second merge is refused while merge lock is held", False, 0.20, 45, False, setup_c5, run_c5, verify_c5),
    "D1": Scenario("D1", "D", "SIGTERM during build can be resumed to completion", False, 0.55, 180, False, setup_d1, run_d1, verify_d1),
    "D2": Scenario("D2", "D", "completed build then --resume prints Last run completed", True, 0.35, 100, False, setup_d2, run_d2, verify_d2),
    "D3": Scenario("D3", "D", "resume rejects a different CLI intent", False, 0.30, 90, False, setup_d3, run_d3, verify_d3),
    "D4": Scenario("D4", "D", "budget-constrained run gives up gracefully when it cannot finish", False, 0.25, 120, False, setup_d4, run_d4, verify_d4),
    "D5": Scenario("D5", "D", "--force-cross-command-resume overrides the resume command gate", False, 0.45, 160, False, setup_d5, run_d5, verify_d5),
    "E1": Scenario("E1", "E", "otto setup generates CLAUDE.md in a fresh repo", False, 0.35, 120, False, setup_e1, run_e1, verify_e1),
    "E2": Scenario("E2", "E", "history lists a recent run and replay regenerates narrative.log", False, 0.35, 100, False, setup_e2, run_e2, verify_e2),
    "E3": Scenario("E3", "E", "otto --version reports version, branch, and source path", False, 0.00, 10, False, setup_e3, run_e3, verify_e3),
    "E4": Scenario("E4", "E", "memory:true records cross-run memory and persists it across certifies", False, 0.45, 150, False, setup_e4, run_e4, verify_e4),
    "E5": Scenario("E5", "E", "standalone certify --thorough runs the deeper certifier mode", False, 0.45, 180, False, setup_e5, run_e5, verify_e5),
    "E6": Scenario("E6", "E", "queue run handles both --concurrent 1 and --concurrent 10", False, 0.70, 220, False, setup_e6, run_e6, verify_e6),
    "U1": Scenario("U1", "U", "dashboard leaves mouse capture off by default", False, 0.00, 15, True, setup_u1, run_u1, verify_u1),
    "U2": Scenario("U2", "U", "Mission Control basic flow: one live build, cancel ack, cancelled history row, clean quit", True, 0.50, 300, True, setup_u2, run_u2, verify_u2),
    "U3": Scenario("U3", "U", "overview yank copies complete row metadata without truncation", False, 0.00, 15, True, setup_u3, run_u3, verify_u3),
    "U4": Scenario("U4", "U", "detail screen renders full branch and absolute log/manifest paths", False, 0.00, 15, True, setup_u4, run_u4, verify_u4),
    "U5": Scenario("U5", "U", "detail scrolling stays keyboard-only and never enables mouse capture", False, 0.00, 15, True, setup_u5, run_u5, verify_u5),
    "U6": Scenario("U6", "U", "help overlay lists the key bindings a developer needs to discover", False, 0.00, 15, True, setup_u6, run_u6, verify_u6),
    "U7": Scenario("U7", "U", "empty-state hint names build, improve, certify, and otto queue", False, 0.00, 15, True, setup_u7, run_u7, verify_u7),
    "U8": Scenario("U8", "U", "--no-dashboard stdout stays grep-friendly with per-task prefixes", False, 0.00, 20, True, setup_u8, run_u8, verify_u8),
    "U9": Scenario("U9", "U", "quitting the dashboard restores terminal mouse/cursor mode", False, 0.00, 15, True, setup_u9, run_u9, verify_u9),
}


def scenario_ids_for_mode(mode: str) -> list[str]:
    if mode == "full":
        return list(SCENARIOS)
    return [scenario_id for scenario_id in QUICK_SCENARIOS if scenario_id in SCENARIOS]


def parse_csv_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def select_scenarios(*, mode: str, scenario_csv: str | None, group_csv: str | None) -> list[Scenario]:
    selected_ids = set(scenario_ids_for_mode(mode))
    if scenario_csv:
        selected_ids = set(parse_csv_ids(scenario_csv))
    if group_csv:
        groups = {value.upper() for value in parse_csv_ids(group_csv)}
        by_group = {scenario_id for scenario_id, scenario in SCENARIOS.items() if scenario.group.upper() in groups}
        selected_ids = by_group if not scenario_csv else selected_ids & by_group

    missing = [scenario_id for scenario_id in selected_ids if scenario_id not in SCENARIOS]
    if missing:
        raise SystemExit(f"unknown scenarios: {', '.join(sorted(missing))}")
    ordered = [SCENARIOS[scenario_id] for scenario_id in SCENARIOS if scenario_id in selected_ids]
    if not ordered:
        raise SystemExit("no scenarios selected")
    return ordered


def print_scenario_list() -> None:
    print("ID  Group  Quick  Cost   Secs  Description")
    for scenario in SCENARIOS.values():
        quick = "yes" if scenario.quick else "no"
        print(
            f"{scenario.name:2}  {scenario.group:5}  {quick:5}  "
            f"${scenario.estimated_cost:>4.2f}  {scenario.estimated_seconds:>4}  {scenario.description}"
        )


def print_summary(run_id: str, provider: str, mode: str, outcomes: list[ScenarioOutcome]) -> None:
    total_cost = sum(item.scenario.estimated_cost for item in outcomes)
    total_secs = sum(item.wall_duration_s or item.run_result.duration_s or item.scenario.estimated_seconds for item in outcomes)
    passed = sum(1 for item in outcomes if item.outcome == "PASS")
    failed = [item for item in outcomes if item.outcome == "FAIL"]
    infra = [item for item in outcomes if item.outcome == "INFRA"]

    def format_result(status: ScenarioStatus) -> str:
        if status == "INFRA":
            return f"{INFRA_COLOR}INFRA{ANSI_RESET}"
        return status

    print()
    print(f"otto-as-user run {run_id}")
    print(f"provider: {provider}   mode: {mode}   scenarios: {len(outcomes)}")
    print()
    print("ID  Description                                   Result  Cost   Duration  Artifact")
    for item in outcomes:
        result = format_result(item.outcome)
        artifact = item.artifact_dir.relative_to(DEFAULT_ARTIFACT_ROOT)
        print(
            f"{item.scenario.name:2}  {item.scenario.description[:43]:43}  "
            f"{result:5}  ${item.scenario.estimated_cost:>4.2f}  "
            f"{int(item.wall_duration_s or item.run_result.duration_s or item.scenario.estimated_seconds):>4}s     "
            f"{artifact}/{item.recording_name}"
        )
    print()
    print(
        f"Totals: {len(outcomes)} scenarios, {passed} PASS, {len(failed)} FAIL, {len(infra)} INFRA\n"
        f"Total estimated cost: ${total_cost:.2f}   Total wall: {int(total_secs // 60)}m {int(total_secs % 60):02d}s\n"
        f"Provider: {provider}"
    )
    if failed:
        print("\nFailed scenarios:")
        for item in failed:
            rel = item.artifact_dir.relative_to(DEFAULT_ARTIFACT_ROOT)
            print(f"  {item.scenario.name}: {item.verify_result.note}")
            print(f"  -> see {rel}/{{recording.cast,recording-retry.cast,debug.log,debug-retry.log,run_result.json,run_result-retry.json,verify.json,verify-retry.json}}")
            print(f"  Replay: asciinema play {rel}/{item.recording_name}")
    if infra:
        print("\nInfra scenarios:")
        for item in infra:
            rel = item.artifact_dir.relative_to(DEFAULT_ARTIFACT_ROOT)
            print(f"  {item.scenario.name}: {item.verify_result.note}")
            print(f"  -> see {rel}/{{recording.cast,recording-retry.cast,debug.log,debug-retry.log,run_result.json,run_result-retry.json,verify.json,verify-retry.json}}")
            print(f"  Replay: asciinema play {rel}/{item.recording_name}")
        print(
            f"\nTransient infra issues: {len(infra)} scenario(s) classified as INFRA; "
            f"{'exiting 0 because no real FAIL outcomes were detected.' if not failed else 'these are excluded from FAIL totals.'}"
        )


def maybe_prune_artifacts(artifact_dir: Path, *, keep_failed_only: bool, passed: bool) -> None:
    if not keep_failed_only or not passed:
        return
    keep_names = {
        "recording.cast",
        "recording-retry.cast",
        "run_result.json",
        "run_result-retry.json",
        "verify.json",
        "verify-retry.json",
    }
    for child in artifact_dir.iterdir():
        if child.name in keep_names:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def internal_run_scenario(
    scenario_id: str,
    repo_path: Path,
    artifact_dir: Path,
    provider: str,
    attempt_index: int = 1,
) -> int:
    scenario = SCENARIOS[scenario_id]
    recording_name = attempt_filename("recording.cast", attempt_index)
    debug_name = attempt_filename("debug.log", attempt_index)
    run_result_name = attempt_filename("run_result.json", attempt_index)
    global EXECUTION_CONTEXT
    EXECUTION_CONTEXT = ExecutionContext(
        scenario=scenario,
        artifact_dir=artifact_dir,
        repo=repo_path,
        provider=provider,
        debug_log=artifact_dir / debug_name,
        recording_path=artifact_dir / recording_name,
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prepare_scenario_isolation(EXECUTION_CONTEXT)
    try:
        result = scenario.run(repo_path, provider)
    except Exception as exc:
        traceback_text = traceback.format_exc()
        result = RunResult(
            scenario_id=scenario_id,
            returncode=1,
            started_at=now_iso(),
            finished_at=now_iso(),
            duration_s=0.0,
            recording_path=str(artifact_dir / recording_name),
            repo_path=str(repo_path),
            debug_log=str(artifact_dir / debug_name),
            output=traceback_text,
            details={"error": str(exc)},
        )
        with (artifact_dir / debug_name).open("a", encoding="utf-8") as handle:
            handle.write(traceback_text)
        write_json(artifact_dir / run_result_name, asdict(result))
        return 1
    write_json(artifact_dir / run_result_name, asdict(result))
    return 0 if result.returncode == 0 else 1


def run_one_scenario_attempt(
    asciinema_bin: Path,
    scenario: Scenario,
    provider: str,
    repo_path: Path,
    artifact_dir: Path,
    *,
    attempt_index: int,
) -> tuple[RunResult, VerifyResult, str]:
    recording_name = attempt_filename("recording.cast", attempt_index)
    run_result_name = attempt_filename("run_result.json", attempt_index)
    verify_name = attempt_filename("verify.json", attempt_index)
    internal_cmd = [
        str(PYTHON_BIN if PYTHON_BIN.exists() else Path(sys.executable)),
        str(Path(__file__).resolve()),
        "_internal-run",
        scenario.name,
        str(repo_path),
        str(artifact_dir),
        provider,
        str(attempt_index),
    ]
    cast_path = artifact_dir / recording_name
    rec_launcher = [str(asciinema_bin)]
    is_shim = asciinema_bin.suffix == ".py"
    if is_shim:
        rec_launcher = [str(PYTHON_BIN if PYTHON_BIN.exists() else Path(sys.executable)), str(asciinema_bin)]
        # Shim accepts v2-style args (--output, --quiet)
        rec_cmd = [
            *rec_launcher,
            "rec",
            "--command",
            shell_join(internal_cmd),
            "--output",
            str(cast_path),
            "--quiet",
        ]
    else:
        # Real asciinema 3.x: positional file, no --quiet, use `record` (or `rec` alias)
        rec_cmd = [
            *rec_launcher,
            "rec",
            "--command",
            shell_join(internal_cmd),
            "--output-format",
            "asciicast-v2",
            str(cast_path),
        ]
    result = subprocess.run(rec_cmd, cwd=REPO_ROOT, text=True)
    run_result_path = artifact_dir / run_result_name
    if run_result_path.exists():
        run_result = RunResult(**read_json(run_result_path))
    else:
        run_result = RunResult(
            scenario_id=scenario.name,
            returncode=result.returncode,
            started_at=now_iso(),
            finished_at=now_iso(),
            duration_s=float(scenario.estimated_seconds),
            recording_path=str(artifact_dir / recording_name),
            repo_path=str(repo_path),
            debug_log=str(artifact_dir / attempt_filename("debug.log", attempt_index)),
            output="run_result.json missing",
            details={},
        )
    verify_result = scenario.verify(repo_path, run_result)
    write_json(artifact_dir / verify_name, asdict(verify_result))
    return run_result, verify_result, recording_name


def record_one_scenario(asciinema_bin: Path, scenario: Scenario, run_id: str, provider: str) -> ScenarioOutcome:
    artifact_dir = DEFAULT_ARTIFACT_ROOT / run_id / scenario.name
    artifact_dir.mkdir(parents=True, exist_ok=True)

    def prepare_attempt_repo() -> Path:
        repo_path = Path(tempfile.mkdtemp(prefix=f"o{scenario.name.lower()}-", dir="/tmp"))
        scenario.setup(repo_path, provider)
        return repo_path

    repo_path = prepare_attempt_repo()

    first_run_result, first_verify_result, first_recording_name = run_one_scenario_attempt(
        asciinema_bin,
        scenario,
        provider,
        repo_path,
        artifact_dir,
        attempt_index=1,
    )
    if first_verify_result.passed:
        return ScenarioOutcome(
            scenario=scenario,
            outcome="PASS",
            run_result=first_run_result,
            verify_result=first_verify_result,
            artifact_dir=artifact_dir,
            recording_name=first_recording_name,
            wall_duration_s=first_run_result.duration_s,
        )

    first_classification = classify_failure(
        latest_narrative_log(repo_path),
        Path(first_run_result.debug_log),
        first_run_result,
    )
    if first_classification != "INFRA":
        return ScenarioOutcome(
            scenario=scenario,
            outcome="FAIL",
            run_result=first_run_result,
            verify_result=first_verify_result,
            artifact_dir=artifact_dir,
            recording_name=first_recording_name,
            wall_duration_s=first_run_result.duration_s,
        )

    print(
        f"[{scenario.name}] INFRA detected; sleeping {format_seconds_for_log(INFRA_RETRY_DELAY_S)} "
        f"and retrying (attempt 2/{INFRA_RETRY_ATTEMPTS})",
        flush=True,
    )
    time.sleep(INFRA_RETRY_DELAY_S)
    retry_repo_path = prepare_attempt_repo()
    retry_run_result, retry_verify_result, retry_recording_name = run_one_scenario_attempt(
        asciinema_bin,
        scenario,
        provider,
        retry_repo_path,
        artifact_dir,
        attempt_index=2,
    )
    wall_duration_s = first_run_result.duration_s + INFRA_RETRY_DELAY_S + retry_run_result.duration_s
    if retry_verify_result.passed:
        retry_verify_result.note = f"{retry_verify_result.note} (retried after INFRA error)"
        return ScenarioOutcome(
            scenario=scenario,
            outcome="PASS",
            run_result=retry_run_result,
            verify_result=retry_verify_result,
            artifact_dir=artifact_dir,
            recording_name=retry_recording_name,
            attempt_count=2,
            wall_duration_s=wall_duration_s,
            retried_after_infra=True,
        )

    retry_classification = classify_failure(
        latest_narrative_log(retry_repo_path),
        Path(retry_run_result.debug_log),
        retry_run_result,
    )
    return ScenarioOutcome(
        scenario=scenario,
        outcome=retry_classification,
        run_result=retry_run_result,
        verify_result=retry_verify_result,
        artifact_dir=artifact_dir,
        recording_name=retry_recording_name,
        attempt_count=2,
        wall_duration_s=wall_duration_s,
        retried_after_infra=True,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Otto as-user scenarios with asciinema artifacts.")
    parser.add_argument("--mode", choices=["quick", "full"], default="quick")
    parser.add_argument("--provider", choices=["claude", "codex"], default="claude")
    parser.add_argument("--scenario", help="Comma-separated scenario ids, e.g. A1,B3,C1")
    parser.add_argument("--group", help="Comma-separated group ids, e.g. A,B,D,U")
    parser.add_argument(
        "--scenario-delay", type=float, default=DEFAULT_SCENARIO_DELAY_S,
        help=f"Seconds to sleep between scenarios (default: {DEFAULT_SCENARIO_DELAY_S}s; "
             "reduces subscription rate-limit pressure when running batches; 0 disables)",
    )
    parser.add_argument(
        "--keep-failed-only", action="store_true",
        help="Only keep artifacts (cast, logs) for FAIL/INFRA scenarios; clean up PASS ones",
    )
    parser.add_argument(
        "--bail-fast", action="store_true",
        help="Stop on first real FAIL (INFRA failures don't trigger bail)",
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument("_internal", nargs="*", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "_internal-run":
        if len(argv) not in {5, 6}:
            raise SystemExit("usage: _internal-run <scenario> <repo> <artifact_dir> <provider> [attempt_index]")
        attempt_index = int(argv[5]) if len(argv) == 6 else 1
        try:
            require_real_cost_opt_in("as-user internal scenario run")
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 2
        ensure_real_otto_cli()
        return internal_run_scenario(argv[1], Path(argv[2]), Path(argv[3]), argv[4], attempt_index)

    args = parse_args(argv)
    if args.list:
        print_scenario_list()
        return 0
    if args.scenario_delay < 0:
        raise SystemExit("--scenario-delay must be >= 0")

    try:
        require_real_cost_opt_in("as-user scenario recording")
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    ensure_real_otto_cli()
    scenarios = select_scenarios(mode=args.mode, scenario_csv=args.scenario, group_csv=args.group)
    asciinema_bin = install_asciinema()
    run_id = utc_run_id()
    outcomes: list[ScenarioOutcome] = []
    for index, scenario in enumerate(scenarios):
        print(f"\n=== {scenario.name} {scenario.description} ===", flush=True)
        outcome = record_one_scenario(asciinema_bin, scenario, run_id, args.provider)
        outcomes.append(outcome)
        maybe_prune_artifacts(
            outcome.artifact_dir,
            keep_failed_only=args.keep_failed_only,
            passed=outcome.outcome == "PASS",
        )
        status = outcome.outcome
        print(f"[{scenario.name}] {status}: {outcome.verify_result.note}", flush=True)
        if args.bail_fast and outcome.outcome == "FAIL":
            break
        if index < len(scenarios) - 1:
            print(
                f"[scenario-delay] sleeping {format_seconds_for_log(args.scenario_delay)} before next scenario",
                flush=True,
            )
            time.sleep(args.scenario_delay)

    print_summary(run_id, args.provider, args.mode, outcomes)
    return 1 if any(item.outcome == "FAIL" for item in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
