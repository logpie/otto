"""Phase C.2 (tick 64): legacy certifier package — gutted.

The legacy ``otto/certifier/__init__.py`` carried the multi-mode dispatch
loop (``run_agentic_certifier``), prompt loaders, helper renderers,
proof-of-work writers, and ~4,400 lines of supporting infrastructure for
the pre-i2p verification path. The new intent-to-product stack
(``otto/audit.py`` + ``otto/audit_loop.py`` + ``orchestrate_run`` /
``orchestrate_certify`` in ``otto/cli_run.py``) replaces every code path
in this module.

Phase B.3 (tick 62) flipped ``default_pipeline`` to ``i2p`` so all four
public commands (``otto build``, ``otto certify``, ``otto improve
{bugs,feature,target}``) route through the new stack by default. Phase
C.1a (tick 63) hard-errored ``--legacy`` for ``otto improve``. Phase C.2
(this tick) does the same for ``otto certify`` and removes the legacy
implementation entirely.

What stays:

- ``otto/certifier/contracts.py`` — pydantic models + helpers re-exported
  here for backwards-compat with a few remaining legacy callers.
- ``otto/certifier/report.py`` — ``CertificationOutcome`` /
  ``CertificationReport`` dataclasses referenced by a handful of legacy
  tests.

What's gone:

- ``run_agentic_certifier`` and the entire dispatch loop. The function
  name is preserved as a hard-error stub so the lingering lazy import in
  ``otto/pipeline.py`` (also a Phase C deletion target) fails fast with a
  clear migration message instead of crashing on an undefined attribute.
  The merge orchestrator's mirror import was eliminated in Phase C.4
  alongside the orchestrator itself.
- All prompt rendering helpers (``_render_certifier_prompt``,
  ``_format_stories_section``, …), all proof-of-work writers
  (``_build_pow_report_data``, ``_write_pow_report``,
  ``_render_pow_html``, ``_render_pow_markdown``), all server-cleanup
  helpers, all standalone-certify history repair helpers. Equivalent
  functionality lives in ``otto/audit.py`` and ``otto/render.py``.

Tests that exercised the legacy certifier exclusively
(``tests/test_certifier_stories.py``) are removed in this tick.
``tests/test_v3_pipeline.py`` continues to test ``otto/pipeline.py``;
the certifier-coupled tests inside it are expected to be removed
together with ``otto/pipeline.py`` in Phase C.3 (parallel agent W8-B).
"""

from __future__ import annotations

from typing import Any

from otto.certifier import contracts as contracts  # re-exported for callers
from otto.certifier import report as report  # re-exported for callers


_PHASE_C2_REMOVED_MESSAGE = (
    "run_agentic_certifier was removed in Phase C.2 (tick 64). "
    "The legacy multi-mode certifier has been replaced by the "
    "intent-to-product audit stack (otto/audit.py + otto/audit_loop.py); "
    "drive it via `otto certify` (default) or `otto run` for the full "
    "pipeline. The `--legacy` flag now hard-errors with the same message."
)


async def run_agentic_certifier(*_args: Any, **_kwargs: Any) -> Any:
    """Hard-error stub for the deleted legacy certifier.

    The real implementation was deleted in Phase C.2. ``otto/pipeline.py``
    (a Phase C.3 deletion target) still lazy-imports this name; it fails
    with a clear migration message instead of an ``ImportError`` so log
    readers see the deletion, not a missing symbol. The merge
    orchestrator's mirror import was eliminated in Phase C.4 alongside
    the orchestrator itself.
    """
    raise RuntimeError(_PHASE_C2_REMOVED_MESSAGE)


__all__ = [
    "contracts",
    "report",
    "run_agentic_certifier",
]
