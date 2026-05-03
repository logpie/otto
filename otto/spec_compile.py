"""Structured-spec compiler for the unified intent-to-product pipeline.

This module is the **compile** stage of the new 4-stage pipeline (compile,
build, audit, render) sketched in
`/root/.claude/plans/here-is-a-draft-quirky-pudding.md` and the design doc
on `cc-i2p-2`.

It coexists with `otto/spec.py` (the markdown spec gate). They are
deliberately separate modules:

* `otto/spec.py` — markdown `spec.md` for `otto build --spec`. Sections
  Intent / Must Have / Must NOT Have Yet / Success Criteria.
* `otto/spec_compile.py` (this file) — structured `Spec` JSON for
  `otto run`. Slices, typed checks, structure decisions, amendments.

The two converge in Phase C once the bench validates the new pipeline.

Key invariants:

* `Spec` JSON is the single artifact handed between stages.
* Once approved, every change appends an `Amendment(reason, actor, ts,
  diff_sha256_before, diff_sha256_after)`. Idempotent rewrites are
  allowed (same content, no amendment); content change without a reason
  is rejected. This carries over the immutability semantics from
  `codex-i2p`'s oracle-plan persistence.
* Validation is schema-only: per-`project_kind` JSON schema in
  `otto/spec_schemas/<kind>.json`. "Concrete enough" reduces to
  schema-pass.
* `BrowserJourney` v1 = `command: list[str]` + `evidence_globs:
  list[str]`. Defer structured `steps: list[BrowserStep]` to a follow-up
  after Microfeed bench validation (matches today's
  `codex-i2p/otto/oracles.py` pattern — no Playwright session in
  checks).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from otto.spec_warnings import ValidationWarning, WarningCollector

if TYPE_CHECKING:
    from otto.budget import RunBudget

logger = logging.getLogger("otto.spec_compile")

SCHEMA_VERSION = 1
SPEC_FILENAME = "spec.json"
COMPILE_PROMPT = "compile-spec.md"
PROJECT_KINDS: tuple[str, ...] = ("webapp", "cli", "library", "api")
SCHEMAS_DIR = Path(__file__).parent / "spec_schemas"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PytestCheck:
    """Run a pytest selector inside the project."""
    kind: Literal["pytest"] = "pytest"
    selector: str = ""              # e.g. "tests/test_foo.py::test_bar"
    timeout_s: int = 300


@dataclass(frozen=True)
class RepoTestCheck:
    """Invoke the repo's own test command (npm test, cargo test, etc.)."""
    kind: Literal["repo_test"] = "repo_test"
    command: tuple[str, ...] = ()   # e.g. ("npm", "test")
    timeout_s: int = 600


@dataclass(frozen=True)
class ApiProbe:
    """HTTP probe against a running app."""
    kind: Literal["api_probe"] = "api_probe"
    method: str = "GET"
    path: str = "/"
    expect_status: int = 200
    expect_body_contains: str = ""
    timeout_s: int = 30


@dataclass(frozen=True)
class BrowserJourney:
    """v1 contract: subprocess + evidence-glob.

    Mirrors `codex-i2p/otto/oracles.py`'s browser_journey: we shell out
    to a runner (typically a Playwright pytest), then glob the configured
    artifact paths. The check executor does not own the Playwright
    session lifecycle; the runner does.
    """
    kind: Literal["browser_journey"] = "browser_journey"
    command: tuple[str, ...] = ()                   # e.g. ("pytest", "tests/browser/test_x.py")
    evidence_globs: tuple[str, ...] = ()            # e.g. ("evidence/*.png", "evidence/*.webm")
    timeout_s: int = 600


@dataclass(frozen=True)
class StateInvariant:
    """Assert a Python expression against a running app's state."""
    kind: Literal["state_invariant"] = "state_invariant"
    description: str = ""
    expression: str = ""                            # evaluated with `eval` against probe results
    timeout_s: int = 30


CheckKind = (
    PytestCheck
    | RepoTestCheck
    | ApiProbe
    | BrowserJourney
    | StateInvariant
)

_CHECK_TYPES: dict[str, type] = {
    "pytest": PytestCheck,
    "repo_test": RepoTestCheck,
    "api_probe": ApiProbe,
    "browser_journey": BrowserJourney,
    "state_invariant": StateInvariant,
}


@dataclass
class Slice:
    """One vertical slice — a build agent owns it end-to-end."""
    id: str
    title: str
    tasks: list[str] = field(default_factory=list)      # plain-language task list
    deps: list[str] = field(default_factory=list)       # other slice ids
    owned_paths: list[str] = field(default_factory=list)  # globs the agent may *modify*
    checks: list[CheckKind] = field(default_factory=list)


@dataclass
class StructureDecisions:
    """Project-kind-specific structure payload validated by JSON schema.

    For v1, an opaque `dict` payload validated against
    `spec_schemas/<project_kind>.json`. After bench feedback stabilises
    schemas, refactor into typed dataclasses (one per kind) — see open
    issues in the plan.
    """
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Amendment:
    """Audit record for any post-approval edit to the spec.

    Idempotent rewrites (no content change) skip `append_amendment`. Real
    edits MUST go through `append_amendment(spec, reason, actor)` — bare
    `persist_spec` on a content change without a fresh amendment is
    rejected.

    `diff_sha256_before` / `diff_sha256_after` hash the canonical-form
    spec content (see `_canonical_dump`) so two runs that produce the
    same bytes round-trip equal.

    v2.2 (`trigger_event_id`, `tier`): every agent-driven amendment must
    cite the journal event that motivated it (a scope warning, a check
    failure, a build error). User-initiated amendments may omit it.
    `tier` records which tier the amendment touched (1/2/3); audit
    review uses this to flag suspicious chains.
    """
    reason: str
    actor: str
    ts: str                                          # ISO-8601 UTC
    diff_sha256_before: str
    diff_sha256_after: str
    trigger_event_id: str = ""                       # v2.2: journal event reference
    tier: int = 0                                    # v2.2: 1, 2, or 3 (0 = unspecified/legacy)


@dataclass
class Spec:
    """Top-level structured product spec.

    `intent` is the user's verbatim intent. `non_goals` and `done_means`
    are the unified replacements for codex-feats' `non_goals` /
    `done_means` ProductContract fields and feed the spec-review UI.

    v2.2 (tiered mutability): `intent_hash` is the SHA-256 of
    `intent` at session start. Persist enforces it never changes
    without an explicit user override. See `otto.spec_amend` for the
    full tiered amendment API.
    """
    schema_version: int = SCHEMA_VERSION
    intent: str = ""
    intent_hash: str = ""  # tier-1 invariant; "" = legacy spec
    project_kind: str = "webapp"
    structure: StructureDecisions = field(default_factory=StructureDecisions)
    slices: list[Slice] = field(default_factory=list)
    cross_slice_checks: list[CheckKind] = field(default_factory=list)
    shared_scaffold: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    done_means: list[str] = field(default_factory=list)
    amendments: list[Amendment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _check_to_dict(check: CheckKind) -> dict[str, Any]:
    return dataclasses.asdict(check)


def _check_from_dict(
    payload: dict[str, Any],
    *,
    collector: WarningCollector | None = None,
    path: str = "",
) -> CheckKind | None:
    """Permissively parse one check payload.

    Returns:
        A CheckKind instance, or None if the payload was so malformed
        that no sensible kind could be selected. In strict-mode callers
        (e.g., test_unknown_check_kind_raises), use the legacy
        `_check_from_dict_strict` wrapper.
    """
    if not isinstance(payload, dict):
        if collector is not None:
            collector.add(
                code="spec.coerce.check",
                path=path,
                message=f"check entry is {type(payload).__name__}, not dict; dropped",
            )
        return None
    kind = str(payload.get("kind") or "").strip()
    cls = _CHECK_TYPES.get(kind)
    if cls is None:
        if collector is not None:
            collector.add(
                code="spec.coerce.unknown_kind",
                path=path,
                message=f"unknown check kind {kind!r}; dropped",
            )
        return None
    fields = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}
    for name in fields:
        if name in payload:
            value = payload[name]
            # Re-tuplify list-typed frozen dataclass fields so equality
            # round-trips without surprising callers who construct from a
            # list and compare against a deserialised tuple.
            if isinstance(value, list) and name in {"command", "evidence_globs"}:
                value = tuple(value)
            kwargs[name] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        if collector is not None:
            collector.add(
                code="spec.coerce.check",
                path=path,
                message=f"{kind!r} check payload invalid ({exc}); dropped",
            )
        return None


def _check_from_dict_strict(payload: dict[str, Any]) -> CheckKind:
    """Strict back-compat wrapper. Raises SpecValidationError on any failure.

    Kept only for tests that explicitly assert strictness; the v2.1
    permissive path uses `_check_from_dict(..., collector=...)` directly.
    """
    if not isinstance(payload, dict):
        raise SpecValidationError(f"check entry must be a dict, got {type(payload).__name__}")
    kind = str(payload.get("kind") or "").strip()
    cls = _CHECK_TYPES.get(kind)
    if cls is None:
        raise SpecValidationError(f"unknown check kind: {kind!r}")
    fields = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}
    for name in fields:
        if name in payload:
            value = payload[name]
            if isinstance(value, list) and name in {"command", "evidence_globs"}:
                value = tuple(value)
            kwargs[name] = value
    return cls(**kwargs)


def compute_intent_hash(intent: str) -> str:
    """Stable SHA-256 of the intent's verbatim bytes (tier-1 lock).

    The intent_hash is computed once at session start and verified on
    every persist_spec call. Any divergence from the locked hash is a
    tampering signal — the run is blocked until a human overrides.
    """
    return hashlib.sha256(intent.encode("utf-8")).hexdigest()


def lock_intent(spec: Spec) -> Spec:
    """Return a Spec with `intent_hash` stamped from `intent`.

    Idempotent: if `spec.intent_hash` already matches `intent`, returns
    the spec unchanged (no `dataclasses.replace`). Used at compile time
    to seal the bedrock layer; existing-spec callers don't need to call
    this — `parse_spec` populates intent_hash on load.
    """
    expected = compute_intent_hash(spec.intent) if spec.intent else ""
    if spec.intent_hash == expected:
        return spec
    return dataclasses.replace(spec, intent_hash=expected)


def spec_to_dict(spec: Spec) -> dict[str, Any]:
    """Convert a `Spec` to a plain dict for JSON serialisation."""
    return {
        "schema_version": spec.schema_version,
        "intent": spec.intent,
        "intent_hash": spec.intent_hash,
        "project_kind": spec.project_kind,
        "structure": {"payload": dict(spec.structure.payload)},
        "slices": [
            {
                "id": s.id,
                "title": s.title,
                "tasks": list(s.tasks),
                "deps": list(s.deps),
                "owned_paths": list(s.owned_paths),
                "checks": [_check_to_dict(c) for c in s.checks],
            }
            for s in spec.slices
        ],
        "cross_slice_checks": [_check_to_dict(c) for c in spec.cross_slice_checks],
        "shared_scaffold": list(spec.shared_scaffold),
        "non_goals": list(spec.non_goals),
        "done_means": list(spec.done_means),
        "amendments": [dataclasses.asdict(a) for a in spec.amendments],
    }


def parse_spec(data: Any) -> tuple[Spec, list[ValidationWarning]]:
    """Permissively parse a Spec from a JSON-decoded payload.

    v2.1 design (docs/intent-to-product-v2-plan.md):

    - **Coerces** obvious mistakes (non-list slices wrapped, non-dict
      amendments synthesized, unknown check kinds dropped).
    - **Warns** for departures from recommended shape. Warnings are
      returned alongside the Spec so callers can surface them in the
      journal/proof packet.
    - **Hard-rejects** ONLY on truly unusable input: not a dict, or no
      slices AND no intent AND no structure (literally nothing to do).

    The compile-stage cheap-fail class (R3, R5, R8, R17, R22, R26) is
    closed by this function: malformed agent output now produces a
    workable Spec + warnings instead of slice-blocking exceptions.
    """
    collector = WarningCollector()

    if not isinstance(data, dict):
        raise SpecValidationError(
            f"spec must be a JSON object, got {type(data).__name__}"
        )

    # ---- structure.payload ----
    raw_structure = data.get("structure")
    if isinstance(raw_structure, dict):
        structure_payload = raw_structure.get("payload")
        if not isinstance(structure_payload, dict):
            if structure_payload is not None:
                collector.add(
                    code="spec.coerce.field",
                    path="structure.payload",
                    message=f"structure.payload should be a dict, got {type(structure_payload).__name__}; using empty dict",
                    coerced_to="{}",
                )
            structure_payload = {}
    else:
        if raw_structure is not None:
            collector.add(
                code="spec.coerce.field",
                path="structure",
                message=f"structure should be a dict, got {type(raw_structure).__name__}; using defaults",
            )
        structure_payload = {}

    # ---- slices ----
    raw_slices = data.get("slices")
    if raw_slices is None:
        slices_data: list[Any] = []
    elif isinstance(raw_slices, list):
        slices_data = raw_slices
    elif isinstance(raw_slices, dict):
        # Single slice declared at top level → wrap.
        collector.add(
            code="spec.coerce.field",
            path="slices",
            message="slices was a dict; wrapped in a single-element list",
            coerced_to="[<dict>]",
        )
        slices_data = [raw_slices]
    else:
        collector.add(
            code="spec.coerce.field",
            path="slices",
            message=f"slices should be a list, got {type(raw_slices).__name__}; using empty list",
            coerced_to="[]",
        )
        slices_data = []

    slices: list[Slice] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(slices_data):
        if not isinstance(entry, dict):
            collector.add(
                code="spec.coerce.slice",
                path=f"slices[{index}]",
                message=f"slice entry is {type(entry).__name__}, not dict; dropped",
            )
            continue

        slice_id = _coerce_slice_id(
            entry.get("id"), index=index, seen=seen_ids, collector=collector
        )
        seen_ids.add(slice_id)

        checks: list[CheckKind] = []
        for c_index, c_payload in enumerate(entry.get("checks") or []):
            check = _check_from_dict(
                c_payload,
                collector=collector,
                path=f"slices[{index}].checks[{c_index}]",
            )
            if check is not None:
                checks.append(check)

        slices.append(Slice(
            id=slice_id,
            title=str(entry.get("title") or ""),
            tasks=[str(t) for t in (entry.get("tasks") or [])],
            deps=[str(d) for d in (entry.get("deps") or [])],
            owned_paths=[str(p) for p in (entry.get("owned_paths") or [])],
            checks=checks,
        ))

    # ---- amendments (coerce non-dict to synthesized record) ----
    amendments: list[Amendment] = []
    raw_amendments = data.get("amendments") or []
    if not isinstance(raw_amendments, list):
        collector.add(
            code="spec.coerce.field",
            path="amendments",
            message=f"amendments should be a list, got {type(raw_amendments).__name__}; using empty list",
            coerced_to="[]",
        )
        raw_amendments = []
    for index, entry in enumerate(raw_amendments):
        if isinstance(entry, dict):
            amendments.append(Amendment(
                reason=str(entry.get("reason") or ""),
                actor=str(entry.get("actor") or ""),
                ts=str(entry.get("ts") or ""),
                diff_sha256_before=str(entry.get("diff_sha256_before") or ""),
                diff_sha256_after=str(entry.get("diff_sha256_after") or ""),
                trigger_event_id=str(entry.get("trigger_event_id") or ""),
                tier=int(entry.get("tier") or 0),
            ))
        else:
            collector.add(
                code="spec.coerce.amendment",
                path=f"amendments[{index}]",
                message=f"amendment entry is {type(entry).__name__}, not dict; coerced",
                coerced_to=f"Amendment(reason={str(entry)[:40]!r}, ...)",
            )
            amendments.append(Amendment(
                reason=str(entry)[:200],
                actor="parser-coerced",
                ts=_iso_now(),
                diff_sha256_before="",
                diff_sha256_after="",
            ))

    # ---- cross-slice checks ----
    cross_slice_checks: list[CheckKind] = []
    for index, c_payload in enumerate(data.get("cross_slice_checks") or []):
        check = _check_from_dict(
            c_payload, collector=collector, path=f"cross_slice_checks[{index}]"
        )
        if check is not None:
            cross_slice_checks.append(check)

    # ---- project_kind: open enum (warn but accept) ----
    project_kind_raw = data.get("project_kind")
    if project_kind_raw is None:
        project_kind = "webapp"
    else:
        project_kind = str(project_kind_raw).strip() or "webapp"
        if project_kind not in PROJECT_KINDS:
            collector.add(
                code="spec.coerce.project_kind",
                path="project_kind",
                message=f"project_kind {project_kind!r} not in {PROJECT_KINDS}; treated as free-form",
            )

    intent = str(data.get("intent") or "")

    # ---- hard reject: truly unusable input ----
    if not intent.strip() and not slices and not structure_payload:
        raise SpecValidationError(
            "spec is empty: no intent, no slices, no structure — nothing to build"
        )

    # intent_hash: preserve what's on disk verbatim. If absent, leave
    # empty — back-compat specs round-trip identically. New specs
    # explicitly call `lock_intent(spec)` (compile_spec does this) to
    # stamp the bedrock layer.
    intent_hash = str(data.get("intent_hash") or "").strip()

    spec = Spec(
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        intent=intent,
        intent_hash=intent_hash,
        project_kind=project_kind,
        structure=StructureDecisions(payload=dict(structure_payload)),
        slices=slices,
        cross_slice_checks=cross_slice_checks,
        shared_scaffold=[str(p) for p in (data.get("shared_scaffold") or [])],
        non_goals=[str(g) for g in (data.get("non_goals") or [])],
        done_means=[str(g) for g in (data.get("done_means") or [])],
        amendments=amendments,
    )
    return spec, list(collector.warnings)


def spec_from_dict(data: dict[str, Any]) -> Spec:
    """Permissive inverse of `spec_to_dict` — drops warnings.

    Kept for backward compatibility with callers that don't care about
    warnings. The compile/persist path uses `parse_spec` directly to
    surface warnings into the journal and proof packet.
    """
    spec, _warnings = parse_spec(data)
    return spec


_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _coerce_slice_id(
    raw: Any,
    *,
    index: int,
    seen: set[str],
    collector: WarningCollector,
) -> str:
    """Slugify and disambiguate a slice id.

    v2.1: accept any non-empty unique string. Internally slugify for
    path-safe use. Empty / non-string values get a synthesized id.
    """
    text = str(raw or "").strip()
    if not text:
        new_id = f"slice_{index}"
        collector.add(
            code="spec.coerce.slice_id",
            path=f"slices[{index}].id",
            message=f"slice id missing; synthesized {new_id!r}",
            coerced_to=new_id,
        )
        return new_id
    slug = _SLUG_RE.sub("-", text.lower()).strip("-_")
    if not slug:
        slug = f"slice_{index}"
    if slug != text:
        collector.add(
            code="spec.coerce.slice_id",
            path=f"slices[{index}].id",
            message=f"slice id {text!r} slugified to {slug!r}",
            coerced_to=slug,
        )
    candidate = slug
    counter = 1
    while candidate in seen:
        counter += 1
        candidate = f"{slug}-{counter}"
    if candidate != slug:
        collector.add(
            code="spec.coerce.duplicate_id",
            path=f"slices[{index}].id",
            message=f"duplicate slice id {slug!r}; renamed to {candidate!r}",
            coerced_to=candidate,
        )
    return candidate


def _canonical_dump(spec: Spec) -> str:
    """Stable JSON form for hashing — excludes the `amendments` list itself."""
    payload = spec_to_dict(spec)
    payload.pop("amendments", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def spec_content_sha256(spec: Spec) -> str:
    """Hash the spec's content (everything except amendments)."""
    return hashlib.sha256(_canonical_dump(spec).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of `validate_spec`.

    v2.1 design: most former "errors" are now `warnings` (informational,
    non-blocking). `errors` is reserved for genuinely unusable specs:
    dep cycles (would loop forever), hash-chain breaks (tampering),
    truly empty input.
    """
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SpecValidationError(ValueError):
    """Raised when a Spec is malformed (parse-time)."""


_SLICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _load_kind_schema(project_kind: str) -> dict[str, Any] | None:
    path = SCHEMAS_DIR / f"{project_kind}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to read %s: %s", path, exc)
        return None


def _validate_against_schema(payload: dict[str, Any], schema: dict[str, Any], path: str = "") -> list[str]:
    """Tiny schema validator — supports the subset we use.

    Skips the `jsonschema` package dependency on purpose; the schemas we
    ship are small and only use a handful of keywords. If we ever want
    drafts/refs, swap this for `jsonschema.validate`.
    """
    errors: list[str] = []
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(payload, dict):
            errors.append(f"{path or 'value'}: expected object, got {type(payload).__name__}")
            return errors
        for required in schema.get("required") or []:
            if required not in payload:
                errors.append(f"{path or 'value'}: missing required field {required!r}")
        properties = schema.get("properties") or {}
        for key, sub_schema in properties.items():
            if key in payload:
                errors.extend(_validate_against_schema(payload[key], sub_schema, f"{path}.{key}" if path else key))
    elif schema_type == "array":
        if not isinstance(payload, list):
            errors.append(f"{path}: expected array, got {type(payload).__name__}")
            return errors
        if "minItems" in schema and len(payload) < int(schema["minItems"]):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s); got {len(payload)}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(payload):
                errors.extend(_validate_against_schema(item, item_schema, f"{path}[{i}]"))
    elif schema_type == "string":
        if not isinstance(payload, str):
            errors.append(f"{path}: expected string")
        elif schema.get("minLength") is not None and len(payload) < int(schema["minLength"]):
            errors.append(f"{path}: shorter than minLength={schema['minLength']}")
    elif schema_type == "integer":
        if not isinstance(payload, int) or isinstance(payload, bool):
            errors.append(f"{path}: expected integer")
    return errors


def validate_spec(spec: Spec, *, strict: bool = False) -> ValidationResult:
    """Validate a Spec.

    v2.1 (default, `strict=False`): every former error becomes a
    warning EXCEPT genuinely unusable specs (dep cycles). The Spec is
    valid as long as no cycles exist; everything else is informational.

    `strict=True`: legacy v1 mode — warnings become errors. Kept for
    tests that explicitly assert v1 strictness; do not use in
    production paths.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ---- always-error: dep cycles (would loop forever) ----
    cycles = _detect_dep_cycles(spec.slices)
    errors.extend(cycles)

    # ---- former-errors-now-warnings ----
    if spec.project_kind not in PROJECT_KINDS:
        warnings.append(f"project_kind {spec.project_kind!r} not in {PROJECT_KINDS} (free-form accepted)")

    if not spec.intent.strip():
        warnings.append("intent is empty")

    if not spec.slices:
        warnings.append("spec declares no slices")

    seen_ids: set[str] = set()
    for slice_ in spec.slices:
        if not _SLICE_ID_RE.match(slice_.id):
            warnings.append(f"slice id {slice_.id!r} does not match recommended pattern {_SLICE_ID_RE.pattern}")
        if slice_.id in seen_ids:
            warnings.append(f"duplicate slice id: {slice_.id!r}")
        seen_ids.add(slice_.id)
        if not slice_.title.strip():
            warnings.append(f"slice {slice_.id!r}: title is empty")
        if not slice_.checks:
            warnings.append(f"slice {slice_.id!r}: no checks declared (vacuously passes)")

    for slice_ in spec.slices:
        for dep in slice_.deps:
            if dep not in seen_ids:
                warnings.append(f"slice {slice_.id!r}: dep {dep!r} not in spec")

    schema = _load_kind_schema(spec.project_kind)
    if schema is not None:
        warnings.extend(_validate_against_schema(spec.structure.payload, schema, path="structure.payload"))

    if strict:
        # Legacy v1 mode: warnings become errors.
        errors.extend(warnings)
        warnings = []

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _detect_dep_cycles(slices: list[Slice]) -> list[str]:
    """Return human-readable cycle errors; empty when DAG."""
    by_id = {s.id: s for s in slices}
    visiting: set[str] = set()
    visited: set[str] = set()
    errors: list[str] = []

    def walk(node: str, path: list[str]) -> None:
        if node in visiting:
            cycle = path[path.index(node):] + [node]
            errors.append(f"slice dep cycle: {' -> '.join(cycle)}")
            return
        if node in visited or node not in by_id:
            return
        visiting.add(node)
        for dep in by_id[node].deps:
            walk(dep, path + [node])
        visiting.discard(node)
        visited.add(node)

    for slice_ in slices:
        walk(slice_.id, [])
    return errors


# ---------------------------------------------------------------------------
# Persistence — immutability semantics
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_amendment(spec: Spec, *, reason: str, actor: str, prior_sha256: str | None = None) -> Spec:
    """Return a new `Spec` with an amendment appended.

    Use this whenever the spec content changes after the initial write.
    `prior_sha256` is the content hash before the user's edit; if absent
    we use `spec.amendments[-1].diff_sha256_after` (or empty for first
    edit). `reason` must be non-empty.
    """
    if not reason or not reason.strip():
        raise ValueError("amendment reason must be non-empty")
    if not actor or not actor.strip():
        raise ValueError("amendment actor must be non-empty")

    after_hash = spec_content_sha256(spec)
    if prior_sha256 is None:
        prior_sha256 = spec.amendments[-1].diff_sha256_after if spec.amendments else ""

    amended = dataclasses.replace(
        spec,
        amendments=[
            *spec.amendments,
            Amendment(
                reason=reason.strip(),
                actor=actor.strip(),
                ts=_iso_now(),
                diff_sha256_before=prior_sha256,
                diff_sha256_after=after_hash,
            ),
        ],
    )
    return amended


def persist_spec(
    spec: Spec,
    path: Path,
    *,
    allow_initial: bool = False,
    user_override_intent: bool = False,
) -> Path:
    """Write `spec.json` to `path`.

    Enforces immutability:

    * **Tier-1 (v2.2)**: `spec.intent` and `spec.intent_hash` are
      bedrock. If the on-disk spec has a non-empty intent_hash, the new
      spec MUST match — both the literal intent and its hash. The only
      escape is `user_override_intent=True` (a deliberate user-driven
      reset). Any other mismatch raises `SpecValidationError` and the
      run is blocked.
    * If `path` does not exist, write directly. The first write is the
      initial spec; pass `allow_initial=True` (the compile path does so).
    * If `path` exists and the on-disk content (excluding amendments) is
      byte-equal to `spec`'s content, the write is idempotent — no-op.
    * If `path` exists and content differs, the new spec MUST carry at
      least one more amendment than the on-disk version, and that
      amendment's `diff_sha256_before` must equal the on-disk content
      hash. Otherwise we raise `SpecValidationError`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        if not allow_initial:
            raise SpecValidationError(
                f"spec {path} does not exist; call persist_spec(..., allow_initial=True) for the first write"
            )
        path.write_text(json.dumps(spec_to_dict(spec), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    if allow_initial:
        # Initial-write mode overrides immutability. Use case from the
        # compile path: the agent writes spec.json itself per the
        # compile-spec prompt, then compile_spec parses + canonicalizes
        # and calls persist_spec(allow_initial=True). The on-disk file
        # exists (agent wrote it), but content may differ in formatting
        # — that's a no-op-equivalent canonicalization, not an
        # amendment-requiring change. Without this branch, fresh runs
        # cheap-fail on flaky agent JSON formatting.
        path.write_text(json.dumps(spec_to_dict(spec), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    on_disk_data = json.loads(path.read_text(encoding="utf-8"))
    on_disk = spec_from_dict(on_disk_data)

    # ---- Tier-1 (BEDROCK): intent and intent_hash are immutable ----
    # If the on-disk spec stamped a hash, the new spec must match —
    # both the literal intent and the hash. The only escape is an
    # explicit user override (`user_override_intent=True`) that the
    # spec-review gate flips deliberately. v2.2 design: see
    # docs/intent-to-product-v2.md "Safe mutability".
    if on_disk.intent_hash and not user_override_intent:
        if spec.intent != on_disk.intent:
            raise SpecValidationError(
                "tier-1 violation: intent changed without user override "
                f"(was {on_disk.intent[:60]!r}..., now {spec.intent[:60]!r}...). "
                "Pass user_override_intent=True via the spec-review gate."
            )
        if spec.intent_hash and spec.intent_hash != on_disk.intent_hash:
            raise SpecValidationError(
                "tier-1 violation: intent_hash changed without user override "
                f"(was {on_disk.intent_hash[:16]}..., now {spec.intent_hash[:16]}...). "
                "Pass user_override_intent=True via the spec-review gate."
            )

    on_disk_hash = spec_content_sha256(on_disk)
    new_hash = spec_content_sha256(spec)

    if on_disk_hash == new_hash:
        # Idempotent — accept any (re)write that doesn't change content.
        # This matches codex-i2p oracle-plan semantics: rewriting an
        # identical plan is a no-op.
        return path

    if len(spec.amendments) <= len(on_disk.amendments):
        raise SpecValidationError(
            "spec content changed but no new amendment was appended; "
            "use append_amendment(spec, reason=..., actor=...)"
        )
    latest = spec.amendments[-1]
    if latest.diff_sha256_before != on_disk_hash:
        raise SpecValidationError(
            "amendment.diff_sha256_before does not match on-disk content hash; "
            f"expected {on_disk_hash!r}, got {latest.diff_sha256_before!r}"
        )
    if latest.diff_sha256_after != new_hash:
        raise SpecValidationError(
            "amendment.diff_sha256_after does not match new content hash; "
            f"expected {new_hash!r}, got {latest.diff_sha256_after!r}"
        )

    path.write_text(json.dumps(spec_to_dict(spec), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_spec(path: Path) -> Spec:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return spec_from_dict(data)


# ---------------------------------------------------------------------------
# Compile
# ---------------------------------------------------------------------------


_SPEC_JSON_RE = re.compile(
    r"<spec_json>\s*(?P<body>\{.*?\})\s*</spec_json>",
    re.DOTALL,
)


def _extract_spec_json(text: str) -> dict[str, Any]:
    """Pull the spec payload out of the compile agent's final message.

    The compile prompt instructs the agent to wrap its JSON output in
    `<spec_json>...</spec_json>` so we don't have to fight markdown
    fences. Falls back to a best-effort `{...}` scan on parse failure.
    """
    match = _SPEC_JSON_RE.search(text)
    if match:
        body = match.group("body")
    else:
        # Tolerant fallback: take the first balanced top-level object.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise SpecValidationError("compile agent did not emit a spec JSON object")
        body = text[start : end + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SpecValidationError(f"compile agent emitted invalid JSON: {exc}") from exc


async def compile_spec(
    intent: str,
    project_dir: Path,
    run_dir: Path,
    config: dict[str, object],
    *,
    project_kind: str = "webapp",
    budget: "RunBudget | None" = None,
) -> Spec:
    """Run the compile agent once and return the structured `Spec`.

    Mirrors `otto.spec.run_spec_agent` plumbing — same prompt rendering,
    same `make_agent_options(..., agent_type="spec")`, same per-version
    log subdir convention. Writes `<run_dir>/spec.json` with the initial
    amendment-free Spec.

    `AgentCallError` from budget-exhaustion or timeout propagates
    UNWRAPPED so callers can write a paused checkpoint, matching the
    contract in `otto/spec.py:259`.
    """
    from otto.agent import make_agent_options, run_agent_with_timeout
    from otto.config import get_spec_timeout
    from otto.observability import save_rendered_prompt, sha256_text, update_input_provenance
    from otto.prompts import render_prompt

    if project_kind not in PROJECT_KINDS:
        raise ValueError(f"project_kind must be one of {PROJECT_KINDS}; got {project_kind!r}")

    run_dir.mkdir(parents=True, exist_ok=True)
    spec_path = run_dir / SPEC_FILENAME

    prompt = render_prompt(
        COMPILE_PROMPT,
        intent=intent,
        spec_path=str(spec_path),
        project_context=f"project_kind={project_kind}",
    )
    prompt_entry = save_rendered_prompt(
        run_dir.parent / "prompts",
        template=COMPILE_PROMPT,
        rendered_text=prompt,
    )
    update_input_provenance(
        run_dir.parent,
        intent={
            "source": str(config.get("_intent_source") or "cli-argument"),
            "fallback_reason": str(config.get("_intent_fallback_reason") or ""),
            "resolved_text": intent,
            "sha256": sha256_text(intent),
        },
        spec={"source": str(config.get("_spec_source") or "compile-agent"), "path": str(spec_path), "sha256": ""},
        prompts=[prompt_entry],
    )

    options = make_agent_options(project_dir, config, agent_type="spec")
    spec_cap = get_spec_timeout(config)
    timeout: int = min(budget.for_call(), spec_cap) if budget is not None else spec_cap

    log_subdir = run_dir / "compile-agent"
    log_subdir.mkdir(parents=True, exist_ok=True)

    text, _cost, _session_id, _breakdown = await run_agent_with_timeout(
        prompt,
        options,
        log_dir=log_subdir,
        phase_name="SPEC_COMPILE",
        phase_label="compile",
        timeout=timeout,
        project_dir=project_dir,
    )

    payload = _extract_spec_json(text)
    if "intent" not in payload or not str(payload["intent"]).strip():
        payload["intent"] = intent
    if "project_kind" not in payload:
        payload["project_kind"] = project_kind

    spec = spec_from_dict(payload)
    result = validate_spec(spec)
    if not result.valid:
        raise SpecValidationError(
            "compiled spec failed validation:\n  - " + "\n  - ".join(result.errors)
        )

    persist_spec(spec, spec_path, allow_initial=True)
    return spec
