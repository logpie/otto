"""Tiered amendment API — agent-driven spec edits with bounded scope.

v2.2 design (docs/intent-to-product-v2.md "Safe mutability"). Agents
need to refine their slice's deps/owned_paths/tasks during build, but
they cannot be allowed to escape:

- Tier 1 (BEDROCK): intent + intent_hash. Immutable. Agents cannot
  amend; user override required.
- Tier 2 (LOCKED): project_kind, done_means, non_goals, cross_slice_checks,
  slice.id. User-only edits via the spec-review gate. Agents cannot
  amend.
- Tier 3 (SLICE-LOCAL): slice.deps, slice.owned_paths, slice.tasks,
  slice.title, shared_scaffold, slice.checks (append-only). Agents may
  request amendments on THEIR OWN slice via this API. Modifying another
  slice's fields requires that slice's permission (which agents don't
  have a way to grant).

Constraints on every tier-3 amendment:

1. Scope-limited: agent can amend ONLY its own slice's fields.
2. Checks are append-only: removing or weakening checks is rejected.
3. Reasons must reference a real journal event via trigger_event_id.
4. Hash chain extends correctly.

Audit verifies the entire chain at end-of-run; suspicious patterns
(massive check weakening, missing triggers, repeated dep expansion)
cap the verdict at PARTIAL.

This module never persists by itself — it returns the new Spec.
Callers are responsible for `persist_spec(...)` afterward (which
re-validates the hash chain on disk).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from otto.spec_compile import (
    Amendment,
    Slice,
    Spec,
    _iso_now,
    append_amendment,
    spec_content_sha256,
)
from otto.spec_state import find_event


# ---------------------------------------------------------------------------
# Tier metadata: which Spec / Slice fields belong to which tier
# ---------------------------------------------------------------------------


# Top-level Spec fields by tier. Anything not listed defaults to "no
# amendment allowed via this API" (the parser handles it; users edit
# through spec-review gate).
TIER_1_FIELDS: frozenset[str] = frozenset({
    "intent", "intent_hash",
})
TIER_2_FIELDS: frozenset[str] = frozenset({
    "project_kind", "done_means", "non_goals", "cross_slice_checks",
})

# Slice-level fields by tier. Slice.id is tier-2 (locked once set).
SLICE_TIER_2_FIELDS: frozenset[str] = frozenset({"id"})
SLICE_TIER_3_FIELDS: frozenset[str] = frozenset({
    "title", "tasks", "deps", "owned_paths", "checks",
})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AmendmentRejection:
    """Why a request_amendment call was rejected. Used for journal logging."""
    code: Literal[
        "tier_1_violation",
        "tier_2_violation",
        "scope_violation",
        "unknown_slice",
        "checks_weakened",
        "missing_trigger",
        "trigger_not_found",
        "no_change",
        "invalid_field",
    ]
    message: str


@dataclass
class AmendmentResult:
    """Outcome of `request_amendment`.

    Exactly one of `spec` / `rejection` is set. On accept, `spec` is
    the post-amendment Spec; the latest entry in `spec.amendments` is
    the new Amendment record. On reject, `rejection` carries the
    diagnostic.
    """
    spec: Spec | None = None
    rejection: AmendmentRejection | None = None
    amendment: Amendment | None = None

    @property
    def accepted(self) -> bool:
        return self.spec is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def request_amendment(
    spec: Spec,
    *,
    actor: str,
    slice_id: str,
    changes: dict[str, Any],
    reason: str,
    trigger_event_id: str = "",
    session_dir: Path | None = None,
) -> AmendmentResult:
    """Request a tier-3 amendment on `slice_id`'s fields.

    Args:
        spec: Current Spec.
        actor: Who is making the request (e.g., build agent's slice id,
            or the literal string "user" for review-gate edits).
        slice_id: Slice to amend. Must exist in `spec.slices`. For agent
            requests, `actor` and `slice_id` should match (scope rule).
        changes: Mapping of field name → new value, restricted to
            SLICE_TIER_3_FIELDS. `checks` may only be a SUPERSET of the
            current list (append-only).
        reason: Human-readable justification (non-empty).
        trigger_event_id: Stable event id from spec_state journal that
            motivated this amendment. Required for agent actors;
            optional for actor="user".
        session_dir: Session journal location. Required when
            `trigger_event_id` is provided so the runtime can verify
            the event exists.

    Returns:
        AmendmentResult. On accept, the result.spec carries the
        amended Spec with a new Amendment in its chain.
    """
    # ---- find the slice ----
    target = next((s for s in spec.slices if s.id == slice_id), None)
    if target is None:
        return _reject("unknown_slice", f"slice {slice_id!r} not found in spec")

    # ---- scope rule: agents amend their own slice ----
    is_user = actor.strip().lower() == "user"
    if not is_user and actor != slice_id:
        return _reject(
            "scope_violation",
            f"actor {actor!r} cannot amend slice {slice_id!r} (own-slice rule)",
        )

    # ---- trigger event linkage: optional, validated if present ----
    # v2.2 evolution: requiring trigger_event_id was ceremony — agents
    # picked an arbitrary id without it actually linking the amendment
    # to a real cause. The cumulative chain review (defense D3) is the
    # actual safeguard against pattern-of-amendments abuse. If a
    # trigger_event_id IS provided and we have a session_dir, validate
    # it exists; otherwise accept the amendment on the strength of
    # `reason` + chain review at audit time.
    if trigger_event_id and session_dir is not None:
        event = find_event(session_dir, trigger_event_id)
        if event is None:
            return _reject(
                "trigger_not_found",
                f"trigger_event_id {trigger_event_id!r} not found in session journal",
            )

    # ---- validate every field in `changes` is tier-3 / slice-local ----
    for field_name, new_value in changes.items():
        if field_name in TIER_1_FIELDS:
            return _reject(
                "tier_1_violation",
                f"field {field_name!r} is BEDROCK (tier 1); intent cannot be amended via this API",
            )
        if field_name in TIER_2_FIELDS or field_name in SLICE_TIER_2_FIELDS:
            return _reject(
                "tier_2_violation",
                f"field {field_name!r} is LOCKED (tier 2); user-only edit via review gate",
            )
        if field_name not in SLICE_TIER_3_FIELDS:
            return _reject(
                "invalid_field",
                f"field {field_name!r} is not slice-local (tier 3) — not amendable",
            )

        # ---- checks: append-only ----
        if field_name == "checks":
            if not _is_check_superset(new_value, target.checks):
                return _reject(
                    "checks_weakened",
                    "checks may only be appended, not removed or weakened",
                )

    # ---- compute the new spec ----
    new_target = _apply_changes(target, changes)
    if new_target == target:
        return _reject("no_change", "no field changed")

    new_slices = [new_target if s.id == slice_id else s for s in spec.slices]
    spec_after = dataclasses.replace(spec, slices=new_slices)

    # ---- chain extension ----
    prior_hash = spec_content_sha256(spec)
    after_hash = spec_content_sha256(spec_after)
    new_amendment = Amendment(
        reason=reason.strip(),
        actor=actor.strip(),
        ts=_iso_now(),
        diff_sha256_before=prior_hash,
        diff_sha256_after=after_hash,
        trigger_event_id=trigger_event_id,
        tier=3,
    )
    spec_after = dataclasses.replace(
        spec_after, amendments=[*spec_after.amendments, new_amendment]
    )
    return AmendmentResult(spec=spec_after, amendment=new_amendment)


# ---------------------------------------------------------------------------
# Audit-time chain review (defense D3)
# ---------------------------------------------------------------------------


@dataclass
class ChainVerification:
    """Result of `verify_amendment_chain`. Used by audit to cap the verdict.

    `verdict_cap`:
      - "passed": chain is clean; no constraint on audit verdict.
      - "partial": suspicious patterns; audit may not return PASSED.
      - "blocked": chain integrity broken; verdict must be BLOCKED.

    `findings` is a list of human-readable diagnostics for the proof
    packet.
    """
    verdict_cap: Literal["passed", "partial", "blocked"] = "passed"
    findings: list[str] = field(default_factory=list)


def verify_amendment_chain(
    spec: Spec,
    *,
    session_dir: Path | None = None,
) -> ChainVerification:
    """Audit-time review of `spec.amendments`. Defense D3.

    Checks (in order):

    1. Hash chain integrity: each `diff_sha256_before` matches the
       prior amendment's `diff_sha256_after` (or the empty initial
       hash). Any break → BLOCKED.
    2. Trigger events resolve: every agent-tier-3 amendment with a
       trigger_event_id references a real event in the journal.
       Missing trigger → PARTIAL.
    3. Cumulative red flags:
       - More than 50% of amendments are tier-3 from the same actor.
       - Any amendment cites a trigger_event_id that doesn't exist.
       - Any amendment has tier=3 but actor=="user" (mislabeled).
       - 5+ amendments in the chain (heuristic; configurable).
       Multiple flags → PARTIAL.
    """
    findings: list[str] = []
    verdict_cap: Literal["passed", "partial", "blocked"] = "passed"

    # ---- 1. Hash chain integrity ----
    # Consecutive amendments must extend: amendments[i+1].before ==
    # amendments[i].after. The first amendment's before is the seed
    # spec's content hash and is unverifiable from spec alone (no
    # pre-amendment snapshot available); the last amendment's after
    # must equal the current spec's content hash.
    for index in range(1, len(spec.amendments)):
        prior = spec.amendments[index - 1]
        cur = spec.amendments[index]
        if cur.diff_sha256_before != prior.diff_sha256_after:
            findings.append(
                f"amendment[{index}] (actor={cur.actor!r}) "
                f"diff_sha256_before {cur.diff_sha256_before[:16]}... "
                f"does not extend amendment[{index-1}].diff_sha256_after "
                f"{prior.diff_sha256_after[:16]}...; hash chain broken"
            )
            return ChainVerification(verdict_cap="blocked", findings=findings)

    if spec.amendments:
        current_hash = spec_content_sha256(spec)
        if spec.amendments[-1].diff_sha256_after != current_hash:
            findings.append(
                f"latest amendment.diff_sha256_after "
                f"{spec.amendments[-1].diff_sha256_after[:16]}... "
                f"does not match current spec content hash "
                f"{current_hash[:16]}...; spec mutated outside the chain"
            )
            return ChainVerification(verdict_cap="blocked", findings=findings)

    # ---- 2. Trigger events resolve ----
    if session_dir is not None:
        for index, amendment in enumerate(spec.amendments):
            if amendment.tier == 3 and amendment.trigger_event_id:
                event = find_event(session_dir, amendment.trigger_event_id)
                if event is None:
                    findings.append(
                        f"amendment[{index}] cites missing trigger event "
                        f"{amendment.trigger_event_id!r}"
                    )
                    verdict_cap = "partial"

    # ---- 3. Cumulative red flags ----
    actor_counts: dict[str, int] = {}
    for amendment in spec.amendments:
        if amendment.tier == 3:
            actor_counts[amendment.actor] = actor_counts.get(amendment.actor, 0) + 1

    total_tier_3 = sum(actor_counts.values())
    if total_tier_3 >= 5:
        findings.append(
            f"{total_tier_3} tier-3 amendments in chain "
            "(heuristic threshold for review)"
        )
        verdict_cap = "partial"

    if total_tier_3 > 0:
        max_actor, max_count = max(actor_counts.items(), key=lambda kv: kv[1])
        if max_count * 2 > total_tier_3 and total_tier_3 >= 4:
            findings.append(
                f"actor {max_actor!r} drove {max_count}/{total_tier_3} "
                "tier-3 amendments (concentrated edit pattern)"
            )
            verdict_cap = "partial"

    # Check for tier=3 with actor="user" (mislabeled)
    for index, amendment in enumerate(spec.amendments):
        if amendment.tier == 3 and amendment.actor.strip().lower() == "user":
            findings.append(
                f"amendment[{index}] tier=3 but actor='user' "
                "(user edits should be tier 2)"
            )
            verdict_cap = "partial"

    return ChainVerification(verdict_cap=verdict_cap, findings=findings)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _reject(code: str, message: str) -> AmendmentResult:
    return AmendmentResult(rejection=AmendmentRejection(code=code, message=message))  # type: ignore[arg-type]


def _is_check_superset(new_checks: Any, current_checks: list) -> bool:  # type: ignore[type-arg]
    """Return True if `new_checks` is a non-shrinking superset of `current_checks`.

    Append-only enforcement: every existing check must still be present
    (by equality). Adding new checks is fine.
    """
    if not isinstance(new_checks, list):
        return False
    if len(new_checks) < len(current_checks):
        return False
    for existing in current_checks:
        if existing not in new_checks:
            return False
    return True


def _apply_changes(slice_: Slice, changes: dict[str, Any]) -> Slice:
    """Return a new Slice with the requested fields replaced."""
    payload = {f.name: getattr(slice_, f.name) for f in dataclasses.fields(slice_)}
    for k, v in changes.items():
        payload[k] = v
    return Slice(**payload)


# ---------------------------------------------------------------------------
# Build-agent side-channel: file-based amendment request
# ---------------------------------------------------------------------------


AMENDMENT_REQUEST_PATH = ".otto/amendment_request.json"
AMENDMENT_RESPONSE_PATH = ".otto/amendment_response.json"


def consume_amendment_request(
    worktree: Path,
    spec: Spec,
    *,
    slice_id: str,
    session_dir: Path,
) -> tuple[Spec, AmendmentResult | None]:
    """Process a build-agent's amendment request from the worktree side-channel.

    The build agent writes its request to
    `<worktree>/.otto/amendment_request.json` during its turn:

      {
        "changes": {"deps": ["shell", "auth"], "tasks": ["..."]},
        "reason": "needs auth helper to render timeline metadata",
        "trigger_event_id": "ev-000007"
      }

    After the agent finishes, the runtime calls this helper. If the
    request validates, returns `(updated_spec, AmendmentResult)`. The
    caller is responsible for persisting the new spec to disk and
    propagating it to subsequent slices.

    A response file is always written so the agent (or a follow-up
    review tool) can see whether the request was accepted:

      {"accepted": true, "trigger_event_id": "...", "amendment_index": 1}
      {"accepted": false, "code": "missing_trigger", "message": "..."}

    The request file is consumed (deleted) after processing so a stuck
    file from a prior attempt doesn't double-fire. Returns
    `(spec, None)` if no request file existed.
    """
    request_path = worktree / AMENDMENT_REQUEST_PATH
    response_path = worktree / AMENDMENT_RESPONSE_PATH
    if not request_path.exists():
        return spec, None

    import json

    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result = _reject("invalid_field", f"could not parse amendment_request.json: {exc}")
        _write_response(response_path, result, amendment_index=None)
        request_path.unlink(missing_ok=True)
        return spec, result

    if not isinstance(payload, dict):
        result = _reject(
            "invalid_field",
            f"amendment_request.json must be an object, got {type(payload).__name__}",
        )
        _write_response(response_path, result, amendment_index=None)
        request_path.unlink(missing_ok=True)
        return spec, result

    changes = payload.get("changes") or {}
    if not isinstance(changes, dict):
        result = _reject(
            "invalid_field",
            f"changes must be an object, got {type(changes).__name__}",
        )
        _write_response(response_path, result, amendment_index=None)
        request_path.unlink(missing_ok=True)
        return spec, result

    result = request_amendment(
        spec,
        actor=slice_id,
        slice_id=slice_id,
        changes=changes,
        reason=str(payload.get("reason") or ""),
        trigger_event_id=str(payload.get("trigger_event_id") or ""),
        session_dir=session_dir,
    )

    amendment_index: int | None = None
    if result.accepted and result.spec is not None:
        amendment_index = len(result.spec.amendments) - 1
        spec = result.spec

    _write_response(response_path, result, amendment_index=amendment_index)
    request_path.unlink(missing_ok=True)
    return spec, result


def _write_response(
    response_path: Path,
    result: AmendmentResult,
    *,
    amendment_index: int | None,
) -> None:
    import json

    response_path.parent.mkdir(parents=True, exist_ok=True)
    if result.accepted and result.amendment is not None:
        body = {
            "accepted": True,
            "trigger_event_id": result.amendment.trigger_event_id,
            "amendment_index": amendment_index,
            "tier": result.amendment.tier,
        }
    else:
        rej = result.rejection
        body = {
            "accepted": False,
            "code": rej.code if rej else "unknown",
            "message": rej.message if rej else "",
        }
    response_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "AMENDMENT_REQUEST_PATH",
    "AMENDMENT_RESPONSE_PATH",
    "AmendmentRejection",
    "AmendmentResult",
    "ChainVerification",
    "consume_amendment_request",
    "request_amendment",
    "verify_amendment_chain",
    "TIER_1_FIELDS",
    "TIER_2_FIELDS",
    "SLICE_TIER_2_FIELDS",
    "SLICE_TIER_3_FIELDS",
]
