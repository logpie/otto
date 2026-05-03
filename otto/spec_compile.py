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
    """
    reason: str
    actor: str
    ts: str                                          # ISO-8601 UTC
    diff_sha256_before: str
    diff_sha256_after: str


@dataclass
class Spec:
    """Top-level structured product spec.

    `intent` is the user's verbatim intent. `non_goals` and `done_means`
    are the unified replacements for codex-feats' `non_goals` /
    `done_means` ProductContract fields and feed the spec-review UI.
    """
    schema_version: int = SCHEMA_VERSION
    intent: str = ""
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


def _check_from_dict(payload: dict[str, Any]) -> CheckKind:
    kind = str(payload.get("kind") or "").strip()
    cls = _CHECK_TYPES.get(kind)
    if cls is None:
        raise SpecValidationError(f"unknown check kind: {kind!r}")
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
    return cls(**kwargs)


def spec_to_dict(spec: Spec) -> dict[str, Any]:
    """Convert a `Spec` to a plain dict for JSON serialisation."""
    return {
        "schema_version": spec.schema_version,
        "intent": spec.intent,
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


def spec_from_dict(data: dict[str, Any]) -> Spec:
    """Inverse of `spec_to_dict`. Raises on unknown check kinds."""
    if not isinstance(data, dict):
        raise SpecValidationError("spec must be a JSON object")

    structure_payload = ((data.get("structure") or {}).get("payload")) or {}
    if not isinstance(structure_payload, dict):
        raise SpecValidationError("structure.payload must be an object")

    slices_data = data.get("slices") or []
    if not isinstance(slices_data, list):
        raise SpecValidationError("slices must be a list")

    slices: list[Slice] = []
    for entry in slices_data:
        if not isinstance(entry, dict):
            raise SpecValidationError("each slice must be an object")
        checks = [_check_from_dict(c) for c in (entry.get("checks") or [])]
        slices.append(Slice(
            id=str(entry.get("id") or ""),
            title=str(entry.get("title") or ""),
            tasks=[str(t) for t in (entry.get("tasks") or [])],
            deps=[str(d) for d in (entry.get("deps") or [])],
            owned_paths=[str(p) for p in (entry.get("owned_paths") or [])],
            checks=checks,
        ))

    amendments: list[Amendment] = []
    for entry in (data.get("amendments") or []):
        if not isinstance(entry, dict):
            raise SpecValidationError("each amendment must be an object")
        amendments.append(Amendment(
            reason=str(entry.get("reason") or ""),
            actor=str(entry.get("actor") or ""),
            ts=str(entry.get("ts") or ""),
            diff_sha256_before=str(entry.get("diff_sha256_before") or ""),
            diff_sha256_after=str(entry.get("diff_sha256_after") or ""),
        ))

    cross_slice_checks = [
        _check_from_dict(c) for c in (data.get("cross_slice_checks") or [])
    ]

    return Spec(
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        intent=str(data.get("intent") or ""),
        project_kind=str(data.get("project_kind") or "webapp"),
        structure=StructureDecisions(payload=dict(structure_payload)),
        slices=slices,
        cross_slice_checks=cross_slice_checks,
        shared_scaffold=[str(p) for p in (data.get("shared_scaffold") or [])],
        non_goals=[str(g) for g in (data.get("non_goals") or [])],
        done_means=[str(g) for g in (data.get("done_means") or [])],
        amendments=amendments,
    )


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
    valid: bool
    errors: list[str] = field(default_factory=list)


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


def validate_spec(spec: Spec) -> ValidationResult:
    """Schema-only validation: structural well-formedness, not LLM judgement.

    Operational meaning of "concrete enough" is: the structure payload
    passes the per-`project_kind` JSON schema, slices have unique ids and
    at least one check each, deps reference real slices, no cycles.
    """
    errors: list[str] = []

    if spec.project_kind not in PROJECT_KINDS:
        errors.append(f"project_kind must be one of {PROJECT_KINDS}; got {spec.project_kind!r}")

    if not spec.intent.strip():
        errors.append("intent must be non-empty")

    if not spec.slices:
        errors.append("spec must declare at least one slice")

    seen_ids: set[str] = set()
    for slice_ in spec.slices:
        if not _SLICE_ID_RE.match(slice_.id):
            errors.append(f"slice id {slice_.id!r} must match {_SLICE_ID_RE.pattern}")
        if slice_.id in seen_ids:
            errors.append(f"duplicate slice id: {slice_.id!r}")
        seen_ids.add(slice_.id)
        if not slice_.title.strip():
            errors.append(f"slice {slice_.id!r}: title must be non-empty")
        if not slice_.checks:
            errors.append(f"slice {slice_.id!r}: must declare at least one check")
        # Empty owned_paths is permitted: a slice may purely add new files
        # (anywhere) or purely modify files owned by transitive deps. The
        # original strict rule was added before the dep-transitivity scope
        # rule and is now over-restrictive — caught by the round-5 Microfeed
        # bench where the agent wanted slices that extend shared scaffold +
        # foundation files only.

    for slice_ in spec.slices:
        for dep in slice_.deps:
            if dep not in seen_ids:
                errors.append(f"slice {slice_.id!r}: dep {dep!r} not in spec")

    errors.extend(_detect_dep_cycles(spec.slices))

    schema = _load_kind_schema(spec.project_kind)
    if schema is not None:
        errors.extend(_validate_against_schema(spec.structure.payload, schema, path="structure.payload"))

    return ValidationResult(valid=not errors, errors=errors)


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


def persist_spec(spec: Spec, path: Path, *, allow_initial: bool = False) -> Path:
    """Write `spec.json` to `path`.

    Enforces immutability:

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

    on_disk_data = json.loads(path.read_text(encoding="utf-8"))
    on_disk = spec_from_dict(on_disk_data)
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
