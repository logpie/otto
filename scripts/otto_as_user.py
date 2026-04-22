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
import tempfile
import textwrap
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "bench-results" / "as-user"
DEFAULT_ROWS = 30
DEFAULT_COLS = 120
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)")
QUICK_SCENARIOS = ["A1", "A2", "B1", "B3", "C1", "D2"]
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
LOCAL_ASCIINEMA = REPO_ROOT / ".venv" / "bin" / "asciinema"
ASCIINEMA_SHIM = REPO_ROOT / "scripts" / "asciinema_shim.py"


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


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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


@dataclass
class VerifyResult:
    passed: bool
    note: str
    details: list[str] = field(default_factory=list)


@dataclass
class ScenarioOutcome:
    scenario: "Scenario"
    run_result: RunResult
    verify_result: VerifyResult
    artifact_dir: Path


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

    @property
    def env(self) -> dict[str, str]:
        path_parts = []
        if OTTO_BIN.parent.exists():
            path_parts.append(str(OTTO_BIN.parent))
        path_parts.append(os.environ.get("PATH", ""))
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(path_parts)
        env["TERM"] = env.get("TERM", "xterm-256color")
        return env


EXECUTION_CONTEXT: ExecutionContext | None = None


def current_ctx() -> ExecutionContext:
    if EXECUTION_CONTEXT is None:
        raise RuntimeError("execution context not initialized")
    return EXECUTION_CONTEXT


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


def load_summary(repo: Path) -> dict[str, Any]:
    return read_json(latest_session_dir(repo) / "summary.json")


def load_manifest(repo: Path) -> dict[str, Any]:
    return read_json(latest_session_dir(repo) / "manifest.json")


def assert_exists(path: Path, message: str) -> None:
    if not path.exists():
        raise AssertionError(f"{message}: missing {path}")


def queue_build(repo: Path, task_id: str, provider: str, intent: str, *extra_inner: str) -> None:
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


def queue_improve_bugs(repo: Path, task_id: str, provider: str, focus: str | None = None, *extra_inner: str) -> None:
    argv = [str(OTTO_BIN), "queue", "improve", "bugs"]
    if focus:
        argv.append(focus)
    argv.extend(["--as", task_id, "--", "--provider", provider, *extra_inner])
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


def run_dashboard_session(
    repo: Path,
    *,
    concurrent: int,
    actions: Callable[[PtySession], dict[str, Any]],
    no_dashboard: bool = False,
    extra_flags: list[str] | None = None,
) -> dict[str, Any]:
    argv = [str(OTTO_BIN), "queue", "run", "--concurrent", str(concurrent)]
    if no_dashboard:
        argv.append("--no-dashboard")
    if extra_flags:
        argv.extend(extra_flags)
    session = PtySession(argv, cwd=repo, env=current_ctx().env)
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
        recording_path=str(ctx.artifact_dir / "recording.cast"),
        repo_path=str(ctx.repo),
        debug_log=str(ctx.debug_log),
        output=output,
        details=details,
    )


def run_build(repo: Path, provider: str, *args: str, timeout_s: float = 1200) -> CommandResult:
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
    summary = load_summary(repo)
    worktrees = sorted((repo / ".worktrees").glob("*"))
    return _base_result(
        result.rc,
        output=result.output,
        started_at=started,
        finished_at=now_iso(),
        duration_s=result.duration_s,
        summary=summary,
        worktrees=[str(path) for path in worktrees],
    )


def verify_a3(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"A3 build --in-worktree failed with rc={run_result.returncode}")
    worktrees = [Path(path) for path in run_result.details.get("worktrees", [])]
    if not worktrees:
        return VerifyResult(False, "A3 expected at least one worktree")
    return verify_summary_passed(repo, run_result, message="build --in-worktree passed")


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
        detail = wait_for_screen_text(session, "otto queue", timeout_s=10, label="detail open")
        time.sleep(2.0)
        session.send("\x1b")
        wait_for_screen_text(session, "add", timeout_s=10, label="overview return")
        time.sleep(2.0)
        session.send("q")
        notice = wait_for_screen_text(session, "Dashboard closed.", timeout_s=10, label="post quit")
        wait_for(
            lambda: sum(1 for task in load_queue_state(repo).get("tasks", {}).values() if task.get("status") == "done") == 2,
            timeout_s=240,
            label="queue drained",
        )
        rc = session.wait(30.0)
        if rc is None:
            raise AssertionError("watcher did not exit after queue drained")
        return {
            "detail_text": detail.screen_text,
            "notice_text": notice.screen_text,
            "watcher_rc": rc,
            "state": load_queue_state(repo),
        }

    started = now_iso()
    details = run_dashboard_session(repo, concurrent=2, actions=actions, extra_flags=["--exit-when-empty"])
    return _base_result(0, started_at=started, finished_at=now_iso(), duration_s=0.0, **details)


def verify_b1(repo: Path, run_result: RunResult) -> VerifyResult:
    state = run_result.details.get("state", {})
    statuses = {task_id: task.get("status") for task_id, task in state.get("tasks", {}).items()}
    if statuses != {"add": "done", "mul": "done"}:
        return VerifyResult(False, f"B1 expected both tasks done, got {statuses}")
    notice = strip_ansi(str(run_result.details.get("notice_text", "")))
    if "Dashboard closed." not in notice:
        return VerifyResult(False, "B1 expected dashboard closed notice")
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
        session.send("c")
        time.sleep(0.2)
        session.send("c")
        wait_for(
            lambda: load_queue_state(repo).get("tasks", {}).get("alpha", {}).get("status") in {"terminating", "cancelled"},
            timeout_s=30,
            label="alpha cancelled",
        )
        wait_for(
            lambda: sum(1 for task in load_queue_state(repo).get("tasks", {}).values() if task.get("status") in {"done", "cancelled"}) == 3,
            timeout_s=240,
            label="queue settled",
        )
        rc = session.wait(30.0)
        return {"state": load_queue_state(repo), "watcher_rc": rc}

    details = run_dashboard_session(repo, concurrent=3, actions=actions, extra_flags=["--exit-when-empty"])
    return _base_result(0, **details)


def verify_b2(repo: Path, run_result: RunResult) -> VerifyResult:
    status = run_result.details.get("state", {}).get("tasks", {}).get("alpha", {}).get("status")
    if status != "cancelled":
        return VerifyResult(False, f"B2 expected alpha cancelled, got {status!r}")
    return VerifyResult(True, "dashboard cancel path reached cancelled state")


def setup_b3(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_b3(repo: Path, provider: str) -> RunResult:
    def actions(session: PtySession) -> dict[str, Any]:
        snap = wait_for_screen_text(session, "No tasks queued.", timeout_s=10, label="empty state")
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
    if "No tasks queued." not in screen:
        return VerifyResult(False, "B3 expected empty queue hint")
    if run_result.details.get("watcher_rc") != 0:
        return VerifyResult(False, f"B3 expected watcher rc 0, got {run_result.details.get('watcher_rc')!r}")
    return VerifyResult(True, "empty queue dashboard exits cleanly")


def setup_b4(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 1"])


def run_b4(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "tail", provider, add_mul_intent("tail", "integer addition", "5"), "--fast")

    def actions(session: PtySession) -> dict[str, Any]:
        wait_for(lambda: load_queue_state(repo).get("tasks", {}).get("tail", {}).get("status") == "running", timeout_s=20, label="tail running")
        session.send("\r")
        wait_for_screen_text(session, "otto queue", timeout_s=10, label="detail")
        narrative = next((repo / ".worktrees").glob("tail*/otto_logs/sessions/*/build/narrative.log"))
        before_lines = narrative.read_text().splitlines()
        wait_for(lambda: len(narrative.read_text().splitlines()) > len(before_lines), timeout_s=60, label="tail growth")
        session.send("q")
        rc = session.wait(180.0)
        return {"watcher_rc": rc, "narrative_path": str(narrative)}

    details = run_dashboard_session(repo, concurrent=1, actions=actions, extra_flags=["--exit-when-empty"])
    return _base_result(0, **details)


def verify_b4(repo: Path, run_result: RunResult) -> VerifyResult:
    narrative = Path(str(run_result.details.get("narrative_path", "")))
    assert_exists(narrative, "B4 expected narrative path")
    if len(narrative.read_text().splitlines()) < 4:
        return VerifyResult(False, "B4 expected streamed narrative lines")
    return VerifyResult(True, "detail tail streamed in real time")


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


def run_c1(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "add", provider, add_mul_intent("add", "integer addition", "5"), "--fast")
    queue_build(repo, "mul", provider, add_mul_intent("mul", "integer multiplication", "6"), "--fast")
    watcher = run_queue(repo, "run", "--concurrent", "2", "--no-dashboard", "--exit-when-empty", timeout_s=900)
    if watcher.rc != 0:
        return _base_result(watcher.rc, output=watcher.output)
    merge = run_merge(repo, "--all", "--cleanup-on-success", timeout_s=1800)
    sessions = sorted((repo / "otto_logs" / "sessions").glob("*"))
    return _base_result(
        merge.rc,
        output=watcher.output + merge.output,
        sessions=[str(path) for path in sessions],
    )


def verify_c1(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"C1 merge failed with rc={run_result.returncode}")
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
    queue_build(
        repo,
        "render-json",
        provider,
        "Modify tools.py so render returns JSON-like text `{\"value\": <n>}` and update tests accordingly.",
        "--fast",
    )
    queue_build(
        repo,
        "render-angle",
        provider,
        "Modify tools.py so render returns angle-bracket text `<value=<n>>` and update tests accordingly.",
        "--fast",
    )
    watcher = run_queue(repo, "run", "--concurrent", "2", "--no-dashboard", "--exit-when-empty", timeout_s=1200)
    merge = run_merge(repo, "--all", timeout_s=2400)
    return _base_result(merge.rc, output=watcher.output + merge.output)


def verify_c2(repo: Path, run_result: RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return VerifyResult(False, f"C2 merge failed with rc={run_result.returncode}")
    merge_log = repo / "otto_logs" / "merge" / "merge.log"
    assert_exists(merge_log, "C2 expected merge log")
    text = merge_log.read_text()
    if "conflict" not in text.lower():
        return VerifyResult(False, "C2 expected merge log to mention conflict handling")
    return VerifyResult(True, "conflicting branches triggered merge conflict path")


def setup_c3(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast", extra_lines=["queue:", "  concurrent: 2"])


def run_c3(repo: Path, provider: str) -> RunResult:
    queue_build(repo, "add", provider, add_mul_intent("add_no_cert", "integer addition", "5"), "--fast")
    queue_build(repo, "mul", provider, add_mul_intent("mul_no_cert", "integer multiplication", "6"), "--fast")
    watcher = run_queue(repo, "run", "--concurrent", "2", "--no-dashboard", "--exit-when-empty", timeout_s=900)
    merge = run_merge(repo, "--all", "--no-certify", "--cleanup-on-success", timeout_s=1200)
    return _base_result(merge.rc, output=watcher.output + merge.output)


def verify_c3(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(run_result.output).lower().replace("--no-certify", "")
    if "verification" in text or "triage" in text:
        return VerifyResult(False, "C3 expected merge --no-certify to skip post-merge verification")
    sessions = list((repo / "otto_logs" / "sessions").glob("*"))
    if len(sessions) < 2:
        return VerifyResult(False, "C3 expected graduation even with --no-certify")
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
    allowed = run_merge(repo, "feature/random", "--allow-any-branch", "--no-certify", timeout_s=120)
    return _base_result(allowed.rc, output=refused.output + allowed.output, refused_output=refused.output, allowed_output=allowed.output)


def verify_c4(repo: Path, run_result: RunResult) -> VerifyResult:
    refused = strip_ansi(str(run_result.details.get("refused_output", "")))
    if "allow-any-branch" not in refused and "not an otto-managed branch" not in refused.lower():
        return VerifyResult(False, "C4 expected non-otto branch refusal message")
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
    first = subprocess.Popen(
        [str(OTTO_BIN), "merge", "feature/a", "--allow-any-branch"],
        cwd=repo,
        env=current_ctx().env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(2.0)
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
            first.terminate()
            first.wait(timeout=20)
        except Exception:
            first.kill()


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
    proc = subprocess.Popen(
        [str(OTTO_BIN), "build", "--provider", provider, intent],
        cwd=repo,
        env=current_ctx().env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(8.0)
    proc.send_signal(signal.SIGTERM)
    partial_output = (proc.communicate(timeout=60)[0] or "")
    resumed = run_build(repo, provider, "--resume", timeout_s=1200)
    summary = load_summary(repo)
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
    intent = tiny_cli_intent("resume_mismatch", "print old", "old")
    proc = subprocess.Popen(
        [str(OTTO_BIN), "build", "--provider", provider, intent],
        cwd=repo,
        env=current_ctx().env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(8.0)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=60)
    mismatch = run_build(repo, provider, "different intent", "--resume", timeout_s=120)
    return _base_result(mismatch.rc, output=mismatch.output)


def verify_d3(repo: Path, run_result: RunResult) -> VerifyResult:
    text = strip_ansi(run_result.output)
    if "Intent mismatch on resume" not in text:
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
    if "run budget exhausted" not in text and "verification failed" not in text and "paused" not in text:
        return VerifyResult(False, "D4 expected a graceful give-up or pause signal")
    return VerifyResult(True, "impossible-or-budget-constrained run gave up gracefully")


def setup_d5(repo: Path, provider: str) -> None:
    init_repo(repo)
    write_otto_yaml(repo, provider=provider, certifier_mode="fast")


def run_d5(repo: Path, provider: str) -> RunResult:
    intent = tiny_cli_intent("cross_resume", "print cross resume", "cross resume")
    proc = subprocess.Popen(
        [str(OTTO_BIN), "build", "--provider", provider, intent],
        cwd=repo,
        env=current_ctx().env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(8.0)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=60)
    rejected = run_improve(repo, provider, "bugs", "--resume", timeout_s=120)
    forced = run_improve(repo, provider, "bugs", "--resume", "--force-cross-command-resume", timeout_s=300)
    return _base_result(
        forced.rc,
        output=rejected.output + forced.output,
        rejected_output=rejected.output,
        forced_output=forced.output,
    )


def verify_d5(repo: Path, run_result: RunResult) -> VerifyResult:
    rejected = strip_ansi(str(run_result.details.get("rejected_output", "")))
    forced = strip_ansi(str(run_result.details.get("forced_output", "")))
    if "Checkpoint command mismatch" not in rejected:
        return VerifyResult(False, "D5 expected cross-command resume rejection without force")
    if "Checkpoint is from" not in forced and run_result.returncode != 0:
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
        return VerifyResult(True, "memory file recorded across runs", ["prompt marker not observed in messages.jsonl"])
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
    total_secs = sum(item.run_result.duration_s or item.scenario.estimated_seconds for item in outcomes)
    passed = sum(1 for item in outcomes if item.verify_result.passed)
    print()
    print(f"otto-as-user run {run_id}")
    print(f"provider: {provider}   mode: {mode}   scenarios: {len(outcomes)}")
    print()
    print("ID  Description                                   Result  Cost   Duration  Artifact")
    for item in outcomes:
        result = "PASS" if item.verify_result.passed else "FAIL"
        artifact = item.artifact_dir.relative_to(DEFAULT_ARTIFACT_ROOT)
        print(
            f"{item.scenario.name:2}  {item.scenario.description[:43]:43}  "
            f"{result:4}   ${item.scenario.estimated_cost:>4.2f}  "
            f"{int(item.run_result.duration_s or item.scenario.estimated_seconds):>4}s     "
            f"{artifact}/recording.cast"
        )
    print()
    print(
        f"Totals: {len(outcomes)} scenarios, {passed} PASS, {len(outcomes) - passed} FAIL\n"
        f"Total estimated cost: ${total_cost:.2f}   Total wall: {int(total_secs // 60)}m {int(total_secs % 60):02d}s\n"
        f"Provider: {provider}"
    )
    failed = [item for item in outcomes if not item.verify_result.passed]
    if failed:
        print("\nFailed scenarios:")
        for item in failed:
            rel = item.artifact_dir.relative_to(DEFAULT_ARTIFACT_ROOT)
            print(f"  {item.scenario.name}: {item.verify_result.note}")
            print(f"  -> see {rel}/{{recording.cast,debug.log,run_result.json,verify.json}}")
            print(f"  Replay: asciinema play {rel}/recording.cast")


def maybe_prune_artifacts(artifact_dir: Path, *, keep_failed_only: bool, passed: bool) -> None:
    if not keep_failed_only or not passed:
        return
    keep_names = {"recording.cast", "run_result.json", "verify.json"}
    for child in artifact_dir.iterdir():
        if child.name in keep_names:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def internal_run_scenario(scenario_id: str, repo_path: Path, artifact_dir: Path, provider: str) -> int:
    scenario = SCENARIOS[scenario_id]
    global EXECUTION_CONTEXT
    EXECUTION_CONTEXT = ExecutionContext(
        scenario=scenario,
        artifact_dir=artifact_dir,
        repo=repo_path,
        provider=provider,
        debug_log=artifact_dir / "debug.log",
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
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
            recording_path=str(artifact_dir / "recording.cast"),
            repo_path=str(repo_path),
            debug_log=str(artifact_dir / "debug.log"),
            output=traceback_text,
            details={"error": str(exc)},
        )
        with (artifact_dir / "debug.log").open("a", encoding="utf-8") as handle:
            handle.write(traceback_text)
        write_json(artifact_dir / "run_result.json", asdict(result))
        return 1
    write_json(artifact_dir / "run_result.json", asdict(result))
    return 0 if result.returncode == 0 else 1


def record_one_scenario(asciinema_bin: Path, scenario: Scenario, run_id: str, provider: str) -> ScenarioOutcome:
    artifact_dir = DEFAULT_ARTIFACT_ROOT / run_id / scenario.name
    repo_path = Path(tempfile.mkdtemp(prefix=f"otto-as-user-{run_id}-{scenario.name.lower()}-"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    scenario.setup(repo_path, provider)

    internal_cmd = [
        str(PYTHON_BIN if PYTHON_BIN.exists() else Path(sys.executable)),
        str(Path(__file__).resolve()),
        "_internal-run",
        scenario.name,
        str(repo_path),
        str(artifact_dir),
        provider,
    ]
    cast_path = artifact_dir / "recording.cast"
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
    run_result_path = artifact_dir / "run_result.json"
    if run_result_path.exists():
        run_result = RunResult(**read_json(run_result_path))
    else:
        run_result = RunResult(
            scenario_id=scenario.name,
            returncode=result.returncode,
            started_at=now_iso(),
            finished_at=now_iso(),
            duration_s=float(scenario.estimated_seconds),
            recording_path=str(artifact_dir / "recording.cast"),
            repo_path=str(repo_path),
            debug_log=str(artifact_dir / "debug.log"),
            output="run_result.json missing",
            details={},
        )
    verify_result = scenario.verify(repo_path, run_result)
    write_json(artifact_dir / "verify.json", asdict(verify_result))
    return ScenarioOutcome(
        scenario=scenario,
        run_result=run_result,
        verify_result=verify_result,
        artifact_dir=artifact_dir,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Otto as-user scenarios with asciinema artifacts.")
    parser.add_argument("--mode", choices=["quick", "full"], default="quick")
    parser.add_argument("--provider", choices=["claude", "codex"], default="claude")
    parser.add_argument("--scenario", help="Comma-separated scenario ids, e.g. A1,B3,C1")
    parser.add_argument("--group", help="Comma-separated group ids, e.g. A,B,D")
    parser.add_argument("--keep-failed-only", action="store_true")
    parser.add_argument("--bail-fast", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("_internal", nargs="*", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "_internal-run":
        if len(argv) != 5:
            raise SystemExit("usage: _internal-run <scenario> <repo> <artifact_dir> <provider>")
        return internal_run_scenario(argv[1], Path(argv[2]), Path(argv[3]), argv[4])

    args = parse_args(argv)
    if args.list:
        print_scenario_list()
        return 0

    scenarios = select_scenarios(mode=args.mode, scenario_csv=args.scenario, group_csv=args.group)
    asciinema_bin = install_asciinema()
    run_id = utc_run_id()
    outcomes: list[ScenarioOutcome] = []
    for scenario in scenarios:
        print(f"\n=== {scenario.name} {scenario.description} ===", flush=True)
        outcome = record_one_scenario(asciinema_bin, scenario, run_id, args.provider)
        outcomes.append(outcome)
        maybe_prune_artifacts(
            outcome.artifact_dir,
            keep_failed_only=args.keep_failed_only,
            passed=outcome.verify_result.passed,
        )
        status = "PASS" if outcome.verify_result.passed else "FAIL"
        print(f"[{scenario.name}] {status}: {outcome.verify_result.note}", flush=True)
        if args.bail_fast and not outcome.verify_result.passed:
            break

    print_summary(run_id, args.provider, args.mode, outcomes)
    return 0 if all(item.verify_result.passed for item in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
