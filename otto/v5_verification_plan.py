"""Deterministic runner-side verification checks for v5 Lead verdicts.

The agent-authored ``verdict.json`` remains the input record. This module reads
that payload plus the structured spec and CHARTER IA, writes
``verification_plan.json``, and returns a final verdict after conservative
downgrades.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from otto.v5_capability_inventory import parse_information_architecture_contract

CHECK_KINDS = (
    "page_resolves",
    "route_resolves",
    "endpoint_resolves",
    "action_has_test",
    "mutating_action_has_feedback",
    "entity_has_empty_state",
    "local_scope_check",
    "no_stub_text",
    "verdict_consistency",
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


def validate_lead_verdict(
    *,
    project_dir: Path,
    worktree_dir: Path,
    session_dir: Path,
    agent_verdict: dict[str, Any],
    initial_verdict: str,
    node_kind: str = "leaf",
    matrix_scope: str = "leaf",
) -> RunnerVerificationOutcome:
    """Run deterministic checks and compute the runner-adjusted verdict."""
    spec_path = session_dir / "spec" / "spec.json"
    spec = _coerce_spec(_load_json(spec_path))
    charter_path = _find_charter(worktree_dir, project_dir)
    ia = parse_information_architecture_contract(charter_path) if charter_path else None
    node_kind = "integration" if node_kind == "integration" else "leaf"
    matrix_scope = matrix_scope if matrix_scope in {"leaf", "integration_only"} else "leaf"
    full_matrix = matrix_scope != "integration_only" or node_kind == "integration"

    checks: list[dict[str, Any]] = []
    if full_matrix and spec and isinstance(ia, dict) and _has_structured_spec(spec):
        checks.extend(_check_pages_resolve(worktree_dir, spec, ia))
        checks.extend(_check_routes_resolve(worktree_dir, ia))
        checks.extend(_check_endpoints_resolve(worktree_dir, ia))
        checks.extend(_check_actions_have_tests(worktree_dir, spec, agent_verdict))
        checks.extend(_check_mutating_actions_have_feedback(worktree_dir, spec))
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

    checks.extend(_check_no_stub_text(worktree_dir))
    checks.extend(_check_verdict_consistency(agent_verdict))
    checks.extend(_check_deprecation_warnings(
        project_dir=project_dir,
        worktree_dir=worktree_dir,
        session_dir=session_dir,
        agent_verdict=agent_verdict,
    ))

    journey_failures = _missing_passed_journeys(spec, agent_verdict) if spec and full_matrix else []
    failed_required = [c for c in checks if c.get("required", True) and c.get("status") == "fail"]

    final_verdict = initial_verdict
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
        "matrix_scope": matrix_scope,
        "full_matrix": full_matrix,
        "agent_verdict": initial_verdict,
        "final_verdict": final_verdict,
        "checks": checks,
        "journey_failures": journey_failures,
        "summary": {
            "total": len(checks),
            "failed": len(failed_required),
            "skipped": len([c for c in checks if c.get("status") == "skipped"]),
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

    return RunnerVerificationOutcome(
        final_verdict=final_verdict,
        verification_plan=plan,
        runner_checks_summary=runner_summary,
        journey_failures=journey_failures,
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


def _check_pages_resolve(root: Path, spec: dict[str, Any], ia: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    routes_by_id = _ia_routes_by_id(ia)
    for page in _spec_product_pages(spec):
        page_id = str(page.get("id") or "")
        if not page_id:
            continue
        route = routes_by_id.get(page_id)
        if not route:
            checks.append(_check(
                "page_resolves",
                page_id,
                False,
                "PM page has no matching CHARTER IA route",
                refs={"purpose": str(page.get("purpose") or "")},
            ))
            continue
        path = str(route.get("path") or "")
        key_text = str(route.get("key_text") or "")
        needles = [path, key_text, _componentish(page_id)]
        passed = _grep_any(root, needles)
        checks.append(_check(
            "page_resolves",
            page_id,
            passed,
            "PM page route/component appears in code"
            if passed
            else "PM page route/component not found in code",
            refs={"path": path, "key_text": key_text, "purpose": str(page.get("purpose") or "")},
        ))
    return checks


def _check_routes_resolve(root: Path, ia: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for route in ia.get("routes") or []:
        if not isinstance(route, dict):
            continue
        route_id = str(route.get("id") or "")
        path = str(route.get("path") or "")
        key_text = str(route.get("key_text") or "")
        needles = [path, _componentish(route_id)]
        passed = _grep_any(root, needles)
        checks.append(_check(
            "route_resolves",
            route_id or path,
            passed,
            "route path/component appears in code" if passed else "route path/component not found in code",
            refs={"path": path, "key_text": key_text},
        ))
    return checks


def _check_endpoints_resolve(root: Path, ia: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for endpoint in ia.get("api_endpoints") or []:
        if not isinstance(endpoint, dict):
            continue
        endpoint_id = str(endpoint.get("id") or "")
        path = str(endpoint.get("path") or "")
        method = str(endpoint.get("method") or "").upper()
        passed = _grep_any(root, [path, endpoint_id])
        checks.append(_check(
            "endpoint_resolves",
            endpoint_id or path,
            passed,
            "endpoint path/id appears in code" if passed else "endpoint path/id not found in code",
            refs={"method": method, "path": path},
        ))
    return checks


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


def _check_actions_have_tests(
    root: Path,
    spec: dict[str, Any],
    agent_verdict: dict[str, Any],
) -> list[dict[str, Any]]:
    test_files = _test_files(root)
    test_index = "\n".join(str(p.relative_to(root)) + "\n" + _read_text(p) for p in test_files)
    evidence_text = json.dumps(agent_verdict.get("evidence") or [], sort_keys=True)
    checks: list[dict[str, Any]] = []
    for action in _spec_actions(spec):
        action_id = str(action.get("id") or "")
        safe_id = action_id.replace(".", "_").replace("-", "_")
        passed = (
            action_id in test_index
            or safe_id in test_index
            or action_id in evidence_text
            or safe_id in evidence_text
        )
        checks.append(_check(
            "action_has_test",
            action_id,
            passed,
            "action is named in tests or evidence" if passed else "action is not named in tests or evidence",
        ))
    return checks


def _check_mutating_actions_have_feedback(root: Path, spec: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
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
        checks.append(_check(
            "mutating_action_has_feedback",
            action_id,
            passed,
            "action code includes a feedback pattern" if passed else "no toast/notification/redirect-style feedback found near action",
        ))
    return checks


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


def _check_no_stub_text(root: Path) -> list[dict[str, Any]]:
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
    return [_check(
        "no_stub_text",
        "no_stub_text",
        passed,
        "no stub text found" if passed else "stub text found in user-facing artifacts",
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


_DEPRECATION_EMISSION_RE = re.compile(
    r"^(?:(?P<path>.+?):(?P<line>\d+):\s*)?"
    r"(?P<category>DeprecationWarning|PendingDeprecationWarning)\s*:\s*"
    r"(?P<message>.+)$",
    re.IGNORECASE,
)
_NO_DEPRECATION_WARNING_RE = re.compile(
    r"\b(?:0|zero)\s+(?:deprecation\s+)?warnings?\b|"
    r"\b0\s+deprecations?\b|"
    r"\bno\s+(?:deprecation\s+)?warnings?\b|"
    r"\bno\s+deprecations?\b|"
    r"\bwithout\s+(?:deprecation\s+)?warnings?\b|"
    r"\bdeprecation(?:warning)?s?\s+filtered\b",
    re.IGNORECASE,
)
_THIRD_PARTY_WARNING_PATH_PARTS = (
    "/site-packages/",
    "/dist-packages/",
    "/node_modules/",
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


def _deprecation_lines(text: str) -> list[str]:
    hits: list[str] = []
    for line in text.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        if _NO_DEPRECATION_WARNING_RE.search(normalized):
            continue
        match = _DEPRECATION_EMISSION_RE.search(normalized)
        if match is None:
            continue
        if _is_third_party_warning_path(str(match.group("path") or "")):
            continue
        hits.append(normalized[:240])
    return hits


def _is_third_party_warning_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(part in normalized for part in _THIRD_PARTY_WARNING_PATH_PARTS)


def _resolve_evidence_path(raw: str, roots: list[Path]) -> Path | None:
    if not raw.strip():
        return None
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [root / path for root in roots]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _check_deprecation_warnings(
    *,
    project_dir: Path,
    worktree_dir: Path,
    session_dir: Path,
    agent_verdict: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fail a passing verdict that leaves deprecation warnings unresolved."""
    hits: list[str] = []
    for text in _iter_agent_verdict_strings(agent_verdict):
        hits.extend(_deprecation_lines(text))

    evidence_roots = [session_dir, worktree_dir, project_dir]
    for raw_path in _iter_evidence_path_strings(agent_verdict.get("evidence") or []):
        resolved = _resolve_evidence_path(raw_path, evidence_roots)
        if resolved is None:
            continue
        text = _read_text(resolved)
        if not text:
            continue
        for line in _deprecation_lines(text):
            hits.append(f"{resolved.name}: {line}")

    # Dedupe without losing order.
    seen: set[str] = set()
    unique_hits: list[str] = []
    for hit in hits:
        if hit in seen:
            continue
        seen.add(hit)
        unique_hits.append(hit)

    passed = not unique_hits
    return [_check(
        "deprecation_warnings",
        "test_output_deprecations",
        passed,
        (
            "no product deprecation warnings found in verdict output or evidence logs"
            if passed
            else "deprecation warnings found in passing test output; fix them or downgrade"
        ),
        refs={"warnings": unique_hits[:20]},
    )]


def _check_local_scope_evidence(agent_verdict: dict[str, Any]) -> list[dict[str, Any]]:
    journeys = agent_verdict.get("journeys")
    evidence = agent_verdict.get("evidence")
    test_command = str(agent_verdict.get("test_command") or "").strip()
    has_journey_evidence = any(
        isinstance(item, dict)
        and (
            item.get("passed") is True
            or str(item.get("detail") or "").strip()
        )
        for item in (journeys if isinstance(journeys, list) else [])
    )
    has_file_evidence = any(
        str(item or "").strip()
        for item in (evidence if isinstance(evidence, list) else [])
    )
    passed = bool(test_command or has_journey_evidence or has_file_evidence)
    return [_check(
        "local_scope_check",
        "test_or_journey_evidence",
        passed,
        (
            "leaf verdict includes local test, journey, or evidence data"
            if passed
            else "leaf verdict lacks local test_command, passed/detailed journeys, and evidence entries"
        ),
    )]


def _missing_passed_journeys(spec: dict[str, Any], agent_verdict: dict[str, Any]) -> list[str]:
    expected = [
        str(j.get("id"))
        for j in spec.get("behavior_journeys") or []
        if isinstance(j, dict) and j.get("id")
    ]
    if not expected:
        return []
    passed = {
        str(j.get("id"))
        for j in agent_verdict.get("journeys") or []
        if isinstance(j, dict) and j.get("passed") is True and j.get("id")
    }
    return [jid for jid in expected if jid not in passed]


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
