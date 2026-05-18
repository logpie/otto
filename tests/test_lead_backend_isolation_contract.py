"""Regression: lead.md must isolate BACKEND shared-but-feature-EXTENDED
concerns and forbid the foundation seeding feature-owned files.

Root cause (fcfix-024555, 2026-05-18, --tier modular iTracker): foundation
v5-99df862963d8 passed+merged, but ALL 4 feature children merge_blocked with
two faces of ONE class (reproduced from task_graph.json structured_reason):

  Face A — v5-d27f8d5eb5c5 `foundation_contract_write_blocked` on
  `backend/models.py` + `backend/schemas.py`: those were monolithic
  foundation_contracts (CHARTER check:semantic) that the Core-Issues feature
  HAD to extend with its entities' models/schemas. lead.md prescribed a
  composition/extension point for shared FRONTEND client state but never
  generalized it to the backend data-model/schema layer (the same hazard),
  so the architect emitted a monolith every feature must edit.

  Face B — v5-2de0b65257b2 & v5-f2ee6e943db5 BYTE-IDENTICAL
  `integration union incomplete: backend/routers/cycles/router.py missing
  line contributed by v5-99df862963d8`, v5-1dd64ee59a39 merge-conflict
  `backend/routers/workspaces/router.py`: the router `__init__.py` was a
  CORRECT auto-discovery loader, but the foundation also SEEDED 501-stub
  `backend/routers/<x>/router.py` for every (feature-owned) resource. lead.md
  forbade assigning a foundation_contract to a feature but stated NO converse
  rule forbidding the scaffold/foundation pre-creating feature-owned files.

Fix: lead.md now (a) generalizes the composition/extension-point rule to ANY
shared module every feature must EXTEND, explicitly the backend data-model &
schema layer (leaf-extensible package, not a monolithic foundation_contract),
and (b) forbids the scaffold/foundation pre-creating any feature-owned file
and requires auto-discovery/composition to tolerate an absent feature file.

These are content assertions on the architect contract (same regression
pattern as test_v5_ia_contract.py::
test_architect_prompt_keeps_concise_ia_contract_requirement). Before the fix
none of the pinned requirements existed in lead.md — the assertions were RED;
they are GREEN after the consistent-by-construction contract change. NOT
gate-weakening: the write/union/merge guards are unchanged; the architect is
constrained to emit a structure where they hold by construction.
"""

from __future__ import annotations

import re
from pathlib import Path

LEAD = Path("otto/prompts/lead.md")


def _text() -> str:
    return LEAD.read_text(encoding="utf-8")


def _norm(s: str) -> str:
    # collapse whitespace/newlines so wrapped prose matches as one string
    return re.sub(r"\s+", " ", s).strip().lower()


def test_lead_md_exists_and_has_registration_isolation() -> None:
    t = _text()
    assert "registration_isolation" in t
    assert "leaf_extension_globs" in t
    assert "shared_registry_files" in t


def test_face_a_backend_model_schema_must_be_leaf_extensible_not_monolith() -> None:
    """The composition/extension-point rule must be generalized beyond the
    frontend to the BACKEND data-model & schema layer: a shared
    models.py/schemas.py every feature extends must be a leaf-extensible
    package, NOT one monolithic foundation_contract file every feature edits.
    """
    n = _norm(_text())
    # explicitly NOT frontend-only / generalized to "extend"
    assert "not frontend-only" in n or "is not frontend-only" in n
    assert "every feature must extend" in n
    # names the backend data-model & schema layer concretely
    assert "backend data-model and schema layer" in n
    assert ("models.py" in n and "schemas.py" in n)
    # requires a leaf-extensible package, rejects the monolith
    assert "leaf-extensible package" in n
    assert "not one monolithic foundation_contract" in n or (
        "monolithic foundation_contract" in n and "leaf-extensible package" in n
    )
    # concrete shape: scaffold-owned aggregator/base + per-feature module
    assert "backend/models/__init__.py" in n
    assert ("backend/models/<feature>.py" in n) or ("models/<feature>.py" in n)


def test_face_b_foundation_must_not_seed_feature_owned_files() -> None:
    """The scaffold/foundation MUST NOT pre-create (seed) feature-owned files;
    auto-discovery/composition MUST tolerate an absent feature file. Seeding a
    feature-owned stub makes the foundation a contributor that fails the
    integration union guard / merge when the owning feature implements it.
    """
    n = _norm(_text())
    assert "must not pre-create" in n and "seed" in n
    # the prohibited set: feature_owned_paths or leaf_extension_globs matches
    assert "feature_owned_paths" in n and "leaf_extension_globs" in n
    # loaders must tolerate an absent feature file (app still boots)
    assert ("tolerate an absent feature file" in n) or (
        "absent feature file" in n and "still boots" in n
    )
    # ties to the real failure modes (union guard / merge)
    assert "integration union" in n or "union guard" in n
    assert "merge" in n
    # foundation owns only aggregators/loaders/base, never feature files
    assert "never feature-owned per-resource files" in n or (
        "foundation owns only" in n and "feature-owned" in n
    )


def test_shared_table_namespace_one_canonical_owner_no_dup_tablename() -> None:
    """fix10-092844 (2026-05-18, --tier modular iTracker): foundation + ALL 4
    features clean-merged (fix-7 working — per-feature model FILE ownership
    sound), but clean_deploy blocked `clean_deploy_ports_not_listening`
    because the integrated backend could not boot:
    `sqlalchemy.exc.InvalidRequestError: Table 'audit_logs' is already
    defined for this MetaData instance`. Independently audited:
    `backend/models/admin.py` (child v5-325c61544c42) AND
    `backend/models/auth.py` (child v5-4c3c1f87b728) each declared
    `__tablename__="audit_logs"` (and "webhooks") on the shared SQLAlchemy
    Base — each passed byte-isolated child-verify, collided only at
    integration. fix-7 isolated per-feature model FILES but was silent on the
    shared TABLE/ENTITY namespace inside them. lead.md must now require ONE
    canonical definition per table/ORM `__tablename__`, on one scaffold-owned
    declarative Base/metadata, with cross-cutting (≥2-feature) entities
    foundation-owned and imported (never re-declared) by features.

    Content assertions on the architect contract (same regression pattern as
    the Face A/B tests above). RED before this change (the phrases did not
    exist in lead.md), GREEN after. NOT gate-weakening: the write/union/
    merge/clean_deploy guards are unchanged; the architect is constrained to
    emit a decomposition where the ORM table namespace cannot collide by
    construction.
    """
    n = _norm(_text())
    # exactly one definition per table / ORM __tablename__
    assert "__tablename__" in n
    assert "exactly one definition" in n
    # one scaffold-owned shared declarative Base / single metadata
    assert "single metadata" in n
    assert ("shared orm declarative" in n) or ("declarative `base`/`metadata`" in n)
    # cross-cutting (≥2-feature) entities are foundation-owned, imported
    assert "cross-cutting" in n
    assert "read or write" in n
    assert ("never re-declare its `__tablename__`" in n) or (
        "import it and never re-declare" in n
    )
    # a feature must not redeclare a __tablename__ owned elsewhere
    assert "must not declare a `__tablename__` already owned" in n
    # ties to the real failure mode + the concrete fix10 evidence
    assert "is already defined for this metadata instance" in n
    assert "audit_logs" in n and "webhooks" in n
    # CHARTER must enumerate the single owner per shared entity
    assert "single foundation owner in charter" in n or (
        "canonical model" in n and "charter" in n
    )


def test_not_gate_weakening_guards_still_named_strict() -> None:
    """Sanity: the fix is consistent-by-construction, not gate relaxation —
    the contract still names the write-block / union / merge consequences as
    failures the architect must AVOID by structure, not gates to soften.
    """
    n = _norm(_text())
    assert "foundation_contract_write_blocked" in n
    # still instructs: never assign a foundation_contract/shared registry to a
    # feature (the original strict rule is preserved alongside the new converse)
    assert "never assign a foundation_contract or shared registry file to a feature" in n
