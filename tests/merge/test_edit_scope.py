from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from otto.merge.edit_scope import EditScopeError, build_edit_scope


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    return tmp_path


def _commit_files(repo: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        path = repo / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixtures"], cwd=repo, check=True)


def test_build_edit_scope_includes_same_package_branch_touched_neighbors(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_files(repo, {
        "app/services/payments.py": "def total():\n    return 1\n",
        "app/services/helpers.py": "def helper():\n    return 2\n",
        "app/models/user.py": "class User:\n    pass\n",
    })

    scope = build_edit_scope(
        project_dir=repo,
        conflict_files={"app/services/payments.py"},
        branch_touch_union={"app/services/helpers.py", "app/models/user.py"},
    )

    assert scope.primary_files == {"app/services/payments.py"}
    assert scope.secondary_files == {"app/services/helpers.py"}


def test_build_edit_scope_includes_direct_import_neighbors_but_not_depth_two(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_files(repo, {
        "app/services/payments.py": (
            "from app.support import helpers\n"
            "def total():\n"
            "    return helpers.fee()\n"
        ),
        "app/support/helpers.py": (
            "from app.finance import tax\n"
            "def fee():\n"
            "    return tax.rate()\n"
        ),
        "app/finance/tax.py": "def rate():\n    return 3\n",
    })

    scope = build_edit_scope(
        project_dir=repo,
        conflict_files={"app/services/payments.py"},
        branch_touch_union={"app/support/helpers.py", "app/finance/tax.py"},
    )

    assert "app/support/helpers.py" in scope.secondary_files
    assert "app/finance/tax.py" not in scope.secondary_files


def test_build_edit_scope_includes_same_test_package_neighbors(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_files(repo, {
        "tests/visible/test_payments.py": "def test_payments():\n    assert True\n",
        "tests/visible/test_weekend_billing.py": "def test_weekend():\n    assert True\n",
        "tests/other/test_other.py": "def test_other():\n    assert True\n",
    })

    scope = build_edit_scope(
        project_dir=repo,
        conflict_files={"tests/visible/test_payments.py"},
        branch_touch_union={"tests/visible/test_weekend_billing.py", "tests/other/test_other.py"},
    )

    assert scope.secondary_files == {"tests/visible/test_weekend_billing.py"}


def test_build_edit_scope_respects_branch_touch_union_intersection(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_files(repo, {
        "tests/visible/test_payments.py": (
            "from app.services.payments import total\n"
            "def test_payments():\n"
            "    assert total() == 1\n"
        ),
        "tests/visible/test_weekend_billing.py": (
            "from app.services.payments import total\n"
            "def test_weekend():\n"
            "    assert total() == 1\n"
        ),
    })

    scope = build_edit_scope(
        project_dir=repo,
        conflict_files={"tests/visible/test_payments.py"},
        branch_touch_union=set(),
    )

    assert scope.secondary_files == set()


def test_build_edit_scope_fails_closed_on_secondary_scope_overflow(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    files = {"tests/visible/test_payments.py": "def test_payments():\n    assert True\n"}
    for index in range(13):
        files[f"tests/visible/test_neighbor_{index}.py"] = f"def test_{index}():\n    assert True\n"
    _commit_files(repo, files)

    with pytest.raises(EditScopeError, match="secondary edit scope too broad"):
        build_edit_scope(
            project_dir=repo,
            conflict_files={"tests/visible/test_payments.py"},
            branch_touch_union={
                f"tests/visible/test_neighbor_{index}.py"
                for index in range(13)
            },
        )
