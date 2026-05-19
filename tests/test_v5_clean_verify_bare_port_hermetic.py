"""Regression: otto's hermetic-port remap must recognize the bare ``PORT``
env var, not only ``*_PORT``-suffixed names.

Root cause (fix11-104116, 2026-05-18, --tier modular iTracker, terminal
`clean_deploy_port_busy [block]: Declared ports [8000] already bound (likely
zombies from prior runs)`): the scaffold did the CORRECT 12-factor thing —
``start.sh`` had ``PORT="${PORT:-8000}"`` and CHARTER declared
``| Backend (uvicorn) | `PORT` | `8000` |`` — but ``_parse_declared_port_envs``
captured the env-var name with ``[A-Z][A-Z0-9_]*PORT``, which requires the
name to be >=5 chars ending in "PORT". The bare conventional ``PORT`` (4
chars, the single most common port env var: Heroku / Cloud Run / 12-factor)
does NOT match (``re.fullmatch("[A-Z][A-Z0-9_]*PORT","PORT")`` is None), while
``FRONTEND_PORT`` does. So the backend port was seen only as the prose
``localhost:8000`` -> ``(None, 8000)`` with no named env to override ->
``_ephemeral_port_plan`` could not remap it -> it fell back to the FIXED
port 8000 -> ``_check_ports_free([8000])`` found a leaked zombie from a prior
run on the shared machine -> false ``clean_deploy_port_busy`` hard-block.
The frontend (``FRONTEND_PORT``) WAS hermetically remapped; only the backend
(bare ``PORT``) was not. That asymmetry is the recurring "numerous times
tripped by port issues" class.

Fix (consistent-by-construction, NOT gate-weakening): the env-name capture
becomes ``(?:[A-Z][A-Z0-9_]*)?PORT`` (optional prefix), so bare ``PORT`` and
every ``*_PORT`` name are recognized. otto's already-designed hermetic
ephemeral-port remap then applies to the backend port too, and the
fixed-port / leaked-zombie collision class becomes structurally impossible —
exactly what the hermetic design already intends. The clean-deploy /
port-busy gates are unchanged; the parser is made complete so the existing
self-healing actually applies.

Before the fix the bare-``PORT`` assertions are RED; GREEN after. The
``FRONTEND_PORT`` assertions guard that the existing >=5-char behavior is
preserved (no regression).
"""

from __future__ import annotations

from pathlib import Path

from otto.v5_clean_verify import _ephemeral_port_plan, _parse_declared_port_envs

_START_SH = """#!/usr/bin/env bash
set -euo pipefail
PORT="${PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
PYTHONUNBUFFERED=1 PORT="$PORT" uvicorn backend.main:app --port "$PORT" &
FRONTEND_PORT="$FRONTEND_PORT" npm run dev -- --port "$FRONTEND_PORT" &
wait
"""

_CHARTER = """# CHARTER

## Ports

| Service | Env | Default |
|---|---|---|
| Backend (uvicorn) | `PORT` | `8000` |
| Frontend (vite dev) | `FRONTEND_PORT` | `5173` |
| API base (fe env) | `VITE_API_URL` | `http://localhost:8000` |
"""


def _project(tmp_path: Path) -> Path:
    (tmp_path / "start.sh").write_text(_START_SH, encoding="utf-8")
    (tmp_path / "CHARTER.md").write_text(_CHARTER, encoding="utf-8")
    return tmp_path


def test_bare_PORT_is_parsed_as_a_named_env(tmp_path: Path) -> None:
    """``PORT="${PORT:-8000}"`` (start.sh) and ``| ... | `PORT` | `8000` |``
    (CHARTER) must yield a NAMED env binding ('PORT', 8000) — not only the
    prose ``(None, 8000)`` — so the hermetic remap has an env to override."""
    envs = _parse_declared_port_envs(_project(tmp_path))
    assert ("PORT", 8000) in envs, (
        f"bare PORT env not recognized as named; parsed={envs}"
    )
    # the existing >=5-char behavior must still work (no regression)
    assert ("FRONTEND_PORT", 5173) in envs, (
        f"FRONTEND_PORT regressed; parsed={envs}"
    )


def test_ephemeral_plan_remaps_bare_PORT_no_fixed_8000_in_probe(
    tmp_path: Path,
) -> None:
    """With bare ``PORT`` recognized, ``_ephemeral_port_plan`` must remap it
    to a fresh ephemeral port and inject a ``PORT`` env override; the probe
    set must NOT contain the fixed 8000 (the value that collided with leaked
    zombies and produced the false ``clean_deploy_port_busy`` block)."""
    envs = _parse_declared_port_envs(_project(tmp_path))
    overrides, probe, _effective = _ephemeral_port_plan(envs)
    assert "PORT" in overrides, (
        f"backend bare PORT not remapped to an ephemeral port; "
        f"overrides={overrides}"
    )
    assert "FRONTEND_PORT" in overrides, (
        f"FRONTEND_PORT remap regressed; overrides={overrides}"
    )
    assert 8000 not in probe, (
        f"fixed 8000 still in probe set (would collide with zombies / "
        f"false clean_deploy_port_busy); probe={probe}"
    )
    assert 5173 not in probe, (
        f"fixed 5173 still in probe set; probe={probe}"
    )


# --- alias-env collapse (linkboard-p0seed-200150, 2026-05-18) --------------
#
# Root cause (FRESH Linkboard P0-seed run, terminal
# `clean_deploy_ports_not_listening [block]: ports [58605] did not bind`,
# both servers actually up): the P0-seeded start.sh declares
# ``FRONTEND_PORT="${FRONTEND_PORT:-5173}"`` and aliases the conventional
# external port via ``PORT="${PORT:-$FRONTEND_PORT}"``; the generated
# CHARTER port table also exposes that SAME single Vite server under the
# name ``PORT`` (``| Frontend | 5173 | PORT | / |``). So
# ``_parse_declared_port_envs`` correctly returns BOTH named envs for the
# one logical frontend port: ``[('FRONTEND_PORT', 5173), ('PORT', 5173),
# ('API_PORT', 8000)]``. ``_ephemeral_port_plan`` then allocated a SEPARATE
# ephemeral port per distinct env_name — two different ephemerals for the
# one Vite listener — so the probe set had 3 ports but only 2 ever bound,
# producing a false ``ports_not_listening`` hard-block. The pre-existing
# ``orig_to_eph`` "same original port == same logical service" collapse was
# consulted ONLY for unnamed prose duplicates, never for two distinct NAMED
# envs aliasing the same port.
#
# Fix (consistent-by-construction, NOT gate-weakening): a named env whose
# original logical port was already remapped by an earlier named env reuses
# that shared ephemeral instead of allocating a second one nothing binds —
# the same invariant the prose-duplicate collapse already relies on. The
# clean-deploy / ports-not-listening gates are unchanged; the probe set is
# made to match the actual listener count.


def test_aliased_named_envs_for_one_port_collapse_to_one_ephemeral() -> None:
    """Two distinct NAMED envs that point at the same original logical port
    (``FRONTEND_PORT`` from start.sh + ``PORT`` from the CHARTER table, both
    5173, one Vite listener) must remap to the SAME ephemeral, and the probe
    set must have exactly two ports (frontend + backend) — not three."""
    port_envs = [("FRONTEND_PORT", 5173), ("PORT", 5173), ("API_PORT", 8000)]
    overrides, probe, effective = _ephemeral_port_plan(port_envs)
    assert overrides["FRONTEND_PORT"] == overrides["PORT"], (
        f"aliased frontend envs got different ephemerals (phantom 3rd port "
        f"-> false ports_not_listening); overrides={overrides}"
    )
    assert overrides["API_PORT"] != overrides["FRONTEND_PORT"], (
        f"backend collapsed onto frontend; overrides={overrides}"
    )
    assert len(probe) == 2, (
        f"probe must match the 2 real listeners (1 Vite + 1 uvicorn), "
        f"not 3; probe={probe} effective={effective}"
    )
    eff = {en: p for en, p in effective}
    assert eff["FRONTEND_PORT"] == eff["PORT"]
    assert str(eff["FRONTEND_PORT"]) == overrides["FRONTEND_PORT"]


def test_distinct_logical_ports_still_get_distinct_ephemerals() -> None:
    """Guard: envs with DIFFERENT original ports must still each get their
    own ephemeral (no over-collapse)."""
    overrides, probe, _effective = _ephemeral_port_plan(
        [("PORT", 8000), ("FRONTEND_PORT", 5173)]
    )
    assert overrides["PORT"] != overrides["FRONTEND_PORT"], (
        f"distinct logical ports wrongly collapsed; overrides={overrides}"
    )
    assert len(probe) == 2, f"probe={probe}"
