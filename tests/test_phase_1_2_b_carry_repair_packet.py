"""Phase 1.2-B: cross-run repair-packet carrying.

When `_resume_root_from_checkpoint` re-enters the integration phase of
a previously-failed run, copy the prior session's
`integration/repair/<unit>/repair_packet.json` (+ archived events.jsonl) into
the new session's mirror path AND rewrite the serialized `packet_dir`
field to the new location. This lets
`_run_preflight_payload_repair_session`'s load-if-exists fire across
runs → the repair agent's prior Claude SDK session is resumed
(option.resume=agent_session_id) instead of starting fresh.

Schema confirmed in research-phase-1.2-b.md: `packet_dir` needs
path-rewriting; `repair_unit.worktree` is project_dir-scoped (stable),
agent_session_id carries, and active budget bookkeeping starts fresh.
"""
import json
import tempfile
import time
from pathlib import Path

from otto.v5_runner import _build_repair_packet, _carry_and_reset_prior_repair_packets


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
    n = _carry_and_reset_prior_repair_packets(pdir, new_session_dir)
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
    # Phase 1.2-B v2: attempt_history + current_state are CLEARED on
    # carry so the new run starts with fresh bookkeeping. Prior events
    # are archived separately and must not become the active budget
    # replay file.
    assert loaded["attempt_history"] == [], "attempt_history must reset"
    assert loaded["current_state"] == {}, "current_state must reset"
    # Static context fields still carry verbatim (the agent doesn't read
    # attempt_history — the runner uses it; static context is the
    # agent-visible setup that doesn't change across runs).
    assert loaded["repair_unit"]["id"] == "root-integration_smoke-pre_agent"


def test_copies_events_jsonl_sibling():
    pdir = Path(tempfile.mkdtemp())
    _make_prior_session(
        pdir, session_name="s-old", unit_name="root-integration_smoke-pre_agent",
        agent_session_id="X", write_events=True,
    )
    new_session_dir = pdir / "otto_logs" / "sessions" / "s-new"
    _carry_and_reset_prior_repair_packets(pdir, new_session_dir)
    active_events = (
        new_session_dir / "integration" / "repair"
        / "root-integration_smoke-pre_agent" / "repair_packet.events.jsonl"
    )
    archived_events = (
        new_session_dir / "integration" / "repair"
        / "root-integration_smoke-pre_agent" / "prior_repair_packet.events.jsonl"
    )
    assert not active_events.exists(), "active events must start empty for fresh budget replay"
    assert archived_events.is_file(), "prior events.jsonl sibling must be archived"
    assert "turn_started" in archived_events.read_text(encoding="utf-8")


def test_build_repair_packet_preserves_existing_agent_session_id():
    pdir = Path(tempfile.mkdtemp())
    session_dir = pdir / "otto_logs" / "sessions" / "s-new"
    packet_dir = session_dir / "repair" / "unit"
    packet_dir.mkdir(parents=True, exist_ok=True)
    (packet_dir / "repair_packet.json").write_text(
        json.dumps(
            {
                "repair_unit": {"id": "unit", "worktree": str(pdir)},
                "acceptance_oracle": {},
                "latest_oracle_result": {},
                "product_contract": {},
                "integration_context": {},
                "attempt_history": [],
                "current_state": {},
                "budget": {"wall_clock_s": 1200.0},
                "packet_dir": str(packet_dir),
                "agent_session_id": "SESS-PRIOR",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    packet = _build_repair_packet(
        session_dir=session_dir,
        repair_slug="unit",
        worktree_path=pdir,
        task_id="task",
        phase="integration",
        repair_phase="pre_agent",
        verify_scope="subtree",
        config={},
        budget_prefix="pre_agent_repair",
        default_agent_turns=1,
        default_oracle_invocations=1,
        latest_oracle_result={},
        product_contract={},
        integration_context={},
        success_criteria={},
        attempt_history_entry={"type": "preflight"},
    )

    assert packet.agent_session_id == "SESS-PRIOR"


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
    _carry_and_reset_prior_repair_packets(pdir, new_session_dir)
    loaded = json.loads(
        (new_session_dir / "integration" / "repair" / "u" / "repair_packet.json")
        .read_text(encoding="utf-8")
    )
    assert loaded["agent_session_id"] == "NEW", "most-recent prior wins per unit"


def test_no_prior_packets_is_noop():
    pdir = Path(tempfile.mkdtemp())
    new_session_dir = pdir / "otto_logs" / "sessions" / "s-new"
    n = _carry_and_reset_prior_repair_packets(pdir, new_session_dir)
    assert n == 0
    # No repair dir should be created.
    assert not (new_session_dir / "integration").exists()
