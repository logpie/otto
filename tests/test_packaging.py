from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


def test_wheel_includes_prompt_markdown(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is required for the packaging smoke test")

    repo_root = Path(__file__).resolve().parents[1]
    source_root = tmp_path / "source"
    dist_dir = tmp_path / "dist"
    source_root.mkdir()
    package_files = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "pyproject.toml",
            "README.md",
            "otto",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for rel in package_files:
        src = repo_root / rel
        if not src.is_file():
            continue
        dst = source_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=source_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    wheels = sorted(dist_dir.glob("otto-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        entry_points = [
            name for name in names
            if name.endswith(".dist-info/entry_points.txt")
        ]
        assert len(entry_points) == 1
        entry_point_text = wheel.read(entry_points[0]).decode("utf-8")
    expected_prompts = {
        f"otto/prompts/{path.name}"
        for path in (source_root / "otto" / "prompts").glob("*.md")
    }
    assert expected_prompts
    assert expected_prompts <= names
    assert "otto = otto.cli:main" in entry_point_text
