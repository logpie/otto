"""Phase 1.2-B: cross-run repair-packet carrying.

When `_resume_root_from_checkpoint` re-enters the integration phase of
a previously-failed run, copy the prior session's
`integration/repair/<unit>/repair_packet.json` (+ events.jsonl) into
the new session's mirror path AND rewrite the serialized `packet_dir`
field to the new location. This lets
`_run_preflight_payload_repair_session`'s load-if-exists fire across
runs → the repair agent's prior Claude SDK session is resumed
(option.resume=agent_session_id) instead of starting fresh.

Schema confirmed in research-phase-1.2-b.md: only `packet_dir` needs
path-rewriting; `repair_unit.worktree` is project_dir-scoped (stable),
`current_state`/`attempt_history`/`agent_session_id` carry verbatim.
"""
import json
import tempfile
import time
from pathlib import Path

from otto.v5_runner import _carry_prior_repair_packets


def _make_prior_session(
    project_dir: Path,
    *,
    session_name: str,
    unit_name: str,
    agent_session_id: str,
    write_events: bool = False,
    sleep_for_mtime: bool = False,
) -> Path:
    """Build a fixture prior-session dir under project_dir with one
    repair packet at <session>/integration/repair/<unit>/. Returns the
    session dir path."""
    session_dir = project_dir / "otto_logs" / "sessions" / session_name
    packet_dir = session_dir / "integration" / "repair" / unit_name
    packet_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "repair_unit": {"id": unit_name, "worktree": str(project_dir)},
        "acceptance_oracle": {},
        "latest_oracle_result": {},
        "product_contract": {},
        "integration_context": {},
        "attempt_history": [{"turn": 1, "summary": "prior attempt"}],
        "current_state": {"branch": "main", "head": "deadbeef", "pre_repair_head": "cafef00d"},
        "budget": {"wall_clock_s": 1200.0},
        "packet_dir": str(packet_dir),  # the OLD path — must be rewritten on carry
        "agent_session_id": agent_session_id,
    }
    (packet_dir / "repair_packet.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    if write_events:
        (packet_dir / "repair_packet.events.jsonl").write_text(
            '{"ts":"2026-05-19T00:00:00Z","event":"turn_started","turn":1}\n',
            encoding="utf-8",
        )
    if sleep_for_mtime:
        # Ensure mtime ordering is observable.
        time.sleep(0.05)
    return session_dir


def test_carries_session_id_and_rewrites_packet_dir():
    pdir = Path(tempfile.mkdtemp())
    _make_prior_session(
        pdir, session_name="s-old", unit_name="root-integration_smoke-pre_agent",
        agent_session_id="SESS-XYZ",
    )
    new_session_dir = pdir / "otto_logs" / "sessions" / "s-new"
    n = _carry_prior_repair_packets(pdir, new_session_dir)
    assert n == 1
    new_packet = (
        new_session_dir / "integration" / "repair"
        / "root-integration_smoke-pre_agent" / "repair_packet.json"
    )
    assert new_packet.is_file(), "must mirror to new session"
    loaded = json.loads(new_packet.read_text(encoding="utf-8"))
    assert loaded["agent_session_id"] == "SESS-XYZ", "session_id must carry"
    assert loaded["packet_dir"] == str(new_packet.parent), (
        "packet_dir MUST be rewritten to new location (else persist() goes "
        "to the old path — schema bug fix is the whole point)"
    )
    # Non-path fields carry verbatim.
    assert loaded["attempt_history"][0]["summary"] == "prior attempt"
    assert loaded["current_state"]["pre_repair_head"] == "cafef00d"


def test_copies_events_jsonl_sibling():
    pdir = Path(tempfile.mkdtemp())
    _make_prior_session(
        pdir, session_name="s-old", unit_name="root-integration_smoke-pre_agent",
        agent_session_id="X", write_events=True,
    )
    new_session_dir = pdir / "otto_logs" / "sessions" / "s-new"
    _carry_prior_repair_packets(pdir, new_session_dir)
    new_events = (
        new_session_dir / "integration" / "repair"
        / "root-integration_smoke-pre_agent" / "repair_packet.events.jsonl"
    )
    assert new_events.is_file(), "events.jsonl sibling must be copied"
    assert "turn_started" in new_events.read_text(encoding="utf-8")


def test_most_recent_per_unit_wins():
    pdir = Path(tempfile.mkdtemp())
    _make_prior_session(
        pdir, session_name="s-older", unit_name="u",
        agent_session_id="OLD", sleep_for_mtime=True,
    )
    _make_prior_session(
        pdir, session_name="s-newer", unit_name="u",
        agent_session_id="NEW",
    )
    new_session_dir = pdir / "otto_logs" / "sessions" / "s-new"
    _carry_prior_repair_packets(pdir, new_session_dir)
    loaded = json.loads(
        (new_session_dir / "integration" / "repair" / "u" / "repair_packet.json")
        .read_text(encoding="utf-8")
    )
    assert loaded["agent_session_id"] == "NEW", "most-recent prior wins per unit"


def test_no_prior_packets_is_noop():
    pdir = Path(tempfile.mkdtemp())
    new_session_dir = pdir / "otto_logs" / "sessions" / "s-new"
    n = _carry_prior_repair_packets(pdir, new_session_dir)
    assert n == 0
    # No repair dir should be created.
    assert not (new_session_dir / "integration").exists()
