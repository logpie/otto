"""Real-pty dashboard audit harness for `otto queue run`.

Uses stdlib `pty` as a fallback for `pexpect`-style interaction because this
workspace cannot fetch `pexpect` at runtime. The harness:

1. Creates throwaway git repos with queued tasks.
2. Points the queue runner at a fake `otto` wrapper that writes lightweight
   manifests plus streaming `narrative.log` files.
3. Spawns `otto queue run` in a real pseudo-terminal.
4. Sends real keystrokes, resizes the terminal, captures the rendered pane,
   and verifies on-disk state.

Usage:
    .venv/bin/python scripts/e2e_dashboard_pexpect.py
    .venv/bin/python scripts/e2e_dashboard_pexpect.py S1 S7
"""

from __future__ import annotations

import errno
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from otto.queue.schema import load_queue


REPO_ROOT = Path(__file__).resolve().parent.parent
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"
FAKE_OTTO = REPO_ROOT / "scripts" / "fake-otto.sh"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)")
DEFAULT_ROWS = 30
DEFAULT_COLS = 120
FAKE_SLEEP_S = 10


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    interval: float = 0.2,
    label: str,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.1f}s waiting for {label}")


def init_repo(base: Path) -> Path:
    repo = base.resolve()
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "e2e@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "E2E"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Otto real-pty dashboard audit\n")
    (repo / "intent.md").write_text("Build a tiny CLI.\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


def make_fake_wrapper(root: Path) -> Path:
    wrapper = root / "fake-otto-wrapper.sh"
    wrapper.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

REAL_FAKE=${REAL_FAKE_OTTO:?}
TASK_ID=${OTTO_QUEUE_TASK_ID:-unknown}
RUN_ID="fake-${TASK_ID}"
SESSION_DIR="$PWD/otto_logs/sessions/$RUN_ID"
BUILD_DIR="$SESSION_DIR/build"
mkdir -p "$BUILD_DIR"
mkdir -p "$PWD/otto_logs"
ln -sfn "sessions/$RUN_ID" "$PWD/otto_logs/latest"

LOG_PATH="$BUILD_DIR/narrative.log"
{
  printf '[+0:00] BUILD starting for %s\\n' "$TASK_ID"
  printf '[+0:00] wrapper online\\n'
} >> "$LOG_PATH"

if [ -n "${FAKE_DASHBOARD_ANSI:-}" ]; then
  printf '[+0:00] \\033[31mANSI-RED\\033[0m from %s\\n' "$TASK_ID" >> "$LOG_PATH"
fi

(
  i=1
  max_lines=${FAKE_TAIL_LINES:-8}
  while [ "$i" -le "$max_lines" ]; do
    sleep 1
    printf '[+0:%02d] streaming line %d for %s\\n' "$i" "$i" "$TASK_ID" >> "$LOG_PATH"
    i=$((i + 1))
  done
) &
writer_pid=$!

"$REAL_FAKE" "$@"
rc=$?
wait "$writer_pid" 2>/dev/null || true

if [ "$rc" -eq 0 ]; then
  printf '[+0:%02d] STORY_RESULT: %s PASS\\n' "${FAKE_TAIL_LINES:-8}" "$TASK_ID" >> "$LOG_PATH"
else
  printf '[+0:%02d] STORY_RESULT: %s FAIL rc=%d\\n' "${FAKE_TAIL_LINES:-8}" "$TASK_ID" "$rc" >> "$LOG_PATH"
fi

exit "$rc"
"""
    )
    wrapper.chmod(0o755)
    return wrapper


def enqueue_tasks(repo: Path, task_ids: list[str]) -> None:
    for task_id in task_ids:
        intent = f"build {task_id} cli"
        subprocess.run(
            [str(OTTO_BIN), "queue", "build", intent, "--as", task_id],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def write_pbcopy_wrapper(root: Path, sentinel: Path) -> Path:
    bindir = root / "mockbin"
    bindir.mkdir(parents=True, exist_ok=True)
    pbcopy = bindir / "pbcopy"
    pbcopy.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
cat > {sh_quote(str(sentinel))}
"""
    )
    pbcopy.chmod(0o755)
    return bindir


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


@dataclass
class PaneSnapshot:
    name: str
    screen_text: str
    raw_tail: str
    timestamp: float


class AnsiScreen:
    """Very small terminal emulator for the subset Textual uses here."""

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
        if self.col >= self.cols - 1:
            self.col = self.cols - 1
        else:
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
            mode = params[0] if params else 0
            if mode == 2:
                self._clear()
            elif mode == 0:
                for r in range(self.row, self.rows):
                    start = self.col if r == self.row else 0
                    for c in range(start, self.cols):
                        self._lines[r][c] = " "
            elif mode == 1:
                for r in range(0, self.row + 1):
                    stop = self.col if r == self.row else self.cols
                    for c in range(0, stop):
                        self._lines[r][c] = " "
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
        if final == "P":
            count = params[0] if params else 1
            line = self._lines[self.row]
            for _ in range(count):
                if self.col < self.cols:
                    line.pop(self.col)
                    line.append(" ")
            return
        if final == "@":
            count = params[0] if params else 1
            line = self._lines[self.row]
            for _ in range(count):
                line.insert(self.col, " ")
                del line[-1]
            return
        if final == "X":
            count = params[0] if params else 1
            for c in range(self.col, min(self.cols, self.col + count)):
                self._lines[self.row][c] = " "
            return
        if final == "L":
            count = params[0] if params else 1
            for _ in range(count):
                self._lines.insert(self.row, [" "] * self.cols)
                self._lines.pop()
            return
        if final == "M":
            count = params[0] if params else 1
            for _ in range(count):
                self._lines.pop(self.row)
                self._lines.append([" "] * self.cols)
            return
        if final == "S":
            count = params[0] if params else 1
            for _ in range(count):
                self._lines.pop(0)
                self._lines.append([" "] * self.cols)
            return
        if final == "T":
            count = params[0] if params else 1
            for _ in range(count):
                self._lines.insert(0, [" "] * self.cols)
                self._lines.pop()
            return
        if final == "s":
            self.saved = (self.row, self.col)
            return
        if final == "u":
            self.row, self.col = self.saved
            return
        if final == "m":
            return
        if private and final in {"h", "l"}:
            if params_text == "1049":
                self._clear()
                self.row = 0
                self.col = 0
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
    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        rows: int = DEFAULT_ROWS,
        cols: int = DEFAULT_COLS,
    ) -> None:
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
        fcntl = __import__("fcntl")
        termios = __import__("termios")
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
        self.drain(0.6)

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
            if len(self.raw_chunks) > 256:
                self.raw_chunks = self.raw_chunks[-256:]
            pieces.append(text)
            self.screen.feed(text)
        return "".join(pieces)

    def snapshot(self, name: str) -> PaneSnapshot:
        self.drain(0.3)
        raw_tail = "".join(self.raw_chunks)[-64000:]
        return PaneSnapshot(
            name=name,
            screen_text=self.screen.render(),
            raw_tail=raw_tail,
            timestamp=time.time(),
        )

    def is_alive(self) -> bool:
        pid, _status = os.waitpid(self.pid, os.WNOHANG)
        return pid == 0

    def wait(self, timeout: float) -> int | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
            self.drain(0.05)
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
        return final if final is not None else 1


@dataclass
class ScenarioResult:
    name: str
    passed: bool = False
    note: str = ""
    elapsed_s: float = 0.0
    findings: list[str] = field(default_factory=list)
    repo: Path | None = None


class ScenarioContext:
    def __init__(self, name: str) -> None:
        self.name = name
        self.base = Path(tempfile.mkdtemp(prefix=f"otto-pty-{name.lower()}-"))
        self.repo = init_repo(self.base / "repo")
        self.wrapper = make_fake_wrapper(self.base)
        self.snapshots_dir = self.base / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.pbcopy_sentinel = self.base / "pbcopy.txt"
        self.pbcopy_bindir = write_pbcopy_wrapper(self.base, self.pbcopy_sentinel)
        self.keep = True

    def env(self, **extra: str) -> dict[str, str]:
        path = f"{self.pbcopy_bindir}:{os.environ.get('PATH', '')}"
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "OTTO_BIN": str(self.wrapper),
            "REAL_FAKE_OTTO": str(FAKE_OTTO),
            "FAKE_OTTO_SLEEP": str(FAKE_SLEEP_S),
            "FAKE_TAIL_LINES": "8",
            "PATH": path,
        }
        env.update(extra)
        return env

    def save_snapshot(self, snap: PaneSnapshot) -> None:
        base = self.snapshots_dir / snap.name
        base.with_suffix(".screen.txt").write_text(snap.screen_text)
        base.with_suffix(".raw.txt").write_text(snap.raw_tail)

    def cleanup(self) -> None:
        if self.keep:
            return
        shutil.rmtree(self.base, ignore_errors=True)


def read_state(repo: Path) -> dict[str, Any]:
    path = repo / ".otto-queue-state.json"
    if not path.exists():
        return {"tasks": {}, "watcher": None}
    return load_json(path)


def status_counts(repo: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    state = read_state(repo)
    state_tasks = state.get("tasks", {})
    for task in load_queue(repo):
        task_state = state_tasks.get(task.id, {})
        status = str(task_state.get("status", "queued"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def queue_commands(repo: Path) -> list[dict[str, Any]]:
    path = repo / ".otto-queue-commands.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


def line_map(screen_text: str, task_ids: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for idx, line in enumerate(screen_text.splitlines(), start=1):
        for task_id in task_ids:
            if task_id in line and task_id not in result:
                result[task_id] = idx
    return result


def launch_dashboard(repo: Path, env: dict[str, str], *, concurrent: int) -> PtySession:
    return PtySession(
        [str(OTTO_BIN), "queue", "run", "--concurrent", str(concurrent)],
        cwd=repo,
        env=env,
        rows=DEFAULT_ROWS,
        cols=DEFAULT_COLS,
    )


def wait_for_screen_text(session: PtySession, needle: str, *, timeout: float, label: str) -> PaneSnapshot:
    deadline = time.time() + timeout
    last = session.snapshot(label)
    while time.time() < deadline:
        last = session.snapshot(label)
        if needle in strip_ansi(last.screen_text):
            return last
        time.sleep(0.1)
    raise AssertionError(f"screen never showed {needle!r} while waiting for {label}")


def scenario_s1(ctx: ScenarioContext) -> None:
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=1)
    try:
        snap = wait_for_screen_text(session, "No tasks queued.", timeout=4.0, label="empty-state")
        ctx.save_snapshot(snap)
        session.send("q")
        rc = session.wait(2.0)
        if rc is None:
            raise AssertionError("dashboard did not exit within 2s on empty queue")
        if rc != 0:
            raise AssertionError(f"expected exit code 0, got {rc}")
    finally:
        session.terminate()


def scenario_s2(ctx: ScenarioContext) -> None:
    task_ids = ["alpha", "beta", "gamma"]
    enqueue_tasks(ctx.repo, task_ids)
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=2)
    try:
        wait_for(
            lambda: status_counts(ctx.repo).get("running", 0) == 2 and status_counts(ctx.repo).get("queued", 0) == 1,
            timeout=6.0,
            label="2 running + 1 queued state",
        )
        first = session.snapshot("s2-initial")
        ctx.save_snapshot(first)
        screen = strip_ansi(first.screen_text)
        if screen.count("RUNNING") < 2 or "QUEUED" not in screen:
            raise AssertionError("initial pane did not show 2 RUNNING rows and 1 QUEUED row")
        before = line_map(screen, task_ids)
        time.sleep(12.0)
        second = session.snapshot("s2-after-12s")
        ctx.save_snapshot(second)
        screen2 = strip_ansi(second.screen_text)
        if "DONE" not in screen2:
            raise AssertionError("after transition wait, pane did not show any DONE row")
        after = line_map(screen2, task_ids)
        stable = [tid for tid in task_ids if tid in before and tid in after and before[tid] == after[tid]]
        if len(stable) < 2:
            raise AssertionError(f"row positions shifted unexpectedly: before={before}, after={after}")
    finally:
        session.terminate()


def scenario_s3(ctx: ScenarioContext) -> None:
    task_ids = ["alpha", "beta", "gamma"]
    enqueue_tasks(ctx.repo, task_ids)
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=2)
    try:
        wait_for(lambda: status_counts(ctx.repo).get("running", 0) == 2, timeout=6.0, label="running tasks")
        session.send("j")
        time.sleep(0.3)
        session.send("\r")
        detail = wait_for_screen_text(session, "otto queue", timeout=4.0, label="detail-open")
        ctx.save_snapshot(detail)
        detail_text = strip_ansi(detail.screen_text)
        if "▸" not in detail_text or "beta" not in detail_text:
            raise AssertionError(f"detail header did not target beta: {detail_text}")
        log_path = ctx.repo / ".worktrees" / "beta" / "otto_logs" / "sessions" / "fake-beta" / "build" / "narrative.log"
        wait_for(lambda: log_path.exists(), timeout=4.0, label="beta narrative path")
        before = log_path.read_text()
        time.sleep(3.2)
        after = log_path.read_text()
        if len(after.splitlines()) <= len(before.splitlines()):
            raise AssertionError("narrative.log did not stream new lines while detail screen was open")
        session.send("\x1b")
        overview = wait_for_screen_text(session, "alpha", timeout=3.0, label="overview-return")
        ctx.save_snapshot(overview)
    finally:
        session.terminate()


def scenario_s4(ctx: ScenarioContext) -> None:
    task_ids = ["alpha", "beta", "gamma"]
    enqueue_tasks(ctx.repo, task_ids)
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=3)
    try:
        wait_for(lambda: status_counts(ctx.repo).get("running", 0) == 3, timeout=6.0, label="3 running tasks")
        session.send("c")
        time.sleep(0.15)
        session.send("c")
        time.sleep(0.4)
        snap = session.snapshot("s4-after-cancel")
        ctx.save_snapshot(snap)
        commands = [cmd for cmd in queue_commands(ctx.repo) if cmd.get("cmd") == "cancel" and cmd.get("id") == "alpha"]
        if len(commands) != 1:
            raise AssertionError(f"expected exactly 1 cancel command for alpha, got {commands}")
        wait_for(
            lambda: read_state(ctx.repo).get("tasks", {}).get("alpha", {}).get("status") in {"terminating", "cancelled"},
            timeout=4.0,
            label="alpha terminating/cancelled",
        )
        state = read_state(ctx.repo)
        status = state["tasks"]["alpha"]["status"]
        snap2 = session.snapshot("s4-final")
        ctx.save_snapshot(snap2)
        screen = strip_ansi(snap2.screen_text)
        if "ALPHA" in screen:
            screen = screen.lower()
        if status == "cancelled" and "cancelled" not in screen and "terminating" not in screen:
            raise AssertionError("dashboard row never reflected cancelling task")
    finally:
        session.terminate()


def scenario_s5(ctx: ScenarioContext) -> None:
    task_ids = ["alpha", "beta", "gamma"]
    enqueue_tasks(ctx.repo, task_ids)
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=2)
    try:
        wait_for(lambda: status_counts(ctx.repo).get("running", 0) == 2, timeout=6.0, label="running tasks")
        session.send("y")
        wait_for(lambda: ctx.pbcopy_sentinel.exists(), timeout=2.0, label="overview pbcopy")
        overview_clip = ctx.pbcopy_sentinel.read_text()
        if "alpha" not in overview_clip:
            raise AssertionError(f"overview clipboard payload missing alpha: {overview_clip!r}")
        ctx.pbcopy_sentinel.unlink()

        session.send("\r")
        wait_for_screen_text(session, "▸ alpha", timeout=4.0, label="detail alpha")
        session.send("y")
        wait_for(lambda: ctx.pbcopy_sentinel.exists(), timeout=2.0, label="detail pbcopy")
        detail_clip = ctx.pbcopy_sentinel.read_text()
        if "BUILD starting" not in detail_clip or "alpha" not in detail_clip:
            raise AssertionError("detail clipboard payload did not contain the narrative log")
        snap = session.snapshot("s5-after-yank")
        ctx.save_snapshot(snap)
        if "clipboard not available" in strip_ansi(snap.screen_text).lower():
            raise AssertionError("clipboard failure was rendered despite pbcopy wrapper")
    finally:
        session.terminate()


def scenario_s6(ctx: ScenarioContext) -> None:
    enqueue_tasks(ctx.repo, ["alpha", "beta", "gamma"])
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=2)
    try:
        wait_for(lambda: status_counts(ctx.repo).get("running", 0) == 2, timeout=6.0, label="running tasks")
        session.send("j")
        time.sleep(0.2)
        session.send("?")
        help_snap = wait_for_screen_text(session, "Overview bindings", timeout=3.0, label="help modal")
        ctx.save_snapshot(help_snap)
        session.send("\x1b")
        time.sleep(0.3)
        session.send("\r")
        detail = wait_for_screen_text(session, "▸ beta", timeout=3.0, label="beta detail after help esc")
        ctx.save_snapshot(detail)
        session.send("\x1b")
        time.sleep(0.2)
        session.send("?")
        wait_for_screen_text(session, "Overview bindings", timeout=3.0, label="help modal 2")
        session.send("q")
        time.sleep(0.3)
        session.send("\r")
        detail2 = wait_for_screen_text(session, "▸ beta", timeout=3.0, label="beta detail after help q")
        ctx.save_snapshot(detail2)
    finally:
        session.terminate()


def scenario_s7(ctx: ScenarioContext) -> None:
    enqueue_tasks(ctx.repo, ["alpha", "beta"])
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=2)
    try:
        wait_for(lambda: status_counts(ctx.repo).get("running", 0) == 2, timeout=6.0, label="2 running tasks")
        before = session.snapshot("s7-before-q")
        ctx.save_snapshot(before)
        session.send("q")
        notice = wait_for_screen_text(session, "Dashboard closed.", timeout=3.0, label="post-quit notice")
        ctx.save_snapshot(notice)
        notice_text = strip_ansi(notice.screen_text)
        if "2 tasks still running; this command will return when they complete." not in notice_text:
            raise AssertionError(f"post-quit notice missing running-task count: {notice_text}")
        if "Press Ctrl-C to interrupt (twice for immediate stop)." not in notice_text:
            raise AssertionError(f"post-quit notice missing Ctrl-C guidance: {notice_text}")
        if session.wait(2.0) is not None:
            raise AssertionError("process exited before running tasks drained after q")
        wait_for(
            lambda: status_counts(ctx.repo).get("done", 0) == 2,
            timeout=15.0,
            label="tasks finished after dashboard quit",
        )
        rc = session.wait(5.0)
        if rc is None:
            raise AssertionError("process was still running after drained tasks should have completed")
        if rc != 0:
            raise AssertionError(f"expected exit code 0 after drained shutdown, got {rc}")
    finally:
        session.terminate()


def scenario_s8(ctx: ScenarioContext) -> None:
    enqueue_tasks(ctx.repo, ["alpha", "beta"])
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=2)
    try:
        wait_for(lambda: status_counts(ctx.repo).get("running", 0) == 2, timeout=6.0, label="2 running tasks")
        state_path = ctx.repo / ".otto-queue-state.json"
        original = state_path.read_text()
        atomic_write(state_path, "not json")
        snap = wait_for_screen_text(session, "state.json parse error", timeout=2.0, label="parse-error banner")
        ctx.save_snapshot(snap)
        atomic_write(state_path, original)
        cleared_deadline = time.time() + 3.0
        last = snap
        while time.time() < cleared_deadline:
            last = session.snapshot("s8-clearing")
            if "state.json parse error" not in strip_ansi(last.screen_text):
                ctx.save_snapshot(last)
                break
            time.sleep(0.1)
        else:
            raise AssertionError("parse-error banner did not clear after restoring state.json")
    finally:
        session.terminate()


def scenario_s9(ctx: ScenarioContext) -> None:
    enqueue_tasks(ctx.repo, ["alpha", "beta", "gamma"])
    session = launch_dashboard(ctx.repo, ctx.env(), concurrent=2)
    try:
        wait_for(lambda: status_counts(ctx.repo).get("running", 0) == 2, timeout=6.0, label="running tasks")
        session.resize(15, 60)
        small = session.snapshot("s9-small")
        ctx.save_snapshot(small)
        if "otto queue" not in strip_ansi(small.screen_text):
            raise AssertionError("dashboard chrome disappeared after shrink resize")
        if session.wait(0.2) is not None:
            raise AssertionError("dashboard exited during shrink resize")
        session.resize(30, 120)
        large = session.snapshot("s9-restored")
        ctx.save_snapshot(large)
        if "otto queue" not in strip_ansi(large.screen_text):
            raise AssertionError("dashboard chrome did not recover after resize restore")
    finally:
        session.terminate()


def scenario_s10(ctx: ScenarioContext) -> None:
    enqueue_tasks(ctx.repo, ["alpha", "beta"])
    session = launch_dashboard(ctx.repo, ctx.env(FAKE_DASHBOARD_ANSI="1"), concurrent=2)
    try:
        wait_for(lambda: status_counts(ctx.repo).get("running", 0) == 2, timeout=6.0, label="running tasks")
        session.send("\r")
        detail = wait_for_screen_text(session, "▸ alpha", timeout=3.0, label="alpha detail")
        ctx.save_snapshot(detail)
        screen = strip_ansi(detail.screen_text)
        if "otto queue" not in screen:
            raise AssertionError("detail chrome was corrupted while tailing ANSI-bearing log")
        if "[31m" in screen or "[0m" in screen:
            raise AssertionError(f"rendered detail still showed raw ANSI fragments: {screen}")
        if "\x1b[31mANSI-RED" in detail.raw_tail or "ANSI-RED\x1b[0m" in detail.raw_tail:
            raise AssertionError("raw narrative ANSI escape codes leaked through terminal output")
    finally:
        session.terminate()


SCENARIOS: dict[str, Callable[[ScenarioContext], None]] = {
    "S1": scenario_s1,
    "S2": scenario_s2,
    "S3": scenario_s3,
    "S4": scenario_s4,
    "S5": scenario_s5,
    "S6": scenario_s6,
    "S7": scenario_s7,
    "S8": scenario_s8,
    "S9": scenario_s9,
    "S10": scenario_s10,
}


def run_one(name: str) -> ScenarioResult:
    result = ScenarioResult(name=name)
    ctx = ScenarioContext(name)
    result.repo = ctx.repo
    t0 = time.time()
    try:
        SCENARIOS[name](ctx)
        result.passed = True
        ctx.keep = False
    except Exception as exc:
        result.note = str(exc)
    finally:
        result.elapsed_s = time.time() - t0
        ctx.cleanup()
    return result


def main(argv: list[str]) -> int:
    wanted = argv[1:] or list(SCENARIOS)
    invalid = [name for name in wanted if name not in SCENARIOS]
    if invalid:
        print(f"unknown scenarios: {', '.join(invalid)}", file=sys.stderr)
        return 2

    log("Real-pty dashboard audit starting")
    log("pexpect unavailable in this workspace; using stdlib pty fallback")
    results = [run_one(name) for name in wanted]

    print("\nScenario outcomes:")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        extra = f" repo={result.repo}" if result.repo and not result.passed else ""
        print(f"  {result.name:>3}  {status:4}  {result.elapsed_s:5.1f}s  {result.note}{extra}")

    failed = [result for result in results if not result.passed]
    if failed:
        print("\nFailing repos were kept for inspection.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
