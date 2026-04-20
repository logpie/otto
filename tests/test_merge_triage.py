from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from otto.merge.triage_agent import (
    _extract_json,
    _validate_plan_shape,
    produce_verification_plan,
)


def test_extract_json_handles_nested_fenced_objects():
    text = """
before
```json
{
  "must_verify": [
    {"name": "Story A", "meta": {"reason": "touches app.py"}}
  ],
  "skip_likely_safe": [
    {"name": "Story B", "meta": {"reason": "docs only"}}
  ],
  "flag_for_human": []
}
```
after
"""
    data = _extract_json(text)
    assert data is not None
    assert data["must_verify"][0]["name"] == "Story A"
    assert data["skip_likely_safe"][0]["meta"]["reason"] == "docs only"


def test_validate_plan_shape_rejects_missing_input_story():
    ok, err = _validate_plan_shape(
        {
            "must_verify": [{"name": "Story A"}],
            "skip_likely_safe": [],
            "flag_for_human": [],
        },
        input_stories=[{"name": "Story A"}, {"name": "Story B"}],
    )
    assert ok is False
    assert "dropped stories" in err or "missing input stories" in err


def test_produce_verification_plan_retries_incomplete_story_coverage(tmp_path: Path):
    calls = 0

    async def fake_run_agent_with_timeout(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                '{"must_verify":[],"skip_likely_safe":[],"flag_for_human":[]}',
                0.25,
                "",
            )
        return (
            '{"must_verify":[{"name":"Story A"}],'
            '"skip_likely_safe":[{"name":"Story B"}],'
            '"flag_for_human":[]}',
            0.5,
            "",
        )

    stories = [{"name": "Story A"}, {"name": "Story B"}]
    with patch("otto.agent.run_agent_with_timeout", side_effect=fake_run_agent_with_timeout):
        plan = asyncio.run(
            produce_verification_plan(
                project_dir=tmp_path,
                config={"provider": "claude"},
                branches=["feat-a"],
                stories=stories,
                merge_diff_files=["app.py"],
            )
        )

    assert calls == 2
    assert [item["name"] for item in plan.must_verify] == ["Story A"]
    assert [item["name"] for item in plan.skip_likely_safe] == ["Story B"]
    assert plan.fallback_used is False
