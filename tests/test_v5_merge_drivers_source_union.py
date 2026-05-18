"""Regression: generic 3-way additive-union driver for shared source files.

F2 root cause (v5-itracker-setupfix-224814, 2026-05-18): v5_merge_drivers
only had drivers for structured config files (package.json, tsconfig*, ...).
A shared SOURCE file (backend/app/auth.py) additively edited by two sibling
children hit `find_driver -> None` -> truly_blocked -> .otto/merge-conflicts
-> LLM repair. The integration_union_guard detected the dropped sibling line
but only emitted feedback (no deterministic re-apply), so the next child's
merge re-dropped it -> child_merge_retry whack-a-mole -> merge_blocked ->
clean_deploy/journeys never ran.

D1 fix: a generic deterministic 3-way merge (`git merge-file --diff3`) as
the fallback when no structured driver matches. Every non-overlapping change
from EITHER side is kept (both siblings' distinct additions survive); a true
overlap (both sides modify the SAME region differently) returns None so the
caller treats it as an honest conflict — NEVER silently unions divergent
edits (not gate-weakening).
"""

from __future__ import annotations

from otto.v5_merge_drivers import find_driver, merge_source_additive_union


def test_two_siblings_distinct_additions_both_kept() -> None:
    base = "import os\n\n\ndef handler():\n    return 1\n"
    ours = "import os\nfrom app.models import User\n\n\ndef handler():\n    return 1\n"
    theirs = "import os\nfrom app.models import Team\n\n\ndef handler():\n    return 1\n"
    merged = merge_source_additive_union(ours, theirs, base)
    assert merged is not None, "non-overlapping sibling additions must union cleanly"
    assert "from app.models import User" in merged
    assert "from app.models import Team" in merged
    assert "<<<<<<<" not in merged and ">>>>>>>" not in merged


def test_divergent_same_line_modification_blocks() -> None:
    base = "TIMEOUT = 10\n"
    ours = "TIMEOUT = 30\n"
    theirs = "TIMEOUT = 60\n"
    # Both sides changed the SAME base line differently -> honest conflict,
    # must NOT be silently unioned.
    assert merge_source_additive_union(ours, theirs, base) is None


def test_one_side_adds_other_unchanged() -> None:
    base = "a = 1\nb = 2\n"
    ours = "a = 1\nb = 2\nc = 3\n"
    theirs = "a = 1\nb = 2\n"
    merged = merge_source_additive_union(ours, theirs, base)
    assert merged is not None
    assert "c = 3" in merged


def test_add_add_no_common_base_blocks() -> None:
    # add/add: file created independently on both branches, NO common
    # ancestor. The two versions are divergent complete contents, not
    # additive contributions to a shared file — unioning would silently
    # merge divergent definitions. Must block (None) so the caller treats
    # it as an honest conflict. The genuine F2 case always HAS a base
    # (shared scaffold file the children add to).
    assert merge_source_additive_union("export const A = 1\n", "export const B = 2\n", "") is None
    assert merge_source_additive_union("a = 2\n", "a = 3\n", None) is None


def test_existing_structured_drivers_unaffected() -> None:
    # D1 is a fallback only; exact-basename structured drivers keep
    # precedence (no regression to package.json/.gitignore/etc.).
    assert find_driver("package.json") is not None
    assert find_driver(".gitignore") is not None
    # A plain source file still has no *named* driver — the generic union
    # is wired as the v5_branching Layer-3 fallback, not via find_driver.
    assert find_driver("backend/app/auth.py") is None
