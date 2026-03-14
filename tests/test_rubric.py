"""Tests for otto.rubric module."""

import subprocess
from pathlib import Path
from otto.rubric import _gather_project_context


class TestGatherProjectContext:
    def test_includes_file_tree(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
        (tmp_path / "src.py").write_text("def hello(): pass")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add src"], cwd=tmp_path, capture_output=True)
        ctx = _gather_project_context(tmp_path)
        assert "src.py" in ctx

    def test_includes_source_content(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
        (tmp_path / "app.py").write_text("class MyApp:\n    pass\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add app"], cwd=tmp_path, capture_output=True)
        ctx = _gather_project_context(tmp_path)
        assert "class MyApp" in ctx

    def test_skips_lockfiles(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, capture_output=True)
        (tmp_path / "poetry.lock").write_text("hash123")
        (tmp_path / "app.py").write_text("x = 1")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add files"], cwd=tmp_path, capture_output=True)
        ctx = _gather_project_context(tmp_path)
        assert "hash123" not in ctx
        assert "x = 1" in ctx
