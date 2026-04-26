"""Tests for `paths.session_dir_for_record` and the artifact-mine consumer.

Background — fixes W1-IMPORTANT-3 / W12b-IMPORTANT-2:
    `artifact_mine_pass` previously resolved every live run's session dir
    under the project root. Queue-domain runs live in a worktree and write
    their session dir under `<worktree>/otto_logs/sessions/<run_id>/`, so
    the invariant scan reported false "missing session dir" failures for
    every queue and merge run.

These tests pin the helper's behaviour so the regression cannot return.
"""

from __future__ import annotations


import pytest

from otto import paths
from otto.runs import read_live_records, write_record
from otto.runs.schema import RunRecord


def _make_record(
    *,
    run_id: str,
    domain: str,
    cwd: str,
    project_dir: str,
    status: str = "running",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        domain=domain,
        run_type=domain if domain in {"queue", "merge", "atomic"} else "build",
        command="build",
        display_name=f"{domain}: {run_id}",
        status=status,
        project_dir=project_dir,
        cwd=cwd,
    )


class TestSessionDirForRecord:
    def test_uses_record_cwd(self, tmp_path):
        """When the record carries a `cwd`, the session dir is rooted there."""
        worktree = tmp_path / "project" / ".worktrees" / "task-1"
        worktree.mkdir(parents=True)
        record = _make_record(
            run_id="2026-04-26-011325-2615a8",
            domain="queue",
            cwd=str(worktree),
            project_dir=str(tmp_path / "project"),
        )

        sess = paths.session_dir_for_record(record)

        assert sess == worktree / "otto_logs" / "sessions" / record.run_id

    def test_falls_back_to_project_dir_when_cwd_absent(self, tmp_path):
        """A record with no `cwd` falls back to the project_dir argument."""
        record = _make_record(
            run_id="2026-04-26-011325-fallback",
            domain="atomic",
            cwd="",  # No recorded cwd.
            project_dir=str(tmp_path / "project"),
        )

        sess = paths.session_dir_for_record(record, project_dir=tmp_path / "project")

        assert sess == paths.session_dir(tmp_path / "project", record.run_id)

    def test_no_cwd_and_no_fallback_raises(self):
        """Bare records with no cwd and no project_dir fallback are an error."""
        record = _make_record(
            run_id="2026-04-26-011325-bare",
            domain="atomic",
            cwd="",
            project_dir="",
        )

        with pytest.raises(ValueError):
            paths.session_dir_for_record(record)

    def test_merge_domain_returns_none(self, tmp_path):
        """Merge-domain records have no `sessions/<id>/` dir by design."""
        record = _make_record(
            run_id="merge-1777169263-43042-04899947",
            domain="merge",
            cwd=str(tmp_path / "project"),
            project_dir=str(tmp_path / "project"),
        )

        assert paths.session_dir_for_record(record) is None

    def test_accepts_plain_dict(self, tmp_path):
        """The helper also accepts a raw record dict (post-JSON load)."""
        worktree = tmp_path / "project" / ".worktrees" / "task-2"
        record_dict = {
            "run_id": "2026-04-26-011325-dict",
            "domain": "queue",
            "cwd": str(worktree),
            "project_dir": str(tmp_path / "project"),
        }

        sess = paths.session_dir_for_record(record_dict)

        assert sess == worktree / "otto_logs" / "sessions" / "2026-04-26-011325-dict"

    def test_dict_merge_returns_none(self, tmp_path):
        record_dict = {
            "run_id": "merge-xyz",
            "domain": "merge",
            "cwd": str(tmp_path / "project"),
        }
        assert paths.session_dir_for_record(record_dict) is None

    def test_missing_run_id_raises(self):
        with pytest.raises(ValueError):
            paths.session_dir_for_record({"domain": "queue", "cwd": "/tmp"})


class TestQueueRecordEndToEnd:
    """Simulate a queue task setup: live record in project, session in worktree."""

    def test_session_dir_resolves_to_worktree(self, tmp_path):
        project_dir = tmp_path / "project"
        worktree_dir = project_dir / ".worktrees" / "build-foo-abc"
        worktree_dir.mkdir(parents=True)

        # Persist a queue-domain live record under project_dir/otto_logs/runs/live/.
        record = _make_record(
            run_id="2026-04-26-011325-2615a8",
            domain="queue",
            cwd=str(worktree_dir),
            project_dir=str(project_dir),
        )
        write_record(project_dir, record)

        # Pre-create the *real* session dir inside the worktree (where Otto writes it).
        real_sess = worktree_dir / "otto_logs" / "sessions" / record.run_id
        real_sess.mkdir(parents=True)

        # Read the record back via the registry and resolve via the helper.
        records = read_live_records(project_dir)
        assert len(records) == 1
        sess = paths.session_dir_for_record(records[0], project_dir=project_dir)

        assert sess == real_sess
        assert sess.exists()

    def test_artifact_mine_pass_does_not_flag_queue_run(self, tmp_path):
        """Regression: artifact_mine_pass must not report queue runs as missing."""
        from scripts.web_as_user import RunFailures, artifact_mine_pass

        project_dir = tmp_path / "project"
        worktree_dir = project_dir / ".worktrees" / "build-foo-abc"
        worktree_dir.mkdir(parents=True)

        record = _make_record(
            run_id="2026-04-26-011325-queue1",
            domain="queue",
            cwd=str(worktree_dir),
            project_dir=str(project_dir),
        )
        write_record(project_dir, record)
        (worktree_dir / "otto_logs" / "sessions" / record.run_id).mkdir(parents=True)

        failures = RunFailures()
        artifact_mine_pass(project_dir, failures)

        assert not failures.failures, (
            f"queue run with worktree session should not be flagged: {failures.failures}"
        )

    def test_artifact_mine_pass_still_flags_truly_missing_queue_session(self, tmp_path):
        """If the worktree session_dir really is missing, it must still be flagged."""
        from scripts.web_as_user import RunFailures, artifact_mine_pass

        project_dir = tmp_path / "project"
        worktree_dir = project_dir / ".worktrees" / "build-foo-xyz"
        worktree_dir.mkdir(parents=True)

        record = _make_record(
            run_id="2026-04-26-011325-orphan",
            domain="queue",
            cwd=str(worktree_dir),
            project_dir=str(project_dir),
        )
        write_record(project_dir, record)
        # Note: do NOT create the session dir.

        failures = RunFailures()
        artifact_mine_pass(project_dir, failures)

        assert failures.failures, "missing worktree session should still be flagged"
        assert any(record.run_id in msg for msg in failures.failures)
        # And the failure points at the worktree, not the project root.
        assert any(str(worktree_dir) in msg for msg in failures.failures)


class TestMergeRecordEndToEnd:
    def test_session_dir_for_merge_record_returns_none(self, tmp_path):
        """Merge records are synthetic and have no sessions/<id>/ dir."""
        project_dir = tmp_path / "project"
        record = _make_record(
            run_id="merge-1777169263-43042-04899947",
            domain="merge",
            cwd=str(project_dir),
            project_dir=str(project_dir),
        )
        write_record(project_dir, record)

        records = read_live_records(project_dir)
        assert len(records) == 1
        assert paths.session_dir_for_record(records[0], project_dir=project_dir) is None

    def test_artifact_mine_pass_skips_merge_record(self, tmp_path):
        """Regression: artifact_mine_pass must not flag merge records."""
        from scripts.web_as_user import RunFailures, artifact_mine_pass

        project_dir = tmp_path / "project"
        record = _make_record(
            run_id="merge-1777169263-43042-04899947",
            domain="merge",
            cwd=str(project_dir),
            project_dir=str(project_dir),
        )
        write_record(project_dir, record)
        # Deliberately do NOT create otto_logs/sessions/merge-.../ — merges
        # have no such dir.

        failures = RunFailures()
        artifact_mine_pass(project_dir, failures)

        assert not failures.failures, (
            f"merge record should not be flagged: {failures.failures}"
        )
