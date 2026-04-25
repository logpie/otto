"""Smoke test for the browser test infrastructure.

This is the parity check that the whole Phase 3 fixture stack works:

  build_bundle (session) → mc_backend (free port + git project) →
  page navigation → hydration → no console errors

If this fails, fix it before adding any other browser tests. Per
``plan-mc-audit.md`` Phase 3 verification: "One example Playwright test passes
end-to-end against built bundle".

Notes for CI / Linux:
- ``pytest-playwright`` ships browser binaries via ``playwright install``.
  On macOS this works out of the box. On Linux CI you also need
  ``playwright install-deps`` for system libs (libnss3, libgbm, etc.).
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.browser


def test_smoke_app_shell_renders(mc_page: Any, console_must_be_clean: Any) -> None:
    """The SPA mounts and the document title reads "Otto Mission Control"."""

    assert_clean = console_must_be_clean(mc_page)

    # Title is set in otto/web/static/index.html — verifies the FastAPI
    # `index()` route served the file and Playwright loaded it.
    assert "Otto Mission Control" in mc_page.title()

    # React mounts into #root; mc_page already waited for at least one child.
    root_children = mc_page.evaluate("document.querySelector('#root').children.length")
    assert root_children >= 1, "expected React to render at least one element into #root"

    assert_clean()
