"""v2.2 amendment API tests.

Covers tiered mutability (BEDROCK / LOCKED / SLICE-LOCAL), the
constraints on tier-3 amendments (scope, append-only checks, trigger
linkage, hash chain), and the audit-time chain verifier.

See docs/intent-to-product-v2.md "Safe mutability" for the design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from otto.spec_amend import (
    AMENDMENT_REQUEST_PATH,
    AMENDMENT_RESPONSE_PATH,
    AmendmentRejection,
    consume_amendment_request,
    request_amendment,
    verify_amendment_chain,
)
from otto.spec_compile import (
    PytestCheck,
    Group,
    Spec,
    SpecValidationError,
    compute_intent_hash,
    lock_intent,
    persist_spec,
)
from otto.spec_state import emit


def _seed_spec() -> Spec:
    spec = Spec(
        intent="build a microfeed-style social app",
        slices=[
            Group(id="shell", title="App shell", tasks=["t"]),
            Group(id="auth", title="User auth", deps=["shell"], tasks=["t"]),
            Group(id="posts", title="Posts feed", deps=["shell"], tasks=["t"]),
        ],
    )
    return lock_intent(spec)


# ---------------------------------------------------------------------------
# Tier-1 (bedrock): intent immutable
# ---------------------------------------------------------------------------


def test_persist_spec_rejects_intent_change_without_override(tmp_path: Path) -> None:
    """v2.2 BEDROCK: changing intent without user override is blocked."""
    spec = _seed_spec()
    target = tmp_path / "spec.json"
    persist_spec(spec, target, allow_initial=True)

    tampered = lock_intent(Spec(**{**spec.__dict__, "intent": "DIFFERENT INTENT"}))
    with pytest.raises(SpecValidationError, match="tier-1 violation"):
        persist_spec(tampered, target)


def test_persist_spec_allows_intent_change_with_user_override(tmp_path: Path) -> None:
    spec = _seed_spec()
    target = tmp_path / "spec.json"
    persist_spec(spec, target, allow_initial=True)

    new_spec = lock_intent(Spec(**{**spec.__dict__, "intent": "user-overridden new intent"}))
    persist_spec(new_spec, target, user_override_intent=True, allow_initial=True)
    # No raise — user override is the deliberate escape hatch.


def test_request_amendment_rejects_intent_field() -> None:
    spec = _seed_spec()
    result = request_amendment(
        spec,
        actor="shell",
        slice_id="shell",
        changes={"intent": "agent attempted to amend intent"},
        reason="trying",
        trigger_event_id="ev-000001",
    )
    assert not result.accepted
    assert isinstance(result.rejection, AmendmentRejection)
    assert result.rejection.code == "tier_1_violation"


# ---------------------------------------------------------------------------
# Tier-2 (locked): user-only fields
# ---------------------------------------------------------------------------


def test_request_amendment_rejects_tier_2_fields() -> None:
    """project_kind, done_means, etc. cannot be amended via the API."""
    spec = _seed_spec()
    result = request_amendment(
        spec,
        actor="shell",
        slice_id="shell",
        changes={"project_kind": "cli"},
        reason="trying",
        trigger_event_id="ev-000001",
    )
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "tier_2_violation"


def test_request_amendment_rejects_slice_id_change() -> None:
    """slice.id is locked — rename = drop + re-add (audit-visible)."""
    spec = _seed_spec()
    result = request_amendment(
        spec,
        actor="shell",
        slice_id="shell",
        changes={"id": "renamed-shell"},
        reason="trying",
        trigger_event_id="ev-000001",
    )
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "tier_2_violation"


# ---------------------------------------------------------------------------
# Tier-3 (slice-local): scope rule
# ---------------------------------------------------------------------------


def test_agent_can_amend_own_slice_deps(tmp_path: Path) -> None:
    """Happy path: an agent adds a dep with a real trigger event."""
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    event = emit(session_dir, "scope.warning", slice_id="posts", detail="touched social/")

    result = request_amendment(
        spec,
        actor="posts",
        slice_id="posts",
        changes={"deps": ["shell", "auth"]},
        reason="needs auth helper for timeline rendering",
        trigger_event_id=event.event_id,
        session_dir=session_dir,
    )
    assert result.accepted
    assert result.spec is not None
    posts_slice = next(s for s in result.spec.slices if s.id == "posts")
    assert posts_slice.deps == ["shell", "auth"]
    # Amendment recorded
    assert len(result.spec.amendments) == 1
    amendment = result.spec.amendments[0]
    assert amendment.tier == 3
    assert amendment.actor == "posts"
    assert amendment.trigger_event_id == event.event_id


def test_agent_cannot_amend_another_slice(tmp_path: Path) -> None:
    """Scope: actor 'posts' cannot amend slice 'auth'."""
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    event = emit(session_dir, "scope.warning", slice_id="posts", detail="x")

    result = request_amendment(
        spec,
        actor="posts",
        slice_id="auth",
        changes={"deps": ["shell"]},
        reason="trying to expand peer",
        trigger_event_id=event.event_id,
        session_dir=session_dir,
    )
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "scope_violation"


def test_user_can_amend_any_slice(tmp_path: Path) -> None:
    """User actor bypasses the scope rule (it's the spec-review gate)."""
    spec = _seed_spec()
    result = request_amendment(
        spec,
        actor="user",
        slice_id="auth",
        changes={"tasks": ["new task from review"]},
        reason="adding task during review",
    )
    assert result.accepted


# ---------------------------------------------------------------------------
# Tier-3: append-only checks
# ---------------------------------------------------------------------------


def test_checks_can_be_appended(tmp_path: Path) -> None:
    spec = _seed_spec()
    spec.slices[0].checks = [PytestCheck(selector="tests/test_a.py::test_x")]

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    event = emit(session_dir, "scope.warning", slice_id="shell", detail="x")

    result = request_amendment(
        spec,
        actor="shell",
        slice_id="shell",
        changes={"checks": [
            PytestCheck(selector="tests/test_a.py::test_x"),
            PytestCheck(selector="tests/test_a.py::test_y"),
        ]},
        reason="adding a coverage test",
        trigger_event_id=event.event_id,
        session_dir=session_dir,
    )
    assert result.accepted
    assert result.spec is not None
    assert len(result.spec.slices[0].checks) == 2


def test_checks_cannot_be_removed(tmp_path: Path) -> None:
    spec = _seed_spec()
    spec.slices[0].checks = [
        PytestCheck(selector="tests/test_a.py::test_x"),
        PytestCheck(selector="tests/test_a.py::test_y"),
    ]

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    event = emit(session_dir, "scope.warning", slice_id="shell", detail="x")

    result = request_amendment(
        spec,
        actor="shell",
        slice_id="shell",
        changes={"checks": [PytestCheck(selector="tests/test_a.py::test_x")]},
        reason="trying to drop a check",
        trigger_event_id=event.event_id,
        session_dir=session_dir,
    )
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "checks_weakened"


# ---------------------------------------------------------------------------
# Tier-3: trigger event linkage required for agents
# ---------------------------------------------------------------------------


def test_agent_amendment_without_trigger_accepted() -> None:
    """v2.2 generalization (post-bench): trigger_event_id is no longer
    required. Cumulative chain review at audit time is the real
    defense; requiring an id-on-every-amendment was ceremony agents
    cargo-culted without it actually linking cause→change."""
    spec = _seed_spec()
    result = request_amendment(
        spec,
        actor="posts",
        slice_id="posts",
        changes={"deps": ["shell", "auth"]},
        reason="needs auth helper for timeline",
    )
    assert result.accepted
    assert result.amendment is not None
    assert result.amendment.tier == 3
    assert result.amendment.trigger_event_id == ""


def test_agent_amendment_with_fake_trigger_rejected(tmp_path: Path) -> None:
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    # Note: no event emitted

    result = request_amendment(
        spec,
        actor="posts",
        slice_id="posts",
        changes={"deps": ["shell", "auth"]},
        reason="trying with bogus id",
        trigger_event_id="ev-999999",
        session_dir=session_dir,
    )
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "trigger_not_found"


# ---------------------------------------------------------------------------
# Hash chain extension
# ---------------------------------------------------------------------------


def test_amendment_extends_hash_chain(tmp_path: Path) -> None:
    """Two amendments in sequence form a valid chain."""
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    e1 = emit(session_dir, "scope.warning", slice_id="posts", detail="r1")
    e2 = emit(session_dir, "scope.warning", slice_id="posts", detail="r2")

    r1 = request_amendment(
        spec,
        actor="posts",
        slice_id="posts",
        changes={"deps": ["shell", "auth"]},
        reason="add auth dep",
        trigger_event_id=e1.event_id,
        session_dir=session_dir,
    )
    assert r1.accepted
    assert r1.spec is not None

    r2 = request_amendment(
        r1.spec,
        actor="posts",
        slice_id="posts",
        changes={"tasks": ["t", "t2"]},
        reason="add a task",
        trigger_event_id=e2.event_id,
        session_dir=session_dir,
    )
    assert r2.accepted
    assert r2.spec is not None
    chain = r2.spec.amendments
    assert len(chain) == 2
    # Second's before == first's after
    assert chain[1].diff_sha256_before == chain[0].diff_sha256_after


# ---------------------------------------------------------------------------
# Audit-time chain verification (defense D3)
# ---------------------------------------------------------------------------


def test_verify_chain_clean_chain_passes(tmp_path: Path) -> None:
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    e1 = emit(session_dir, "scope.warning", slice_id="posts", detail="x")

    r1 = request_amendment(
        spec, actor="posts", slice_id="posts",
        changes={"deps": ["shell", "auth"]},
        reason="add dep", trigger_event_id=e1.event_id, session_dir=session_dir,
    )
    assert r1.accepted
    assert r1.spec is not None

    review = verify_amendment_chain(r1.spec, session_dir=session_dir)
    assert review.verdict_cap == "passed"
    assert review.findings == []


def test_verify_chain_consecutive_break_blocks(tmp_path: Path) -> None:
    """Two amendments where the second's `before` doesn't match the
    first's `after` — chain broken."""
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    e1 = emit(session_dir, "scope.warning", slice_id="posts", detail="r1")
    e2 = emit(session_dir, "scope.warning", slice_id="posts", detail="r2")

    r1 = request_amendment(
        spec, actor="posts", slice_id="posts",
        changes={"deps": ["shell", "auth"]},
        reason="r1", trigger_event_id=e1.event_id, session_dir=session_dir,
    )
    assert r1.accepted
    assert r1.spec is not None

    r2 = request_amendment(
        r1.spec, actor="posts", slice_id="posts",
        changes={"tasks": ["t", "t-extra"]},
        reason="r2", trigger_event_id=e2.event_id, session_dir=session_dir,
    )
    assert r2.accepted
    assert r2.spec is not None

    # Tamper with the second amendment's `before` so it doesn't match
    # the first's `after`.
    import dataclasses

    bad_amendment = dataclasses.replace(
        r2.spec.amendments[1], diff_sha256_before="deadbeef" * 8
    )
    tampered = dataclasses.replace(
        r2.spec, amendments=[r2.spec.amendments[0], bad_amendment]
    )

    review = verify_amendment_chain(tampered, session_dir=session_dir)
    assert review.verdict_cap == "blocked"
    assert any("hash chain broken" in f for f in review.findings)


def test_verify_chain_spec_mutated_outside_chain_blocks(tmp_path: Path) -> None:
    """Last amendment's `after` should match current spec content hash.
    If not, the spec was mutated outside the amendment flow → BLOCKED.
    """
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    e1 = emit(session_dir, "scope.warning", slice_id="posts", detail="r1")

    r1 = request_amendment(
        spec, actor="posts", slice_id="posts",
        changes={"deps": ["shell", "auth"]},
        reason="r1", trigger_event_id=e1.event_id, session_dir=session_dir,
    )
    assert r1.accepted
    assert r1.spec is not None

    # Tamper with spec content directly (simulating raw spec.json edit
    # bypassing request_amendment) — final-hash check catches it.
    import dataclasses

    tampered_slice = dataclasses.replace(r1.spec.slices[2], deps=["shell", "auth", "extra"])
    tampered = dataclasses.replace(
        r1.spec,
        slices=[r1.spec.slices[0], r1.spec.slices[1], tampered_slice],
    )

    review = verify_amendment_chain(tampered, session_dir=session_dir)
    assert review.verdict_cap == "blocked"
    assert any("mutated outside the chain" in f for f in review.findings)


def test_verify_chain_missing_trigger_caps_partial(tmp_path: Path) -> None:
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    # Construct an amendment that cites a non-existent event
    import dataclasses

    from otto.spec_compile import Amendment, _iso_now, spec_content_sha256

    h0 = spec_content_sha256(spec)
    spec_after = dataclasses.replace(
        spec,
        slices=[
            dataclasses.replace(spec.slices[2], deps=["shell", "auth"]),
            *[s for s in spec.slices if s.id != "posts"],
        ],
    )
    h1 = spec_content_sha256(spec_after)
    bad_amendment = Amendment(
        reason="r", actor="posts", ts=_iso_now(),
        diff_sha256_before=h0, diff_sha256_after=h1,
        trigger_event_id="ev-999999", tier=3,
    )
    spec_after = dataclasses.replace(spec_after, amendments=[bad_amendment])

    review = verify_amendment_chain(spec_after, session_dir=session_dir)
    assert review.verdict_cap == "partial"
    assert any("missing trigger" in f for f in review.findings)


def test_verify_chain_concentrated_amendments_caps_partial(tmp_path: Path) -> None:
    """Many amendments by one actor → suspicious pattern."""
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    current = spec
    for i in range(5):
        ev = emit(session_dir, "scope.warning", slice_id="posts", detail=f"r{i}")
        result = request_amendment(
            current,
            actor="posts", slice_id="posts",
            changes={"tasks": [f"task-{i}"]},
            reason=f"round {i}", trigger_event_id=ev.event_id, session_dir=session_dir,
        )
        assert result.accepted
        assert result.spec is not None
        current = result.spec

    review = verify_amendment_chain(current, session_dir=session_dir)
    assert review.verdict_cap == "partial"
    # Both heuristics fire (5+ tier-3 amendments, AND single-actor concentration).
    assert any("tier-3 amendments" in f for f in review.findings)


# ---------------------------------------------------------------------------
# Other rejections
# ---------------------------------------------------------------------------


def test_request_amendment_rejects_unknown_slice() -> None:
    spec = _seed_spec()
    result = request_amendment(
        spec,
        actor="ghost",
        slice_id="ghost",
        changes={"deps": []},
        reason="r",
        trigger_event_id="ev-000001",
    )
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "unknown_slice"


def test_request_amendment_rejects_no_change(tmp_path: Path) -> None:
    spec = _seed_spec()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    e1 = emit(session_dir, "scope.warning", slice_id="posts", detail="x")

    # Request with the SAME deps (no actual change)
    result = request_amendment(
        spec,
        actor="posts", slice_id="posts",
        changes={"deps": list(spec.slices[2].deps)},
        reason="r", trigger_event_id=e1.event_id, session_dir=session_dir,
    )
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "no_change"


# ---------------------------------------------------------------------------
# Integration: amend + persist round-trip
# ---------------------------------------------------------------------------


def test_amended_spec_persists_and_reloads(tmp_path: Path) -> None:
    """End-to-end: amendment → persist → load → chain valid."""
    from otto.spec_compile import load_spec

    spec = _seed_spec()
    target = tmp_path / "spec.json"
    persist_spec(spec, target, allow_initial=True)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    event = emit(session_dir, "scope.warning", slice_id="posts", detail="x")

    result = request_amendment(
        spec, actor="posts", slice_id="posts",
        changes={"deps": ["shell", "auth"]},
        reason="add dep", trigger_event_id=event.event_id, session_dir=session_dir,
    )
    assert result.accepted
    assert result.spec is not None

    persist_spec(result.spec, target)
    loaded = load_spec(target)

    assert loaded.slices[2].deps == ["shell", "auth"]
    assert len(loaded.amendments) == 1
    assert loaded.amendments[0].trigger_event_id == event.event_id
    assert loaded.amendments[0].tier == 3
    # Chain still verifies after round-trip.
    review = verify_amendment_chain(loaded, session_dir=session_dir)
    assert review.verdict_cap == "passed"


# ---------------------------------------------------------------------------
# lock_intent + intent_hash
# ---------------------------------------------------------------------------


def test_lock_intent_stamps_hash() -> None:
    spec = Spec(intent="hello world")
    sealed = lock_intent(spec)
    assert sealed.intent_hash == compute_intent_hash("hello world")


def test_lock_intent_idempotent() -> None:
    spec = lock_intent(Spec(intent="hello"))
    again = lock_intent(spec)
    assert again is spec  # same instance, no replace


def test_compute_intent_hash_stable() -> None:
    """Same intent → same hash, every time."""
    a = compute_intent_hash("the same intent")
    b = compute_intent_hash("the same intent")
    assert a == b
    assert len(a) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Build-agent side-channel: consume_amendment_request
# ---------------------------------------------------------------------------


def test_consume_no_request_file_is_noop(tmp_path: Path) -> None:
    """If the agent didn't write the request file, return spec unchanged."""
    spec = _seed_spec()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    new_spec, result = consume_amendment_request(
        worktree, spec, slice_id="posts", session_dir=session_dir,
    )
    assert new_spec is spec
    assert result is None


def test_consume_valid_request_applies_and_writes_response(tmp_path: Path) -> None:
    import json

    spec = _seed_spec()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    event = emit(session_dir, "scope.warning", slice_id="posts", detail="touched auth/")

    request_path = worktree / AMENDMENT_REQUEST_PATH
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps({
        "changes": {"deps": ["shell", "auth"]},
        "reason": "needs auth helper for timeline",
        "trigger_event_id": event.event_id,
    }))

    new_spec, result = consume_amendment_request(
        worktree, spec, slice_id="posts", session_dir=session_dir,
    )
    assert result is not None
    assert result.accepted
    posts = next(s for s in new_spec.slices if s.id == "posts")
    assert posts.deps == ["shell", "auth"]

    # Request file consumed; response file written.
    assert not request_path.exists()
    response = json.loads((worktree / AMENDMENT_RESPONSE_PATH).read_text())
    assert response["accepted"] is True
    assert response["trigger_event_id"] == event.event_id
    assert response["amendment_index"] == 0
    assert response["tier"] == 3


def test_consume_rejected_request_writes_rejection_response(tmp_path: Path) -> None:
    """Bad request → response file says why; spec untouched.
    Use a tier-1 violation as the rejection trigger (intent change)."""
    import json

    spec = _seed_spec()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    request_path = worktree / AMENDMENT_REQUEST_PATH
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps({
        "changes": {"intent": "agent escalation attempt"},
        "reason": "trying to amend tier-1 field",
    }))

    new_spec, result = consume_amendment_request(
        worktree, spec, slice_id="posts", session_dir=session_dir,
    )
    assert result is not None
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "tier_1_violation"
    # Spec returned unchanged.
    assert new_spec is spec
    # Response file written with rejection.
    response = json.loads((worktree / AMENDMENT_RESPONSE_PATH).read_text())
    assert response["accepted"] is False
    assert response["code"] == "tier_1_violation"


def test_consume_malformed_json_is_rejected(tmp_path: Path) -> None:
    """Garbage in the request file → rejection, not crash."""
    import json

    spec = _seed_spec()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    request_path = worktree / AMENDMENT_REQUEST_PATH
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text("{this is not json")

    new_spec, result = consume_amendment_request(
        worktree, spec, slice_id="posts", session_dir=session_dir,
    )
    assert result is not None
    assert not result.accepted
    assert result.rejection is not None
    assert result.rejection.code == "invalid_field"
    assert new_spec is spec
    # Request file consumed even when malformed.
    assert not request_path.exists()
    response = json.loads((worktree / AMENDMENT_RESPONSE_PATH).read_text())
    assert response["accepted"] is False


def test_consume_side_channel_hardcodes_actor_to_slice(tmp_path: Path) -> None:
    """The side-channel binds actor = slice_id by construction. There's
    no field for the agent to specify a different actor — the runtime
    sets it from the slice context. This makes scope_violation
    unreachable via the side-channel (the cross-slice rejection is
    covered by test_agent_cannot_amend_another_slice on the underlying
    request_amendment API)."""
    import json

    spec = _seed_spec()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    event = emit(session_dir, "scope.warning", slice_id="posts", detail="x")

    # Even if the agent tries to put "actor" in the JSON, it's ignored.
    request_path = worktree / AMENDMENT_REQUEST_PATH
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps({
        "actor": "user",  # agent attempts privilege escalation
        "changes": {"deps": ["shell", "auth"]},
        "reason": "legitimately needs auth",
        "trigger_event_id": event.event_id,
    }))

    new_spec, result = consume_amendment_request(
        worktree, spec, slice_id="posts", session_dir=session_dir,
    )
    assert result is not None
    assert result.accepted
    assert result.amendment is not None
    # Actor is the slice id, not "user" — the JSON's actor field was ignored.
    assert result.amendment.actor == "posts"
    assert result.amendment.tier == 3
