from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.queue.task_graph import read_graph, record_task, update_task_metadata
from otto.v5_capability_inventory import (
    persist_feature_owned_paths_from_charter,
    persist_foundation_contracts_from_charter,
)
from otto.v5_runner import _foundation_isolation_feedback, _reenter_or_block_architect_contract


def _record_root_with_foundation_and_features(project_dir: Path) -> None:
    record_task(project_dir, task_id="root", intent="build app")
    record_task(
        project_dir,
        task_id="foundation",
        intent="Build scaffold",
        parent_task_id="root",
        task_role="foundation",
        owned_paths=["src/shared/api.ts"],
    )
    record_task(
        project_dir,
        task_id="feature-a",
        intent="Feature A",
        parent_task_id="root",
        task_role="feature",
        depends_on=["foundation"],
        owned_paths=["src/shared/api.ts"],
    )


@pytest.mark.asyncio
async def test_unambiguous_foundation_contract_collision_is_rescoped_without_redispatch(
    tmp_path: Path,
) -> None:
    _record_root_with_foundation_and_features(tmp_path)
    contracts = [{
        "path": "src/shared/api.ts",
        "owner_task_id": "foundation",
        "check": "literal",
    }]
    update_task_metadata(tmp_path, "root", foundation_contracts=contracts)
    feedback = _foundation_isolation_feedback(
        parent_task_id="root",
        architect_task_id="foundation",
        tasks=read_graph(tmp_path)["tasks"],
        contracts=contracts,
    )
    assert feedback is not None
    events: list[dict[str, object]] = []

    blocked = await _reenter_or_block_architect_contract(
        project_dir=tmp_path,
        architect_tid="foundation",
        child_results={},
        completed={"foundation"},
        feedback=feedback,
        origin="architect_contract",
        config={},
        on_event=events.append,
    )

    assert blocked is False
    assert read_graph(tmp_path)["tasks"]["feature-a"]["owned_paths"] == []
    assert [event["event"] for event in events] == ["architect_contract_rescoped"]


def test_charter_partition_overlap_reaches_isolation_gate_for_deterministic_rescope(
    tmp_path: Path,
) -> None:
    record_task(tmp_path, task_id="root", intent="build app")
    record_task(
        tmp_path,
        task_id="foundation",
        intent="Build scaffold",
        parent_task_id="root",
        task_role="foundation",
        owned_paths=["src/shared/api.ts"],
    )
    record_task(
        tmp_path,
        task_id="feature-a",
        intent="Feature A",
        parent_task_id="root",
        task_role="feature",
        depends_on=["foundation"],
    )
    (tmp_path / "CHARTER.md").write_text(
        """# Charter

## Information Architecture Contract

```json
{
  "foundation_contracts": [
    {"path": "src/shared/api.ts", "owner_task_id": "foundation", "check": "literal"}
  ],
  "registration_isolation": {"leaf_extension_globs": ["src/features/*"]},
  "feature_owned_paths": {"feature-a": ["src/shared/api.ts"]}
}
```
""",
        encoding="utf-8",
    )

    contracts, contract_findings = persist_foundation_contracts_from_charter(
        tmp_path,
        parent_task_id="root",
    )
    feature_paths, feature_findings = persist_feature_owned_paths_from_charter(
        tmp_path,
        parent_task_id="root",
    )
    feedback = _foundation_isolation_feedback(
        parent_task_id="root",
        architect_task_id="foundation",
        tasks=read_graph(tmp_path)["tasks"],
        contracts=contracts,
    )

    assert contract_findings == []
    assert feature_findings == []
    assert feature_paths == {"feature-a": ["src/shared/api.ts"]}
    assert feedback is not None
    assert feedback["findings"][0]["kind"] == "feature_overlaps_foundation_contract"


@pytest.mark.asyncio
async def test_feature_feature_overlap_uses_plan_amendment_repair_not_architect_redispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_root_with_foundation_and_features(tmp_path)
    record_task(
        tmp_path,
        task_id="feature-b",
        intent="Feature B",
        parent_task_id="root",
        task_role="feature",
        depends_on=["foundation"],
        owned_paths=["src/features/shared.tsx"],
    )
    update_task_metadata(tmp_path, "feature-a", owned_paths=["src/features/shared.tsx"])
    contracts: list[dict[str, object]] = []
    feedback = _foundation_isolation_feedback(
        parent_task_id="root",
        architect_task_id="foundation",
        tasks=read_graph(tmp_path)["tasks"],
        contracts=contracts,
    )
    assert feedback is not None
    captured: dict[str, object] = {}

    async def fake_repair_agent(packet: object, **_kwargs: object) -> object:
        captured["allowed_paths"] = list(packet.repair_unit["allowed_paths"])
        captured["scope_policy"] = packet.repair_unit["scope_policy"]
        captured["prompt_template"] = packet.repair_unit["prompt_template"]
        update_task_metadata(tmp_path, "feature-b", owned_paths=["src/features/other.tsx"])
        return SimpleNamespace(
            verdict="pass",
            summary="amended partition",
            packet_path=str(packet.packet_path),
        )

    monkeypatch.setattr("otto.v5_runner.run_oracle_repair_agent", fake_repair_agent)
    events: list[dict[str, object]] = []

    blocked = await _reenter_or_block_architect_contract(
        project_dir=tmp_path,
        architect_tid="foundation",
        child_results={},
        completed={"foundation"},
        feedback=feedback,
        origin="architect_contract",
        config={},
        on_event=events.append,
    )

    assert blocked is False
    assert captured["scope_policy"] == "allowed_paths"
    assert captured["prompt_template"] == "plan-amendment.md"
    assert captured["allowed_paths"] == [
        "CHARTER.md",
        "otto_logs/cross-sessions/task_graph.json",
        "otto_logs/cross-sessions/v5_pending.jsonl",
    ]
    assert "architect_retry" not in [event["event"] for event in events]
