from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_replay_browser_check_reruns_one_browser_journey(tmp_path: Path) -> None:
    script = tmp_path / "journey.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('evidence').mkdir(exist_ok=True)\n"
        "Path('evidence/step.png').write_bytes(b'png')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/replay_browser_check.py",
            "--project-dir",
            str(tmp_path),
            "--command",
            sys.executable,
            "--command",
            str(script),
            "--evidence-glob",
            "evidence/*.png",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["artifacts"][0].endswith("evidence/step.png")
