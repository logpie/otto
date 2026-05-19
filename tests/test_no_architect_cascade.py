"""Phase 5: kill the architect re-entry / fresh-Lead re-decomposition
cascade (the p0fix2/3/4 root cause from the session-opening quote).

The smoking gun is `clear_verdict_for_retry(...)` on an architect task —
it clears the architect's verdict so the scheduler dispatches a FRESH
Lead from zero, discarding all prior child work + child branches. That's
the macro version of this whole session's "throw away work on failure"
anti-pattern, surviving at the within-run orchestration layer.

After Phase 5: no `clear_verdict_for_retry` call survives in
otto/v5_runner.py (outside an explicit `# ALLOWED-ARCHITECT-RETRY:`
audit comment). Contract-gate failure routes through the terminal
chokepoint instead → architect lands `partial`+annotation via Part A.
"""
import pathlib


def test_no_clear_verdict_for_architect_retry():
    src = pathlib.Path("otto/v5_runner.py").read_text()
    lines = src.splitlines()
    bad = []
    for i, ln in enumerate(lines):
        if "clear_verdict_for_retry(" in ln:
            ctx = "\n".join(lines[max(0, i - 3):i + 1])
            if "ALLOWED-ARCHITECT-RETRY:" in ctx:
                continue
            bad.append(i + 1)
    assert not bad, (
        f"architect re-entry cascade survives — clear_verdict_for_retry "
        f"still called at lines {bad}; route through the chokepoint instead"
    )


def test_no_explicit_retry_architect_assignment():
    """Belt-and-suspenders: an explicit `retry_architect = True` outside
    `_reenter_or_block_architect_contract`'s return-value assignment is
    the secondary smell. After Phase 5 there should be no such literal
    assignment in v5_runner.py."""
    src = pathlib.Path("otto/v5_runner.py").read_text()
    lines = src.splitlines()
    bad = []
    for i, ln in enumerate(lines):
        # Allow `retry_architect = await _reenter_or_block_architect_contract(...)`
        # (assignment from the function, not literal True).
        if "retry_architect = True" in ln:
            ctx = "\n".join(lines[max(0, i - 3):i + 1])
            if "ALLOWED-ARCHITECT-RETRY:" in ctx:
                continue
            bad.append(i + 1)
    assert not bad, (
        f"explicit retry_architect=True (fresh-Lead cascade) at lines {bad}"
    )
