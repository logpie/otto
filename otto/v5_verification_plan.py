"""Deterministic runner-side verification checks for v5 Lead verdicts.

The agent-authored ``verdict.json`` remains the input record. This module reads
that payload plus the structured spec and CHARTER IA, writes
``verification_plan.json``, and returns a final verdict after conservative
downgrades from hard gates only. Unstructured text/source heuristics are
recorded as advisory telemetry and never participate in verdict computation.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from otto.journey_contracts import VerificationContractError, normalize_journey_contracts
from otto.journey_scope_policy import ExecutionScope, infer_execution_scope
from otto.journey_verdict_sink import failed_journey_ids, resolve_journey_verdicts
from otto.spec_compile_flat import SCHEMA_VERSION as FLAT_SPEC_SCHEMA_VERSION
from otto.v5_capability_inventory import parse_information_architecture_contract

CHECK_KINDS = (
    "structured_contract_present",
    "page_has_ia_route",
    "entity_has_empty_state",
    "local_scope_check",
    "verdict_consistency",
)
ADVISORY_KINDS = (
    "page_resolves",
    "route_resolves",
    "endpoint_resolves",
    "action_has_test",
    "mutating_action_has_feedback",
    "no_stub_text",
    "deprecation_warnings",
)

_NOISE_DIRS = {
    ".git",
    ".venv",
    ".next",
    ".worktrees",
    "venv",
    "build",
    "dist",
    "node_modules",
    "out",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "otto_logs",
    "session",
}
_CODE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".html",
    ".css",
    ".json",
}
_FEEDBACK_PATTERNS = (
    "toast",
    "notification",
    "notify",
    "alert(",
    "redirect",
    "navigate(",
    "success",
    "error",
    "aria-live",
)
_STUB_TOKEN_RE = re.compile(r"\b(?:TODO|Lorem\s+ipsum|placeholder(?!-))\b", re.IGNORECASE)
_STRING_LITERAL_RE = re.compile(
    r"(?P<quote>['\"`])(?P<value>(?:\\.|(?!(?P=quote)).)*)(?P=quote)"
)
_USER_VISIBLE_STRING_CONTEXT_RE = re.compile(
    r"\b(?:children|label|title|message|placeholder)\s*(?:=|:)\s*(?:\{\s*)?$",
    re.IGNORECASE,
)
_TOAST_STRING_CONTEXT_RE = re.compile(r"\btoast\.\w+\s*\([^)]*$", re.IGNORECASE)
_STRUCTURED_IA_PROJECT_KINDS = {"webapp"}


@dataclass
class RunnerVerificationOutcome:
    final_verdict: str
    verification_plan: dict[str, Any]
    runner_checks_summary: list[dict[str, Any]]
    journey_failures: list[str]
    verification_contract_failures: list[dict[str, Any]] = field(default_factory=list)


def validate_lead_verdict(
    *,
    project_dir: Path,
    worktree_dir: Path,
    session_dir: Path,
    agent_verdict: dict[str, Any],
    initial_verdict: str,
    node_kind: str = "leaf",
    matrix_scope: str = "leaf",
    execution_scope: ExecutionScope | None = None,
) -> RunnerVerificationOutcome:
    """Run deterministic checks and compute the runner-adjusted verdict."""
    spec_path = session_dir / "spec" / "spec.json"
    spec = _coerce_spec(_load_json(spec_path))
    resolved_execution_scope = execution_scope or infer_execution_scope(
        kind="integration" if node_kind == "integration" else "plan_or_inline",
        integration_branch=None,
    )
    verification_contract_failures: list[dict[str, Any]] = []
    if spec:
        try:
            spec = normalize_journey_contracts(
                spec,
                current_schema_version=FLAT_SPEC_SCHEMA_VERSION,
            )
        except VerificationContractError as exc:
            verification_contract_failures.append({
                "kind": exc.code,
                "id": exc.path,
                "status": "fail",
                "detail": exc.message,
                "repair_domain": "spec_contract",
            })
    charter_path = _find_charter(worktree_dir, project_dir)
    ia = parse_information_architecture_contract(charter_path) if charter_path else None
    node_kind = "integration" if node_kind == "integration" else "leaf"
    matrix_scope = matrix_scope if matrix_scope in {"leaf", "integration_only"} else "leaf"
    full_matrix = matrix_scope != "integration_only" or node_kind == "integration"

    checks: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    if full_matrix and spec and isinstance(ia, dict) and _has_structured_spec(spec):
        checks.extend(_check_page_ia_routes(spec, ia))
        advisories.extend(_advise_pages_resolve(worktree_dir, spec, ia))
        advisories.extend(_advise_routes_resolve(worktree_dir, ia))
        advisories.extend(_advise_endpoints_resolve(worktree_dir, ia))
        advisories.extend(_advise_actions_have_tests(worktree_dir, spec, agent_verdict))
        advisories.extend(_advise_mutating_actions_have_feedback(worktree_dir, spec))
        checks.extend(_check_entities_have_empty_states(spec, ia))
    elif not full_matrix:
        checks.append(_check(
            "structured_contract_present",
            "structured_contract",
            True,
            "leaf local-scope mode skipped the full structured IA matrix; integration node runs it",
            required=False,
            skipped=True,
        ))
        checks.extend(_check_local_scope_evidence(agent_verdict))
    else:
        checks.append(_structured_contract_presence_check(spec, ia))

    advisories.extend(_advise_no_stub_text(worktree_dir))
    checks.extend(_check_verdict_consistency(agent_verdict))
    advisories.extend(_advise_deprecation_warnings(
        project_dir=project_dir,
        worktree_dir=worktree_dir,
        session_dir=session_dir,
        agent_verdict=agent_verdict,
    ))

    journey_verdicts = (
        _journey_verdicts_from_sink(
            spec,
            session_dir=session_dir,
            execution_scope=resolved_execution_scope,
        )
        if spec and full_matrix and not verification_contract_failures
        else []
    )
    journey_failures = failed_journey_ids(journey_verdicts) if journey_verdicts else []
    failed_required = [c for c in checks if c.get("required", True) and c.get("status") == "fail"]

    final_verdict = initial_verdict
    if verification_contract_failures:
        final_verdict = "partial" if final_verdict == "pass" else final_verdict
    if final_verdict == "pass" and failed_required:
        final_verdict = "partial"
    if final_verdict == "pass" and journey_failures:
        final_verdict = "partial"

    plan = {
        "schema_version": 1,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spec_path": str(spec_path) if spec_path.exists() else "",
        "charter_path": str(charter_path) if charter_path else "",
        "node_kind": node_kind,
        "execution_scope": resolved_execution_scope,
        "matrix_scope": matrix_scope,
        "full_matrix": full_matrix,
        "agent_verdict": initial_verdict,
        "final_verdict": final_verdict,
        "checks": checks,
        "advisories": advisories,
        "journey_verdicts": journey_verdicts,
        "verification_contract_failures": verification_contract_failures,
        "journey_failures": journey_failures,
        "summary": {
            "total": len(checks),
            "failed": len(failed_required),
            "skipped": len([c for c in checks if c.get("status") == "skipped"]),
            "advisories": len(advisories),
            "advisory_warnings": len([a for a in advisories if a.get("status") == "warn"]),
            "missing_passed_journeys": len(journey_failures),
        },
    }
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "verification_plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8"
    )

    runner_summary = [
        {
            "kind": c.get("kind"),
            "id": c.get("id"),
            "status": c.get("status"),
            "detail": c.get("detail"),
        }
        for c in checks
        if c.get("status") in {"fail", "skipped"}
    ]
    for journey_id in journey_failures:
        runner_summary.append({
            "kind": "journey_passed",
            "id": journey_id,
            "status": "fail",
            "detail": "required behavior_journey has no passed verdict entry",
        })
    runner_summary.extend(verification_contract_failures)

    return RunnerVerificationOutcome(
        final_verdict=final_verdict,
        verification_plan=plan,
        runner_checks_summary=runner_summary,
        journey_failures=journey_failures,
        verification_contract_failures=verification_contract_failures,
    )


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _coerce_spec(spec: Any) -> dict[str, Any]:
    """Return a JSON-shaped flat spec payload for deterministic checks."""
    if isinstance(spec, dict):
        return dict(spec)
    if is_dataclass(spec) and not isinstance(spec, type):
        payload = asdict(spec)
        return payload if isinstance(payload, dict) else {}
    return {}


def _find_charter(worktree_dir: Path, project_dir: Path) -> Path | None:
    for candidate in (worktree_dir / "CHARTER.md", project_dir / "CHARTER.md"):
        if candidate.exists():
            return candidate
    return None


def _has_structured_spec(spec: dict[str, Any]) -> bool:
    return bool(
        spec.get("core_entities")
        or spec.get("intent_claims")
        or spec.get("cold_start_states")
        or spec.get("quality_constraints")
    )


def _requires_structured_ia_contract(spec: dict[str, Any]) -> bool:
    return str(spec.get("project_kind") or "").strip().casefold() in _STRUCTURED_IA_PROJECT_KINDS


def _structured_contract_presence_check(
    spec: dict[str, Any],
    ia: dict[str, Any] | None,
) -> dict[str, Any]:
    missing: list[str] = []
    if not spec:
        missing.append("structured spec is absent or invalid")
    elif not _has_structured_spec(spec):
        missing.append("structured spec fields are absent")
    if not isinstance(ia, dict):
        missing.append("CHARTER information architecture contract is absent or invalid")
    if _requires_structured_ia_contract(spec):
        detail = "missing required webapp structured contract: " + "; ".join(missing)
        return _check(
            "structured_contract_present",
            "structured_contract",
            False,
            detail,
            required=True,
            refs={"project_kind": str(spec.get("project_kind") or "")},
        )
    return _check(
        "structured_contract_present",
        "structured_contract",
        True,
        "skipped structured checks because spec or CHARTER IA is absent/legacy",
        required=False,
        skipped=True,
        refs={"missing": missing},
    )


def _check(
    kind: str,
    check_id: str,
    passed: bool,
    detail: str,
    *,
    required: bool = True,
    skipped: bool = False,
    refs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": check_id,
        "status": "skipped" if skipped else ("pass" if passed else "fail"),
        "passed": bool(passed),
        "required": required,
        "detail": detail,
        "refs": refs or {},
    }


def _advisory(
    kind: str,
    advisory_id: str,
    *,
    status: str,
    detail: str,
    refs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": advisory_id,
        "status": status,
        "passed": status != "warn",
        "required": False,
        "detail": detail,
        "refs": refs or {},
    }


def _iter_files(root: Path, *, include_tests: bool = False) -> list[Path]:
    if not root.exists():
        return []
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _NOISE_DIRS for part in rel.parts):
            continue
        rel_text = str(rel)
        if not include_tests and _is_test_path(rel_text):
            continue
        if path.suffix.lower() not in _CODE_SUFFIXES and path.name not in {"package.json"}:
            continue
        out.append(path)
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _grep_any(root: Path, needles: list[str], *, include_tests: bool = False) -> bool:
    needles = [n for n in needles if n]
    if not needles:
        return False
    for path in _iter_files(root, include_tests=include_tests):
        text = _read_text(path)
        if any(needle in text for needle in needles):
            return True
    return False


def _spec_product_pages(spec: dict[str, Any]) -> list[dict[str, Any]]:
    product_overview = spec.get("product_overview")
    if not isinstance(product_overview, dict):
        return []
    pages = product_overview.get("top_level_pages")
    return [page for page in pages if isinstance(page, dict)] if isinstance(pages, list) else []


def _ia_routes_by_id(ia: dict[str, Any]) -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    for route in ia.get("routes") or []:
        if isinstance(route, dict) and route.get("id"):
            routes[str(route["id"])] = route
    return routes


def _check_page_ia_routes(spec: dict[str, Any], ia: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    routes_by_id = _ia_routes_by_id(ia)
    for page in _spec_product_pages(spec):
        page_id = str(page.get("id") or "")
        if not page_id:
            continue
        route = routes_by_id.get(page_id)
        checks.append(_check(
            "page_has_ia_route",
            page_id,
            bool(route),
            (
                "PM page has a matching CHARTER IA route"
                if route
                else "PM page has no matching CHARTER IA route"
            ),
            refs={"purpose": str(page.get("purpose") or "")},
        ))
    return checks


def _advise_pages_resolve(root: Path, spec: dict[str, Any], ia: dict[str, Any]) -> list[dict[str, Any]]:
    advisories: list[dict[str, Any]] = []
    routes_by_id = _ia_routes_by_id(ia)
    for page in _spec_product_pages(spec):
        page_id = str(page.get("id") or "")
        if not page_id:
            continue
        route = routes_by_id.get(page_id)
        if not route:
            advisories.append(_advisory(
                "page_resolves",
                page_id,
                status="warn",
                detail=(
                    "PM page has no matching CHARTER IA route; structured "
                    "page_has_ia_route gate records this contract issue"
                ),
                refs={"purpose": str(page.get("purpose") or "")},
            ))
            continue
        path = str(route.get("path") or "")
        key_text = str(route.get("key_text") or "")
        needles = [path, key_text, _componentish(page_id)]
        passed = _grep_any(root, needles)
        advisories.append(_advisory(
            "page_resolves",
            page_id,
            status="info" if passed else "warn",
            detail=(
                "PM page route/component appears in code"
                if passed
                else "PM page route/component not found by text search"
            ),
            refs={"path": path, "key_text": key_text, "purpose": str(page.get("purpose") or "")},
        ))
    return advisories


def _advise_routes_resolve(root: Path, ia: dict[str, Any]) -> list[dict[str, Any]]:
    advisories: list[dict[str, Any]] = []
    for route in ia.get("routes") or []:
        if not isinstance(route, dict):
            continue
        route_id = str(route.get("id") or "")
        path = str(route.get("path") or "")
        key_text = str(route.get("key_text") or "")
        needles = [path, _componentish(route_id)]
        passed = _grep_any(root, needles)
        advisories.append(_advisory(
            "route_resolves",
            route_id or path,
            status="info" if passed else "warn",
            detail=(
                "route path/component appears in code"
                if passed
                else "route path/component not found by text search"
            ),
            refs={"path": path, "key_text": key_text},
        ))
    return advisories


def _advise_endpoints_resolve(root: Path, ia: dict[str, Any]) -> list[dict[str, Any]]:
    advisories: list[dict[str, Any]] = []
    for endpoint in ia.get("api_endpoints") or []:
        if not isinstance(endpoint, dict):
            continue
        endpoint_id = str(endpoint.get("id") or "")
        path = str(endpoint.get("path") or "")
        method = str(endpoint.get("method") or "").upper()
        passed = _grep_any(root, [path, endpoint_id])
        advisories.append(_advisory(
            "endpoint_resolves",
            endpoint_id or path,
            status="info" if passed else "warn",
            detail=(
                "endpoint path/id appears in code"
                if passed
                else "endpoint path/id not found by text search"
            ),
            refs={"method": method, "path": path},
        ))
    return advisories


def _spec_actions(spec: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for entity in spec.get("core_entities") or []:
        if not isinstance(entity, dict):
            continue
        entity_id = str(entity.get("id") or "")
        entity_name = str(entity.get("name") or "")
        for action in entity.get("primary_actions") or []:
            if not isinstance(action, dict):
                continue
            item = dict(action)
            item["_entity_id"] = entity_id
            item["_entity_name"] = entity_name
            actions.append(item)
    return actions


def _test_files(root: Path) -> list[Path]:
    return [
        path for path in _iter_files(root, include_tests=True)
        if _is_test_path(str(path.relative_to(root)))
    ]


def _is_test_path(rel_text: str) -> bool:
    parts = {part.lower() for part in Path(rel_text).parts}
    if "tests" in parts or "__tests__" in parts:
        return True
    name = Path(rel_text).name.lower()
    return (
        name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
    )


def _advise_actions_have_tests(
    root: Path,
    spec: dict[str, Any],
    agent_verdict: dict[str, Any],
) -> list[dict[str, Any]]:
    test_files = _test_files(root)
    test_index = "\n".join(str(p.relative_to(root)) + "\n" + _read_text(p) for p in test_files)
    evidence_text = json.dumps(agent_verdict.get("evidence") or [], sort_keys=True)
    advisories: list[dict[str, Any]] = []
    for action in _spec_actions(spec):
        action_id = str(action.get("id") or "")
        safe_id = action_id.replace(".", "_").replace("-", "_")
        passed = (
            action_id in test_index
            or safe_id in test_index
            or action_id in evidence_text
            or safe_id in evidence_text
        )
        advisories.append(_advisory(
            "action_has_test",
            action_id,
            status="info" if passed else "warn",
            detail=(
                "action is named in tests or evidence"
                if passed
                else "action is not named in tests or evidence by text search"
            ),
        ))
    return advisories


def _advise_mutating_actions_have_feedback(root: Path, spec: dict[str, Any]) -> list[dict[str, Any]]:
    advisories: list[dict[str, Any]] = []
    for action in _spec_actions(spec):
        action_id = str(action.get("id") or "")
        if not str(action.get("success_observable") or "").strip():
            continue
        verb = str(action.get("verb") or "")
        entity = str(action.get("_entity_id") or action.get("_entity_name") or "")
        needles = [action_id, verb, entity]
        passed = False
        for path in _iter_files(root):
            text = _read_text(path)
            lower = text.lower()
            if any(n and n.lower() in lower for n in needles) and any(p in lower for p in _FEEDBACK_PATTERNS):
                passed = True
                break
        advisories.append(_advisory(
            "mutating_action_has_feedback",
            action_id,
            status="info" if passed else "warn",
            detail=(
                "action code includes a feedback pattern"
                if passed
                else "no toast/notification/redirect-style feedback found near action by text search"
            ),
        ))
    return advisories


def _check_entities_have_empty_states(spec: dict[str, Any], ia: dict[str, Any]) -> list[dict[str, Any]]:
    empty_states = [
        item for item in ia.get("empty_states") or []
        if isinstance(item, dict) and item.get("cta_present") is True
    ]
    checks: list[dict[str, Any]] = []
    for entity in spec.get("core_entities") or []:
        if not isinstance(entity, dict):
            continue
        entity_id = str(entity.get("id") or "")
        entity_name = str(entity.get("name") or "").lower()
        passed = any(
            str(es.get("entity") or "").lower() in {entity_id.lower(), entity_name}
            for es in empty_states
        )
        checks.append(_check(
            "entity_has_empty_state",
            entity_id,
            passed,
            "IA declares cta_present empty state for entity" if passed else "IA lacks cta_present empty state for entity",
        ))
    return checks


def _advise_no_stub_text(root: Path) -> list[dict[str, Any]]:
    offenders: list[str] = []
    for path in _iter_files(root, include_tests=False):
        rel = str(path.relative_to(root))
        if "test" in rel.lower() or rel == "package.json":
            continue
        for line_no, line in enumerate(_read_text(path).splitlines(), start=1):
            if _line_has_user_facing_stub_text(line):
                offenders.append(f"{rel}:{line_no}")
                break
    passed = not offenders
    return [_advisory(
        "no_stub_text",
        "no_stub_text",
        status="info" if passed else "warn",
        detail="no stub text found" if passed else "stub text found by broad source scanner",
        refs={"offenders": offenders[:20]},
    )]


def _line_has_user_facing_stub_text(line: str) -> bool:
    code = _strip_inline_comment(line)
    stripped = code.strip()
    if not stripped or stripped.startswith(("//", "#", "/*", "*")):
        return False
    if stripped.startswith(("import ", "from ")):
        return False

    if _jsx_text_has_stub(stripped):
        return True

    for match in _STRING_LITERAL_RE.finditer(stripped):
        value = match.group("value")
        if not _literal_has_sentence_stub(value):
            continue
        prefix = stripped[max(0, match.start() - 80):match.start()]
        if (
            _USER_VISIBLE_STRING_CONTEXT_RE.search(prefix)
            or _TOAST_STRING_CONTEXT_RE.search(prefix)
            or _literal_has_general_stub(value)
        ):
            return True
    return False


def _strip_inline_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if line.startswith("//", index):
            return line[:index]
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _jsx_text_has_stub(line: str) -> bool:
    for match in re.finditer(r">([^<>]+)<", line):
        text = match.group(1).strip()
        if not text:
            continue
        if text.startswith("{") and text.endswith("}"):
            if any(_literal_has_sentence_stub(item.group("value")) for item in _STRING_LITERAL_RE.finditer(text)):
                return True
            continue
        if _STUB_TOKEN_RE.search(text):
            return True
    return False


def _literal_has_sentence_stub(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip()
    return len(normalized) >= 10 and " " in normalized and bool(_STUB_TOKEN_RE.search(normalized))


def _literal_has_general_stub(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    return (
        "lorem ipsum" in normalized
        or "placeholder" in normalized
        or bool(re.search(r"\btodo\b\s*[:\-\s]", normalized))
    )


def _check_verdict_consistency(agent_verdict: dict[str, Any]) -> list[dict[str, Any]]:
    coverage = agent_verdict.get("intent_coverage") or {}
    if not isinstance(coverage, dict):
        return [_check("verdict_consistency", "intent_coverage", True, "no structured intent_coverage to compare", required=False)]
    built = [_coverage_label(item) for item in coverage.get("built") or []]
    gaps = [_coverage_label(item) for item in (coverage.get("partial") or []) + (coverage.get("skipped") or [])]
    contradictions: list[str] = []
    for built_item in built:
        b = _normalize_label(built_item)
        if not b:
            continue
        for gap_item in gaps:
            g = _normalize_label(gap_item)
            if b and g and (b == g or b in g or g in b):
                contradictions.append(f"{built_item} <> {gap_item}")
    passed = not contradictions
    return [_check(
        "verdict_consistency",
        "intent_coverage",
        passed,
        "built claims do not contradict partial/skipped claims" if passed else "built claims overlap partial/skipped claims",
        refs={"contradictions": contradictions[:20]},
    )]


_DEPRECATION_WARNING_TEXT_RE = re.compile(
    r"\b(?:DeprecationWarning|PendingDeprecationWarning)\b",
    re.IGNORECASE,
)


def _iter_agent_verdict_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_iter_agent_verdict_strings(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_iter_agent_verdict_strings(item))
    return out


def _iter_evidence_path_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key in ("path", "file", "log", "output_path", "artifact"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                out.append(candidate)
    elif isinstance(value, list):
        for item in value:
            out.extend(_iter_evidence_path_strings(item))
    return out


def _deprecation_warning_lines(text: str) -> list[str]:
    hits: list[str] = []
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        if not _DEPRECATION_WARNING_TEXT_RE.search(normalized):
            continue
        hits.append(normalized[:240])
    return hits


def _resolve_evidence_path(raw: str, roots: list[Path]) -> Path | None:
    if not raw.strip():
        return None
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [root / path for root in roots]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _advise_deprecation_warnings(
    *,
    project_dir: Path,
    worktree_dir: Path,
    session_dir: Path,
    agent_verdict: dict[str, Any],
) -> list[dict[str, Any]]:
    """Record deprecation warning text as non-gating telemetry."""
    hits: list[str] = []
    for text in _iter_agent_verdict_strings(agent_verdict):
        hits.extend(_deprecation_warning_lines(text))

    evidence_roots = [session_dir, worktree_dir, project_dir]
    for raw_path in _iter_evidence_path_strings(agent_verdict.get("evidence") or []):
        resolved = _resolve_evidence_path(raw_path, evidence_roots)
        if resolved is None:
            continue
        text = _read_text(resolved)
        if not text:
            continue
        for line in _deprecation_warning_lines(text):
            hits.append(f"{resolved.name}: {line}")

    # Dedupe without losing order.
    seen: set[str] = set()
    unique_hits: list[str] = []
    for hit in hits:
        if hit in seen:
            continue
        seen.add(hit)
        unique_hits.append(hit)

    return [_advisory(
        "deprecation_warnings",
        "test_output_deprecations",
        status="info" if not unique_hits else "warn",
        detail=(
            "no deprecation warning text found in verdict output or evidence logs"
            if not unique_hits
            else "deprecation warning text found in verdict output or evidence logs"
        ),
        refs={"warnings": unique_hits[:20]},
    )]


def _check_local_scope_evidence(agent_verdict: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = agent_verdict.get("evidence")
    test_command = str(agent_verdict.get("test_command") or "").strip()
    has_file_evidence = any(
        str(item or "").strip()
        for item in (evidence if isinstance(evidence, list) else [])
    )
    passed = bool(test_command or has_file_evidence)
    return [_check(
        "local_scope_check",
        "test_or_journey_evidence",
        passed,
        (
            "leaf verdict includes local test or evidence data"
            if passed
            else "leaf verdict lacks local test_command and evidence entries"
        ),
    )]


def _journey_verdicts_from_sink(
    spec: dict[str, Any],
    *,
    session_dir: Path,
    execution_scope: ExecutionScope,
) -> list[dict[str, Any]]:
    journeys = [
        journey
        for journey in spec.get("behavior_journeys") or []
        if isinstance(journey, dict)
    ]
    return resolve_journey_verdicts(
        journeys=journeys,
        execution_scope=execution_scope,
        executor_results=_load_controller_executor_results(session_dir),
        registered_executor_levels={"ui", "api"},
    )


def _load_controller_executor_results(session_dir: Path) -> list[dict[str, Any]]:
    candidates = [session_dir / "verify" / "api-executor-results.json"]
    journeys_dir = session_dir / "journeys"
    if journeys_dir.exists():
        candidates.extend(journeys_dir.rglob("journey-verdicts.json"))
    existing = [path for path in candidates if path.is_file()]
    existing.sort(key=lambda path: path.stat().st_mtime)
    results: list[dict[str, Any]] = []
    for path in existing:
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        for item in payload.get("executor_results") or []:
            if isinstance(item, dict):
                results.append(dict(item))
    return results


def _coverage_label(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("feature", "name", "id", "what_works", "gap", "reason"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return json.dumps(item, sort_keys=True)
    return str(item)


def _normalize_label(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _componentish(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value)
    return "".join(word[:1].upper() + word[1:] for word in words if word)
