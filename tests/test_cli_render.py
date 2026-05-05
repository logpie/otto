from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from otto.cli import main
from otto.render import PROOF_PACKET_HTML, PROOF_PACKET_JSON


def test_render_command_rerenders_session_path_without_llm(tmp_path: Path) -> None:
    (tmp_path / PROOF_PACKET_JSON).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "intent": "render existing proof",
                "project_kind": "webapp",
                "verdict": "passed",
                "wall_s": 1.0,
                "cost_usd": 0.0,
                "groups": [
                    {
                        "group_id": "shell",
                        "name": "App shell",
                        "status": "landed",
                        "landed": True,
                        "landed_commit": "abc",
                    }
                ],
                "landed_group_ids": ["shell"],
                "blocked_group_ids": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / PROOF_PACKET_HTML).write_text("stale", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["render", str(tmp_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Rendered proof packet" in result.output
    html = (tmp_path / PROOF_PACKET_HTML).read_text(encoding="utf-8")
    assert "stale" not in html
    assert "<h2>Groups</h2>" in html
    assert "render existing proof" in html

