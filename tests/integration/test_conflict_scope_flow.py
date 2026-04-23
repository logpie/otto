from __future__ import annotations

from pathlib import Path

import pytest

from otto.merge.orchestrator import MergeOptions, run_merge

from .conftest import commit_all, git, assistant_text, result_message


def _seed_three_branch_conflict(repo: Path) -> None:
    billing = repo / "app/services/billing.py"
    billing.parent.mkdir(parents=True, exist_ok=True)
    billing.write_text(
        "def charge():\n"
        "    return 'billing'\n"
    )
    test_payments = repo / "tests/visible/test_payments.py"
    test_payments.parent.mkdir(parents=True, exist_ok=True)
    test_payments.write_text(
        "from app.services.billing import charge\n\n"
        "def test_charge():\n"
        "    assert charge() == 'billing'\n"
    )
    (repo / "tests/visible/test_unrelated.py").write_text(
        "def test_unrelated():\n"
        "    assert True\n"
    )
    commit_all(repo, "base")

    git(repo, "checkout", "-b", "feat-a")
    billing.rename(repo / "app/services/payments.py")
    test_payments.write_text(
        "from app.services.payments import charge\n\n"
        "def test_charge():\n"
        "    assert charge() == 'payments'\n"
    )
    commit_all(repo, "rename billing to payments")
    git(repo, "checkout", "main")

    git(repo, "checkout", "-b", "feat-b")
    (repo / "tests/visible/test_weekend_billing.py").write_text(
        "from app.services.billing import charge\n\n"
        "def test_weekend_charge():\n"
        "    assert charge() == 'billing'\n"
    )
    commit_all(repo, "add weekend billing test from stale context")
    git(repo, "checkout", "main")

    git(repo, "checkout", "-b", "feat-c")
    test_payments.write_text(
        "from app.services.billing import charge\n\n"
        "def test_charge():\n"
        "    assert charge() == 'branch-c'\n"
    )
    commit_all(repo, "change payments test from stale context")
    git(repo, "checkout", "main")


@pytest.mark.asyncio
async def test_conflict_scope_flow_allows_in_scope_secondary_edit(
    tmp_otto_repo: Path,
    mock_sdk,
) -> None:
    _seed_three_branch_conflict(tmp_otto_repo)

    async def resolve_conflict(*, prompt: str, options):
        project_dir = Path(options.cwd)
        assert "tests/visible/test_weekend_billing.py" in prompt
        assert "tests/visible/test_unrelated.py" not in prompt
        (project_dir / "tests/visible/test_payments.py").write_text(
            "from app.services.payments import charge\n\n"
            "def test_charge():\n"
            "    assert charge() == 'payments'\n"
        )
        (project_dir / "tests/visible/test_weekend_billing.py").write_text(
            "from app.services.payments import charge\n\n"
            "def test_weekend_charge():\n"
            "    assert charge() == 'payments'\n"
        )
        yield assistant_text("Resolved the conflicted test and updated the adjacent stale test.")
        yield result_message(total_cost_usd=0.21)

    mock_sdk.install(resolve_conflict)

    result = await run_merge(
        project_dir=tmp_otto_repo,
        config={
            "provider": "claude",
            "default_branch": "main",
            "queue": {"bookkeeping_files": []},
        },
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b", "feat-c"],
    )

    assert result.success is True, result.note
    assert "secondary edits: tests/visible/test_weekend_billing.py" in result.note
    assert "app.services.payments" in (tmp_otto_repo / "tests/visible/test_payments.py").read_text()
    assert "app.services.payments" in (
        tmp_otto_repo / "tests/visible/test_weekend_billing.py"
    ).read_text()


@pytest.mark.asyncio
async def test_conflict_scope_flow_rejects_out_of_scope_edit(
    tmp_otto_repo: Path,
    mock_sdk,
) -> None:
    _seed_three_branch_conflict(tmp_otto_repo)

    async def resolve_conflict(*, prompt: str, options):
        project_dir = Path(options.cwd)
        assert "tests/visible/test_weekend_billing.py" in prompt
        (project_dir / "tests/visible/test_payments.py").write_text(
            "from app.services.payments import charge\n\n"
            "def test_charge():\n"
            "    assert charge() == 'payments'\n"
        )
        (project_dir / "tests/visible/test_unrelated.py").write_text(
            "def test_unrelated():\n"
            "    assert False\n"
        )
        yield assistant_text("Edited a file outside the approved scope.")
        yield result_message(total_cost_usd=0.21)

    mock_sdk.install(resolve_conflict)

    result = await run_merge(
        project_dir=tmp_otto_repo,
        config={
            "provider": "claude",
            "default_branch": "main",
            "queue": {"bookkeeping_files": []},
        },
        options=MergeOptions(target="main", no_certify=True, allow_any_branch=True),
        explicit_ids_or_branches=["feat-a", "feat-b", "feat-c"],
    )

    assert result.success is False
    assert "outside conflict edit scope" in result.note
