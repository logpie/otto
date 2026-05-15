"""Conservative context slicing for v5 child Leads.

The first shipped shape is intentionally safe:

- disabled by default at the runner level;
- full context fallback when scope detection is ambiguous;
- full IA contract JSON preserved in every CHARTER slice;
- full artifact paths written beside the slice so a child can recover.
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from otto.v5_capability_inventory import parse_information_architecture_contract

CHARTER_TARGET_LINES = 500


@dataclass(frozen=True)
class ChildScope:
    """Scope hints available for one child Lead."""

    child_id: str
    task_intent: str = ""
    owned_paths: list[str] = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)


@dataclass
class ContextSliceResult:
    """Files and audit payload produced for a child context slice."""

    spec: dict[str, Any]
    charter: str
    audit: dict[str, Any]
    artifact_index: dict[str, Any]
    context_note: str


@dataclass
class _ScopeAnalysis:
    included_entity_ids: set[str]
    included_action_ids: set[str]
    included_claim_ids: set[str]
    fallback_to_full: bool
    fallback_reason: str = ""


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_IA_HEADING_RE = re.compile(r"^##\s+Information Architecture Contract\s*$", re.MULTILINE)
_GLOBAL_SECTION_NAMES = {
    "agent operating notes",
    "detected infrastructure",
}
ScopeResolver = Callable[[dict[str, Any], ChildScope, str], ChildScope | dict[str, Any] | None]


def slice_spec_for_child(spec: Any, child_scope: ChildScope | dict[str, Any]) -> dict[str, Any]:
    """Return a child-scoped flat-spec payload.

    Full fallback is returned when scope cannot be detected confidently. In a
    real run this function is called only when the runner's opt-in slicing flag
    is enabled, so legacy projects continue to receive the full spec.
    """
    payload = _coerce_spec(spec)
    scope = _coerce_child_scope(child_scope)
    analysis = _analyze_spec_scope(payload, scope)
    if analysis.fallback_to_full:
        return copy.deepcopy(payload)
    return _slice_spec_payload(payload, analysis)


def _slice_spec_payload(
    spec_payload: dict[str, Any],
    analysis: _ScopeAnalysis,
) -> dict[str, Any]:
    out = copy.deepcopy(spec_payload)
    original_claims = [
        claim
        for claim in _as_list(spec_payload.get("intent_claims"))
        if isinstance(claim, dict)
    ]
    out["intent_claims"] = [
        copy.deepcopy(claim)
        for claim in original_claims
        if _obj_id(claim) in analysis.included_claim_ids
    ]

    sliced_entities: list[dict[str, Any]] = []
    for entity in _as_list(spec_payload.get("core_entities")):
        if not isinstance(entity, dict):
            continue
        entity_id = _obj_id(entity)
        if entity_id in analysis.included_entity_ids:
            sliced_entities.append(copy.deepcopy(entity))
            continue
        registry: dict[str, Any] = {"id": entity_id}
        registry["primary_actions"] = [
            {"id": action_id}
            for action_id in _entity_action_ids(entity)
        ]
        registry["_slice"] = "registry_only"
        sliced_entities.append(registry)
    out["core_entities"] = sliced_entities
    return out


def slice_charter_for_child(
    charter: str,
    child_scope: ChildScope | dict[str, Any],
) -> str:
    """Return a child-scoped CHARTER markdown string.

    The IA JSON block is preserved verbatim. Prose sections are filtered by
    scope terms, with operating notes and detected infrastructure retained as
    cross-cutting operational context.
    """
    scope = _coerce_child_scope(child_scope)
    if not _scope_has_any_hint(scope) or parse_information_architecture_contract(charter) is None:
        return charter

    tokens = _scope_tokens(scope)
    if not tokens:
        return charter

    sections = _split_markdown_sections(charter)
    if not sections:
        return charter

    kept: list[str] = []
    omitted_headings: list[str] = []
    kept_any_scoped_prose = False
    for heading, body in sections:
        heading_norm = heading.strip().lower()
        section_text = (heading + "\n" + body).strip("\n")
        if not heading:
            if body.strip():
                kept.append(body.strip("\n"))
            continue
        if _IA_HEADING_RE.match(heading):
            kept.append(section_text)
            continue
        if _section_name(heading_norm) in _GLOBAL_SECTION_NAMES:
            kept.append(section_text)
            continue
        if _section_matches_tokens(section_text, tokens):
            kept.append(section_text)
            kept_any_scoped_prose = True
        else:
            omitted_headings.append(_section_title(heading))

    if not kept:
        return charter

    if omitted_headings:
        note_lines = [
            "## Context Slice Notes",
            "",
            "Some CHARTER prose sections were omitted from this child slice.",
            "The full CHARTER path is listed in the artifact index below.",
        ]
        if not kept_any_scoped_prose:
            note_lines.append("No non-global prose section matched this child scope.")
        kept.append("\n".join(note_lines))

    return "\n\n".join(part.rstrip() for part in kept if part.strip()) + "\n"


def write_context_slice_for_child(
    *,
    project_dir: Path,
    child_session_dir: Path,
    child_scope: ChildScope | dict[str, Any],
    parent_spec_path: Path,
    full_charter_path: Path,
    child_spec_path: Path,
    scope_resolver: ScopeResolver | None = None,
) -> ContextSliceResult:
    """Write child slice artifacts and an auditable decision log."""
    scope = _coerce_child_scope(child_scope)
    spec_payload = _read_json_object(parent_spec_path)
    charter_text = _read_text(full_charter_path)

    spec_analysis = _analyze_spec_scope(spec_payload, scope)
    fallback_to_full = spec_analysis.fallback_to_full
    fallback_reason = spec_analysis.fallback_reason
    scope_resolution: dict[str, Any] = {
        "attempted": False,
        "status": "not_needed",
        "reason": "",
    }
    if fallback_to_full and _scope_resolution_needed(fallback_reason):
        scope_resolution = {
            "attempted": scope_resolver is not None,
            "status": "last_resort_full_context",
            "reason": fallback_reason,
        }
        if scope_resolver is not None:
            resolved_raw = scope_resolver(spec_payload, scope, fallback_reason)
            if resolved_raw is not None:
                resolved_scope = _coerce_child_scope(resolved_raw)
                resolved_analysis = _analyze_spec_scope(spec_payload, resolved_scope)
                if not resolved_analysis.fallback_to_full:
                    scope = resolved_scope
                    spec_analysis = resolved_analysis
                    fallback_to_full = False
                    fallback_reason = ""
                    scope_resolution = {
                        "attempted": True,
                        "status": "resolved",
                        "reason": "",
                        "resolved_scope": {
                            "task_intent": scope.task_intent,
                            "owned_paths": list(scope.owned_paths),
                            "action_ids": list(scope.action_ids),
                        },
                    }
                else:
                    scope_resolution = {
                        "attempted": True,
                        "status": "unresolved_last_resort_full_context",
                        "reason": resolved_analysis.fallback_reason,
                    }
    if not charter_text.strip():
        fallback_to_full = True
        fallback_reason = fallback_reason or "missing CHARTER.md"
    elif parse_information_architecture_contract(charter_text) is None:
        fallback_to_full = True
        fallback_reason = fallback_reason or "missing or invalid CHARTER IA contract"

    if fallback_to_full:
        spec_slice = copy.deepcopy(spec_payload)
        charter_slice = charter_text
    else:
        spec_slice = _slice_spec_payload(spec_payload, spec_analysis)
        charter_slice = slice_charter_for_child(charter_text, scope)

    child_spec_path.parent.mkdir(parents=True, exist_ok=True)
    child_spec_path.write_text(
        json.dumps(spec_slice, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    context_dir = child_session_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    charter_slice_path = context_dir / "CHARTER.slice.md"
    artifact_index_path = context_dir / "artifact_index.json"
    charter_with_index = _append_artifact_index(
        charter_slice,
        artifact_index={
            "child_spec": str(child_spec_path),
            "child_charter_slice": str(charter_slice_path),
            "full_spec": str(parent_spec_path),
            "full_charter": str(full_charter_path),
            "decisions": str((full_charter_path.parent / "decisions.md")),
            "context_slice_log": str(child_session_dir / "context_slice.json"),
        },
    )
    charter_slice_path.write_text(charter_with_index, encoding="utf-8")

    original_claims_n = len([
        claim for claim in _as_list(spec_payload.get("intent_claims")) if isinstance(claim, dict)
    ])
    included_claims_n = len([
        claim for claim in _as_list(spec_slice.get("intent_claims")) if isinstance(claim, dict)
    ])
    entity_ids = _spec_entity_ids(spec_payload)
    included_entities = sorted(spec_analysis.included_entity_ids) if not fallback_to_full else entity_ids
    excluded_entities = [] if fallback_to_full else sorted(set(entity_ids) - set(included_entities))

    artifact_index = {
        "schema_version": 1,
        "_written_at": _now_iso(),
        "child_id": scope.child_id,
        "artifacts": {
            "child_spec": str(child_spec_path),
            "child_charter_slice": str(charter_slice_path),
            "full_spec": str(parent_spec_path),
            "full_charter": str(full_charter_path),
            "decisions": str((full_charter_path.parent / "decisions.md")),
            "context_slice_log": str(child_session_dir / "context_slice.json"),
        },
    }
    artifact_index_path.write_text(
        json.dumps(artifact_index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    audit = {
        "schema_version": 1,
        "_written_at": _now_iso(),
        "child_id": scope.child_id,
        "included_entities": included_entities,
        "excluded_entities": excluded_entities,
        "included_intent_claims_n": included_claims_n,
        "excluded_intent_claims_n": max(original_claims_n - included_claims_n, 0),
        "fallback_to_full": fallback_to_full,
        "fallback_reason": fallback_reason,
        "fallback_last_resort": (
            fallback_to_full
            and scope_resolution.get("status")
            in {"last_resort_full_context", "unresolved_last_resort_full_context"}
        ),
        "scope_resolution": scope_resolution,
        "scope": {
            "task_intent": scope.task_intent,
            "owned_paths": list(scope.owned_paths),
            "action_ids": list(scope.action_ids),
        },
        "artifacts": artifact_index["artifacts"],
    }
    (child_session_dir / "context_slice.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    context_note = (
        "Scoped context slice is enabled for this child. Read "
        f"`{charter_slice_path}` before repo-root `CHARTER.md`; it preserves "
        "the full IA JSON contract and lists full fallback artifact paths. "
        f"Use `{child_spec_path}` for the scoped spec. Slice audit: "
        f"`{child_session_dir / 'context_slice.json'}`."
    )
    if fallback_to_full:
        context_note = (
            "Context slicing was requested, but Otto fell back to full context "
            f"for this child ({fallback_reason or 'unknown reason'}). Full "
            f"context paths and the audit log are in `{child_session_dir / 'context_slice.json'}`."
        )

    return ContextSliceResult(
        spec=spec_slice,
        charter=charter_with_index,
        audit=audit,
        artifact_index=artifact_index,
        context_note=context_note,
    )


def _analyze_spec_scope(spec: dict[str, Any], scope: ChildScope) -> _ScopeAnalysis:
    if not isinstance(spec, dict) or not _as_list(spec.get("core_entities")):
        return _ScopeAnalysis(set(), set(), set(), True, "legacy or missing core_entities")
    if not _scope_has_any_hint(scope):
        return _ScopeAnalysis(set(), set(), set(), True, "child scope has no intent, paths, or action IDs")

    all_action_ids = _spec_action_ids(spec)
    explicit_actions = {aid for aid in scope.action_ids if aid in all_action_ids}
    unknown_explicit = [aid for aid in scope.action_ids if aid and aid not in all_action_ids]
    if unknown_explicit:
        return _ScopeAnalysis(
            set(), set(), set(), True,
            f"unknown explicit action IDs: {', '.join(sorted(unknown_explicit))}",
        )

    inferred_actions = _infer_action_ids_from_scope(spec, scope)
    included_action_ids = explicit_actions | inferred_actions
    included_entity_ids = _entities_for_actions(spec, included_action_ids)
    included_entity_ids.update(_infer_entity_ids_from_paths(spec, scope.owned_paths))

    if not included_action_ids and not included_entity_ids:
        return _ScopeAnalysis(set(), set(), set(), True, "scope did not match spec entities or actions")

    if included_entity_ids:
        for entity in _as_list(spec.get("core_entities")):
            if not isinstance(entity, dict):
                continue
            if _obj_id(entity) in included_entity_ids:
                included_action_ids.update(_entity_action_ids(entity))

    included_claim_ids: set[str] = set()
    for entity in _as_list(spec.get("core_entities")):
        if not isinstance(entity, dict):
            continue
        for action in _as_list(entity.get("primary_actions")):
            if not isinstance(action, dict):
                continue
            if _obj_id(action) not in included_action_ids:
                continue
            included_claim_ids.update(
                str(cid)
                for cid in _as_list(action.get("intent_claim_ids"))
                if str(cid).strip()
            )

    return _ScopeAnalysis(
        included_entity_ids=included_entity_ids,
        included_action_ids=included_action_ids,
        included_claim_ids=included_claim_ids,
        fallback_to_full=False,
    )


def _scope_resolution_needed(reason: str) -> bool:
    lowered = str(reason or "").lower()
    return (
        "unknown explicit action ids" in lowered
        or "scope did not match" in lowered
        or "child scope has no intent" in lowered
    )


def _infer_action_ids_from_scope(spec: dict[str, Any], scope: ChildScope) -> set[str]:
    text = " ".join([scope.task_intent, *scope.owned_paths]).lower()
    if not text.strip():
        return set()
    found: set[str] = set()
    for entity in _as_list(spec.get("core_entities")):
        if not isinstance(entity, dict):
            continue
        entity_terms = _terms_for_entity(entity)
        for action in _as_list(entity.get("primary_actions")):
            if not isinstance(action, dict):
                continue
            action_id = _obj_id(action)
            if not action_id:
                continue
            if action_id.lower() in text:
                found.add(action_id)
                continue
            action_terms = set(_split_identifier(action_id))
            verb = str(action.get("verb") or "").strip().lower()
            if verb:
                action_terms.add(verb)
            if entity_terms and action_terms:
                if any(term in text for term in entity_terms) and any(
                    term in text for term in action_terms
                ):
                    found.add(action_id)
    return found


def _infer_entity_ids_from_paths(spec: dict[str, Any], owned_paths: list[str]) -> set[str]:
    path_text = " ".join(owned_paths).lower()
    if not path_text:
        return set()
    found: set[str] = set()
    for entity in _as_list(spec.get("core_entities")):
        if not isinstance(entity, dict):
            continue
        entity_id = _obj_id(entity)
        if not entity_id:
            continue
        if any(term in path_text for term in _terms_for_entity(entity)):
            found.add(entity_id)
    return found


def _terms_for_entity(entity: dict[str, Any]) -> set[str]:
    terms = set(_split_identifier(_obj_id(entity)))
    name = str(entity.get("name") or "")
    terms.update(_split_identifier(name))
    return {term for term in terms if len(term) >= 3}


def _scope_tokens(scope: ChildScope) -> set[str]:
    tokens = set(_split_identifier(scope.task_intent))
    for path in scope.owned_paths:
        tokens.update(_split_identifier(path))
    for action_id in scope.action_ids:
        tokens.update(_split_identifier(action_id))
        tokens.add(action_id.lower())
    return {token for token in tokens if len(token) >= 3}


def _split_identifier(text: str) -> list[str]:
    return [
        token
        for token in re.split(r"[^a-zA-Z0-9]+", str(text).lower())
        if token
    ]


def _section_matches_tokens(text: str, tokens: set[str]) -> bool:
    haystack = text.lower()
    return any(token in haystack for token in tokens)


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return []
    sections: list[tuple[str, str]] = []
    preamble = text[:matches[0].start()].strip("\n")
    if preamble:
        sections.append(("", preamble))
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        chunk = text[start:end].strip("\n")
        lines = chunk.splitlines()
        if not lines:
            continue
        heading = lines[0]
        body = "\n".join(lines[1:]).strip("\n")
        sections.append((heading, body))
    return sections


def _section_name(heading_lower: str) -> str:
    return re.sub(r"^#+\s*", "", heading_lower).strip()


def _section_title(heading: str) -> str:
    return re.sub(r"^#+\s*", "", heading).strip()


def _append_artifact_index(charter_text: str, artifact_index: dict[str, str]) -> str:
    lines = [charter_text.rstrip(), "", "## Full Artifact Index", ""]
    for label, path in artifact_index.items():
        lines.append(f"- `{label}`: `{path}`")
    return "\n".join(lines) + "\n"


def _coerce_child_scope(raw: ChildScope | dict[str, Any]) -> ChildScope:
    if isinstance(raw, ChildScope):
        return raw
    if not isinstance(raw, dict):
        return ChildScope(child_id="")
    return ChildScope(
        child_id=str(raw.get("child_id") or raw.get("task_id") or ""),
        task_intent=str(raw.get("task_intent") or raw.get("intent") or ""),
        owned_paths=_string_list(raw.get("owned_paths")),
        action_ids=_string_list(raw.get("action_ids") or raw.get("primary_action_ids")),
    )


def _scope_has_any_hint(scope: ChildScope) -> bool:
    return bool(scope.task_intent.strip() or scope.owned_paths or scope.action_ids)


def _coerce_spec(spec: Any) -> dict[str, Any]:
    if isinstance(spec, dict):
        return copy.deepcopy(spec)
    if is_dataclass(spec) and not isinstance(spec, type):
        payload = asdict(spec)
        return payload if isinstance(payload, dict) else {}
    return {}


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"[]", "{}", "null", "None"}:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _obj_id(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("id") or "").strip()


def _entity_action_ids(entity: dict[str, Any]) -> list[str]:
    return [
        _obj_id(action)
        for action in _as_list(entity.get("primary_actions"))
        if isinstance(action, dict) and _obj_id(action)
    ]


def _spec_entity_ids(spec: dict[str, Any]) -> list[str]:
    return [
        _obj_id(entity)
        for entity in _as_list(spec.get("core_entities"))
        if isinstance(entity, dict) and _obj_id(entity)
    ]


def _spec_action_ids(spec: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for entity in _as_list(spec.get("core_entities")):
        if isinstance(entity, dict):
            out.update(_entity_action_ids(entity))
    return out


def _entities_for_actions(spec: dict[str, Any], action_ids: set[str]) -> set[str]:
    out: set[str] = set()
    for entity in _as_list(spec.get("core_entities")):
        if not isinstance(entity, dict):
            continue
        if set(_entity_action_ids(entity)) & action_ids:
            entity_id = _obj_id(entity)
            if entity_id:
                out.add(entity_id)
    return out


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
