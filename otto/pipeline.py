"""Phase C.3 shim — the legacy v3 pipeline has been deleted.

Originally this module hosted ~2,875 LOC of the legacy v3 build pipeline
(``build_agentic_v3``, ``run_certify_fix_loop``) plus a pile of shared
run-lifecycle helpers. Phase C.3 deletes the v3 entry points; the
shared helpers moved to :mod:`otto.runs.lifecycle`.

This shim survives only to bridge the gap until ``otto/certifier`` is
gone (Phase C.2, parallel deletion). Once the certifier package is
removed, this file can be deleted too. ``otto/agent.py`` and
``otto/cli.py`` already moved to the new import path.

Direct access to ``build_agentic_v3`` / ``run_certify_fix_loop`` raises
``RuntimeError`` with a migration message — there is no behavior to
preserve.
"""

from __future__ import annotations

# Re-export shared lifecycle helpers so any remaining caller (notably
# ``otto.certifier``, slated for deletion in Phase C.2) keeps working
# until that deletion lands. Do NOT add new consumers — import from
# ``otto.runs.lifecycle`` directly.
from otto.runs.lifecycle import (  # noqa: F401
    _ack_atomic_cancel_commands,
    _append_cancelled_atomic_history,
    _append_session_history,
    _atomic_artifacts,
    _atomic_publisher,
    _atomic_registry_enabled,
    _cleanup_orphan_processes,
    _current_branch_name,
    _current_head_sha,
    _finalize_atomic_publisher,
    _history_story_counts,
    _intent_provenance_payload,
    _make_atomic_heartbeat_callback,
    _make_atomic_terminal_callback,
    _persist_atomic_cancelled_terminal_state,
    _process_group_identity_matches,
    _raise_if_atomic_cancel_requested,
    _read_json_dict,
    _record_prompt_provenance,
    _repair_atomic_history,
    _runtime_metadata,
    _spec_provenance_payload,
    _write_runtime_artifact,
    _write_session_summary,
)


_LEGACY_REMOVED_MSG = (
    "Legacy v3 build pipeline ({name}) was deleted in Phase C.3. "
    "Use `otto build` (i2p stack, now the default) instead."
)


def __getattr__(name: str):  # PEP 562 module-level __getattr__
    if name in ("build_agentic_v3", "run_certify_fix_loop", "BuildResult", "InfraFailureError"):
        raise RuntimeError(_LEGACY_REMOVED_MSG.format(name=name))
    raise AttributeError(f"module 'otto.pipeline' has no attribute {name!r}")
