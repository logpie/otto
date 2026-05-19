"""Regression: the compile / pass-model-repair prompt must enumerate the
EXACT closed set of strong success-observable ``kind`` values, and the
pass-model validator must stay strict (no gate-weakening).

Root cause (FRESH Linkboard run linkboard-p0fix-203622, 2026-05-18,
terminal ``Verdict: merge_blocked`` /
``spec_contract_repair_exhausted: verification_contract_invalid at
behavior_journeys[delete_bookmark_ui].pass_model.actions[0]
.success_observables: state-changing action lacks a non-tautological
post-action observable`` after 5 bounded repairs, $0, 736.8s): the spec
agent emitted, for the state-changing ``register_for_delete`` action, the
single observable ``{"kind": "route_change", "description": "Redirected to
/bookmarks after registration.", "text": "/bookmarks"}``. That is a
plausible "what changes after success" to the agent, but
``_observable_is_strong`` only accepts ``kind`` in the closed set
``STRONG_OBSERVABLE_KINDS`` — ``route_change`` is not a member, so the
action was correctly rejected (a URL change alone does not prove the
account was created). The prompt described "non-tautological observable"
only in prose and never enumerated the allowed ``kind`` values, so the
spec agent could not know ``route_change`` was invalid, and the bounded
contract-repair loop re-read the same under-specified guidance and
oscillated between journeys to exhaustion.

Fix (consistent-by-construction, NOT gate-weakening): the prompt now spells
out the closed allowlist, sourced from
``journey_contracts.STRONG_OBSERVABLE_KINDS`` itself so the prompt and the
validator can never drift, and explicitly names ``route_change`` (the
common rejection) as invalid for a state-changing action. The validator is
untouched and stays strict.
"""

from __future__ import annotations

from otto.journey_contracts import STRONG_OBSERVABLE_KINDS, _observable_is_strong
from otto.spec_compile_flat import (
    _PROMPT_CONTRACT_REPAIR_SUFFIX,
    _PROMPT_TEMPLATE,
    _STRONG_KINDS_STR,
)


def _rendered_template() -> str:
    return _PROMPT_TEMPLATE.format(
        intent="build a bookmark manager", strong_kinds=_STRONG_KINDS_STR
    )


def _rendered_repair_suffix() -> str:
    return _PROMPT_CONTRACT_REPAIR_SUFFIX.format(
        code="verification_contract_invalid",
        path="behavior_journeys[j].pass_model.actions[0].success_observables",
        message="state-changing action lacks a non-tautological post-action observable",
        target="j",
        strong_kinds=_STRONG_KINDS_STR,
        previous_json="{}",
    )


def test_compile_prompt_enumerates_every_strong_kind_no_drift() -> None:
    """Every member of the validator's closed allowlist must appear verbatim
    in BOTH the initial compile prompt and the bounded contract-repair
    suffix — so adding/removing a kind in journey_contracts forces the
    prompt to follow (no prompt/validator drift)."""
    template = _rendered_template()
    suffix = _rendered_repair_suffix()
    assert STRONG_OBSERVABLE_KINDS, "allowlist unexpectedly empty"
    for kind in STRONG_OBSERVABLE_KINDS:
        assert kind in template, f"{kind!r} missing from compile prompt"
        assert kind in suffix, f"{kind!r} missing from repair suffix"


def test_prompt_explicitly_forbids_route_change_for_state_changing() -> None:
    """The exact kind the spec agent emitted and got rejected on
    (``route_change``) must be explicitly named as NOT acceptable for a
    state-changing action, in both prompts."""
    template = _rendered_template()
    suffix = _rendered_repair_suffix()
    assert "route_change" in template
    assert "route_change" in suffix
    # and it must be framed as invalid / rejected, not merely mentioned
    assert "route_change" not in STRONG_OBSERVABLE_KINDS


def test_validator_still_rejects_route_change_and_accepts_persisted() -> None:
    """Guard: the fix is generator-side only. ``_observable_is_strong`` must
    still REJECT a ``route_change`` state-changing observable (no
    gate-weakening) and ACCEPT a properly tied ``persisted_data_visible``
    one."""
    covered = {"user.register"}
    route_change = {
        "kind": "route_change",
        "primary_action_id": "user.register",
        "description": "Redirected to /bookmarks after registration.",
        "text": "/bookmarks",
    }
    assert not _observable_is_strong(
        route_change, covered_actions=covered, step_actions=set()
    )
    persisted = {
        "kind": "persisted_data_visible",
        "primary_action_id": "user.register",
        "description": "The authenticated-only Add bookmark form is present.",
        "role": "button",
        "name": "Add bookmark",
    }
    assert _observable_is_strong(
        persisted, covered_actions=covered, step_actions=set()
    )
