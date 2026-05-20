"""v5 runner submodules.

These modules are extracted from ``otto/v5_runner.py`` to keep the public
runner module under a manageable size. The public surface is still
``otto.v5_runner`` — every symbol moved here is re-exported from
``otto.v5_runner`` so external imports and ``monkeypatch.setattr`` on
``otto.v5_runner.X`` keep working.

Cross-module call discipline (to preserve patchability):

- Submodules do NOT import patched symbols by name at the top. They use
  ``from otto import v5_runner as _v5r`` and dereference attributes
  lazily (``_v5r.compile_flat_spec(...)``). That way, when a test does
  ``monkeypatch.setattr("otto.v5_runner.compile_flat_spec", fake)``, the
  submodule picks up the patched object at call time.
- The same indirection applies to symbols that move BETWEEN submodules
  but that tests may patch on ``otto.v5_runner``.
"""

from __future__ import annotations
