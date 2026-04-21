from __future__ import annotations

import json
from pathlib import Path

from otto.merge.state import MERGE_STATE_SCHEMA_VERSION, load_state


def test_load_state_drops_unknown_top_level_keys(tmp_path: Path):
    merge_id = "merge-test"
    state_dir = tmp_path / "otto_logs" / "merge" / merge_id
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(json.dumps({
        "schema_version": MERGE_STATE_SCHEMA_VERSION,
        "merge_id": merge_id,
        "started_at": "2026-04-20T00:00:00Z",
        "target": "main",
        "target_head_before": "abc123",
        "branches_in_order": ["feat-a"],
        "outcomes": [
            {"branch": "feat-a", "status": "merged", "merge_commit": "def456"},
        ],
        "verification_plan_path": "otto_logs/merge/merge-test/verify-plan.json",
        "future_deleted_field": {"note": "should be ignored"},
    }))

    loaded = load_state(tmp_path, merge_id)

    assert loaded.merge_id == merge_id
    assert loaded.target == "main"
    assert len(loaded.outcomes) == 1
    assert loaded.outcomes[0].branch == "feat-a"
