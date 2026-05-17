"""Root-cause regression: the clean-state deploy verifier must reliably tear
down the ENTIRE temp-deploy process tree, not leak orphaned servers.

Observed across run #16 resumes: after a clean_deploy round an orphaned
uvicorn kept LISTENing on :8000 (confirmed via lsof after the otto run
exited). The product's start.sh backgrounds its servers in subshells and
itself exits after its own wait-loops (~60s), while the verifier polls the
full deploy budget (~282s). The old cleanup was:

    if proc and proc.poll() is None:
        os.killpg(os.getpgid(proc.pid), SIGKILL)

By cleanup time start.sh (proc) has long exited, so `proc.poll()` is NOT
None and the killpg is SKIPPED entirely — the orphaned servers survive.
They then poison subsequent clean_deploy rounds/runs by holding the fixed
ports 5173/8000 (spurious port_busy / mis-bind → bogus merge_blocked).

Fix: capture start.sh's process-group id while it is still alive (it is a
session+group leader via start_new_session=True; its servers inherit the
pgid) and SIGKILL that pgid UNCONDITIONALLY in `finally`, regardless of
whether start.sh itself already exited.

This test isolates the pgid teardown from the existing `lsof -ti :PORT`
belt-and-suspenders fallback by leaking a canary process that does NOT
listen on any declared port — only a process-group kill can reap it.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path

from otto.v5_clean_verify import _subtree_verify_start_sh


def _logger():
    return lambda *_a, **_k: None


def test_orphaned_subshell_server_is_torn_down(tmp_path: Path) -> None:
    token = f"OTTO_LEAK_CANARY_{uuid.uuid4().hex}"
    port = 5396
    # start.sh: a declared-port server (so the function returns passed) AND a
    # canary that lives 600s but binds NOTHING — backgrounded in a subshell
    # exactly like the product's vite/uvicorn. start.sh then EXITS (no
    # `wait`) while both children live: the precise leak scenario. The
    # canary cannot be caught by the lsof-:PORT fallback (it has no port),
    # so its death proves the process-group teardown ran.
    (tmp_path / "start.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"( python3 -m http.server {port} >/dev/null 2>&1 & ) &\n"
        f'( python3 -c "import time; time.sleep(600)  # {token}" & ) &\n'
        "sleep 2\n"  # start.sh exits here while both children are alive
    )
    try:
        passed, kind, msg, _steps, listening = _subtree_verify_start_sh(
            tmp_path,
            declared_ports=[port],
            timeout_s=5,
            port_wait_s=2,
            log=_logger(),
        )
        assert passed, f"declared-port server should bind: {kind} {msg}"
        assert port in listening
        # Give SIGKILL a beat to propagate, then assert NO canary survives.
        time.sleep(1)
        found = subprocess.run(
            ["pgrep", "-f", token], capture_output=True, text=True
        )
        assert found.returncode != 0 and not found.stdout.strip(), (
            "orphaned subshell process leaked — process-group teardown did "
            f"not run (survivors: {found.stdout.strip()})"
        )
    finally:
        # Never leave the canary behind even if the assertion fails.
        subprocess.run(["pkill", "-9", "-f", token], check=False)
        subprocess.run(
            ["bash", "-c", f"lsof -ti tcp:{port} | xargs -r kill -9"],
            check=False,
        )
