"""Root-cause regression: startup declared-port cleanup must recognize a
LEAKED clean-deploy server as Otto-owned and reclaim it.

Observed: after a run #16 resume exited, an orphaned uvicorn kept
LISTENing on :8000 (PID reparented to init=PPID 1). The next resume died
in 0.1s: "Startup declared-port cleanup could not be repaired: Declared
port(s) are still busy after killing Otto-owned processes: 8000".

`_is_otto_owned_process` failed to recognize it because:
  * it checked only `cwd.name.startswith("otto-clean-")` — but the leaked
    uvicorn runs from the SUBDIR `otto-clean-XXX/backend`, so cwd.name is
    "backend"; and
  * a leaked server outlives its temp dir (verify_from_clean rm's it in
    finally), so `proc.cwd()` raises and the whole predicate returned
    False before any cmdline check ran.

Fix: the clean-deploy process tree is tagged with an inherited env var
(OTTO_CLEAN_DEPLOY_TEMP) — the one signal that survives temp-dir
deletion, cwd loss and exe resolution — plus an `otto-clean-`
cmdline/exe substring fallback. Unrelated processes must NOT be matched.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from otto.v5_clean_verify import _is_otto_owned_process

psutil = pytest.importorskip("psutil")

_UNRELATED = Path("/tmp/some-unrelated-project-xyz")


def _spawn(cmd: list[str], env_extra: dict[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        env={**os.environ, **(env_extra or {})},
        cwd="/tmp",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_env_tagged_process_is_owned_even_with_foreign_cwd() -> None:
    # PRIMARY signal: the OTTO_CLEAN_DEPLOY_TEMP env tag. cwd is /tmp and
    # cmdline mentions neither the project nor otto — only the env tag
    # marks it. This is what survives a leaked server losing its temp dir.
    p = _spawn(
        ["python3", "-c", "import time; time.sleep(30)"],
        {"OTTO_CLEAN_DEPLOY_TEMP": "/private/var/folders/x/otto-clean-abcd"},
    )
    try:
        time.sleep(0.5)
        assert _is_otto_owned_process(p.pid, _UNRELATED) is True
    finally:
        p.kill()
        p.wait()


def test_otto_clean_in_cmdline_is_owned() -> None:
    # Fallback signal: a server whose cmdline references an otto-clean-
    # temp path is a leaked deploy server even with NO env tag (e.g. env
    # unreadable). Mimics uvicorn launched from otto-clean-XXX/backend.
    p = _spawn(
        [
            "python3",
            "-c",
            "import time; time.sleep(30)  "
            "# /private/var/folders/x/otto-clean-abcd/backend uvicorn main:app",
        ]
    )
    try:
        time.sleep(0.5)
        assert _is_otto_owned_process(p.pid, _UNRELATED) is True
    finally:
        p.kill()
        p.wait()


def test_unrelated_process_is_not_owned() -> None:
    # No env tag, no otto-clean- path, cwd not under the project: must NOT
    # be reclaimed (no over-broad matching that would kill a user's own
    # process that merely happens to sit on a declared port).
    p = _spawn(["python3", "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.5)
        assert _is_otto_owned_process(p.pid, _UNRELATED) is False
    finally:
        p.kill()
        p.wait()
