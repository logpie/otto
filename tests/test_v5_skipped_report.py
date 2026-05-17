from __future__ import annotations

from pathlib import Path

from otto.lead import LeadResult, _write_skipped_report


def test_write_skipped_report_surfaces_manual_followup(tmp_path: Path) -> None:
    result = LeadResult(
        task_id="v5-child",
        verdict="partial",
        verify_called=True,
        verify_result={
            "verdict": "partial",
            "intent_coverage": {
                "built": ["core flow"],
                "partial": [],
                "skipped": [
                    {"feature": "CSV import", "reason": "parser not wired"},
                    "bulk edit workflow",
                ],
            },
        },
    )

    path = _write_skipped_report(tmp_path, result)

    assert path == tmp_path / "skipped_report.md"
    text = (tmp_path / "skipped_report.md").read_text(encoding="utf-8")
    assert "Manual follow-up required" in text
    assert "task=v5-child" in text
    assert "CSV import: parser not wired" in text
    assert "bulk edit workflow" in text
