"""Codex first-time-user #4: launcher must explain that the launching
repo is intentionally excluded and that Otto manages projects in
isolated git worktrees.

Source: mc-audit/findings.md, theme "First-run clarity (Codex
first-time-user)" — IMPORTANT row "Launcher doesn't explain what
Mission Control does; 'Managed root' looks like user's repo
disappeared".

A first-run user opens Mission Control inside (say) ``~/some-repo``,
sees the launcher panel pointing at ``~/otto-projects``, and panics:
"where did my repo go?" The fix is descriptive copy that answers
this question up-front. The exact wording should mention isolated
worktrees + an explicit pick-or-create CTA so the user knows what
to do next.

Test asserts: when the launcher is visible, both
* the subhead (``launcher-subhead``)
* the managed-root help (``launcher-managed-root-help``)
collectively mention "isolated" + "git worktree" + a "pick or create"
guidance phrase, so the explanation is on-screen regardless of which
panel the user is reading.

Run::

    OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 \\
        uv run pytest tests/browser/test_launcher_managed_root_explanation.py \\
        -m browser -p playwright -v
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.browser


def _projects_payload(*, projects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "launcher_enabled": True,
        "projects_root": "/tmp/managed-projects",
        "current": None,
        "projects": projects or [],
    }


def _install_route(page: Any, payload: dict[str, Any]) -> None:
    body = json.dumps(payload)
    page.route(
        "**/api/projects",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=body
        ),
    )


def _hydrate(page: Any, mc_backend: Any) -> None:
    page.goto(mc_backend.url, wait_until="networkidle")
    page.wait_for_selector('[data-mc-shell="ready"]', timeout=10_000)


def test_launcher_explains_isolated_worktrees(
    mc_backend: Any, page: Any
) -> None:
    """The launcher visible to a first-run user must mention isolated
    git worktrees so they understand why their current repo isn't
    showing up in the project list.
    """

    _install_route(page, _projects_payload())
    _hydrate(page, mc_backend)

    subhead = page.get_by_test_id("launcher-subhead")
    subhead.wait_for(state="visible", timeout=5_000)
    sub_text = (subhead.text_content() or "").lower()

    help_el = page.get_by_test_id("launcher-managed-root-help")
    help_el.wait_for(state="visible", timeout=5_000)
    help_text = (help_el.text_content() or "").lower()

    combined = f"{sub_text} :: {help_text}"

    # The phrase "isolated" + "worktree" must appear on the launcher
    # somewhere visible to the user. Splitting across subhead and help
    # is fine — both are above the fold on the launcher view.
    assert "isolated" in combined, (
        f"launcher copy must explain isolation; got subhead={sub_text!r} "
        f"help={help_text!r}"
    )
    assert "worktree" in combined, (
        f"launcher copy must mention worktrees so users know where their "
        f"projects live; got subhead={sub_text!r} help={help_text!r}"
    )


def test_launcher_explains_other_repos_unaffected(
    mc_backend: Any, page: Any
) -> None:
    """The user must see, on the launcher, that other repos on the
    machine are NOT touched by Otto — that's the panicked first-run
    question ("did Otto eat my current repo?"). The copy should answer
    this without making the user click into Settings or docs.
    """

    _install_route(page, _projects_payload())
    _hydrate(page, mc_backend)

    help_el = page.get_by_test_id("launcher-managed-root-help")
    help_el.wait_for(state="visible", timeout=5_000)
    help_text = (help_el.text_content() or "").lower()

    # Either "never touches" or "aren't affected" or "intentionally
    # excluded" — they all answer the same question. Require at least
    # one of them so the copy can evolve without thrashing tests.
    reassurances = ["never touches", "aren't affected", "are not affected", "intentionally excluded", "doesn't touch"]
    matched = [phrase for phrase in reassurances if phrase in help_text]
    assert matched, (
        f"launcher managed-root help must reassure users that other repos "
        f"are not touched; got {help_text!r}; expected one of {reassurances!r}"
    )


def test_launcher_provides_explicit_pick_or_create_guidance(
    mc_backend: Any, page: Any
) -> None:
    """The launcher must explicitly tell the user what to do next:
    pick an existing project or create one. Without this CTA the
    first-run experience is "blank workspace, no instructions".
    """

    _install_route(page, _projects_payload())
    _hydrate(page, mc_backend)

    help_el = page.get_by_test_id("launcher-managed-root-help")
    help_el.wait_for(state="visible", timeout=5_000)
    help_text = (help_el.text_content() or "").lower()

    # Either "pick or create" or "open or create" or similar — focus on
    # the dual CTA so users know both paths exist.
    assert "pick" in help_text or "open" in help_text, (
        f"launcher copy must guide the user to pick/open a project; got {help_text!r}"
    )
    assert "create" in help_text, (
        f"launcher copy must guide the user to create a project; got {help_text!r}"
    )
