"""Protocol-level regression replacing the recurring clean-deploy
"port conflict / port did not bind" patch cluster.

Two evidence-backed root causes, each proven from a live resume #16 run:

1. DUAL-STACK PROBE. The bind timeline showed `t=9s :5173=node/5384
   connect=fail | :8000=Python/5400 connect=ok`: vite WAS in LISTEN state
   on :5173 at t=9s but the oracle's AF_INET/127.0.0.1-only probe could
   never connect, while the identical probe to uvicorn (--host 127.0.0.1)
   on :8000 succeeded. start.sh ran `npm run dev -- --port $FRONTEND_PORT`
   WITHOUT `--host 127.0.0.1`, so vite bound `localhost`, which resolves
   IPv6-first on macOS → it listened on `[::1]:PORT`. A correct product
   was mis-blocked purely because the readiness probe was IPv4-only.

2. HERMETIC PORTS. start.sh parameterizes ports (`${FRONTEND_PORT:-5173}`)
   but the oracle never overrode them, so every deploy fought over the
   same fixed 5173/8000 — colliding with prior rounds / leaked orphans /
   concurrent runs. Allocating fresh ephemeral ports per deploy makes the
   whole collision class structurally impossible (deterministic).

Neither weakens the gate: a server that genuinely is not accepting on any
address still fails, and an unreachable port still fails.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from otto.v5_clean_verify import (
    _ephemeral_port_plan,
    _port_connectable,
    _subtree_verify_start_sh,
)


def _logger():
    return lambda *_a, **_k: None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_port_connectable_detects_ipv6_only_server() -> None:
    # A server bound to ::1 ONLY (vite's effective behavior when start.sh
    # omits --host 127.0.0.1) must be detected. The old AF_INET-only probe
    # returned False here — the exact false negative that mis-blocked.
    try:
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.bind(("::1", 0))
    except OSError:
        pytest.skip("no IPv6 loopback on this host")
    port = srv.getsockname()[1]
    srv.listen(1)
    try:
        assert _port_connectable(port) is True, (
            "IPv6-only (::1) listener must be detected (dual-stack probe)"
        )
        # IPv4-only sanity: nothing should be on 127.0.0.1:port.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        v4 = s.connect_ex(("127.0.0.1", port))
        s.close()
        assert v4 != 0, "precondition: nothing on IPv4 for this port"
    finally:
        srv.close()


def test_port_connectable_false_when_nothing_listening() -> None:
    # Gate preserved: no listener on any family → not connectable.
    assert _port_connectable(_free_port()) is False


def test_ephemeral_plan_remaps_named_envs_only() -> None:
    env_out, probe, effective = _ephemeral_port_plan(
        [("API_PORT", 8000), ("FRONTEND_PORT", 5173), (None, 9999)]
    )
    # Port envs are remapped to ephemeral ports...
    assert {"API_PORT", "FRONTEND_PORT"} <= set(env_out)
    assert int(env_out["API_PORT"]) not in (8000, 5173)
    assert int(env_out["FRONTEND_PORT"]) not in (8000, 5173)
    assert int(env_out["API_PORT"]) != int(env_out["FRONTEND_PORT"])
    # ...and the matching frontend origin is published under conventional
    # CORS/origin env names so a port-coupled backend allow-list still
    # accepts the (now ephemeral) frontend origin. Backends ignore the
    # names they do not read — generic, not iTracker-specific.
    fe = int(env_out["FRONTEND_PORT"])
    assert env_out["CORS_ORIGINS"] == (
        f"http://127.0.0.1:{fe},http://localhost:{fe}"
    )
    assert env_out["ALLOWED_ORIGINS"] == env_out["CORS_ORIGINS"]
    assert "5173" not in env_out["CORS_ORIGINS"]
    # Prose-only port (no env to inject) is kept as-is.
    assert (None, 9999) in effective
    assert 9999 in probe
    assert fe in probe
    assert 5173 not in probe and 8000 not in probe


def test_prose_duplicate_of_named_port_is_remapped_too() -> None:
    # The exact resume16i bug: _parse_declared_port_envs yields the same
    # logical port BOTH as a named env (start.sh ${FRONTEND_PORT:-5173})
    # AND as unnamed prose duplicates (CHARTER "127.0.0.1:5173" / "port
    # 5173"). The prose (None, 5173) entries must inherit FRONTEND_PORT's
    # ephemeral remap — otherwise 5173 leaked into the probe set and the
    # deploy (correctly bound to the ephemeral port) was falsely blocked
    # as "ports [5173] did not bind".
    port_envs = [
        (None, 5173),
        (None, 8000),
        ("FRONTEND_PORT", 5173),
        ("API_PORT", 8000),
    ]
    overrides, probe, effective = _ephemeral_port_plan(port_envs)
    fe = int(overrides["FRONTEND_PORT"])
    be = int(overrides["API_PORT"])
    # The original fixed ports must NOT remain anywhere in the probe set.
    assert 5173 not in probe and 8000 not in probe, probe
    assert sorted(probe) == sorted({fe, be})
    # Every entry — including the unnamed prose duplicates — maps to the
    # ephemeral port, so nothing probes the original 5173/8000.
    assert {p for _en, p in effective} == {fe, be}


def test_hermetic_ipv6_deploy_passes_even_with_5173_busy(
    tmp_path: Path,
) -> None:
    # End-to-end: both fixes together. start.sh honors $FRONTEND_PORT and
    # binds it on ::1 ONLY (no --host 127.0.0.1, IPv6-first). Port 5173 is
    # occupied (the "fixed port busy from a prior round" scenario). With
    # ephemeral ports the deploy ignores the busy 5173, and with the
    # dual-stack probe the ::1 bind is detected → PASS. Pre-fix this was a
    # guaranteed merge_blocked.
    try:
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 5173))
        busy.listen(1)
    except OSError:
        pytest.skip("port 5173 not bindable in this environment")
    (tmp_path / "start.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "FRONTEND_PORT=${FRONTEND_PORT:-5173}\n"
        # IPv6-only listener (mimics vite defaulting to localhost->::1).
        'python3 -m http.server --bind ::1 "$FRONTEND_PORT" '
        ">/dev/null 2>&1 &\n"
        "wait\n"
    )
    try:
        overrides, probe, _eff = _ephemeral_port_plan(
            [("FRONTEND_PORT", 5173)]
        )
        assert 5173 not in probe  # deploy will NOT use the busy fixed port
        passed, kind, msg, _steps, listening = _subtree_verify_start_sh(
            tmp_path,
            declared_ports=probe,
            timeout_s=6,
            port_wait_s=2,
            log=_logger(),
            port_env_overrides=overrides,
        )
        assert passed, (
            f"hermetic IPv6 deploy must pass despite busy 5173: {kind} {msg}"
        )
        assert listening == probe
        time.sleep(0.5)
    finally:
        busy.close()
