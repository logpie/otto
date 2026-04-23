from __future__ import annotations

import subprocess
from pathlib import Path

from otto.merge.conflict_agent import validate_post_agent
from otto.merge.edit_scope import EditScope


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    for rel_path, content in {
        "tests/visible/test_payments.py": "def test_payments():\n    assert True\n",
        "tests/visible/test_weekend_billing.py": "def test_weekend():\n    assert True\n",
        "tests/visible/test_unrelated.py": "def test_unrelated():\n    assert True\n",
    }.items():
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixtures"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return tmp_path, head


def _scope() -> EditScope:
    return EditScope(
        primary_files={"tests/visible/test_payments.py"},
        secondary_files={"tests/visible/test_weekend_billing.py"},
        branch_touch_union={
            "tests/visible/test_payments.py",
            "tests/visible/test_weekend_billing.py",
        },
    )


def test_validate_post_agent_allows_primary_scope_edits(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    (repo / "tests/visible/test_payments.py").write_text("def test_payments():\n    assert 1 == 1\n")

    result = validate_post_agent(
        project_dir=repo,
        pre_diff_files=set(),
        edit_scope=_scope(),
        pre_untracked_files=set(),
        pre_head=head,
    )

    assert result.ok is True
    assert result.edited_primary_files == {"tests/visible/test_payments.py"}
    assert result.edited_secondary_files == set()


def test_validate_post_agent_allows_secondary_scope_edits_and_reports_them(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    (repo / "tests/visible/test_weekend_billing.py").write_text(
        "def test_weekend():\n    assert 2 == 2\n"
    )

    result = validate_post_agent(
        project_dir=repo,
        pre_diff_files=set(),
        edit_scope=_scope(),
        pre_untracked_files=set(),
        pre_head=head,
    )

    assert result.ok is True
    assert result.edited_secondary_files == {"tests/visible/test_weekend_billing.py"}


def test_validate_post_agent_rejects_out_of_scope_edits(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)
    (repo / "tests/visible/test_unrelated.py").write_text("def test_unrelated():\n    assert False\n")

    result = validate_post_agent(
        project_dir=repo,
        pre_diff_files=set(),
        edit_scope=_scope(),
        pre_untracked_files=set(),
        pre_head=head,
    )

    assert result.ok is False
    assert result.error is not None
    assert "outside conflict edit scope" in result.error


def test_validate_post_agent_allows_empty_edit_set(tmp_path: Path) -> None:
    repo, head = _init_repo(tmp_path)

    result = validate_post_agent(
        project_dir=repo,
        pre_diff_files=set(),
        edit_scope=_scope(),
        pre_untracked_files=set(),
        pre_head=head,
    )

    assert result.ok is True
    assert result.edited_files == set()
