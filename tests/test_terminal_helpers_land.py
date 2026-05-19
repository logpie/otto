"""Keystone Task 2: the two terminal-recording helpers must LAND
(verdict 'partial' + annotation) instead of setting 'merge_blocked',
for every origin that maps to a non-INFRA cause. Disk write is
monkeypatched — this asserts the control DECISION, not persistence.
"""
from pathlib import Path

from otto.lead import LeadResult
from otto import v5_runner as R


def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(
        R, "set_verdict_and_metadata",
        lambda pd, tid, verdict, **kw: calls.append((verdict, kw)),
    )
    return calls


def test_merge_blocked_reason_lands_on_verification(monkeypatch):
    calls = _capture(monkeypatch)
    res = LeadResult(task_id="t")
    R._record_task_merge_blocked_reason(
        project_dir=Path("/tmp/none"), task_id="t", result=res,
        reason="journey j1 not proven", origin="verification",
    )
    assert res.verdict == "partial", "must land, not merge_blocked"
    assert calls and calls[0][0] == "partial"
    anns = (res.verify_result or {}).get("annotations") or []
    assert anns and anns[0]["origin"] == "verification"
    assert anns[0]["cause"] == "verification"


def test_structured_merge_failed_lands_on_conflict(monkeypatch):
    calls = _capture(monkeypatch)
    res = LeadResult(task_id="c")
    R._record_structured_merge_failed(
        project_dir=Path("/tmp/none"), task_id="c", result=res,
        reason="conflict on app.py", origin="merge_repair_helper",
        phase="merge", structured_reason={"k": "v"}, on_event=None,
    )
    assert res.verdict == "partial", "must land, not merge_blocked"
    assert (res.verify_result or {}).get("verdict") == "partial"
    assert calls and calls[-1][0] == "partial"


def test_no_helper_path_writes_merge_blocked_for_land_causes(monkeypatch):
    calls = _capture(monkeypatch)
    res = LeadResult(task_id="t")
    R._record_task_merge_blocked_reason(
        project_dir=Path("/tmp/none"), task_id="t", result=res,
        reason="feature incomplete", origin="some_unmapped_origin",
    )
    assert "merge_blocked" not in [c[0] for c in calls]
    assert res.verdict == "partial"
