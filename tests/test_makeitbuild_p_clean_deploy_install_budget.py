"""Root-cause regression: the clean-state deploy port-bind window must be
install-inclusive, not conflate slow-cold-install with server bind.

smoke_clean_deploy makes a temp copy that deliberately EXCLUDES node_modules
/ .venv, so start.sh does a FULL cold dependency install (npm install for the
whole frontend + python venv + pip) BEFORE any server can bind. The deadline
was `timeout_s + port_wait_s` (~100-130s) — a single window covering both the
install AND the bind. A correct production-grade product (cold npm install
alone is routinely 60-150s) then failed `ports_not_listening` even though
`npm run build` passed (observed: resume of run #16, frontend :5173 "did not
bind within 102s" while vite build succeeded in 935ms).

The build path already accounts for cold install (`timeout_s * 3`, "install
often slower than build"); the deploy path now does too:
`deploy_budget_s = timeout_s * 3 + port_wait_s`. Correctness preserved — a
server that never binds STILL fails, just after a fair install-inclusive wait.
"""

from __future__ import annotations

import time
from pathlib import Path

from otto.v5_clean_verify import _subtree_verify_start_sh


def _logger():
    return lambda *_a, **_k: None


def test_port_binding_after_slow_install_now_succeeds(tmp_path: Path) -> None:
    # start.sh simulates a cold install that takes longer than the OLD
    # (timeout_s + port_wait_s) window, then binds the port — i.e. a correct
    # product whose deps merely install slowly. With timeout_s=3,
    # port_wait_s=1: OLD budget=4s (would fail), NEW budget=3*3+1=10s.
    # Sleep 6s (between the two) then bind → must PASS now.
    port = 5191
    (tmp_path / "start.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "sleep 6\n"  # 'cold install' longer than old 4s window, within new 10s
        f"python3 -m http.server {port} >/dev/null 2>&1 &\n"
        "wait\n"
    )
    t0 = time.time()
    passed, kind, msg, _steps, listening = _subtree_verify_start_sh(
        tmp_path,
        declared_ports=[port],
        timeout_s=3,
        port_wait_s=1,
        log=_logger(),
    )
    assert passed, f"slow-but-correct cold install must pass now: {kind} {msg}"
    assert port in listening
    # It genuinely waited past the old 4s window (proves the fix took effect).
    assert time.time() - t0 >= 6


def test_server_that_never_binds_still_fails(tmp_path: Path) -> None:
    # Correctness preserved: a start.sh that never binds the port STILL
    # fails ports_not_listening (we did not relax the gate, only the
    # install-vs-bind conflation). Use tiny budget so the test is fast:
    # NEW budget = 1*3 + 1 = 4s.
    (tmp_path / "start.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nsleep 30\n"  # never binds
    )
    passed, kind, msg, _steps, _listening = _subtree_verify_start_sh(
        tmp_path,
        declared_ports=[5192],
        timeout_s=1,
        port_wait_s=1,
        log=_logger(),
    )
    assert not passed
    assert kind == "ports_not_listening", (kind, msg)
    assert "install-inclusive" in (msg or "")


def test_absent_start_sh_is_skip_not_failure(tmp_path: Path) -> None:
    passed, kind, _msg, _steps, _listening = _subtree_verify_start_sh(
        tmp_path, declared_ports=[5193], timeout_s=1, port_wait_s=1, log=_logger()
    )
    assert passed and kind is None
