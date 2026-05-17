"""Runs #6/#8/#10/#11 regression: the foundation-scheduler post-pass contracts
check must not spuriously fire while persist hasn't yet written contracts to
the graph.

THE single biggest <45min obstacle this campaign. Every one of runs #6/#8/#10/
#11 failed the architect contract gate on attempt 1, then self-corrected on a
~14min architect re-dispatch. Definitive diagnosis (run #11 foundation
retry_reason): kind=`foundation_contracts_missing_after_pass`,
step_id=`foundation_scheduler_contracts_after_pass`, `contract_findings: []`,
`contracts_present: false` — i.e. the contracts were perfectly valid, the
check just could not see them yet.

Root cause (ordering race): `_foundation_contracts_for_parent` read contracts
only from task-graph metadata, which is populated by
`persist_foundation_contracts_from_charter` in the architect-contract gate
path — NOT before the scheduler's post-pass check. A foundation child that has
passed but whose contracts have not yet been persisted made every graph-only
lookup return empty, so `_foundation_scheduler_feedback` fired
`foundation_contracts_missing_after_pass` and re-dispatched the architect.

Fix: `_foundation_contracts_for_parent` falls back to parsing CHARTER.md (the
source of truth the scaffold actually produced, already on disk by this point)
when graph metadata is empty. This pins the exact race state: foundation child
PASSED, NO contracts persisted on parent/child, CHARTER on disk → contracts
must be found (old code → 0 → spurious gate-fail).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from otto.queue.task_graph import SCHEMA_VERSION, task_graph_path
from otto.v5_runner import _foundation_contracts_for_parent

_REAL_CHARTER = (
    Path(__file__).parent / "fixtures" / "charter_architect_real_run10_multiblock.md"
)


def _seed(tmp: Path, tasks: dict) -> None:
    gp = task_graph_path(tmp)
    gp.parent.mkdir(parents=True, exist_ok=True)
    gp.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "tasks": tasks}),
        encoding="utf-8",
    )


def test_charter_fallback_when_graph_metadata_empty(tmp_path: Path) -> None:
    # The exact gate-time race: foundation child PASSED, persist has NOT
    # written foundation_contracts onto parent or child, CHARTER.md on disk.
    shutil.copy(_REAL_CHARTER, tmp_path / "CHARTER.md")
    _seed(
        tmp_path,
        {
            "root": {"task_role": "feature", "intent": "root"},
            "v5-arch": {
                "task_role": "foundation",
                "parent_task_id": "root",
                "verdict": "pass",
            },
        },
    )
    contracts = _foundation_contracts_for_parent(tmp_path, "root")
    # Old behaviour: 0 → scheduler spuriously fires
    # foundation_contracts_missing_after_pass → ~14min re-dispatch.
    assert len(contracts) >= 10, f"CHARTER fallback failed: {len(contracts)}"
    # Every contract has a non-empty owner. The architect declares owner_task_id
    # in the CHARTER, so setdefault preserves those (does NOT clobber to the
    # child id); it only fills in the foundation child when an owner is absent.
    assert all(str(c.get("owner_task_id") or "").strip() for c in contracts), contracts[0]


def test_graph_metadata_still_authoritative_when_present(tmp_path: Path) -> None:
    # When persist HAS written contracts to the parent, those win (no parse).
    shutil.copy(_REAL_CHARTER, tmp_path / "CHARTER.md")
    _seed(
        tmp_path,
        {
            "root": {
                "task_role": "feature",
                "intent": "root",
                "foundation_contracts": [
                    {"path": "backend/db.py", "owner_task_id": "v5-arch", "check": "literal"}
                ],
            },
            "v5-arch": {
                "task_role": "foundation",
                "parent_task_id": "root",
                "verdict": "pass",
            },
        },
    )
    contracts = _foundation_contracts_for_parent(tmp_path, "root")
    assert len(contracts) == 1
    assert contracts[0]["path"] == "backend/db.py"


def test_no_charter_no_metadata_returns_empty(tmp_path: Path) -> None:
    # No graph metadata AND no CHARTER → genuinely empty (no crash, no false
    # positive). The scheduler SHOULD flag this real case.
    _seed(
        tmp_path,
        {
            "root": {"task_role": "feature", "intent": "root"},
            "v5-arch": {
                "task_role": "foundation",
                "parent_task_id": "root",
                "verdict": "pass",
            },
        },
    )
    assert _foundation_contracts_for_parent(tmp_path, "root") == []
