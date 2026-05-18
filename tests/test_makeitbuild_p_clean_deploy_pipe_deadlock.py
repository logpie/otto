"""Root-cause regression: the clean-state deploy verifier must NOT deadlock
when start.sh is verbose, and must persist start.sh output for diagnosis.

Observed in run #16 (every resume): the merged iTracker product was correct
(`npm ci` 1.2s, `npm run build` 2s, and a faithful manual `bash start.sh`
clean-copy repro bound :5173 in 2s), yet otto's clean-deploy oracle reported
`ports [5173] did not bind within 282s. Listening: [8000]` — and it was
NOT reproducible by hand.

The bug was in the verifier harness itself, not the product. `_subtree_
verify_start_sh` ran `bash start.sh` with `stdout=subprocess.PIPE,
stderr=subprocess.STDOUT` but never drained `proc.stdout` during the
port-poll loop (it only read it in the `start_exited_early` branch, which
cannot fire for a normal start.sh that ends in `wait` — servers run forever,
proc never exits). A cold `pip install` (FastAPI/SQLAlchemy is very verbose)
+ `npm install` + vite-through-`sed` exceeds the ~64KB OS pipe buffer; with
no concurrent reader the child processes BLOCK on write(), so the dev server
never starts and the port never binds — a classic subprocess.PIPE deadlock
surfacing as a spurious `ports_not_listening`. (Manual repros redirected to
a file, so they never hit it — hence non-reproducible.)

Fix: start.sh stdout/stderr go to a real FILE in temp_root, never an
undrained PIPE. The file sink never blocks the writers, AND its tail is
persisted into the failure message on every path (previously the timeout
path captured nothing — un-debuggable from logs alone). Correctness/anti-
false-pass preserved: a server that never binds STILL fails.
"""

from __future__ import annotations

import time
from pathlib import Path

from otto.v5_clean_verify import _subtree_verify_start_sh


def _logger():
    return lambda *_a, **_k: None


def test_verbose_startsh_does_not_deadlock_before_bind(tmp_path: Path) -> None:
    # start.sh writes 200KB to stdout (>3x the ~64KB pipe buffer) BEFORE
    # binding the port — exactly what a verbose cold install does. Under the
    # OLD undrained subprocess.PIPE this blocks the writer so http.server is
    # never reached and the port never binds within the budget. With the file
    # sink the writes never block, so the port binds within seconds.
    port = 5394
    (tmp_path / "start.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        # ~200KB to stdout, then a sentinel, BEFORE any server binds.
        "python3 -c \"import sys; sys.stdout.write('X'*200000); "
        'sys.stdout.flush()"\n'
        "echo SENTINEL_DEPLOY_4242\n"
        f"python3 -m http.server {port} >/dev/null 2>&1 &\n"
        "wait\n"
    )
    t0 = time.time()
    passed, kind, msg, _steps, listening = _subtree_verify_start_sh(
        tmp_path,
        declared_ports=[port],
        timeout_s=5,
        port_wait_s=2,
        log=_logger(),
    )
    assert passed, f"verbose start.sh must not deadlock before bind: {kind} {msg}"
    assert port in listening
    # Sanity: with the deadlock this would have consumed the full ~17s
    # budget and failed; the fix lets it bind well inside it.
    assert time.time() - t0 < 15


def test_failure_path_captures_startsh_output(tmp_path: Path) -> None:
    # A start.sh that emits an identifiable marker then never binds must
    # STILL fail ports_not_listening (anti-false-pass preserved) AND the
    # failure message must now include the captured start.sh output tail
    # (the instrumentation gap that made this un-debuggable for 3 sessions).
    (tmp_path / "start.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "echo SENTINEL_NEVERBIND_9999\n"
        "sleep 60\n"  # never binds
    )
    passed, kind, msg, _steps, _listening = _subtree_verify_start_sh(
        tmp_path,
        declared_ports=[5395],
        timeout_s=1,
        port_wait_s=1,
        log=_logger(),
    )
    assert not passed
    assert kind == "ports_not_listening", (kind, msg)
    assert "install-inclusive" in (msg or "")
    assert "SENTINEL_NEVERBIND_9999" in (msg or ""), (
        "failure message must persist start.sh output for diagnosis"
    )
    # The per-port LISTEN/connect timeline must be present so the next
    # failure is root-causable (bound-late vs listening-but-unreachable).
    assert "Bind timeline:" in (msg or ""), (
        "ports_not_listening must include the bind/listen timeline"
    )
    assert "Port holders [" in (msg or "")
