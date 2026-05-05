"""Tests for legacy v3 / certifier entry points after Phase C deletion.

B.4 originally added DeprecationWarnings on these entry points. Phase C
deleted both implementations: Phase C.3 hard-errored
``build_agentic_v3`` / ``run_certify_fix_loop``, and Phase C.2 (tick 64)
hard-errored ``run_agentic_certifier``. The names persist as shims so
the legacy lazy import in ``otto/pipeline.py`` (also a Phase C deletion
target) fails with a clear migration message instead of an opaque
``ImportError``. ``otto/merge/orchestrator.py`` had the mirror import;
the orchestrator was deleted entirely in Phase C.4.
"""

from __future__ import annotations

import asyncio
import warnings
from pathlib import Path

import pytest


def test_legacy_v3_build_entry_points_hard_error() -> None:
    """Phase C.3: importing the legacy v3 build entry points raises
    ``RuntimeError`` with a migration hint pointing at the i2p stack.
    """
    import otto.pipeline as _pipeline_mod

    for name in ("build_agentic_v3", "run_certify_fix_loop"):
        with pytest.raises(RuntimeError) as excinfo:
            getattr(_pipeline_mod, name)
        msg = str(excinfo.value)
        assert "Phase C" in msg
        assert "i2p" in msg
        assert name in msg


def test_run_agentic_certifier_hard_errors_post_c2() -> None:
    """Phase C.2 (tick 64): ``run_agentic_certifier`` raises RuntimeError
    naming the deletion. The shim exists so the lazy import in
    ``otto/pipeline.py`` (a Phase C.3 deletion target) fails loudly
    instead of with ``ImportError``. ``otto/merge/orchestrator.py`` had
    the mirror import; that orchestrator was deleted in Phase C.4.
    """
    from otto.certifier import run_agentic_certifier

    with pytest.raises(RuntimeError, match="Phase C.2"):
        asyncio.run(
            run_agentic_certifier(
                intent="test",
                project_dir=Path("/nonexistent"),
                config={},
            )
        )


def test_deprecation_warnings_do_not_fire_at_module_import() -> None:
    """Importing pipeline / certifier must NOT fire DeprecationWarning —
    only calling the deprecated function should. This keeps the noise
    bounded: passive import (e.g. for type hints) doesn't spam users.

    Post-Phase C.2, ``otto.certifier`` is a pure shim with no top-level
    side effects, so the import-time silence invariant continues to hold
    even though the warning machinery is gone.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        # Force fresh import semantics via getattr (modules already
        # cached, but we just want to check the act of import itself
        # didn't queue a warning).
        import importlib
        import otto.pipeline as _pipeline_mod
        import otto.certifier as _certifier_mod  # noqa: F401  - import-time check
        importlib.reload(_pipeline_mod)
        # Don't reload certifier — historically it had heavy side effects
        # (deleted in Phase C.2); the shim is intentionally bare.

    deprecation_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
        and ("build_agentic_v3" in str(w.message)
             or "run_agentic_certifier" in str(w.message))
    ]
    # Either the import is silent OR Python's caching already returned
    # the modules without re-executing top-level. Both are fine.
    assert len(deprecation_warnings) == 0
