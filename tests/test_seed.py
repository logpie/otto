"""Tests for otto/seed.py — Seed stage (A1.5-seed).

Coverage:
- Per-fixture-kind happy path: user, channel, follow, data
- Per-fixture-kind failure path: missing script + script-exits-nonzero
- Idempotency: applying the same fixture twice produces the same state
- Bundling: multiple fixtures applied in declared order
- Invalid kind: SeedResult(succeeded=False) with kind in error
- Empty audit_fixtures: success no-op
- Session log written when session_dir provided

The throwaway seed scripts live in tmp project dirs and persist their
state to a JSON file so tests can verify idempotent dedup.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from otto.seed import SUPPORTED_KINDS, SeedResult, seed_fixtures
from otto.spec_compile import AuditFixture, Spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_OK_SCRIPT_TEMPLATE = """\
#!/usr/bin/env python3
\"\"\"Throwaway idempotent seed script for tests.

Reads JSON payload from stdin, dedups against a state file, exits 0.
Honours OTTO_TEST_SEED_FAIL=1 to simulate failure.
\"\"\"
import json
import os
import sys
from pathlib import Path

STATE_FILE = Path(os.environ['OTTO_TEST_STATE_FILE'])
KIND = '{kind}'
KEY_FIELD = '{key_field}'

if os.environ.get('OTTO_TEST_SEED_FAIL') == '1':
    print('simulated failure', file=sys.stderr)
    sys.exit(2)

payload = json.loads(sys.stdin.read())
state = {{}}
if STATE_FILE.exists():
    state = json.loads(STATE_FILE.read_text())
state.setdefault(KIND, [])

key = payload.get(KEY_FIELD)
existing_keys = [e.get(KEY_FIELD) for e in state[KIND]]
if key in existing_keys:
    print(f'{{KIND}}:{{key}} already present (idempotent skip)')
else:
    state[KIND].append(payload)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f'{{KIND}}:{{key}} created')
"""


def _make_seed_script(project_dir: Path, kind: str, key_field: str) -> Path:
    """Drop a throwaway idempotent seed script into scripts/otto/seed_<kind>.py."""
    scripts_dir = project_dir / "scripts" / "otto"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script = scripts_dir / f"seed_{kind}.py"
    script.write_text(_OK_SCRIPT_TEMPLATE.format(kind=kind, key_field=key_field))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tmp project dir with a state-file env var pointing at a fresh JSON."""
    state_file = tmp_path / "seed_state.json"
    monkeypatch.setenv("OTTO_TEST_STATE_FILE", str(state_file))
    monkeypatch.delenv("OTTO_TEST_SEED_FAIL", raising=False)
    return tmp_path


def _spec_with(*fixtures: AuditFixture) -> Spec:
    return Spec(audit_fixtures=list(fixtures))


def _read_state(project_dir: Path) -> dict:
    state_file = Path(os.environ["OTTO_TEST_STATE_FILE"])
    if not state_file.exists():
        return {}
    return json.loads(state_file.read_text())


# ---------------------------------------------------------------------------
# Happy paths — one per kind
# ---------------------------------------------------------------------------


def test_seed_user_happy(project: Path) -> None:
    _make_seed_script(project, "user", "username")
    spec = _spec_with(AuditFixture(kind="user", payload={"username": "alice", "email": "a@x"}))

    result = seed_fixtures(spec, project)

    assert result.succeeded, result.errors
    assert result.detail == "applied 1 fixture(s)"
    assert _read_state(project)["user"] == [{"username": "alice", "email": "a@x"}]


def test_seed_channel_happy(project: Path) -> None:
    _make_seed_script(project, "channel", "name")
    spec = _spec_with(AuditFixture(kind="channel", payload={"name": "general", "members": ["alice"]}))

    result = seed_fixtures(spec, project)

    assert result.succeeded
    assert _read_state(project)["channel"][0]["name"] == "general"


def test_seed_follow_happy(project: Path) -> None:
    _make_seed_script(project, "follow", "follower")
    spec = _spec_with(AuditFixture(kind="follow", payload={"follower": "alice", "followee": "bob"}))

    result = seed_fixtures(spec, project)

    assert result.succeeded
    assert _read_state(project)["follow"][0]["followee"] == "bob"


def test_seed_data_happy(project: Path) -> None:
    _make_seed_script(project, "data", "id")
    spec = _spec_with(AuditFixture(kind="data", payload={"id": "p1", "title": "Widget"}))

    result = seed_fixtures(spec, project)

    assert result.succeeded
    assert _read_state(project)["data"][0]["title"] == "Widget"


# ---------------------------------------------------------------------------
# Failure paths — one per kind, plus the cross-cutting failure modes
# ---------------------------------------------------------------------------


def test_seed_user_missing_script(project: Path) -> None:
    # Note: script intentionally not created.
    spec = _spec_with(AuditFixture(kind="user", payload={"username": "alice"}))

    result = seed_fixtures(spec, project)

    assert not result.succeeded
    assert "seed_user.py" in result.errors[0]
    assert result.fixtures_applied[0]["ok"] is False


def test_seed_channel_script_exits_nonzero(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_seed_script(project, "channel", "name")
    monkeypatch.setenv("OTTO_TEST_SEED_FAIL", "1")
    spec = _spec_with(AuditFixture(kind="channel", payload={"name": "x"}))

    result = seed_fixtures(spec, project)

    assert not result.succeeded
    assert "exit=2" in result.errors[0]


def test_seed_follow_missing_script(project: Path) -> None:
    spec = _spec_with(AuditFixture(kind="follow", payload={"follower": "a", "followee": "b"}))

    result = seed_fixtures(spec, project)

    assert not result.succeeded
    assert "seed_follow.py" in result.errors[0]


def test_seed_data_script_exits_nonzero(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_seed_script(project, "data", "id")
    monkeypatch.setenv("OTTO_TEST_SEED_FAIL", "1")
    spec = _spec_with(AuditFixture(kind="data", payload={"id": "p1"}))

    result = seed_fixtures(spec, project)

    assert not result.succeeded
    assert "exit=2" in result.errors[0]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_repeat_apply(project: Path) -> None:
    """Applying the same fixture twice must not duplicate state."""
    _make_seed_script(project, "user", "username")
    spec = _spec_with(AuditFixture(kind="user", payload={"username": "alice"}))

    first = seed_fixtures(spec, project)
    assert first.succeeded
    state_after_first = _read_state(project)

    second = seed_fixtures(spec, project)
    assert second.succeeded
    state_after_second = _read_state(project)

    assert state_after_first == state_after_second
    assert len(state_after_second["user"]) == 1


# ---------------------------------------------------------------------------
# Bundling: multiple fixtures, declared order preserved
# ---------------------------------------------------------------------------


def test_bundling_applies_in_declared_order(project: Path) -> None:
    _make_seed_script(project, "user", "username")
    _make_seed_script(project, "channel", "name")
    _make_seed_script(project, "follow", "follower")
    _make_seed_script(project, "data", "id")

    spec = _spec_with(
        AuditFixture(kind="user", payload={"username": "alice"}),
        AuditFixture(kind="user", payload={"username": "bob"}),
        AuditFixture(kind="channel", payload={"name": "general"}),
        AuditFixture(kind="follow", payload={"follower": "alice", "followee": "bob"}),
        AuditFixture(kind="data", payload={"id": "p1"}),
    )

    result = seed_fixtures(spec, project)

    assert result.succeeded
    kinds_in_order = [r["kind"] for r in result.fixtures_applied]
    assert kinds_in_order == ["user", "user", "channel", "follow", "data"]
    state = _read_state(project)
    assert [u["username"] for u in state["user"]] == ["alice", "bob"]


def test_bundling_fail_fast_on_first_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing fixture aborts the run; later fixtures are NOT attempted."""
    _make_seed_script(project, "user", "username")
    _make_seed_script(project, "channel", "name")
    monkeypatch.setenv("OTTO_TEST_SEED_FAIL", "1")

    spec = _spec_with(
        AuditFixture(kind="user", payload={"username": "alice"}),
        AuditFixture(kind="channel", payload={"name": "general"}),
    )

    result = seed_fixtures(spec, project)

    assert not result.succeeded
    # Only the first fixture should appear in fixtures_applied.
    assert len(result.fixtures_applied) == 1
    assert result.fixtures_applied[0]["kind"] == "user"


# ---------------------------------------------------------------------------
# Invalid kind, empty list, log writing
# ---------------------------------------------------------------------------


def test_invalid_kind_returns_failure_with_kind_in_error(project: Path) -> None:
    spec = _spec_with(AuditFixture(kind="bogus", payload={}))

    result = seed_fixtures(spec, project)

    assert not result.succeeded
    assert "bogus" in result.errors[0]
    assert "unsupported kind" in result.errors[0]
    # Supported kinds should be referenced for the operator.
    for kind in SUPPORTED_KINDS:
        assert kind in result.errors[0]


def test_empty_audit_fixtures_is_noop_success(project: Path) -> None:
    spec = _spec_with()

    result = seed_fixtures(spec, project)

    assert result.succeeded
    assert result.fixtures_applied == []
    assert "nothing to seed" in result.detail


def test_session_log_written(project: Path, tmp_path: Path) -> None:
    _make_seed_script(project, "user", "username")
    spec = _spec_with(AuditFixture(kind="user", payload={"username": "alice"}))
    session_dir = tmp_path / "session"

    result = seed_fixtures(spec, project, session_dir=session_dir)

    log_path = session_dir / "seed" / "seed.log"
    assert result.succeeded
    assert log_path.exists()
    body = log_path.read_text()
    assert "kind=user" in body
    assert "succeeded=True" in body


def test_seed_result_dataclass_shape() -> None:
    """SeedResult shape is part of the public contract — pin it."""
    r = SeedResult(succeeded=True, detail="ok")
    assert r.succeeded is True
    assert r.detail == "ok"
    assert r.fixtures_applied == []
    assert r.errors == []


# ---------------------------------------------------------------------------
# Stage-timeline lifecycle events (RUA W6-C: seed stage stuck on "pending")
# ---------------------------------------------------------------------------


def _read_journal(session_dir: Path) -> list[dict]:
    """Read every event from <session_dir>/spec-state.jsonl."""
    journal = session_dir / "spec-state.jsonl"
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]


def test_seed_emits_started_and_finished_on_success(
    project: Path, tmp_path: Path
) -> None:
    """Successful seed run writes seed.started + seed.finished (succeeded=True)."""
    _make_seed_script(project, "user", "username")
    spec = _spec_with(AuditFixture(kind="user", payload={"username": "alice"}))
    session_dir = tmp_path / "session"

    result = seed_fixtures(spec, project, session_dir=session_dir)

    assert result.succeeded
    events = _read_journal(session_dir)
    kinds = [e["kind"] for e in events]
    assert "seed.started" in kinds
    assert "seed.finished" in kinds
    # Order must be started → finished
    assert kinds.index("seed.started") < kinds.index("seed.finished")
    # Finished event carries the per-stage success flag
    finished = next(e for e in events if e["kind"] == "seed.finished")
    assert finished.get("extra", {}).get("succeeded") is True


def test_seed_emits_started_and_finished_on_failure(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed seed run still emits both lifecycle events; finished.succeeded=False."""
    _make_seed_script(project, "user", "username")
    monkeypatch.setenv("OTTO_TEST_SEED_FAIL", "1")
    spec = _spec_with(AuditFixture(kind="user", payload={"username": "alice"}))
    session_dir = tmp_path / "session"

    result = seed_fixtures(spec, project, session_dir=session_dir)

    assert not result.succeeded
    events = _read_journal(session_dir)
    kinds = [e["kind"] for e in events]
    assert "seed.started" in kinds
    assert "seed.finished" in kinds
    finished = next(e for e in events if e["kind"] == "seed.finished")
    assert finished.get("extra", {}).get("succeeded") is False


def test_seed_emits_lifecycle_for_empty_fixtures(
    project: Path, tmp_path: Path
) -> None:
    """Even no-op runs flip the seed stage from pending — empty fixtures are
    a successful seed by contract, not a stuck stage."""
    spec = _spec_with()
    session_dir = tmp_path / "session"

    result = seed_fixtures(spec, project, session_dir=session_dir)

    assert result.succeeded
    events = _read_journal(session_dir)
    kinds = [e["kind"] for e in events]
    assert kinds == ["seed.started", "seed.finished"]


def test_seed_lifecycle_noop_without_session_dir(project: Path) -> None:
    """When session_dir is None, no journal write is attempted (no crash)."""
    _make_seed_script(project, "user", "username")
    spec = _spec_with(AuditFixture(kind="user", payload={"username": "alice"}))

    # Must not raise even though there is no journal target.
    result = seed_fixtures(spec, project, session_dir=None)
    assert result.succeeded
