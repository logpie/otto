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
  `otto run`. Groups, typed checks, structure decisions, amendments.

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
  after Microfeed bench validation. The runtime lives in
  `otto/checks.py` (browser_journey executor); the check shells out to
  a runner — no Playwright session lifecycle owned by the check
  executor.
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

SCHEMA_VERSION = 2
# Schema v1 → v2 (round-3 audit gap 4): the redesign added Feature,
# Component, Guardrail, dispatch_plan, audit_fixtures, shared_paths,
# non_goals, done_means, amendments + 3 new CheckKinds, plus the
# Slice→Group rename. The parser still reads legacy v1 keys with a
# deprecation warning (see `_warn_legacy_keys_for_v2`); v2 specs that
# carry leftover legacy keys are flagged loudly so they get cleaned up
# before the next bump removes the read-fallback entirely.
SCHEMA_LEGACY_KEYS_V1: tuple[str, ...] = (
    "slices",
    "cross_slice_checks",
)
# Per-group legacy keys (v1 → v2 rename map). Read-fallback handled in
# `_coerce_str_list` already; this tuple is the canonical reference set.
SCHEMA_LEGACY_GROUP_FIELDS_V1: tuple[str, ...] = (
    "title",   # → name
    "tasks",   # → feature_ids
    "deps",    # → dependencies
)
SPEC_FILENAME = "spec.json"
COMPILE_PROMPT = "compile-spec.md"
COMPILE_PROMPT_BROWNFIELD = "compile-spec-brownfield.md"
PROJECT_KINDS: tuple[str, ...] = ("webapp", "cli", "library", "api")
SCHEMAS_DIR = Path(__file__).parent / "spec_schemas"


# Per-kind default evidence kinds for new Features (research §2.7).
# Compile uses this to suggest evidence_kinds when creating Features for a
# given project_kind. Users can override per-Feature in the spec-review gate.
#
# CLIProbe / ImportCheck / TypeCheck are A1b additions to otto/checks.py;
# referenced here as forward-declared names. The list is reference data —
# the Check classes themselves are added when A1b lands.
DEFAULT_EVIDENCE_KINDS_PER_KIND: dict[str, tuple[str, ...]] = {
    "webapp": ("BrowserJourney", "ApiProbe", "StateInvariant", "RepoTestCheck"),
    "api": ("ApiProbe", "StateInvariant", "RepoTestCheck"),
    "library": ("ImportCheck", "TypeCheck", "RepoTestCheck"),
    "cli": ("CLIProbe", "RepoTestCheck"),
}


def default_evidence_kinds_for(project_kind: str) -> tuple[str, ...]:
    """Return the suggested evidence_kinds for a Feature in a project of
    the given kind. Falls back to webapp defaults for unknown kinds.
    """
    return DEFAULT_EVIDENCE_KINDS_PER_KIND.get(
        project_kind, DEFAULT_EVIDENCE_KINDS_PER_KIND["webapp"]
    )


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

    Implemented in `otto/checks.py` as the `browser_journey` executor:
    we shell out to a runner (typically a Playwright pytest), then glob
    the configured artifact paths. The check executor does not own the
    Playwright session lifecycle; the runner does.
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


# ---------------------------------------------------------------------------
# A1b: new Check kinds for non-webapp project_kinds (research §2.7).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CLIProbe:
    """Invoke a subprocess; assert exit code, stdout/stderr substrings.

    Used primarily by `project_kind=cli` — exercises the built CLI tool
    by running its commands and checking output. Subprocess timeout via
    `timeout_s`.
    """
    kind: Literal["cli_probe"] = "cli_probe"
    command: tuple[str, ...] = ()                   # e.g. ("./mytool", "--help")
    expect_exit_code: int = 0
    expect_stdout_substring: str = ""
    expect_stderr_substring: str = ""
    timeout_s: int = 60


@dataclass(frozen=True)
class ImportCheck:
    """Verify a Python package imports cleanly.

    Used by `project_kind=library` — confirms `python -c "import <pkg>"`
    returns exit 0. Optional `expect_version` checks `<pkg>.__version__`
    matches.
    """
    kind: Literal["import_check"] = "import_check"
    package_name: str = ""
    expect_version: str = ""                        # "" = no version check
    timeout_s: int = 30


@dataclass(frozen=True)
class TypeCheck:
    """Run a Python type checker on declared paths.

    Used by `project_kind=library` and any project wanting strict type
    safety. `tool` is mypy by default; pyright/basedpyright also accepted.
    Passes when the tool exits 0.
    """
    kind: Literal["type_check"] = "type_check"
    paths: tuple[str, ...] = ()                     # e.g. ("src/", "tests/")
    tool: str = "mypy"                              # "mypy" | "pyright" | "basedpyright"
    timeout_s: int = 300


CheckKind = (
    PytestCheck
    | RepoTestCheck
    | ApiProbe
    | BrowserJourney
    | StateInvariant
    | CLIProbe
    | ImportCheck
    | TypeCheck
)

_CHECK_TYPES: dict[str, type] = {
    "pytest": PytestCheck,
    "repo_test": RepoTestCheck,
    "api_probe": ApiProbe,
    "browser_journey": BrowserJourney,
    "state_invariant": StateInvariant,
    "cli_probe": CLIProbe,
    "import_check": ImportCheck,
    "type_check": TypeCheck,
}


@dataclass
class Group:
    """One vertical product surface — a build agent owns it end-to-end.

    A Group bundles related Features that share files or sequencing.
    `feature_ids` is the canonical reference to the Features the Group
    builds (research §2 vocabulary). `name` is the user-facing title.
    `dependencies` lists other Group/Component ids that must land first;
    the merge queue resolves the kind by looking up the id in the spec.

    `dispatch_plan` is reserved (research §2.6) for an optional
    structured plan describing how the build agent should sequence
    sub-tasks within the Group. The shape is intentionally
    underspecified at this stage — plan.md only mentions it as a
    placeholder. We accept a free-form ``str`` so callers can stash a
    markdown plan today; if/when the new design pins down a structured
    shape, this field tightens. Until then, treat it as optional
    metadata. Persisted to JSON only when non-empty.
    """
    id: str
    name: str
    feature_ids: list[str] = field(default_factory=list)  # Features this Group builds
    dependencies: list[str] = field(default_factory=list)  # other group/component ids
    owned_paths: list[str] = field(default_factory=list)  # globs the agent may *modify*
    checks: list[CheckKind] = field(default_factory=list)
    dispatch_plan: str = ""  # optional free-form plan; see docstring


# ===========================================================================
# A1a — New design data model (research §2 vocabulary, §2.6, §2.7, §4)
# ===========================================================================
#
# Feature, Component, Guardrail are the new design's user-visible vocabulary.
# They live alongside Group during the A0/A1 transition. Compile starts
# populating them in A1a; user-facing surfaces (MC, proof packet, spec.md)
# read them in A3/A4. Group remains the dispatch unit (research §2.6:
# "Group = unit of dispatch; Feature = unit of value").
#
# Field semantics per research §2 vocabulary table.


@dataclass
class Feature:
    """One unit of value with an audit verdict — the user's concern.

    Per research §3 atomic units: `Feature` is atomic for value/verdict.
    One Feature → one verdict in the proof packet.

    Fields per research §2 vocabulary table + §4 audit honesty contract:
      - `id`: stable slug; never changes across user edits / renames
      - `name`: user-facing title
      - `description`: prose explanation
      - `acceptance_detail`: optional structured pass/fail criterion
      - `evidence_kinds`: which Check kinds this Feature uses
      - `group_id`: the Group whose agent builds this Feature
      - `verdict`: populated post-Audit (None pre-Audit)
      - `evidence_completeness`: full | proxy_only | partial
      - `coverage_confidence`: high | medium | low
      - `multi_actor_required`: True for cross-user features (DM
        delivery, presence, etc.)
      - `audit_pre_merge`: opt-in per-Group audit-gate before merge

    Feature ids use the same slug rules as Group ids (lowercase letters,
    digits, dashes, underscores). Renames change `name`, never `id`.
    """
    id: str
    name: str
    description: str = ""
    acceptance_detail: str = ""
    evidence_kinds: list[str] = field(default_factory=list)
    group_id: str = ""
    verdict: str | None = None
    evidence_completeness: str = "full"
    coverage_confidence: str = "high"
    multi_actor_required: bool = False
    audit_pre_merge: bool = False


@dataclass
class Component:
    """A non-Feature dispatch unit — shared infrastructure (research §2.6).

    Components are dispatched like Groups (own agent, branch, worktree)
    but have NO user-facing verdict. They bundle code that supports
    Features but isn't itself a unit of value: WebSocket hubs, search
    indexers, notification fan-out, job queues.

    The audit pass verifies the Features that consume a Component;
    transitively this proves the Component works. Render shows
    Components in a "Components" section with build-status + checks +
    cost — but no pass/fail verdict.
    """
    id: str
    name: str
    description: str = ""
    owned_paths: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    checks: list[CheckKind] = field(default_factory=list)
    consumed_by: list[str] = field(default_factory=list)  # Feature ids


@dataclass
class Guardrail:
    """A pinned negative scope — a `don't` constraint (research §2 vocab).

    Guardrails do not dispatch any work. They sit in the Spec as
    constraints; the audit pass verifies nothing in the built product
    violates them. Guardrails appear in the proof packet under
    "Guardrails verified".

    `applies_to`: "*" for whole product; a Group id or Feature id for
    scoped guardrails.
    """
    id: str
    text: str
    applies_to: str = "*"


# ===========================================================================
# Severity ladder for findings (research §4 severity ladder)
# ===========================================================================

# Quality findings carry a severity. `critical` flips a Feature verdict to
# `partial` and triggers Layer 2 audit-loop repair within budget.
# `important` surfaces in the Proof under the Feature; verdict unchanged.
# `polish` surfaces in a "Polish suggestions" Proof section, never blocks.
FINDING_SEVERITIES = ("critical", "important", "polish")


@dataclass
class Finding:
    """An audit finding tagged by severity (research §4)."""
    severity: str  # one of FINDING_SEVERITIES
    text: str
    feature_id: str = ""  # empty = whole-product finding


# ===========================================================================
# Audit-fixture seed entries (research §4 audit fixtures)
# ===========================================================================


@dataclass
class AuditFixture:
    """Pre-seed entry for multi-user products (research §4 audit fixtures).

    Applied to the live product by the Seed stage between Build and
    Audit. Without this, audit wastes its budget creating test users,
    follows, channels.
    """
    kind: str  # "user", "channel", "follow", "data", ...
    payload: dict[str, Any] = field(default_factory=dict)


# ===========================================================================
# A2 — Audit Feature-tagging (research §4 + §2.7 walkthrough schema)
# ===========================================================================
#
# WalkthroughEntry is one line of audit/attempt-NN/walkthrough.jsonl.
# Every line is tagged with the Feature(s) it evidences. The audit pass
# must achieve ≥90% tagging coverage on non-exploration entries (research
# §A2 backup plan); below that, audit prompt is rewritten before
# proceeding.
#
# `action_kind` is the discriminator for kind-specific fields:
#   - browser_navigation: screenshot, dom_snapshot, url, method
#   - api_request: method, path, request_body, response_status, response_body
#   - cli_invoke: command, exit_code, stdout, stderr
#   - import_check: package, version, import_succeeded
#   - exploration: no feature_ids required (catch-all for setup/cleanup)
#   - type_check: tool, paths, exit_code

# Action kinds for WalkthroughEntry (research §2.7 + audit prompt contract)
WALKTHROUGH_ACTION_KINDS: tuple[str, ...] = (
    "browser_navigation",
    "api_request",
    "cli_invoke",
    "import_check",
    "type_check",
    "exploration",       # untagged: setup/cleanup/site-survey actions
)


@dataclass
class WalkthroughEntry:
    """One audit walkthrough action line (research §4 + §2.7).

    Tagged with `feature_ids[]` so per-Feature proof aggregates correctly.
    Empty `feature_ids` is allowed only when `action_kind == "exploration"`
    (catch-all for non-evidence-bearing setup actions).

    `t` is wall-clock time (or "0:0X" mm:ss offset from audit start).
    `narrative` is the agent's human-readable description of what it did.

    Kind-specific fields are stored in `extras` as a dict to keep the
    dataclass shape stable across action kinds. Helpers like
    `entry.screenshot()` look in `extras` for the relevant field.
    """
    t: str                                            # ISO-8601 or mm:ss offset
    feature_ids: list[str] = field(default_factory=list)
    action_kind: str = "exploration"                  # one of WALKTHROUGH_ACTION_KINDS
    narrative: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class CoverageReport:
    """Result of audit walkthrough coverage validation (research §A2)."""
    total_entries: int
    exploration_entries: int
    tagged_entries: int                               # non-exploration with feature_ids[]
    untagged_entries: int                             # non-exploration with empty feature_ids
    unknown_feature_id_refs: list[str] = field(default_factory=list)
    per_feature_evidence_count: dict[str, int] = field(default_factory=dict)

    @property
    def non_exploration_total(self) -> int:
        return self.total_entries - self.exploration_entries

    @property
    def coverage_ratio(self) -> float:
        """Fraction of non-exploration entries that have feature_ids[].

        Vacuously 1.0 when there are no non-exploration entries (purely
        setup walkthrough is fine).
        """
        denom = self.non_exploration_total
        if denom == 0:
            return 1.0
        return self.tagged_entries / denom

    def meets_threshold(self, threshold: float = 0.90) -> bool:
        """Default threshold from research §A2: ≥90%."""
        return self.coverage_ratio >= threshold


def parse_walkthrough_entry(
    payload: dict[str, Any],
    spec: Spec,
    *,
    strict: bool = False,
) -> tuple[WalkthroughEntry | None, list[str]]:
    """Parse one walkthrough.jsonl line.

    Two modes:

    * `strict=False` (default, back-compat): all oddities surface as
      warnings; the entry is dropped only if `payload` is not a dict.
    * `strict=True` (A1b.7 enforcement): the three contract violations
      below are promoted from warnings to errors and the entry is
      dropped (return value is `(None, [error_msg, ...])`):
        - unknown `action_kind` (not in WALKTHROUGH_ACTION_KINDS)
        - untagged action whose `action_kind` is not `"exploration"`
        - any `feature_id` that does not match a `Feature.id` in spec

      Other oddities (e.g. `feature_ids` is not a list) still surface
      as advisory warnings even in strict mode.

    Returns:
        (entry, messages). In permissive mode `messages` is a warnings
        list and `entry` is non-None unless `payload` is malformed. In
        strict mode, when a contract violation is detected `entry` is
        None and `messages` contains one or more error strings (same
        text as the warnings, prefixed by the violation reason).

    Validation (research §A2):
        * Every emitted feature_id must match a Feature.id in spec.
        * If action_kind != "exploration" and feature_ids is empty,
          message "untagged_non_exploration".
        * If action_kind not in WALKTHROUGH_ACTION_KINDS, message
          "unknown_action_kind".
    """
    messages: list[str] = []
    if not isinstance(payload, dict):
        return None, [f"walkthrough entry is {type(payload).__name__}, not dict"]

    t = str(payload.get("t") or "")
    raw_feature_ids = payload.get("feature_ids") or []
    if not isinstance(raw_feature_ids, list):
        # Always advisory — never an A1b.7 contract violation.
        messages.append(
            f"feature_ids should be a list, got {type(raw_feature_ids).__name__}; using []"
        )
        raw_feature_ids = []
    feature_ids = [str(fid) for fid in raw_feature_ids]

    action_kind = str(payload.get("action_kind") or payload.get("note") or "exploration")

    # Track strict-mode violations separately so we can drop the entry
    # without losing the diagnostic text.
    strict_errors: list[str] = []

    if action_kind not in WALKTHROUGH_ACTION_KINDS:
        msg = f"unknown_action_kind: {action_kind!r}"
        if strict:
            strict_errors.append(msg)
        else:
            messages.append(msg)

    narrative = str(payload.get("narrative") or "")

    # Validate feature_ids exist in spec.
    known_feature_ids = {f.id for f in spec.features}
    for fid in feature_ids:
        if fid and fid not in known_feature_ids:
            msg = f"unknown_feature_id: {fid!r}"
            if strict:
                strict_errors.append(msg)
            else:
                messages.append(msg)

    # Untagged-non-exploration (research §A2 contract).
    if action_kind != "exploration" and not feature_ids:
        msg = "untagged_non_exploration"
        if strict:
            strict_errors.append(msg)
        else:
            messages.append(msg)

    if strict and strict_errors:
        # Drop the entry — surface advisories alongside the errors so
        # callers see the full diagnostic, but flag the entry as
        # rejected by returning None.
        return None, strict_errors + messages

    # Stash kind-specific extras (everything not in core fields).
    core_keys = {"t", "feature_ids", "action_kind", "narrative", "note"}
    extras = {k: v for k, v in payload.items() if k not in core_keys}

    entry = WalkthroughEntry(
        t=t,
        feature_ids=feature_ids,
        action_kind=action_kind,
        narrative=narrative,
        extras=extras,
    )
    return entry, messages


def filter_walkthrough_by_feature(
    entries: list[WalkthroughEntry],
    feature_id: str,
) -> list[WalkthroughEntry]:
    """Return walkthrough entries that evidence a given Feature.

    An entry evidences the Feature iff `feature_id in entry.feature_ids`.
    Multi-Feature entries (entries that evidence multiple Features at
    once — e.g. "user uploads image AND comments on it") appear in
    each relevant Feature's filtered subset. This is research §7's
    "multi-Feature evidence cross-linking" rule: don't double-store,
    cross-link.

    Empty `feature_ids` (exploration entries) never appear in any
    Feature's filtered subset.
    """
    return [
        entry for entry in entries
        if feature_id and feature_id in entry.feature_ids
    ]


@dataclass
class FeatureProofBlock:
    """One Feature's proof packet section (research §7).

    Aggregates everything needed to render `proof/features/<id>/proof.html`:
    verdict + detail, walkthrough entries tagged for this Feature,
    deterministic check evidence refs, group/files info, repair history,
    audit narrative excerpt, and spec context (which Compile-stage
    decision produced this Feature).

    Multi-Feature entries appear in this block AND in any other
    Features they tagged — see `filter_walkthrough_by_feature` for the
    rule.

    Fields:
      - feature_id, name, description, group_id: from Spec.feature
      - verdict: from feature-verdicts.json (None pre-Audit)
      - detail: from feature-verdicts.json detail
      - walkthrough_entries: tagged entries for this Feature
      - shared_with: other Feature ids whose walkthroughs include any
        of this Feature's entries (multi-Feature cross-link)
      - evidence_completeness, coverage_confidence: research §4
        audit honesty fields
      - check_evidence_refs: deterministic-check evidence paths
      - files_changed: files the Feature's Group touched
      - repair_history: list of repair attempts
      - audit_narrative_excerpt: human paragraph from audit
      - findings: severity-tagged quality findings for this Feature
    """
    feature_id: str
    name: str
    description: str = ""
    group_id: str = ""
    verdict: str | None = None
    detail: str = ""
    walkthrough_entries: list[WalkthroughEntry] = field(default_factory=list)
    shared_with: list[str] = field(default_factory=list)
    evidence_completeness: str = "full"
    coverage_confidence: str = "high"
    check_evidence_refs: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    repair_history: list[dict[str, Any]] = field(default_factory=list)
    audit_narrative_excerpt: str = ""
    findings: list[Finding] = field(default_factory=list)


def build_feature_proof_blocks(
    spec: Spec,
    walkthrough_entries: list[WalkthroughEntry],
    feature_verdicts: list[dict[str, Any]],
    *,
    findings: list[Finding] | None = None,
    files_per_group: dict[str, list[str]] | None = None,
) -> list[FeatureProofBlock]:
    """Assemble per-Feature proof blocks from spec + audit outputs.

    Args:
        spec: the approved Spec.
        walkthrough_entries: parsed audit walkthrough lines.
        feature_verdicts: list of dicts from feature-verdicts.json
            (one per Feature in spec).
        findings: optional severity-tagged quality findings to attach
            per Feature based on each finding's `feature_id`.
        files_per_group: optional map group_id → list of files the
            Group's agent touched (rendered in the Feature block as
            "Built in <group> · files: ...").

    Returns:
        One FeatureProofBlock per Feature in spec (in spec order).
        Features with no tagged walkthrough entries get an empty
        `walkthrough_entries` list — render layer surfaces that as
        verdict=missing per research §4 honesty rule.
    """
    findings = findings or []
    files_per_group = files_per_group or {}
    verdicts_by_id = {
        str(v.get("feature_id") or ""): v
        for v in feature_verdicts
        if isinstance(v, dict)
    }

    blocks: list[FeatureProofBlock] = []
    for feature in spec.features:
        feature_entries = filter_walkthrough_by_feature(
            walkthrough_entries, feature.id
        )
        # Multi-Feature cross-link: collect other Feature ids whose
        # walkthroughs share any of this Feature's tagged entries.
        shared_with: list[str] = []
        for entry in feature_entries:
            for fid in entry.feature_ids:
                if fid != feature.id and fid not in shared_with:
                    shared_with.append(fid)

        verdict_data = verdicts_by_id.get(feature.id, {})
        evidence_completeness = str(
            verdict_data.get("evidence_completeness")
            or feature.evidence_completeness
        )
        coverage_confidence = str(
            verdict_data.get("coverage_confidence")
            or feature.coverage_confidence
        )

        feature_findings = [
            f for f in findings if f.feature_id == feature.id
        ]

        block = FeatureProofBlock(
            feature_id=feature.id,
            name=feature.name,
            description=feature.description,
            group_id=feature.group_id,
            verdict=verdict_data.get("verdict") or feature.verdict,
            detail=str(verdict_data.get("detail") or ""),
            walkthrough_entries=feature_entries,
            shared_with=shared_with,
            evidence_completeness=evidence_completeness,
            coverage_confidence=coverage_confidence,
            check_evidence_refs=[
                str(r) for r in (verdict_data.get("evidence_refs") or [])
            ],
            files_changed=list(files_per_group.get(feature.group_id, [])),
            repair_history=list(verdict_data.get("repair_history") or []),
            audit_narrative_excerpt=str(verdict_data.get("audit_narrative_excerpt") or ""),
            findings=feature_findings,
        )
        blocks.append(block)
    return blocks


def walkthrough_entry_to_dict(entry: WalkthroughEntry) -> dict[str, Any]:
    """Serialise WalkthroughEntry to a dict for JSON emission.

    Inverse-shaped of `parse_walkthrough_entry`: kind-specific extras
    are flattened back to top-level keys (e.g. extras["url"] becomes
    payload["url"]).
    """
    payload: dict[str, Any] = {
        "t": entry.t,
        "feature_ids": list(entry.feature_ids),
        "action_kind": entry.action_kind,
        "narrative": entry.narrative,
    }
    payload.update(entry.extras)
    return payload


def feature_proof_block_to_dict(block: FeatureProofBlock) -> dict[str, Any]:
    """Serialise FeatureProofBlock to a JSON-shaped dict (research §7).

    The shape matches what `proof/features/<feature-id>/proof.json`
    contains. The whole-product `proof-packet.json` `features[]` array
    holds these dicts.
    """
    return {
        "feature_id": block.feature_id,
        "name": block.name,
        "description": block.description,
        "group_id": block.group_id,
        "verdict": block.verdict,
        "detail": block.detail,
        "evidence_completeness": block.evidence_completeness,
        "coverage_confidence": block.coverage_confidence,
        "shared_with": list(block.shared_with),
        "walkthrough_entries": [
            walkthrough_entry_to_dict(e) for e in block.walkthrough_entries
        ],
        "check_evidence_refs": list(block.check_evidence_refs),
        "files_changed": list(block.files_changed),
        "repair_history": list(block.repair_history),
        "audit_narrative_excerpt": block.audit_narrative_excerpt,
        "findings": [
            {
                "severity": f.severity,
                "text": f.text,
                "feature_id": f.feature_id,
            }
            for f in block.findings
        ],
    }


def feature_proof_blocks_to_dicts(
    blocks: list[FeatureProofBlock],
) -> list[dict[str, Any]]:
    """Serialise a list of FeatureProofBlocks for embedding in
    proof-packet.json `features[]`."""
    return [feature_proof_block_to_dict(b) for b in blocks]


# ---------------------------------------------------------------------------
# A3: per-Feature HTML rendering with per-project_kind template branching
# (research §7 + §2.7)
# ---------------------------------------------------------------------------


def _html_escape(s: str) -> str:
    """Minimal HTML escape — sufficient for our trusted-content render."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _verdict_badge_html(verdict: str | None) -> str:
    if verdict is None:
        return '<span class="verdict pending">pending</span>'
    v = verdict.lower()
    cls = {
        "passed": "ok",
        "partial": "warn",
        "blocked": "fail",
        "failed": "fail",
        "missing": "warn",
    }.get(v, "info")
    return f'<span class="verdict {cls}">{_html_escape(v)}</span>'


def _render_walkthrough_webapp(entries: list[WalkthroughEntry]) -> str:
    """Webapp variant — screenshot grid, DOM-snapshot links."""
    if not entries:
        return '<p class="empty">No walkthrough entries tagged for this feature.</p>'
    rows: list[str] = []
    for entry in entries:
        screenshot = entry.extras.get("screenshot")
        dom = entry.extras.get("dom_snapshot")
        url = entry.extras.get("url", "")
        method = entry.extras.get("method", "")
        rows.append(
            "<div class='walkthrough-step'>"
            f"<span class='t'>{_html_escape(entry.t)}</span> "
            f"<span class='kind'>{_html_escape(entry.action_kind)}</span> "
            f"{_html_escape(method)} {_html_escape(url)}"
            f"<p>{_html_escape(entry.narrative)}</p>"
            + (f"<a class='screenshot' href='{_html_escape(str(screenshot))}'>screenshot</a>" if screenshot else "")
            + (f" <a class='dom' href='{_html_escape(str(dom))}'>DOM</a>" if dom else "")
            + "</div>"
        )
    return "<div class='walkthrough webapp'>" + "".join(rows) + "</div>"


def _render_walkthrough_api(entries: list[WalkthroughEntry]) -> str:
    """API variant — request/response trace table."""
    if not entries:
        return '<p class="empty">No walkthrough entries tagged for this feature.</p>'
    rows = ["<tr><th>t</th><th>method</th><th>path</th><th>status</th><th>narrative</th></tr>"]
    for entry in entries:
        rows.append(
            "<tr>"
            f"<td>{_html_escape(entry.t)}</td>"
            f"<td>{_html_escape(str(entry.extras.get('method', '')))}</td>"
            f"<td>{_html_escape(str(entry.extras.get('path', '')))}</td>"
            f"<td>{_html_escape(str(entry.extras.get('response_status', '')))}</td>"
            f"<td>{_html_escape(entry.narrative)}</td>"
            "</tr>"
        )
    return "<table class='walkthrough api'>" + "".join(rows) + "</table>"


def _render_walkthrough_cli(entries: list[WalkthroughEntry]) -> str:
    """CLI variant — terminal-style transcript."""
    if not entries:
        return '<p class="empty">No walkthrough entries tagged for this feature.</p>'
    rows: list[str] = []
    for entry in entries:
        cmd = entry.extras.get("command", [])
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        rows.append(
            "<div class='terminal-line'>"
            f"<span class='t'>{_html_escape(entry.t)}</span> "
            f"<code>$ {_html_escape(cmd_str)}</code>"
            f" <span class='exit'>exit={_html_escape(str(entry.extras.get('exit_code', '?')))}</span>"
            + (f"<pre class='stdout'>{_html_escape(str(entry.extras.get('stdout', '')))}</pre>" if entry.extras.get('stdout') else "")
            + "</div>"
        )
    return "<div class='walkthrough cli'>" + "".join(rows) + "</div>"


def _render_walkthrough_library(entries: list[WalkthroughEntry]) -> str:
    """Library variant — import + type-check status table."""
    if not entries:
        return '<p class="empty">No walkthrough entries tagged for this feature.</p>'
    rows = ["<tr><th>t</th><th>kind</th><th>target</th><th>status</th><th>narrative</th></tr>"]
    for entry in entries:
        target = (
            entry.extras.get("package")
            or " ".join(entry.extras.get("paths", []))
            or ""
        )
        status = (
            "ok" if entry.extras.get("import_succeeded") is True
            else f"exit={entry.extras.get('exit_code', '?')}"
        )
        rows.append(
            "<tr>"
            f"<td>{_html_escape(entry.t)}</td>"
            f"<td>{_html_escape(entry.action_kind)}</td>"
            f"<td>{_html_escape(str(target))}</td>"
            f"<td>{_html_escape(status)}</td>"
            f"<td>{_html_escape(entry.narrative)}</td>"
            "</tr>"
        )
    return "<table class='walkthrough library'>" + "".join(rows) + "</table>"


_WALKTHROUGH_RENDERERS = {
    "webapp": _render_walkthrough_webapp,
    "api": _render_walkthrough_api,
    "cli": _render_walkthrough_cli,
    "library": _render_walkthrough_library,
}


# ---------------------------------------------------------------------------
# A5: Spec.md round-trip (research §2.1)
#
# `render_spec_md(spec) -> str` produces a human-readable Markdown rendering
# of a Spec. `parse_spec_md(md_text, base=None) -> Spec | ParseError`
# (next tick) recovers a Spec from the Markdown. Round-trip property:
#   parse_spec_md(render_spec_md(s), base=s) == s
#
# HTML comments carry mechanical metadata (feature ids, group ids, evidence
# kinds) so prose stays clean while ids round-trip stably.
# ---------------------------------------------------------------------------


def render_spec_md(spec: Spec) -> str:
    """Render a Spec as user-facing Markdown (research §2.1).

    Layout (matches docs/otto-wireframes.md screen 4a):
        # <intent first line>

        <intent body if multi-line>

        ## Project kind

        <project_kind>

        ## Features

        ### <Group.name>
        <!-- group: <group_id> -->

        #### <Feature.name>
        <!-- feature: <feature_id> | evidence: <kinds> -->

        <feature.description>

        **Acceptance:** <feature.acceptance_detail>

        ## Guardrails

        - ⊘ <guardrail.text>

    Multi-line intent: first line is the H1 title; remaining lines fall
    under it as body prose. Empty fields (description, acceptance_detail)
    are omitted from the rendered output.
    """
    parts: list[str] = []

    # Intent: first line as H1, rest as body
    intent = (spec.intent or "").strip()
    if intent:
        intent_lines = intent.splitlines()
        title = intent_lines[0].lstrip("# ").strip() or "Untitled"
        parts.append(f"# {title}\n")
        body = "\n".join(intent_lines[1:]).strip()
        if body:
            parts.append(f"\n{body}\n")
    else:
        parts.append("# Untitled\n")

    # Project kind
    parts.append("\n## Project kind\n")
    parts.append(f"\n{spec.project_kind}\n")

    # Features (grouped by Group)
    if spec.features or spec.groups:
        parts.append("\n## Features\n")
        # Group features by group_id, preserving spec.groups order
        features_by_group: dict[str, list[Feature]] = {}
        for f in spec.features:
            features_by_group.setdefault(f.group_id, []).append(f)
        # Render in spec.groups order
        for group in spec.groups:
            group_features = features_by_group.get(group.id, [])
            if not group_features and not group.name:
                continue
            parts.append(f"\n### {group.name or group.id}\n")
            parts.append(f"<!-- group: {group.id} -->\n")
            for feature in group_features:
                parts.append(f"\n#### {feature.name}\n")
                evidence_str = ", ".join(feature.evidence_kinds) if feature.evidence_kinds else ""
                if evidence_str:
                    parts.append(f"<!-- feature: {feature.id} | evidence: {evidence_str} -->\n")
                else:
                    parts.append(f"<!-- feature: {feature.id} -->\n")
                if feature.description:
                    parts.append(f"\n{feature.description}\n")
                if feature.acceptance_detail:
                    parts.append(f"\n**Acceptance:** {feature.acceptance_detail}\n")
        # Orphan features (no matching group)
        orphan_features = features_by_group.get("", [])
        if orphan_features:
            parts.append("\n### Ungrouped\n")
            for feature in orphan_features:
                parts.append(f"\n#### {feature.name}\n")
                evidence_str = ", ".join(feature.evidence_kinds) if feature.evidence_kinds else ""
                if evidence_str:
                    parts.append(f"<!-- feature: {feature.id} | evidence: {evidence_str} -->\n")
                else:
                    parts.append(f"<!-- feature: {feature.id} -->\n")
                if feature.description:
                    parts.append(f"\n{feature.description}\n")
                if feature.acceptance_detail:
                    parts.append(f"\n**Acceptance:** {feature.acceptance_detail}\n")

    # Guardrails
    if spec.guardrails:
        parts.append("\n## Guardrails\n\n")
        for g in spec.guardrails:
            scope_note = (
                f" _(applies to: {g.applies_to})_" if g.applies_to and g.applies_to != "*" else ""
            )
            parts.append(f"- ⊘ {g.text}{scope_note}\n")

    return "".join(parts)


def parse_spec_md(
    md_text: str,
    base: Spec | None = None,
) -> tuple[Spec, list[str]]:
    """Inverse of `render_spec_md` (research §2.1).

    Recovers a Spec from its Markdown rendering. Mechanical fields not
    present in the Markdown surface (owned_paths, dependencies, ...)
    are preserved from `base` when supplied.

    Args:
        md_text: full Markdown source as produced by render_spec_md.
        base: optional Spec to inherit mechanical fields from when
            re-parsing edited Markdown. Group.owned_paths / .dependencies and
            Feature.audit_pre_merge / .multi_actor_required come from
            base (matched by id). Same shape rule for Component
            owned_paths / dependencies.

    Returns:
        (spec, warnings) — spec is the parsed Spec; warnings is a list
        of human-readable strings flagging malformed sections (no
        crash on missing comments or partial sections).

    Round-trip property: parse_spec_md(render_spec_md(s), base=s)
    yields a Spec equal to s on all surface fields (intent,
    project_kind, group ids/titles, feature ids/names/descriptions/
    evidence_kinds/group_ids, guardrail ids/text/applies_to). Mechanical
    fields require base to round-trip stably.
    """
    import re

    warnings: list[str] = []
    base_groups_by_id: dict[str, Group] = (
        {g.id: g for g in base.groups} if base else {}
    )
    base_features_by_id: dict[str, Feature] = (
        {f.id: f for f in base.features} if base else {}
    )

    lines = md_text.splitlines()

    # Section parser: walk the lines and bucket by H1/H2 markers.
    intent_lines: list[str] = []
    project_kind = "webapp"
    features_section: list[str] = []
    guardrails_section: list[str] = []

    section = "intent"
    h1_seen = False
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("# ") and not h1_seen:
            intent_lines.append(line[2:].strip())
            h1_seen = True
            continue
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            if heading == "project kind":
                section = "project_kind"
            elif heading == "features":
                section = "features"
            elif heading == "guardrails":
                section = "guardrails"
            else:
                section = "other"
            continue
        if section == "intent":
            if line.strip():
                intent_lines.append(line)
        elif section == "project_kind":
            stripped = line.strip()
            if stripped:
                project_kind = stripped
        elif section == "features":
            features_section.append(line)
        elif section == "guardrails":
            guardrails_section.append(line)

    intent = "\n".join(line for line in intent_lines if line is not None).strip()
    if not project_kind:
        project_kind = "webapp"

    # Feature/Group recovery from the features section
    groups_out: list[Group] = []
    features_out: list[Feature] = []
    current_group_id: str = ""
    current_group_title: str = ""
    current_feature_id: str = ""
    current_feature_name: str = ""
    current_feature_evidence: list[str] = []
    current_feature_description: list[str] = []
    current_feature_acceptance: str = ""
    in_feature = False

    group_comment_re = re.compile(r"<!--\s*group:\s*([\w-]+)\s*-->")
    feature_comment_re = re.compile(
        r"<!--\s*feature:\s*([\w-]+)(?:\s*\|\s*evidence:\s*([^>-][^>]*?))?\s*-->"
    )
    acceptance_re = re.compile(r"^\*\*Acceptance:\*\*\s*(.*)$")

    def _flush_feature() -> None:
        nonlocal current_feature_id, current_feature_name
        nonlocal current_feature_evidence, current_feature_description
        nonlocal current_feature_acceptance, in_feature
        if not in_feature or not current_feature_id:
            in_feature = False
            return
        base_feat = base_features_by_id.get(current_feature_id)
        description = "\n".join(current_feature_description).strip()
        acceptance = current_feature_acceptance.strip()
        # "ungrouped" sentinel from "### Ungrouped" header is not a real group
        effective_group_id = "" if current_group_id == "ungrouped" else current_group_id
        feature = Feature(
            id=current_feature_id,
            name=current_feature_name or current_feature_id,
            description=description,
            acceptance_detail=acceptance,
            evidence_kinds=list(current_feature_evidence),
            group_id=effective_group_id,
            verdict=base_feat.verdict if base_feat else None,
            evidence_completeness=base_feat.evidence_completeness if base_feat else "full",
            coverage_confidence=base_feat.coverage_confidence if base_feat else "high",
            multi_actor_required=base_feat.multi_actor_required if base_feat else False,
            audit_pre_merge=base_feat.audit_pre_merge if base_feat else False,
        )
        features_out.append(feature)
        # Reset feature state
        current_feature_id = ""
        current_feature_name = ""
        current_feature_evidence = []
        current_feature_description = []
        current_feature_acceptance = ""
        in_feature = False

    def _flush_group() -> None:
        nonlocal current_group_id, current_group_title
        if not current_group_id:
            return
        if current_group_id == "ungrouped":
            # "### Ungrouped" is the orphan-features bucket; not a real group
            current_group_id = ""
            current_group_title = ""
            return
        if any(g.id == current_group_id for g in groups_out):
            return
        base_grp = base_groups_by_id.get(current_group_id)
        groups_out.append(
            Group(
                id=current_group_id,
                name=current_group_title or current_group_id,
                feature_ids=list(base_grp.feature_ids) if base_grp else [],
                dependencies=list(base_grp.dependencies) if base_grp else [],
                owned_paths=list(base_grp.owned_paths) if base_grp else [],
                checks=list(base_grp.checks) if base_grp else [],
            )
        )

    for line in features_section:
        stripped = line.rstrip()
        if stripped.startswith("### "):
            # Group header
            _flush_feature()
            _flush_group()
            current_group_title = stripped[4:].strip()
            # Group id will be set by the next <!-- group: id --> comment;
            # if no comment appears, fall through to "ungrouped"
            current_group_id = (
                "ungrouped" if current_group_title.lower() == "ungrouped" else ""
            )
            continue
        m_group = group_comment_re.search(stripped)
        if m_group:
            current_group_id = m_group.group(1)
            continue
        if stripped.startswith("#### "):
            _flush_feature()
            current_feature_name = stripped[5:].strip()
            in_feature = True
            continue
        m_feature = feature_comment_re.search(stripped)
        if m_feature and in_feature:
            current_feature_id = m_feature.group(1)
            evidence_raw = m_feature.group(2)
            if evidence_raw:
                current_feature_evidence = [
                    s.strip() for s in evidence_raw.split(",") if s.strip()
                ]
            else:
                current_feature_evidence = []
            continue
        m_acc = acceptance_re.match(stripped)
        if m_acc and in_feature:
            current_feature_acceptance = m_acc.group(1).strip()
            continue
        if in_feature and stripped.strip():
            # Description line (free prose)
            current_feature_description.append(stripped)

    _flush_feature()
    _flush_group()

    # Guardrails section
    guardrails_out: list[Guardrail] = []
    guardrail_re = re.compile(
        r"^\s*-\s*⊘\s*(.*?)(?:\s+_\(applies to:\s*([^)]+)\)_)?\s*$"
    )
    for line in guardrails_section:
        m = guardrail_re.match(line)
        if not m:
            continue
        text = m.group(1).strip()
        applies_to = (m.group(2) or "*").strip()
        if not text:
            continue
        # Stable id: re-use base id when text matches
        gid = ""
        if base:
            for g in base.guardrails:
                if g.text == text:
                    gid = g.id
                    break
        if not gid:
            gid = "g-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32]
        guardrails_out.append(Guardrail(id=gid, text=text, applies_to=applies_to))

    spec_kwargs: dict[str, Any] = {
        "intent": intent,
        "project_kind": project_kind,
        "groups": groups_out,
        "features": features_out,
        "guardrails": guardrails_out,
    }
    if base:
        # Preserve mechanical fields not encoded in markdown
        spec_kwargs["intent_hash"] = base.intent_hash
        spec_kwargs["structure"] = base.structure
        spec_kwargs["cross_group_checks"] = list(base.cross_group_checks)
        spec_kwargs["shared_scaffold"] = list(base.shared_scaffold)
        spec_kwargs["non_goals"] = list(base.non_goals)
        spec_kwargs["done_means"] = list(base.done_means)
        spec_kwargs["amendments"] = list(base.amendments)
        spec_kwargs["components"] = list(base.components)
        spec_kwargs["shared_paths"] = list(base.shared_paths)
        spec_kwargs["audit_fixtures"] = list(base.audit_fixtures)

    return Spec(**spec_kwargs), warnings


_FEATURE_PROOF_TEMPLATE = Path(__file__).parent / "web" / "templates" / "feature-proof.html.j2"


def _render_feature_template(context: dict[str, str]) -> str:
    template = _FEATURE_PROOF_TEMPLATE.read_text(encoding="utf-8")
    for key, value in context.items():
        template = template.replace("{{ " + key + " }}", value)
    return template


def feature_proof_block_to_html(
    block: FeatureProofBlock,
    *,
    project_kind: str = "webapp",
) -> str:
    """Render a per-Feature proof block as HTML (research §7).

    Layout: header (feature name + verdict badge), description, audit
    narrative excerpt, walkthrough segment (per-`project_kind`),
    deterministic check refs, group/files context, repair history,
    findings with severity tags, spec context (which Group built it).

    `project_kind` controls the walkthrough rendering: webapp →
    screenshot grid, api → request/response table, cli → terminal
    transcript, library → import-status table. Unknown kinds fall
    back to webapp.
    """
    walkthrough_renderer = _WALKTHROUGH_RENDERERS.get(
        project_kind, _render_walkthrough_webapp,
    )
    walkthrough_html = walkthrough_renderer(block.walkthrough_entries)

    description_html = ""
    if block.description:
        description_html = f'<p class="description">{_html_escape(block.description)}</p>'
    detail_html = ""
    if block.detail:
        detail_html = f'<p class="detail">{_html_escape(block.detail)}</p>'

    # Audit honesty fields (research §4)
    honesty_html = (
        '<div class="honesty">'
        f'<span class="completeness">completeness: {_html_escape(block.evidence_completeness)}</span> '
        f'<span class="confidence">confidence: {_html_escape(block.coverage_confidence)}</span>'
        "</div>"
    )

    audit_narrative_html = ""
    if block.audit_narrative_excerpt:
        audit_narrative_html = (
            f'<blockquote class="narrative">{_html_escape(block.audit_narrative_excerpt)}</blockquote>'
        )

    check_refs_html = ""
    if block.check_evidence_refs:
        check_parts = ["<h3>Deterministic checks</h3><ul>"]
        check_parts.extend(
            f"<li><code>{_html_escape(ref)}</code></li>"
            for ref in block.check_evidence_refs
        )
        check_parts.append("</ul>")
        check_refs_html = "".join(check_parts)

    built_in_html = ""
    if block.group_id or block.files_changed:
        built_parts = ["<h3>Built in</h3>"]
        if block.group_id:
            built_parts.append(f"<p>Group: <code>{_html_escape(block.group_id)}</code></p>")
        if block.files_changed:
            built_parts.append("<ul class='files'>")
            for f in block.files_changed:
                built_parts.append(f"<li><code>{_html_escape(f)}</code></li>")
            built_parts.append("</ul>")
        built_in_html = "".join(built_parts)

    shared_with_html = ""
    if block.shared_with:
        shared_with_html = (
            "<h3>Cross-linked features</h3>"
            "<p>This walkthrough also evidences: "
            + ", ".join(
                f'<a href="#feature-{_html_escape(fid)}">{_html_escape(fid)}</a>'
                for fid in block.shared_with
            )
            + "</p>"
        )

    repair_history_html = ""
    if block.repair_history:
        repair_parts = ["<h3>Repair history</h3><ol>"]
        for entry in block.repair_history:
            attempt_n = entry.get("attempt") or "?"
            succeeded = entry.get("succeeded")
            label = "succeeded" if succeeded else "failed"
            repair_parts.append(
                f"<li>Attempt {_html_escape(str(attempt_n))}: {_html_escape(label)}</li>"
            )
        repair_parts.append("</ol>")
        repair_history_html = "".join(repair_parts)

    findings_html = ""
    if block.findings:
        finding_parts = ["<h3>Quality findings</h3><ul>"]
        for finding in block.findings:
            finding_parts.append(
                f'<li class="finding {_html_escape(finding.severity)}">'
                f"[{_html_escape(finding.severity)}] {_html_escape(finding.text)}"
                "</li>"
            )
        finding_parts.append("</ul>")
        findings_html = "".join(finding_parts)

    return _render_feature_template(
        {
            "feature_id": _html_escape(block.feature_id),
            "name": _html_escape(block.name),
            "verdict_badge": _verdict_badge_html(block.verdict),
            "description_html": description_html,
            "detail_html": detail_html,
            "honesty_html": honesty_html,
            "audit_narrative_html": audit_narrative_html,
            "walkthrough_html": walkthrough_html,
            "check_refs_html": check_refs_html,
            "built_in_html": built_in_html,
            "shared_with_html": shared_with_html,
            "repair_history_html": repair_history_html,
            "findings_html": findings_html,
        }
    )


def validate_walkthrough_coverage(
    entries: list[WalkthroughEntry],
    spec: Spec,
) -> CoverageReport:
    """Compute coverage stats over a list of parsed walkthrough entries.

    Per research §A2: ≥90% of non-exploration entries must have
    feature_ids[] populated, and emitted feature_ids must reference
    known Feature ids. Use `report.meets_threshold()` to gate.

    Per-Feature evidence count helps surface Features with zero
    evidence (research §4: a Feature with 0 evidence refs returns
    `verdict: missing` not `passed`).
    """
    total = len(entries)
    exploration = sum(1 for e in entries if e.action_kind == "exploration")
    tagged = sum(
        1 for e in entries
        if e.action_kind != "exploration" and e.feature_ids
    )
    untagged = sum(
        1 for e in entries
        if e.action_kind != "exploration" and not e.feature_ids
    )
    known_feature_ids = {f.id for f in spec.features}
    unknown_refs: list[str] = []
    per_feature_count: dict[str, int] = {f.id: 0 for f in spec.features}
    for entry in entries:
        for fid in entry.feature_ids:
            if fid and fid in known_feature_ids:
                per_feature_count[fid] = per_feature_count.get(fid, 0) + 1
            elif fid and fid not in known_feature_ids and fid not in unknown_refs:
                unknown_refs.append(fid)
    return CoverageReport(
        total_entries=total,
        exploration_entries=exploration,
        tagged_entries=tagged,
        untagged_entries=untagged,
        unknown_feature_id_refs=unknown_refs,
        per_feature_evidence_count=per_feature_count,
    )


def validate_walkthrough_jsonl_strict(
    jsonl_path: Path,
    spec: Spec,
) -> tuple[CoverageReport, list[str]]:
    """Strict A1b.7 enforcement of a walkthrough.jsonl file.

    Reads `jsonl_path` line-by-line, parses each entry with
    `parse_walkthrough_entry(strict=True)`, drops entries that violate
    the action_kind / feature_id contract, and returns the resulting
    coverage report plus a flat list of human-readable rejection
    reasons (one entry per dropped line, prefixed with `line N:`).

    Distinct from the permissive `_validate_walkthrough_jsonl` in
    `otto/audit.py`, which keeps malformed-but-recoverable entries and
    surfaces oddities as warnings only.

    The returned `CoverageReport` is computed only over surviving
    (i.e. contract-compliant) entries — callers can then call
    `report.meets_threshold(...)` to gate on the §A2 ≥90% rule.
    """
    rejections: list[str] = []
    entries: list[WalkthroughEntry] = []

    if not jsonl_path.exists():
        return validate_walkthrough_coverage(entries, spec), rejections

    raw = jsonl_path.read_text(encoding="utf-8")
    for i, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            rejections.append(f"line {i}: invalid JSON ({exc})")
            continue
        entry, messages = parse_walkthrough_entry(payload, spec, strict=True)
        if entry is None:
            rejections.append(f"line {i}: " + "; ".join(messages))
            continue
        entries.append(entry)

    return validate_walkthrough_coverage(entries, spec), rejections


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
    groups: list[Group] = field(default_factory=list)
    cross_group_checks: list[CheckKind] = field(default_factory=list)
    shared_scaffold: list[str] = field(default_factory=list)
    # A1a additions (research §2.6, §2.7, §4):
    features: list[Feature] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    guardrails: list[Guardrail] = field(default_factory=list)
    shared_paths: list[str] = field(default_factory=list)
    audit_fixtures: list[AuditFixture] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    done_means: list[str] = field(default_factory=list)
    amendments: list[Amendment] = field(default_factory=list)

    def __init__(
        self,
        *,
        schema_version: int = SCHEMA_VERSION,
        intent: str = "",
        intent_hash: str = "",
        project_kind: str = "webapp",
        structure: StructureDecisions | None = None,
        groups: list[Group] | None = None,
        cross_group_checks: list[CheckKind] | None = None,
        shared_scaffold: list[str] | None = None,
        non_goals: list[str] | None = None,
        done_means: list[str] | None = None,
        amendments: list[Amendment] | None = None,
        # A1a additions
        features: list[Feature] | None = None,
        components: list[Component] | None = None,
        guardrails: list[Guardrail] | None = None,
        shared_paths: list[str] | None = None,
        audit_fixtures: list[AuditFixture] | None = None,
    ) -> None:
        self.schema_version = schema_version
        self.intent = intent
        self.intent_hash = intent_hash
        self.project_kind = project_kind
        self.structure = structure if structure is not None else StructureDecisions()
        self.groups = groups if groups is not None else []
        self.cross_group_checks = cross_group_checks if cross_group_checks is not None else []
        self.shared_scaffold = shared_scaffold if shared_scaffold is not None else []
        self.non_goals = non_goals if non_goals is not None else []
        self.done_means = done_means if done_means is not None else []
        self.amendments = amendments if amendments is not None else []
        # A1a additions
        self.features = features if features is not None else []
        self.components = components if components is not None else []
        self.guardrails = guardrails if guardrails is not None else []
        self.shared_paths = shared_paths if shared_paths is not None else []
        self.audit_fixtures = audit_fixtures if audit_fixtures is not None else []


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
    group_entries = []
    for s in spec.groups:
        entry: dict[str, Any] = {
            "id": s.id,
            "name": s.name,
            "feature_ids": list(s.feature_ids),
            "dependencies": list(s.dependencies),
            "owned_paths": list(s.owned_paths),
            "checks": [_check_to_dict(c) for c in s.checks],
        }
        if s.dispatch_plan:
            entry["dispatch_plan"] = s.dispatch_plan
        group_entries.append(entry)
    return {
        "schema_version": spec.schema_version,
        "intent": spec.intent,
        "intent_hash": spec.intent_hash,
        "project_kind": spec.project_kind,
        "structure": {"payload": dict(spec.structure.payload)},
        "groups": group_entries,
        "cross_group_checks": [_check_to_dict(c) for c in spec.cross_group_checks],
        "shared_scaffold": list(spec.shared_scaffold),
        "non_goals": list(spec.non_goals),
        "done_means": list(spec.done_means),
        "amendments": [dataclasses.asdict(a) for a in spec.amendments],
        # A1a additions: features, components, guardrails, shared_paths,
        # audit_fixtures. Empty lists serialise to [] (legacy specs without
        # these keys read as empty lists via parse_spec defaults).
        "features": [_feature_to_dict(f) for f in spec.features],
        "components": [_component_to_dict(c) for c in spec.components],
        "guardrails": [dataclasses.asdict(g) for g in spec.guardrails],
        "shared_paths": list(spec.shared_paths),
        "audit_fixtures": [dataclasses.asdict(fx) for fx in spec.audit_fixtures],
    }


# ---------------------------------------------------------------------------
# A1a serialisation helpers
# ---------------------------------------------------------------------------


def _feature_to_dict(feature: Feature) -> dict[str, Any]:
    return {
        "id": feature.id,
        "name": feature.name,
        "description": feature.description,
        "acceptance_detail": feature.acceptance_detail,
        "evidence_kinds": list(feature.evidence_kinds),
        "group_id": feature.group_id,
        "verdict": feature.verdict,
        "evidence_completeness": feature.evidence_completeness,
        "coverage_confidence": feature.coverage_confidence,
        "multi_actor_required": feature.multi_actor_required,
        "audit_pre_merge": feature.audit_pre_merge,
    }


def _feature_from_dict(payload: dict[str, Any]) -> Feature:
    """Permissive parser; missing fields take dataclass defaults."""
    return Feature(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        acceptance_detail=str(payload.get("acceptance_detail") or ""),
        evidence_kinds=[str(e) for e in (payload.get("evidence_kinds") or [])],
        group_id=str(payload.get("group_id") or ""),
        verdict=payload.get("verdict"),
        evidence_completeness=str(payload.get("evidence_completeness") or "full"),
        coverage_confidence=str(payload.get("coverage_confidence") or "high"),
        multi_actor_required=bool(payload.get("multi_actor_required", False)),
        audit_pre_merge=bool(payload.get("audit_pre_merge", False)),
    )


def _component_to_dict(component: Component) -> dict[str, Any]:
    return {
        "id": component.id,
        "name": component.name,
        "description": component.description,
        "owned_paths": list(component.owned_paths),
        "dependencies": list(component.dependencies),
        "checks": [_check_to_dict(c) for c in component.checks],
        "consumed_by": list(component.consumed_by),
    }


def _component_from_dict(
    payload: dict[str, Any],
    *,
    collector: WarningCollector | None = None,
    path: str = "",
) -> Component:
    raw_checks = payload.get("checks") or []
    checks: list[CheckKind] = []
    for index, c_payload in enumerate(raw_checks):
        check = _check_from_dict(
            c_payload, collector=collector, path=f"{path}.checks[{index}]"
        )
        if check is not None:
            checks.append(check)
    return Component(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        owned_paths=[str(p) for p in (payload.get("owned_paths") or [])],
        dependencies=[str(d) for d in (payload.get("dependencies") or [])],
        checks=checks,
        consumed_by=[str(f) for f in (payload.get("consumed_by") or [])],
    )


def _guardrail_from_dict(payload: dict[str, Any]) -> Guardrail:
    return Guardrail(
        id=str(payload.get("id") or ""),
        text=str(payload.get("text") or ""),
        applies_to=str(payload.get("applies_to") or "*"),
    )


def _audit_fixture_from_dict(payload: dict[str, Any]) -> AuditFixture:
    return AuditFixture(
        kind=str(payload.get("kind") or ""),
        payload=dict(payload.get("payload") or {}),
    )


def parse_spec(data: Any) -> tuple[Spec, list[ValidationWarning]]:
    """Permissively parse a Spec from a JSON-decoded payload.

    v2.1 design (docs/intent-to-product-v2-plan.md):

    - **Coerces** obvious mistakes (non-list groups wrapped, non-dict
      amendments synthesized, unknown check kinds dropped).
    - **Warns** for departures from recommended shape. Warnings are
      returned alongside the Spec so callers can surface them in the
      journal/proof packet.
    - **Hard-rejects** ONLY on truly unusable input: not a dict, or no
      groups AND no intent AND no structure (literally nothing to do).

    The compile-stage cheap-fail class (R3, R5, R8, R17, R22, R26) is
    closed by this function: malformed agent output now produces a
    workable Spec + warnings instead of group-blocking exceptions.
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

    # ---- groups ----
    # Canonical key is "groups". One-cycle deprecation: still read
    # "slices" if "groups" is absent so legacy on-disk specs round-trip,
    # but emit a deprecation warning so callers migrate.
    raw_groups = data.get("groups")
    if raw_groups is None and "slices" in data:
        collector.add(
            code="spec.deprecated.slices_key",
            path="slices",
            message="'slices' JSON key is deprecated; use 'groups'",
        )
        raw_groups = data.get("slices")
    if raw_groups is None:
        groups_data: list[Any] = []
    elif isinstance(raw_groups, list):
        groups_data = raw_groups
    elif isinstance(raw_groups, dict):
        # Single group declared at top level → wrap.
        collector.add(
            code="spec.coerce.field",
            path="groups",
            message="groups was a dict; wrapped in a single-element list",
            coerced_to="[<dict>]",
        )
        groups_data = [raw_groups]
    else:
        collector.add(
            code="spec.coerce.field",
            path="groups",
            message=f"groups should be a list, got {type(raw_groups).__name__}; using empty list",
            coerced_to="[]",
        )
        groups_data = []

    groups: list[Group] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(groups_data):
        if not isinstance(entry, dict):
            collector.add(
                code="spec.coerce.group",
                path=f"groups[{index}]",
                message=f"group entry is {type(entry).__name__}, not dict; dropped",
            )
            continue

        group_id = _coerce_group_id(
            entry.get("id"), index=index, seen=seen_ids, collector=collector
        )
        seen_ids.add(group_id)

        checks: list[CheckKind] = []
        # Pattern E: warn explicitly when checks is missing or wrong-typed,
        # rather than silently coercing to []. A group with no checks
        # vacuously passes — operators must see this in the validator
        # report, not have it hidden by a permissive `or []`.
        raw_checks = entry.get("checks")
        if raw_checks is None:
            collector.add(
                code="spec.coerce.field",
                path=f"groups[{index}].checks",
                message="checks field missing on group; using []",
                coerced_to="[]",
            )
            raw_checks = []
        elif not isinstance(raw_checks, list):
            collector.add(
                code="spec.coerce.field",
                path=f"groups[{index}].checks",
                message=f"checks should be a list, got {type(raw_checks).__name__}; using []",
                coerced_to="[]",
            )
            raw_checks = []
        for c_index, c_payload in enumerate(raw_checks):
            check = _check_from_dict(
                c_payload,
                collector=collector,
                path=f"groups[{index}].checks[{c_index}]",
            )
            if check is not None:
                checks.append(check)

        # S5 fix: warn on silent coercion of feature_ids/dependencies/owned_paths.
        # Pattern E already did this for `checks`; the same hole exists
        # for the other list fields. Under-specified groups that look
        # valid because the parser swallowed the gap are exactly the
        # bugs the validator should catch.
        def _coerce_str_list(field_name: str, *legacy_aliases: str) -> list[str]:
            raw = entry.get(field_name)
            used = field_name
            if raw is None:
                for alias in legacy_aliases:
                    if alias in entry:
                        raw = entry.get(alias)
                        used = alias
                        collector.add(
                            code="spec.deprecated.group_field",
                            path=f"groups[{index}].{alias}",
                            message=f"'{alias}' is deprecated; use '{field_name}'",
                        )
                        break
            if raw is None:
                collector.add(
                    code="spec.coerce.field",
                    path=f"groups[{index}].{field_name}",
                    message=f"{field_name} field missing on group; using []",
                    coerced_to="[]",
                )
                return []
            if not isinstance(raw, list):
                collector.add(
                    code="spec.coerce.field",
                    path=f"groups[{index}].{used}",
                    message=f"{used} should be a list, got {type(raw).__name__}; using []",
                    coerced_to="[]",
                )
                return []
            return [str(item) for item in raw]

        # Accept legacy "title" key for "name" (one-cycle deprecation).
        name_raw = entry.get("name")
        if name_raw is None and "title" in entry:
            collector.add(
                code="spec.deprecated.group_field",
                path=f"groups[{index}].name",
                message="'title' is deprecated; use 'name'",
            )
            name_raw = entry.get("title")
        groups.append(Group(
            id=group_id,
            name=str(name_raw or ""),
            feature_ids=_coerce_str_list("feature_ids", "tasks"),
            dependencies=_coerce_str_list("dependencies", "deps"),
            owned_paths=_coerce_str_list("owned_paths"),
            checks=checks,
            dispatch_plan=str(entry.get("dispatch_plan") or ""),
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

    # ---- cross-group checks ----
    # Pattern E: same as per-group checks — warn on missing/wrong-typed
    # field rather than silently defaulting to [].
    cross_group_checks: list[CheckKind] = []
    raw_cross = data.get("cross_group_checks")
    if raw_cross is None and "cross_slice_checks" in data:
        collector.add(
            code="spec.deprecated.cross_slice_checks_key",
            path="cross_slice_checks",
            message="'cross_slice_checks' JSON key is deprecated; use 'cross_group_checks'",
        )
        raw_cross = data.get("cross_slice_checks")
    if raw_cross is None:
        # Field absent is fine — many specs have no cross-group checks.
        # Don't warn for this case; only warn when present-but-wrong-type.
        raw_cross = []
    elif not isinstance(raw_cross, list):
        collector.add(
            code="spec.coerce.field",
            path="cross_group_checks",
            message=f"cross_group_checks should be a list, got {type(raw_cross).__name__}; using []",
            coerced_to="[]",
        )
        raw_cross = []
    for index, c_payload in enumerate(raw_cross):
        check = _check_from_dict(
            c_payload, collector=collector, path=f"cross_group_checks[{index}]"
        )
        if check is not None:
            cross_group_checks.append(check)

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
    if not intent.strip() and not groups and not structure_payload:
        raise SpecValidationError(
            "spec is empty: no intent, no groups, no structure — nothing to build"
        )

    # intent_hash: preserve what's on disk verbatim. If absent, leave
    # empty — back-compat specs round-trip identically. New specs
    # explicitly call `lock_intent(spec)` (compile_spec does this) to
    # stamp the bedrock layer.
    intent_hash = str(data.get("intent_hash") or "").strip()

    # A1a fields — permissively absent on legacy specs; default empty.
    raw_features = data.get("features") or []
    features_parsed: list[Feature] = []
    for index, f_payload in enumerate(raw_features):
        if isinstance(f_payload, dict):
            features_parsed.append(_feature_from_dict(f_payload))
        else:
            collector.add(
                code="spec.coerce.field",
                path=f"features[{index}]",
                message=f"feature entry is {type(f_payload).__name__}, not dict; skipped",
            )

    raw_components = data.get("components") or []
    components_parsed: list[Component] = []
    for index, c_payload in enumerate(raw_components):
        if isinstance(c_payload, dict):
            components_parsed.append(
                _component_from_dict(
                    c_payload, collector=collector, path=f"components[{index}]"
                )
            )
        else:
            collector.add(
                code="spec.coerce.field",
                path=f"components[{index}]",
                message=f"component entry is {type(c_payload).__name__}, not dict; skipped",
            )

    raw_guardrails = data.get("guardrails") or []
    guardrails_parsed: list[Guardrail] = []
    for index, g_payload in enumerate(raw_guardrails):
        if isinstance(g_payload, dict):
            guardrails_parsed.append(_guardrail_from_dict(g_payload))
        else:
            collector.add(
                code="spec.coerce.field",
                path=f"guardrails[{index}]",
                message=f"guardrail entry is {type(g_payload).__name__}, not dict; skipped",
            )

    raw_audit_fixtures = data.get("audit_fixtures") or []
    audit_fixtures_parsed: list[AuditFixture] = []
    for index, fx_payload in enumerate(raw_audit_fixtures):
        if isinstance(fx_payload, dict):
            audit_fixtures_parsed.append(_audit_fixture_from_dict(fx_payload))
        else:
            collector.add(
                code="spec.coerce.field",
                path=f"audit_fixtures[{index}]",
                message=f"audit_fixture entry is {type(fx_payload).__name__}, not dict; skipped",
            )

    raw_schema_version = data.get("schema_version")
    if raw_schema_version is None or raw_schema_version == "":
        # Absent schema_version on disk — treat as v1 because every spec
        # written before the round-3 bump omitted the field. The parser
        # has already accepted any legacy keys present (with deprecation
        # warnings); recording v1 here makes the on-disk lineage honest.
        coerced_schema_version = 1 if any(
            k in data for k in SCHEMA_LEGACY_KEYS_V1
        ) else SCHEMA_VERSION
    else:
        try:
            coerced_schema_version = int(raw_schema_version)
        except (TypeError, ValueError):
            collector.add(
                code="spec.coerce.field",
                path="schema_version",
                message=(
                    f"schema_version is {raw_schema_version!r} "
                    f"(not an integer); using {SCHEMA_VERSION}"
                ),
                coerced_to=str(SCHEMA_VERSION),
            )
            coerced_schema_version = SCHEMA_VERSION

    # Round-3 audit gap 4: time-bound the deprecation window.
    # v1 specs read with a single advisory warning; v2 specs that carry
    # leftover legacy keys get a louder warning so the next bump can
    # safely drop the read-fallback entirely.
    if coerced_schema_version < 2:
        if any(k in data for k in SCHEMA_LEGACY_KEYS_V1):
            collector.add(
                code="spec.deprecated.schema_v1_read",
                path="schema_version",
                message=(
                    "spec.json was written under schema v1 (legacy keys "
                    f"{[k for k in SCHEMA_LEGACY_KEYS_V1 if k in data]}); "
                    "read-fallback active for one more cycle. Re-persist "
                    "to migrate to v2."
                ),
            )
    elif coerced_schema_version >= 2:
        leftover_top = [k for k in SCHEMA_LEGACY_KEYS_V1 if k in data]
        if leftover_top:
            collector.add(
                code="spec.deprecated.schema_v2_legacy_top_keys",
                path="schema_version",
                message=(
                    f"spec declares schema_version={coerced_schema_version} "
                    f"but still carries legacy v1 top-level keys "
                    f"{leftover_top}; these will be dropped on the next "
                    "schema bump. Remove them from on-disk specs."
                ),
            )
        # Per-group legacy fields too: same deal at field granularity.
        leftover_group: list[str] = []
        if isinstance(data.get("groups"), list):
            for index, entry in enumerate(data.get("groups") or []):
                if not isinstance(entry, dict):
                    continue
                for legacy_field in SCHEMA_LEGACY_GROUP_FIELDS_V1:
                    if legacy_field in entry:
                        leftover_group.append(f"groups[{index}].{legacy_field}")
        if leftover_group:
            collector.add(
                code="spec.deprecated.schema_v2_legacy_group_fields",
                path="groups",
                message=(
                    f"spec declares schema_version={coerced_schema_version} "
                    f"but still carries legacy v1 per-group fields "
                    f"{leftover_group}; these will be dropped on the next "
                    "schema bump. Re-persist via the spec-review edit flow "
                    "to migrate."
                ),
            )

    spec = Spec(
        schema_version=coerced_schema_version,
        intent=intent,
        intent_hash=intent_hash,
        project_kind=project_kind,
        structure=StructureDecisions(payload=dict(structure_payload)),
        groups=groups,
        cross_group_checks=cross_group_checks,
        shared_scaffold=[str(p) for p in (data.get("shared_scaffold") or [])],
        non_goals=[str(g) for g in (data.get("non_goals") or [])],
        done_means=[str(g) for g in (data.get("done_means") or [])],
        amendments=amendments,
        # A1a fields — permissively read; absent → empty list
        features=features_parsed,
        components=components_parsed,
        guardrails=guardrails_parsed,
        shared_paths=[str(p) for p in (data.get("shared_paths") or [])],
        audit_fixtures=audit_fixtures_parsed,
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


def _coerce_group_id(
    raw: Any,
    *,
    index: int,
    seen: set[str],
    collector: WarningCollector,
) -> str:
    """Slugify and disambiguate a group id.

    v2.1: accept any non-empty unique string. Internally slugify for
    path-safe use. Empty / non-string values get a synthesized id.
    """
    text = str(raw or "").strip()
    if not text:
        new_id = f"group_{index}"
        collector.add(
            code="spec.coerce.group_id",
            path=f"groups[{index}].id",
            message=f"group id missing; synthesized {new_id!r}",
            coerced_to=new_id,
        )
        return new_id
    slug = _SLUG_RE.sub("-", text.lower()).strip("-_")
    if not slug:
        slug = f"group_{index}"
    if slug != text:
        collector.add(
            code="spec.coerce.group_id",
            path=f"groups[{index}].id",
            message=f"group id {text!r} slugified to {slug!r}",
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
            path=f"groups[{index}].id",
            message=f"duplicate group id {slug!r}; renamed to {candidate!r}",
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


_GROUP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


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
    cycles = _detect_dep_cycles(spec.groups)
    errors.extend(cycles)

    # ---- former-errors-now-warnings ----
    if spec.project_kind not in PROJECT_KINDS:
        warnings.append(f"project_kind {spec.project_kind!r} not in {PROJECT_KINDS} (free-form accepted)")

    if not spec.intent.strip():
        warnings.append("intent is empty")

    if not spec.groups:
        warnings.append("spec declares no groups")
    elif len(spec.groups) > 1 and not spec.cross_group_checks:
        # S4 fix: a multi-group product without integration tests means
        # each group passes in isolation but their composition is
        # unverified. Surface as a warning so the spec author can
        # decide whether to add checks or accept the gap.
        warnings.append(
            "multi-group spec declares no cross_group_checks "
            "(integration testing is missing — groups may pass in "
            "isolation while their composition is broken)"
        )

    seen_ids: set[str] = set()
    for group_ in spec.groups:
        if not _GROUP_ID_RE.match(group_.id):
            warnings.append(f"group id {group_.id!r} does not match recommended pattern {_GROUP_ID_RE.pattern}")
        if group_.id in seen_ids:
            warnings.append(f"duplicate group id: {group_.id!r}")
        seen_ids.add(group_.id)
        if not group_.name.strip():
            warnings.append(f"group {group_.id!r}: name is empty")
        if not group_.checks:
            warnings.append(f"group {group_.id!r}: no checks declared (vacuously passes)")
        # S1 fix: feature_ids must be present and concrete enough that
        # two independent build agents cannot drift on what to do. Empty
        # feature_ids → vacuous group (BLOCKED downstream is unhelpful;
        # better to surface at validate). Single-word/very-short entries
        # → likely vague prose (e.g. "implement", "build it") rather
        # than actionable steps tied to file paths or API shapes.
        if not group_.feature_ids:
            warnings.append(
                f"group {group_.id!r}: feature_ids field empty (no concrete work declared)"
            )
        else:
            for t_idx, fid in enumerate(group_.feature_ids):
                stripped = fid.strip()
                if len(stripped) < 10:
                    warnings.append(
                        f"group {group_.id!r}.feature_ids[{t_idx}]: entry too vague "
                        f"({stripped!r}); needs a concrete action tied to file "
                        f"paths, API shapes, or behaviors"
                    )

    for group_ in spec.groups:
        for dep in group_.dependencies:
            if dep not in seen_ids:
                warnings.append(f"group {group_.id!r}: dep {dep!r} not in spec")

    schema = _load_kind_schema(spec.project_kind)
    if schema is not None:
        warnings.extend(_validate_against_schema(spec.structure.payload, schema, path="structure.payload"))

    if strict:
        # Legacy v1 mode: warnings become errors.
        errors.extend(warnings)
        warnings = []

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _detect_dep_cycles(groups: list[Group]) -> list[str]:
    """Return human-readable cycle errors; empty when DAG."""
    by_id = {s.id: s for s in groups}
    visiting: set[str] = set()
    visited: set[str] = set()
    errors: list[str] = []

    def walk(node: str, path: list[str]) -> None:
        if node in visiting:
            cycle = path[path.index(node):] + [node]
            errors.append(f"group dep cycle: {' -> '.join(cycle)}")
            return
        if node in visited or node not in by_id:
            return
        visiting.add(node)
        for dep in by_id[node].dependencies:
            walk(dep, path + [node])
        visiting.discard(node)
        visited.add(node)

    for group_ in groups:
        walk(group_.id, [])
    return errors


# ---------------------------------------------------------------------------
# Persistence — immutability semantics
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_amendment(
    spec: Spec,
    *,
    reason: str,
    actor: str,
    prior_sha256: str | None = None,
    trigger_event_id: str = "",
    tier: int = 0,
) -> Spec:
    """Return a new `Spec` with an amendment appended.

    Use this whenever the spec content changes after the initial write.
    `prior_sha256` is the content hash before the user's edit; if absent
    we use `spec.amendments[-1].diff_sha256_after` (or empty for first
    edit). `reason` must be non-empty.

    S2 fix: callers may pass `trigger_event_id` and `tier` so the
    amendment record cites the journal event that motivated it.
    Without these, replay can't reconstruct what triggered an amendment
    and the v2.2 traceability promise is broken.
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
                trigger_event_id=trigger_event_id,
                tier=tier,
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
# A6.1 — Brownfield project preamble (research §9.4)
# ---------------------------------------------------------------------------
#
# `build_project_preamble(project_dir)` composes a Markdown summary of an
# existing project that the brownfield compile-spec prompt prepends to its
# instructions. It is intentionally narrow: file list (capped), README
# excerpt (capped), and the first manifest file we recognize (capped).
# The compile agent uses Claude Code's Read/Glob/Grep tools to dive deeper
# from there. We do NOT preload entire files into the prompt — that's the
# agent's job.

# Common directories we never want in the file list. Skipped both in
# git-fallback mode and during glob enumeration.
_BROWNFIELD_PREAMBLE_IGNORE_PARTS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".worktrees",
    "otto_logs",
    "bench-results",
    ".idea",
    ".vscode",
    ".DS_Store",
})

# Manifest files we surface, in priority order (first found wins).
_BROWNFIELD_PREAMBLE_MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "setup.py",
    "Gemfile",
)


def _git_tracked_files(project_dir: Path) -> list[str] | None:
    """Return tracked file paths (relative, posix) or None if not a git repo."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def _glob_filtered_files(project_dir: Path) -> list[str]:
    """Fallback enumeration: walk the tree, skipping ignored dirs."""
    out: list[str] = []
    for path in project_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(project_dir)
        except ValueError:
            continue
        parts = rel.parts
        if any(p in _BROWNFIELD_PREAMBLE_IGNORE_PARTS for p in parts):
            continue
        out.append(rel.as_posix())
    return sorted(out)


def _read_capped(path: Path, max_lines: int) -> tuple[str, int]:
    """Read up to `max_lines` lines from `path`. Returns (text, total_lines)."""
    try:
        full = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ("", 0)
    lines = full.splitlines()
    capped = "\n".join(lines[:max_lines])
    return capped, len(lines)


def build_project_preamble(project_dir: Path) -> str:
    """Compose the brownfield-compile preamble (research §9.4, A6.1).

    Sections:
        ## File tree
            git-tracked files (or glob fallback), capped at
            `BROWNFIELD_PREAMBLE_MAX_FILES`.
        ## README
            README.md first `BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE` lines
            (omitted if not present).
        ## Manifest
            First detected manifest from `_BROWNFIELD_PREAMBLE_MANIFESTS`,
            capped at `BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE` lines
            (omitted if none present).

    Pure-Python; no LLM calls. Deterministic given fixed `project_dir`
    contents (file order is sorted, caps are stable).

    Returns a Markdown string suitable for direct interpolation into the
    `compile-spec-brownfield.md` prompt template.
    """
    from otto.defaults import (
        BROWNFIELD_PREAMBLE_MAX_FILES,
        BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE,
    )

    # ---- File tree ----
    tracked = _git_tracked_files(project_dir)
    if tracked is None:
        files = _glob_filtered_files(project_dir)
    else:
        files = sorted(
            f for f in tracked
            if not any(
                p in _BROWNFIELD_PREAMBLE_IGNORE_PARTS
                for p in Path(f).parts
            )
        )
    truncated_count = max(0, len(files) - BROWNFIELD_PREAMBLE_MAX_FILES)
    file_lines = files[:BROWNFIELD_PREAMBLE_MAX_FILES]

    parts: list[str] = []
    if file_lines:
        parts.append("## File tree")
        parts.append("")
        parts.append("```")
        parts.extend(file_lines)
        if truncated_count:
            parts.append(f"… ({truncated_count} more files; not shown)")
        parts.append("```")
    else:
        parts.append("## File tree")
        parts.append("")
        parts.append("(empty project — no tracked files found)")

    # ---- README ----
    readme = project_dir / "README.md"
    if not readme.exists():
        # Common variants
        for alt in ("README", "README.rst", "README.txt"):
            cand = project_dir / alt
            if cand.exists():
                readme = cand
                break
    if readme.exists():
        text, total = _read_capped(readme, BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE)
        if text.strip():
            parts.append("")
            parts.append(f"## README ({readme.name})")
            parts.append("")
            parts.append("```")
            parts.append(text)
            if total > BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE:
                parts.append(
                    f"… ({total - BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE}"
                    " more lines; not shown)"
                )
            parts.append("```")

    # ---- Manifest ----
    for mfile in _BROWNFIELD_PREAMBLE_MANIFESTS:
        cand = project_dir / mfile
        if not cand.exists():
            continue
        text, total = _read_capped(cand, BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE)
        if not text.strip():
            continue
        parts.append("")
        parts.append(f"## Manifest ({mfile})")
        parts.append("")
        parts.append("```")
        parts.append(text)
        if total > BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE:
            parts.append(
                f"… ({total - BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE}"
                " more lines; not shown)"
            )
        parts.append("```")
        break  # First manifest wins; agent can Read others if needed.

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# A6.5 — Out-of-scope intent guard (research §9.5b)
# ---------------------------------------------------------------------------
#
# Otto cannot meaningfully verify systems-level products (browser engines,
# kernels, language compilers, database engines, embedded firmware,
# device drivers, ...). The audit instruments (BrowserJourney, ApiProbe,
# StateInvariant, RepoTestCheck) cannot judge memory safety, sandboxing,
# spec compliance, or correctness under adversarial inputs. Producing a
# verdict for these intents would be dishonest by construction.
#
# We detect out-of-scope intents BEFORE LLM cost via a small, deliberate
# keyword list. Phrases are precise enough to avoid false positives (e.g.
# "browser-based UI" contains "browser" but is not a "browser engine").
#
# User override: if the intent literally contains "override-scope", the
# guard is bypassed. The proof packet renderer will surface this on the
# rendered output (research §9.5b: "this is outside Otto's verified scope;
# treat verdict as suggestive").

# Keep these phrases multi-token so single-word matches like "browser" in
# benign contexts ("browser-based UI", "kernel of the algorithm") don't
# false-positive. v1: keyword match. v2: LLM classifier for nuance.
OUT_OF_SCOPE_KEYWORDS: tuple[str, ...] = (
    "browser engine",
    "web browser",
    "javascript runtime",
    "language compiler",
    "database engine",
    "operating system kernel",
    "os kernel",
    "linux kernel",
    "hypervisor",
    "embedded firmware",
    "device driver",
    "memory allocator",
    "garbage collector",
)

# Literal token in the intent text that disables the guard. Documented
# in the SpecValidationError message so users discover the override.
OUT_OF_SCOPE_OVERRIDE_TOKEN = "override-scope"


def detect_out_of_scope_intent(intent: str) -> str | None:
    """Return the matched out-of-scope keyword, or None if intent is in scope.

    Case-insensitive substring match. Returns None if the intent contains
    `OUT_OF_SCOPE_OVERRIDE_TOKEN` (user-explicit override).
    """
    if not intent:
        return None
    lowered = intent.lower()
    if OUT_OF_SCOPE_OVERRIDE_TOKEN in lowered:
        return None
    for keyword in OUT_OF_SCOPE_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


# ---------------------------------------------------------------------------
# A6.4 — Brownfield additive-mode reconciliation
# ---------------------------------------------------------------------------
#
# When `compile_spec(brownfield=True, base_spec=...)` runs against a project
# that already has a Spec, the agent's output is the "what changed / what's
# new" view. We reconcile against `base_spec` to preserve mechanical state
# (Group owned_paths/deps, Feature audit verdicts) that the agent doesn't
# track but downstream stages depend on.
#
# Rules (research §9.4):
#   * Group: id-keyed; new spec wins on title/owned_paths/etc. (agent has
#     fresh view), but conflicting titles emit a warning so silent drift
#     is visible.
#   * Feature: id-keyed; PRESERVE base.feature audit/coverage state
#     (verdict, evidence_completeness, coverage_confidence,
#     multi_actor_required, audit_pre_merge) on matching ids — these come
#     from prior audit runs, not the compile agent.
#   * Component: id-keyed; new wins on overlap; base-only components
#     carry forward.
#   * Guardrail: union by `text` (case-sensitive); dedupe.
#   * intent + intent_hash: from base. Brownfield additive does NOT
#     change the intent — that's the user's intent override path, not
#     the agent's.
#   * structure / shared_paths / non_goals / done_means / amendments /
#     audit_fixtures: from base if present (these are mechanical/
#     historical fields the agent doesn't author).


def _reconcile_brownfield(new_spec: Spec, base_spec: Spec) -> Spec:
    """Merge agent-emitted brownfield Spec with the prior base Spec.

    Preserves base mechanical/historical state where the new spec doesn't
    own that field; surfaces conflicts as warnings rather than silent drift.

    Returns a new Spec; neither input is mutated.
    """
    # ---- Groups ----
    base_groups_by_id = {g.id: g for g in base_spec.groups}
    seen_group_ids: set[str] = set()
    merged_groups: list[Group] = []
    for new_group in new_spec.groups:
        seen_group_ids.add(new_group.id)
        base_group = base_groups_by_id.get(new_group.id)
        if base_group is not None and base_group.name != new_group.name:
            logger.warning(
                "brownfield reconcile: group %r title changed %r → %r "
                "(accepting agent's title)",
                new_group.id,
                base_group.name,
                new_group.name,
            )
        merged_groups.append(new_group)
    # Carry forward base groups not re-emitted by the agent
    for base_group in base_spec.groups:
        if base_group.id not in seen_group_ids:
            merged_groups.append(base_group)

    # ---- Features ----
    base_features_by_id = {f.id: f for f in base_spec.features}
    seen_feature_ids: set[str] = set()
    merged_features: list[Feature] = []
    for new_feat in new_spec.features:
        seen_feature_ids.add(new_feat.id)
        base_feat = base_features_by_id.get(new_feat.id)
        if base_feat is None:
            merged_features.append(new_feat)
            continue
        # Preserve audit/coverage state from base; everything else from new.
        merged_features.append(
            dataclasses.replace(
                new_feat,
                verdict=base_feat.verdict,
                evidence_completeness=base_feat.evidence_completeness,
                coverage_confidence=base_feat.coverage_confidence,
                multi_actor_required=base_feat.multi_actor_required,
                audit_pre_merge=base_feat.audit_pre_merge,
            )
        )
    # Carry forward base features not re-emitted by the agent
    for base_feat in base_spec.features:
        if base_feat.id not in seen_feature_ids:
            merged_features.append(base_feat)

    # ---- Components ----
    seen_component_ids: set[str] = set()
    merged_components: list[Component] = []
    for new_comp in new_spec.components:
        seen_component_ids.add(new_comp.id)
        merged_components.append(new_comp)
    for base_comp in base_spec.components:
        if base_comp.id not in seen_component_ids:
            merged_components.append(base_comp)

    # ---- Guardrails (union by text) ----
    seen_texts: set[str] = set()
    merged_guardrails: list[Guardrail] = []
    for g in list(base_spec.guardrails) + list(new_spec.guardrails):
        if g.text in seen_texts:
            continue
        seen_texts.add(g.text)
        merged_guardrails.append(g)

    # ---- Mechanical / historical fields preserved from base ----
    return dataclasses.replace(
        new_spec,
        intent=base_spec.intent,
        intent_hash=base_spec.intent_hash,
        groups=merged_groups,
        features=merged_features,
        components=merged_components,
        guardrails=merged_guardrails,
        structure=base_spec.structure or new_spec.structure,
        shared_paths=list(base_spec.shared_paths) or list(new_spec.shared_paths),
        non_goals=list(base_spec.non_goals) or list(new_spec.non_goals),
        done_means=base_spec.done_means or new_spec.done_means,
        amendments=list(base_spec.amendments),
        audit_fixtures=list(base_spec.audit_fixtures)
        or list(new_spec.audit_fixtures),
        cross_group_checks=(
            list(base_spec.cross_group_checks)
            or list(new_spec.cross_group_checks)
        ),
        shared_scaffold=(
            list(base_spec.shared_scaffold)
            or list(new_spec.shared_scaffold)
        ),
    )


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
    brownfield: bool = False,
    base_spec: Spec | None = None,
) -> Spec:
    """Run the compile agent once and return the structured `Spec`.

    Mirrors `otto.spec.run_spec_agent` plumbing — same prompt rendering,
    same `make_agent_options(..., agent_type="spec")`, same per-version
    log subdir convention. Writes `<run_dir>/spec.json` with the initial
    amendment-free Spec.

    Args:
        brownfield: If True, dispatch to the brownfield prompt variant
            (`compile-spec-brownfield.md`) and prepend a Python-built
            project preamble. The agent reads the existing project and
            documents Features rather than designing new ones (A6.3).
        base_spec: Reserved for A6.4 additive mode (delta vs base spec).
            Currently surfaces a warning if non-None and ignored.

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

    # A6.5: out-of-scope guard runs BEFORE LLM cost. Greenfield AND
    # brownfield share this check — the intent is what's out of scope,
    # not the project state.
    matched_keyword = detect_out_of_scope_intent(intent)
    if matched_keyword is not None:
        raise SpecValidationError(
            f"intent matches out-of-scope keyword {matched_keyword!r} "
            f"(research §9.5b: Otto cannot meaningfully verify systems-level "
            f"products like {matched_keyword}). To override, include "
            f"{OUT_OF_SCOPE_OVERRIDE_TOKEN!r} in your intent text — the "
            f"proof packet will mark the verdict as suggestive."
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    spec_path = run_dir / SPEC_FILENAME

    if brownfield:
        prompt_template = COMPILE_PROMPT_BROWNFIELD
        prompt = render_prompt(
            prompt_template,
            intent=intent,
            spec_path=str(spec_path),
            project_context=f"project_kind={project_kind}",
            project_preamble=build_project_preamble(project_dir),
        )
    else:
        prompt_template = COMPILE_PROMPT
        prompt = render_prompt(
            prompt_template,
            intent=intent,
            spec_path=str(spec_path),
            project_context=f"project_kind={project_kind}",
        )
    prompt_entry = save_rendered_prompt(
        run_dir.parent / "prompts",
        template=prompt_template,
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

    # A6.4: brownfield additive mode reconciles the agent's "what's new"
    # output with the prior base_spec, preserving mechanical / historical
    # fields the agent doesn't author (audit verdicts, intent_hash,
    # amendments, structure, etc.).
    if brownfield and base_spec is not None:
        spec = _reconcile_brownfield(spec, base_spec)

    result = validate_spec(spec)
    if not result.valid:
        raise SpecValidationError(
            "compiled spec failed validation:\n  - " + "\n  - ".join(result.errors)
        )
    # E2E rubric finding: validator warnings used to be discarded
    # silently — operators never saw "feature_ids too vague" or "no
    # cross_group_checks" alerts that the validator emitted. Surface
    # them via the standard logger so they hit narrative.log AND
    # attach to the spec object so callers can render them.
    for warning in result.warnings:
        logger.warning("spec validator: %s", warning)
    setattr(spec, "_validator_warnings", list(result.warnings))

    persist_spec(spec, spec_path, allow_initial=True)
    return spec
