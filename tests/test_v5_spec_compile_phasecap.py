"""Regression: the spec_compile phase cap must accommodate the phase's OWN
bounded pass-model repair loop.

Root cause (v5-itracker-setupfix2-002240 terminal merge_blocked at 1205s,
"spec_compile phase cap exceeded (1200s; run_budget_seconds=7200s)"):
v5_runner wrapped the ENTIRE compile_flat_spec (whose internal loop allows
max_retries + 1 + PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS = 8 full LLM attempts)
in _await_with_run_deadline with a flat ~900-1200s cap. 8 LLM compile/repair
rounds cannot fit; the cap fired ~round 3 and killed a *monotonically
converging* bounded spec-repair (3 distinct journeys fixed one-per-round, no
repeats) while only 1205s of the 7200s run budget was used. The phase cap and
the phase's own retry budget were independent contradictory constants.

Fix: derive the spec_compile cap from the phase's own attempt budget
(single source of truth, spec_compile_attempt_budget), so cap and the
bounded-repair loop are consistent by construction. Explicit
spec_timeout / spec_compile_timeout_s config override still wins.
_await_with_run_deadline still clamps to remaining run budget (the global
ceiling), so this is not unbounded and not gate-weakening.
"""

from __future__ import annotations

from otto.spec_compile_flat import (
    PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS,
    SPEC_COMPILE_PER_ATTEMPT_BUDGET_S,
    spec_compile_attempt_budget,
)
from otto.v5_runner import _spec_compile_cap_s


def test_attempt_budget_tracks_repair_constant() -> None:
    # max_retries(default 2) + 1 + PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS.
    assert spec_compile_attempt_budget() == 2 + 1 + PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS
    assert spec_compile_attempt_budget(0) == 1 + PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS
    # Tracks the repair constant (single source of truth — if the bounded
    # repair budget grows, the attempt budget grows with it).
    assert spec_compile_attempt_budget() >= PASS_MODEL_CONTRACT_REPAIR_ATTEMPTS + 1


def test_default_cap_fits_full_bounded_repair_loop() -> None:
    cap = _spec_compile_cap_s({})
    expected = SPEC_COMPILE_PER_ATTEMPT_BUDGET_S * spec_compile_attempt_budget()
    assert cap == expected
    # The historical flat 1200s cap was the villain — the derived cap must be
    # comfortably larger so a converging bounded-repair loop is never killed
    # by the phase sub-cap (run_budget remains the real global ceiling, and
    # _await_with_run_deadline still clamps cap to remaining run budget).
    assert cap >= 1200.0 * 3, cap


def test_explicit_config_override_still_wins() -> None:
    assert _spec_compile_cap_s({"spec_timeout": 321}) == 321.0
    assert _spec_compile_cap_s({"spec_compile_timeout_s": 654}) == 654.0
    # spec_timeout takes precedence over spec_compile_timeout_s (call-site order).
    assert _spec_compile_cap_s({"spec_timeout": 100, "spec_compile_timeout_s": 999}) == 100.0
    # A zero / falsy override does not win — falls back to the derived cap.
    assert _spec_compile_cap_s({"spec_timeout": 0}) == _spec_compile_cap_s({})
