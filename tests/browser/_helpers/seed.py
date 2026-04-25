"""Seed Mission Control project state from recorded fixture trees.

Phase 3.5 of ``plan-mc-audit.md`` will fill in the recording side: real otto
runs captured to ``tests/browser/_fixtures/recorded-runs/<scenario>/``, then
sanitized (timestamps frozen, IDs deterministic, ``$PROJECT_ROOT`` placeholders).
This module provides the consumer side — a single helper that copies a
recorded scenario tree into a tmp project dir, hydrates path placeholders,
and reconstructs git state from the bundle.

For Phase 3 (this commit) the function is a stub: callers can import it and
the helper raises ``NotImplementedError`` with a pointer to the plan section.
The signature is final; implementation lands with the first recording.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

RECORDINGS_ROOT: Final[Path] = Path(__file__).resolve().parent.parent / "_fixtures" / "recorded-runs"


def seed_from_recording(scenario: str, *, project_dir: Path) -> Path:  # noqa: ARG001 — project_dir wired for Phase 3.5 implementation
    _ = project_dir  # signature-stable stub; consumed once Phase 3.5 lands
    """Hydrate ``project_dir`` from the named recorded scenario.

    Args:
        scenario: Recording id under ``tests/browser/_fixtures/recorded-runs/``,
            e.g. ``"R1"`` (successful kanban build) or ``"R9"`` (mixed-row queue).
            See ``plan-mc-audit.md`` section 3.5B for the full table.
        project_dir: Empty target directory — typically pytest's ``tmp_path``.
            Must exist; must be empty (the git-bundle restore expects an empty
            cwd, and overlaying onto a populated dir is not safe).

    Returns:
        ``project_dir`` for convenience chaining.

    Raises:
        NotImplementedError: Until Phase 3.5 lands the recording capture
            script. Callers can already write their tests against this
            signature; once recordings exist the implementation lands without
            touching test code.
        FileNotFoundError: Once implemented — when ``scenario`` has no
            corresponding directory under ``RECORDINGS_ROOT``.
    """

    msg = (
        f"seed_from_recording('{scenario}') is not yet implemented; "
        + "see plan-mc-audit.md Phase 3.5 (recorded-from-reality fixtures). "
        + f"Recordings will live under {RECORDINGS_ROOT}/<scenario>/."
    )
    raise NotImplementedError(msg)
