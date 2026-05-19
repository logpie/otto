"""Phase 4: when the foundation clean-boot probe fails, otto must
DEGRADE to the deterministic P0 scaffold (materialize_seed) so feature
children can still build/merge on a working scaffold — instead of the
prior behavior of marking every ready feature merge_blocked and
stripping them from the ready queue (the `project_otto_structural_block_gap`
memory: "no recovery when foundation group blocks; partial worktree
often substantially complete").

Phase 5's chokepoint already turned the foundation's own verdict from
merge_blocked → partial+annotation, so the feature-blocking branch is
effectively unreachable. Phase 4 adds the corresponding worktree-state
fix: materialize the P0 scaffold so features have a functioning base."""

from otto.v5_runner import _foundation_failure_action


def test_degrades_when_probe_blocks():
    assert _foundation_failure_action(probe_blocks=True) == "degrade_to_scaffold"


def test_proceeds_when_probe_clean():
    assert _foundation_failure_action(probe_blocks=False) == "proceed"
