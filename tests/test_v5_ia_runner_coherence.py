from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from otto.lead import LeadResult
from otto.queue.subtask import v5_pending_path
from otto.queue.task_graph import record_task, set_decomposition, set_verdict
from otto.v5_branching import integration_branch_name
from otto.v5_clean_verify import CleanOracleResult, CleanOracleStepResult, ToolchainPreflightResult
from otto.v5_runner import _process_children


def _clean_scaffold_result(project_dir: Path, *_args: Any, **_kwargs: Any) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="scaffold",
        status="passed",
        return_code=0,
        command_identity="deterministic scaffold check",
        command=["true"],
        cwd=str(project_dir),
        env={},
    )
    return CleanOracleResult.from_parts(
        passed=True,
        scope="scaffold",
        issues=[],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env={},
        project_dir=project_dir,
    )


def _append_architect(project_dir: Path, task_id: str) -> None:
    entry = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": "root",
        "parent_session_dir": str(project_dir / "otto_logs" / "sessions" / "root"),
        "intent": "Architect the webapp scaffold.",
        "depends_on": [],
        "owned_paths": [],
        "action_ids": [],
        "integration_branch": integration_branch_name("root"),
        "review_state": "approved",
        "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    pending = v5_pending_path(project_dir)
    pending.parent.mkdir(parents=True, exist_ok=True)
    with pending.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    record_task(project_dir, task_id=task_id, intent=str(entry["intent"]), parent_task_id="root")


@pytest.mark.asyncio
async def test_architect_preflight_emits_ia_page_route_coherence_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_task(tmp_path, task_id="root", intent="root")
    _append_architect(tmp_path, "v5-architect")
    spec_dir = tmp_path / "otto_logs" / "sessions" / "root" / "spec"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "project_kind": "webapp",
                "product_overview": {
                    "top_level_pages": [
                        {"id": "home", "purpose": "home"},
                        {"id": "settings", "purpose": "manage settings"},
                    ],
                    "primary_navigation": {"sidebar": ["home", "settings"]},
                },
                "core_entities": [],
            }
        ),
        encoding="utf-8",
    )

    async def fake_run_child(**kwargs: Any) -> LeadResult:
        tid = kwargs["entry"]["task_id"]
        charter = {
            "entry_states": [{"id": "empty", "route": "/", "expected": "Home"}],
            "routes": [{"id": "home", "path": "/", "key_text": "Home"}],
            "nav_surfaces": [{"id": "sidebar", "must_link_routes": ["home"]}],
            "action_surfaces": [],
        }
        (tmp_path / "CHARTER.md").write_text(
            "# CHARTER\n\n## Information Architecture Contract\n\n```json\n"
            + json.dumps(charter)
            + "\n```\n\n## Agent operating notes\n\n- Use `npm run test`.\n",
            encoding="utf-8",
        )
        set_decomposition(tmp_path, tid, "inline")
        set_verdict(tmp_path, tid, "pass")
        return LeadResult(task_id=tid, verdict="pass", decomposition="inline")

    monkeypatch.setattr("otto.v5_runner._run_child", fake_run_child)
    monkeypatch.setattr("otto.v5_runner.verify_from_clean_oracle", _clean_scaffold_result)
    monkeypatch.setattr(
        "otto.v5_clean_verify.preflight_shared_toolchains",
        lambda worktree, **_kwargs: ToolchainPreflightResult(
            passed=True,
            worktree=str(worktree),
            _written_at="2026-05-14T00:00:00Z",
            manifest_counts={"package_json": 0, "pyproject": 0},
        ),
    )
    events: list[dict[str, Any]] = []

    await _process_children(
        project_dir=tmp_path,
        parent_task_id="root",
        config={},
        max_parallel=1,
        tree_budget_usd=10.0,
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    assert any(
        e.get("event") == "coherence_finding"
        and e.get("kind") == "ia_missing_product_page_route"
        and e.get("reference") == "settings"
        for e in events
    )
