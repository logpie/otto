"""E2E harness helpers for `otto queue` + `otto merge` scenarios.

Each scenario:
1. Builds a fresh temp git repo via `make_repo`.
2. Sets `OTTO_BIN=scripts/fake-otto.sh` so the queue spawns the fake.
3. Drives `otto queue` / `otto merge` via real CLI subprocess calls.
4. Inspects state.json, queue.yml, manifests, logs.

Run individual scenarios:
    .venv/bin/python scripts/e2e_runner.py A1
    .venv/bin/python scripts/e2e_runner.py A   # all of set A
    .venv/bin/python scripts/e2e_runner.py all # fake/local scenarios only
    OTTO_ALLOW_REAL_COST=1 .venv/bin/python scripts/e2e_runner.py real
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_OTTO = REPO_ROOT / "scripts" / "fake-otto.sh"
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"


# ---------- terminal output ----------

def hr(label: str = "") -> None:
    print(f"\n{'─' * 6} {label} {'─' * (72 - len(label))}", flush=True)


def ok(msg: str) -> None:
    print(f"  ✓ {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  ✗ {msg}", flush=True)


def info(msg: str) -> None:
    print(f"  · {msg}", flush=True)


# ---------- repo factory ----------

@dataclass
class Repo:
    path: Path
    cleanup: bool = True

    def __post_init__(self) -> None:
        self.path = Path(self.path).resolve()

    def __enter__(self) -> "Repo":
        return self

    def __exit__(self, exc_type: Any, *exc: Any) -> None:
        # Keep the dir on failure (set OTTO_E2E_KEEP=1 to keep on success too)
        keep = exc_type is not None or os.environ.get("OTTO_E2E_KEEP")
        if keep:
            print(f"  [debug] keeping {self.path} for inspection", flush=True)
            return
        if self.cleanup and self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)

    def run(
        self,
        *argv: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = 60,
        fake_otto: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        full_env = dict(os.environ)
        if fake_otto:
            full_env["OTTO_BIN"] = str(FAKE_OTTO)
        else:
            full_env.pop("OTTO_BIN", None)
        if env:
            full_env.update(env)
            if not fake_otto:
                full_env.pop("OTTO_BIN", None)
        result = subprocess.run(
            list(argv),
            cwd=self.path,
            env=full_env,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            stdout = result.stdout if capture else "<not captured>"
            stderr = result.stderr if capture else "<not captured>"
            raise AssertionError(
                f"command failed (rc={result.returncode}): {' '.join(argv)}\n"
                f"  stdout: {stdout}\n  stderr: {stderr}"
            )
        return result

    def otto(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self.run(str(OTTO_BIN), *args, **kwargs)

    def git(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self.run("git", *args, **kwargs)

    def state(self) -> dict[str, Any]:
        p = self.path / ".otto-queue-state.json"
        if not p.exists():
            return {"tasks": {}, "watcher": None}
        return json.loads(p.read_text())

    def queue_yml(self) -> str:
        p = self.path / ".otto-queue.yml"
        return p.read_text() if p.exists() else ""


def make_repo(prefix: str = "otto-e2e-") -> Repo:
    base = Path(tempfile.mkdtemp(prefix=prefix))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "e2e@example.com"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "E2E"], cwd=base, check=True)
    (base / "README.md").write_text("# E2E test repo\n")
    subprocess.run(["git", "add", "."], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=base, check=True)
    return Repo(base)


# ---------- watcher control ----------

@dataclass
class Watcher:
    proc: subprocess.Popen[bytes]
    repo: Repo
    log_path: Path

    def stop(self, timeout: float = 5.0) -> int:
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        return self.proc.returncode or 0

    def kill_hard(self) -> int:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        return self.proc.returncode or 0

    def alive(self) -> bool:
        return self.proc.poll() is None

    def log(self) -> str:
        return self.log_path.read_text() if self.log_path.exists() else ""


def start_watcher(repo: Repo, *, concurrent: int = 3, extra_env: dict[str, str] | None = None) -> Watcher:
    log_path = repo.path / ".watcher.log"
    env = {**os.environ, "OTTO_BIN": str(FAKE_OTTO)}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [str(OTTO_BIN), "queue", "run", "--concurrent", str(concurrent)],
        cwd=repo.path,
        env=env,
        stdout=open(log_path, "wb"),
        stderr=subprocess.STDOUT,
    )
    # Give the watcher a moment to acquire its lock
    time.sleep(0.5)
    return Watcher(proc, repo, log_path)


def wait_for(predicate, *, timeout: float = 30.0, interval: float = 0.2, label: str = "predicate") -> None:
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for: {label}")


def task_status(repo: Repo, task_id: str) -> str:
    return repo.state().get("tasks", {}).get(task_id, {}).get("status") or "(missing)"


def task_count_by_status(repo: Repo, status: str) -> int:
    return sum(1 for ts in repo.state().get("tasks", {}).values() if ts.get("status") == status)


# ---------- scenario reporting ----------

@dataclass
class Result:
    name: str
    passed: bool
    note: str = ""
    elapsed: float = 0.0
    findings: list[str] = field(default_factory=list)


@contextmanager
def scenario(name: str, results: list[Result]) -> Iterator[Result]:
    hr(name)
    r = Result(name=name, passed=False)
    t0 = time.time()
    try:
        yield r
        r.passed = True
        r.elapsed = time.time() - t0
        ok(f"{name} passed in {r.elapsed:.1f}s")
    except Exception as exc:
        r.elapsed = time.time() - t0
        r.note = str(exc)
        fail(f"{name} FAILED: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        results.append(r)


def summarize(results: list[Result]) -> int:
    hr("SUMMARY")
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    for r in results:
        sigil = "✓" if r.passed else "✗"
        print(f"  {sigil} {r.name:40s}  {r.elapsed:5.1f}s  {r.note}")
    print(f"\n  {passed}/{total} passed")
    return 0 if passed == total else 1
