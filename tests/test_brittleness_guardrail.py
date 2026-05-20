from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
OTTO_ROOT = REPO_ROOT / "otto"


@dataclass(frozen=True)
class Allow:
    reason: str


ALLOWLIST: dict[str, Allow] = {
    "otto/mission_control/service.py:_merge_target:identity_default_fallback": Allow(
        reason=(
            "Legacy Mission Control merge target fallback when otto.yaml is absent "
            "or unreadable; not child worktree dependency identity. Medium debt."
        )
    ),
}


SUCCESS_NAMES = {"passed", "succeeded", "success", "ok"}
SUCCESS_STRINGS = {"pass", "passed", "success", "ok", "done"}
SUSPICIOUS_CONTEXT = {
    "error",
    "malformed",
    "fallback",
    "degraded",
    "no_op",
}
SUSPICIOUS_BRANCH_CONTEXT = SUSPICIOUS_CONTEXT | {"default"}
STATE_LOAD_PREFIXES = ("read_", "load_", "parse_")
STATE_PARSE_EXCEPTIONS = {
    "JSONDecodeError",
    "ValueError",
    "OSError",
    "FileNotFoundError",
}
ERROR_TEXT_NAMES = {
    "error",
    "errors",
    "stderr",
    "stdout",
    "message",
    "msg",
    "reason",
    "output",
    "out",
}
CONTROL_FLOW_NAMES = {
    "infra",
    "provider",
    "fallback",
    "repair",
    "preflight",
    "classif",
    "environment_failure",
}
FORBIDDEN_REPAIR_PROMPT_STRINGS = {
    "Failure kind:",
    "Raw issue JSON",
    "Likely paths:",
    "PRE-FLIGHT REPAIR ONLY",
    "narrowest relevant check",
    "and stop",
}
FULL_PACKET_REPAIR_CONTEXT_STRINGS = {
    "repair_packet",
    "Repair packet",
    "latest_oracle_result",
    "attempt_history",
    "acceptance_oracle",
}
SYMPTOM_CAP_STRINGS = {
    "max_attempts_per_kind",
    "kind_attempt_cap",
    "total_attempt_cap",
    "repeated_fingerprint",
    "_attempts_by_kind",
}
VERDICT_TEXT_SCAN_GATE_MARKERS = {
    "deprecation",
    "stdout",
    "stderr",
    "test_output",
    "transcript",
    "stub_text",
    "grep",
    "page_resolves",
    "pages_resolve",
    "route_resolves",
    "routes_resolve",
    "endpoint_resolves",
    "endpoints_resolve",
    "action_has_test",
    "actions_have_tests",
    "mutating_action_has_feedback",
}
VERDICT_GATE_COLLECTIONS = {"checks", "failed_required"}
VERDICT_ADVISORY_COLLECTIONS = {"advisories"}
BAD_DEPENDENCY_VERDICTS = {"catastrophic", "merge_blocked", "unverified"}
DEPENDENCY_CONTEXT = {"dependency", "dependencies", "ready", "done", "completed", "terminal"}
IDENTITY_CONTEXT = {"branch", "worktree", "identity", "cwd", "project_dir", "base_ref"}
JOURNEY_VERDICT_SINK_REL = "otto/journey_verdict_sink.py"
JOURNEY_AGGREGATE_HEURISTIC_MARKERS = {
    "overall_native_passed",
    "overall_browser_passed",
    "native tests passed",
    "browser journey passed",
}
TOLERATED_STATE_READER_SYMBOL_PARTS = {
    "rows",
    "events",
    "history",
    "manifest",
    "lock_record",
    "agent_session",
    "jsonl",
    "package_scripts",
}


@dataclass(frozen=True)
class Violation:
    rel_path: str
    symbol: str
    rule: str
    line: int
    detail: str

    @property
    def key(self) -> str:
        return f"{self.rel_path}:{self.symbol}:{self.rule}"

    def render(self) -> str:
        return f"{self.rel_path}:{self.line} [{self.rule}] {self.symbol}: {self.detail}"


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _name_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_text(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        parts = [_name_text(node.func)]
        parts.extend(_name_text(arg) for arg in node.args)
        parts.extend(f"{kw.arg}={_name_text(kw.value)}" for kw in node.keywords if kw.arg)
        return " ".join(part for part in parts if part)
    if isinstance(node, ast.Tuple | ast.List):
        return " ".join(_name_text(item) for item in node.elts)
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""


def _normalized_symbol(name: str) -> str:
    return name.lstrip("_")


def _contains_any(text: str, needles: set[str] | tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(needle in lower for needle in needles)


def _constant_string_set(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Set):
        return {elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
    if (
        isinstance(node, ast.Call)
        and _name_text(node.func).endswith("frozenset")
        and node.args
        and isinstance(node.args[0], ast.Set)
    ):
        return _constant_string_set(node.args[0])
    return set()


def _constant_strings(node: ast.AST) -> list[tuple[str, ast.AST]]:
    strings: list[tuple[str, ast.AST]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            strings.append((child.value, child))
    return strings


def _is_true_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _success_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            key_name = key.value if isinstance(key, ast.Constant) else ""
            if key_name in SUCCESS_NAMES and _is_true_constant(value):
                return True
            if key_name in {"verdict", "status", "result", "outcome"}:
                if isinstance(value, ast.Constant) and value.value in SUCCESS_STRINGS:
                    return True
    if isinstance(node, ast.Call):
        for keyword in node.keywords:
            if keyword.arg in SUCCESS_NAMES and _is_true_constant(keyword.value):
                return True
            if keyword.arg in {"verdict", "status", "result", "outcome"}:
                if isinstance(keyword.value, ast.Constant) and keyword.value.value in SUCCESS_STRINGS:
                    return True
    if isinstance(node, ast.Constant) and node.value in SUCCESS_STRINGS:
        return True
    return False


def _empty_container_return(node: ast.AST) -> bool:
    if not isinstance(node, ast.Return):
        return False
    if isinstance(node.value, ast.Dict):
        return not node.value.keys
    if isinstance(node.value, (ast.List, ast.Tuple)):
        return not node.value.elts
    return False


def _except_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    if isinstance(handler.type, ast.Tuple):
        return {_name_text(item).split(".")[-1] for item in handler.type.elts}
    return {_name_text(handler.type).split(".")[-1]}


def _swallows_state_parse_failure(handler: ast.ExceptHandler) -> bool:
    names = _except_names(handler)
    if names and not (names & STATE_PARSE_EXCEPTIONS):
        return False
    if _handler_logs(handler):
        return False
    for stmt in handler.body:
        if _empty_container_return(stmt):
            return True
    return False


def _handler_logs(handler: ast.ExceptHandler) -> bool:
    for child in ast.walk(handler):
        if isinstance(child, ast.Call):
            name = _name_text(child.func).lower()
            if any(name.endswith(f".{level}") for level in ("debug", "info", "warning", "error", "exception")):
                return True
    return False


def _tolerated_state_reader(symbol: str) -> bool:
    normalized = _normalized_symbol(symbol).lower()
    return any(part in normalized for part in TOLERATED_STATE_READER_SYMBOL_PARTS)


def _string_substring_on_error_text(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
        return False
    if not isinstance(node.left, ast.Constant) or not isinstance(node.left.value, str):
        return False
    right_text = " ".join(_name_text(comparator) for comparator in node.comparators).lower()
    if ".lower()" not in right_text and not _contains_any(right_text, ERROR_TEXT_NAMES):
        return False
    return _contains_any(right_text, ERROR_TEXT_NAMES)


def _walk_conditions(node: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Compare):
            out.append(child)
    return out


class GuardrailVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.violations: list[Violation] = []
        self.symbol_stack: list[str] = ["<module>"]
        self.context_stack: list[bool] = [False]
        self.assignment_target_stack: list[str] = []

    @property
    def symbol(self) -> str:
        return self.symbol_stack[-1]

    @property
    def suspicious_context(self) -> bool:
        return any(self.context_stack)

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        self.violations.append(
            Violation(
                rel_path=self.rel_path,
                symbol=self.symbol,
                rule=rule,
                line=getattr(node, "lineno", 1),
                detail=detail,
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.symbol_stack.append(node.name)
        self.context_stack.append(_contains_any(node.name, SUSPICIOUS_CONTEXT))
        self.generic_visit(node)
        self.context_stack.pop()
        self.symbol_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self.symbol_stack.append(node.name)
        self.context_stack.append(_contains_any(node.name, SUSPICIOUS_CONTEXT))
        self.generic_visit(node)
        self.context_stack.pop()
        self.symbol_stack.pop()

    def visit_If(self, node: ast.If) -> Any:
        test_text = _name_text(node.test)
        self.context_stack.append(_contains_any(test_text, SUSPICIOUS_BRANCH_CONTEXT))
        self.generic_visit(node)
        self.context_stack.pop()

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> Any:
        symbol = _normalized_symbol(self.symbol)
        if (
            symbol.startswith(STATE_LOAD_PREFIXES)
            and not _tolerated_state_reader(symbol)
            and _swallows_state_parse_failure(node)
        ):
            self._add(
                node,
                "swallowed_state_parse_failure",
                "state loader/parser swallows malformed JSON or IO into empty/default state",
            )
        self.context_stack.append(_contains_any(_name_text(node.type), SUSPICIOUS_CONTEXT))
        self.generic_visit(node)
        self.context_stack.pop()

    def visit_Return(self, node: ast.Return) -> Any:
        if self.suspicious_context and node.value is not None and _success_literal(node.value):
            self._add(
                node,
                "success_on_malformed_path",
                "error/malformed/fallback/default path returns a truthy success verdict",
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> Any:
        targets = " ".join(_name_text(target) for target in node.targets)
        self.assignment_target_stack.append(targets)
        self._check_assignment(node, targets)
        self.generic_visit(node)
        self.assignment_target_stack.pop()

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        target = _name_text(node.target)
        self.assignment_target_stack.append(target)
        self._check_assignment(node, target)
        self.generic_visit(node)
        self.assignment_target_stack.pop()

    def _check_assignment(self, node: ast.Assign | ast.AnnAssign, targets: str) -> None:
        value = node.value
        if value is None:
            return
        if self.suspicious_context and _success_literal(value):
            self._add(
                node,
                "success_on_malformed_path",
                "error/malformed/fallback/default path sets a truthy success verdict",
            )
        literal_set = _constant_string_set(value)
        if "pass" in literal_set and literal_set & BAD_DEPENDENCY_VERDICTS:
            target_text = targets.lower()
            symbol_text = self.symbol.lower()
            if (
                _contains_any(target_text, DEPENDENCY_CONTEXT)
                or _contains_any(symbol_text, DEPENDENCY_CONTEXT)
            ) and "redispatch" not in target_text and "non_runnable" not in target_text:
                self._add(
                    node,
                    "nonpass_dependency_satisfaction_set",
                    "completion/dependency set contains pass plus non-pass terminal verdicts",
                )

    def visit_Compare(self, node: ast.Compare) -> Any:
        symbol_text = self.symbol.lower()
        if _contains_any(symbol_text, CONTROL_FLOW_NAMES) and _string_substring_on_error_text(node):
            self._add(
                node,
                "substring_error_classifier",
                "string-substring branch on error/provider/infra text controls behavior",
            )
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.Or):
            values_text = [_name_text(value).lower() for value in node.values]
            target_text = " ".join(self.assignment_target_stack).lower()
            bool_text = " ".join([target_text, *values_text])
            if any(text == "'main'" or text == '"main"' for text in values_text):
                if _contains_any(bool_text, {"branch", "base_branch", "target_branch", "integration_branch"}):
                    self._add(
                        node,
                        "identity_default_fallback",
                        "branch/worktree identity resolution falls back to literal main",
                    )
            if any(text == "project_dir" for text in values_text):
                if _contains_any(target_text, {"worktree", "cwd"}):
                    self._add(
                        node,
                        "identity_default_fallback",
                        "worktree/cwd identity resolution falls back to project_dir",
                    )
        self.generic_visit(node)


def _scan_file(path: Path) -> list[Violation]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = GuardrailVisitor(_rel(path))
    visitor.visit(tree)
    visitor.violations.extend(_repair_prompt_packet_violations(tree, _rel(path)))
    visitor.violations.extend(_repair_symptom_cap_violations(tree, _rel(path)))
    visitor.violations.extend(_verdict_text_scan_gate_violations(tree, _rel(path)))
    visitor.violations.extend(_journey_verdict_authority_violations(tree, _rel(path)))
    return visitor.violations


def _repair_prompt_packet_violations(tree: ast.AST, rel_path: str) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        normalized = name.lower()
        if not (normalized.startswith("_run_") and "repair" in normalized and "agent" in normalized):
            continue
        strings = _constant_strings(node)
        combined = "\n".join(value for value, _node in strings)
        forbidden = [needle for needle in FORBIDDEN_REPAIR_PROMPT_STRINGS if needle in combined]
        if not forbidden:
            continue
        if any(marker in combined for marker in FULL_PACKET_REPAIR_CONTEXT_STRINGS):
            continue
        first_node = next(
            (string_node for value, string_node in strings if any(needle in value for needle in forbidden)),
            node,
        )
        violations.append(Violation(
            rel_path=rel_path,
            symbol=name,
            rule="repair_prompt_without_full_packet",
            line=getattr(first_node, "lineno", getattr(node, "lineno", 1)),
            detail=(
                "repair-agent prompt uses forbidden narrow failure framing "
                "without full repair-packet context"
            ),
        ))
    return violations


def _repair_symptom_cap_violations(tree: ast.AST, rel_path: str) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in SYMPTOM_CAP_STRINGS:
            violations.append(Violation(
                rel_path=rel_path,
                symbol="<module>",
                rule="repair_symptom_cap",
                line=getattr(node, "lineno", 1),
                detail=f"repair loop references symptom/per-kind cap {node.id!r}",
            ))
        elif isinstance(node, ast.arg) and node.arg in SYMPTOM_CAP_STRINGS:
            violations.append(Violation(
                rel_path=rel_path,
                symbol="<module>",
                rule="repair_symptom_cap",
                line=getattr(node, "lineno", 1),
                detail=f"repair loop exposes symptom/per-kind cap {node.arg!r}",
            ))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in SYMPTOM_CAP_STRINGS:
                violations.append(Violation(
                    rel_path=rel_path,
                    symbol="<module>",
                    rule="repair_symptom_cap",
                    line=getattr(node, "lineno", 1),
                    detail=f"repair loop emits symptom/per-kind cap reason {node.value!r}",
                ))
    return violations


def _call_receiver_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return _name_text(node.func.value)
    return ""


def _is_collection_append_or_extend(node: ast.Call, collection_names: set[str]) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in {"append", "extend"}:
        return False
    return _call_receiver_name(node) in collection_names


def _uses_text_scan_gate_marker(node: ast.AST) -> bool:
    return _contains_any(_name_text(node), VERDICT_TEXT_SCAN_GATE_MARKERS)


def _assigns_to_name(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Assign):
        return any(_name_text(target) in names for target in node.targets)
    if isinstance(node, ast.AnnAssign):
        return _name_text(node.target) in names
    return False


def _verdict_text_scan_gate_violations(tree: ast.AST, rel_path: str) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "validate_lead_verdict":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if _is_collection_append_or_extend(child, VERDICT_GATE_COLLECTIONS):
                    if any(_uses_text_scan_gate_marker(arg) for arg in child.args):
                        violations.append(Violation(
                            rel_path=rel_path,
                            symbol=node.name,
                            rule="verdict_text_scan_gate",
                            line=getattr(child, "lineno", getattr(node, "lineno", 1)),
                            detail=(
                                "verdict gate collection receives an unstructured "
                                "text/source scanner; route it to advisory telemetry"
                            ),
                        ))
                if _is_collection_append_or_extend(child, VERDICT_ADVISORY_COLLECTIONS):
                    continue
            elif _assigns_to_name(child, {"failed_required"}):
                if _contains_any(_name_text(child), VERDICT_ADVISORY_COLLECTIONS):
                    violations.append(Violation(
                        rel_path=rel_path,
                        symbol=node.name,
                        rule="advisory_controls_verdict",
                        line=getattr(child, "lineno", getattr(node, "lineno", 1)),
                        detail="failed-required verdict computation depends on advisory telemetry",
                    ))
            elif isinstance(child, ast.If):
                assigns_final_verdict = any(
                    _assigns_to_name(stmt, {"final_verdict"})
                    for stmt in child.body
                )
                if assigns_final_verdict and _contains_any(
                    _name_text(child.test),
                    VERDICT_ADVISORY_COLLECTIONS,
                ):
                    violations.append(Violation(
                        rel_path=rel_path,
                        symbol=node.name,
                        rule="advisory_controls_verdict",
                        line=getattr(child, "lineno", getattr(node, "lineno", 1)),
                        detail="final verdict branch depends on advisory telemetry",
                    ))
    return violations


def _journey_verdict_authority_violations(tree: ast.AST, rel_path: str) -> list[Violation]:
    if rel_path == JOURNEY_VERDICT_SINK_REL:
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        text = ast.unparse(node)
        lowered = text.lower()
        if _reads_agent_narrated_journey_passed(node):
            violations.append(Violation(
                rel_path=rel_path,
                symbol=name,
                rule="journey_verdict_from_agent_narration",
                line=getattr(node, "lineno", 1),
                detail=(
                    "behavior journey verdicts must come from resolve_journey_verdicts "
                    "using controller-run executor results, not agent_verdict['journeys'][].passed"
                ),
            ))
        if any(marker in lowered for marker in JOURNEY_AGGREGATE_HEURISTIC_MARKERS):
            violations.append(Violation(
                rel_path=rel_path,
                symbol=name,
                rule="journey_verdict_from_aggregate_tests",
                line=getattr(node, "lineno", 1),
                detail=(
                    "native-test/browser aggregate success must not synthesize "
                    "behavior journey pass verdicts"
                ),
            ))
    return violations


def _reads_agent_narrated_journey_passed(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    reads_agent_journeys = False
    reads_passed = False
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "get"
            and _name_text(child.func.value) == "agent_verdict"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and child.args[0].value == "journeys"
        ):
            reads_agent_journeys = True
        if (
            isinstance(child, ast.Subscript)
            and _name_text(child.value) == "agent_verdict"
            and isinstance(child.slice, ast.Constant)
            and child.slice.value == "journeys"
        ):
            reads_agent_journeys = True
        if isinstance(child, ast.Constant) and child.value == "passed":
            reads_passed = True
        if isinstance(child, ast.Attribute) and child.attr == "passed":
            reads_passed = True
    return reads_agent_journeys and reads_passed


def test_brittleness_guardrail_has_reasoned_allowlist() -> None:
    assert ALLOWLIST, "ALLOWLIST must be explicit; do not hide violations inline"
    for key, allow in ALLOWLIST.items():
        assert allow.reason.strip(), f"{key} must include a non-empty reason"


def test_otto_brittleness_guardrail() -> None:
    violations: list[Violation] = []
    for path in sorted(OTTO_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        violations.extend(_scan_file(path))

    unexpected = [violation for violation in violations if violation.key not in ALLOWLIST]
    stale_allowlist = sorted(set(ALLOWLIST) - {violation.key for violation in violations})

    if unexpected or stale_allowlist:
        rendered = [
            "Brittleness guardrail failed.",
            "",
            "Unexpected violations:",
            *[f"  - {violation.render()}" for violation in unexpected],
            "",
            "Stale allowlist entries:",
            *[f"  - {key}" for key in stale_allowlist],
            "",
            "Fix the production anti-pattern, or add a narrow ALLOWLIST entry ",
            "with a concrete reason and doc/commit reference.",
        ]
        raise AssertionError("\n".join(rendered))


def test_verdict_text_scan_guardrail_step2() -> None:
    path = OTTO_ROOT / "v5_verification_plan.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = _verdict_text_scan_gate_violations(tree, _rel(path))
    assert not violations, "\n".join(violation.render() for violation in violations)


def test_journey_verdict_authority_guardrail_unit3() -> None:
    violations: list[Violation] = []
    for path in sorted(OTTO_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_journey_verdict_authority_violations(tree, _rel(path)))

    assert not violations, "\n".join(violation.render() for violation in violations)
