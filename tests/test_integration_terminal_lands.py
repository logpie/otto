"""Task #5: the post-agent integration terminal must route through the
chokepoint. A post-agent smoke block is a VERIFICATION cause → LAND
(partial) + annotation, NEVER merge_blocked. Catastrophic is preserved;
non-blocking passes through. Evidence: Linkboard e2e terminated
root=merge_blocked at v5_runner.py:4757 (deferred direct literal).
"""
from otto.v5_runner import _integration_terminal_verdict


def test_post_agent_smoke_block_lands_partial_not_merge_blocked():
    verdict, reason = _integration_terminal_verdict(
        blocks=True, current_verdict="pass", reason="smoke still blocking"
    )
    assert verdict == "partial", f"must LAND, got {verdict}"
    assert reason == "smoke still blocking"


def test_catastrophic_is_preserved():
    verdict, reason = _integration_terminal_verdict(
        blocks=True, current_verdict="catastrophic", reason="x"
    )
    assert verdict == "catastrophic"


def test_non_blocking_passes_through():
    verdict, _ = _integration_terminal_verdict(
        blocks=False, current_verdict="pass", reason=""
    )
    assert verdict == "pass"


def test_never_returns_merge_blocked_for_smoke_block():
    # VERIFICATION cause is LAND in the chokepoint, so the integration
    # terminal can never refuse on a post-agent smoke block.
    for cur in ("pass", "partial", "unverified"):
        v, _ = _integration_terminal_verdict(
            blocks=True, current_verdict=cur, reason="r"
        )
        assert v != "merge_blocked"
