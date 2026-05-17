"""Execution-scope applicability policy for behavior journeys."""

from __future__ import annotations

from typing import Literal

ExecutionScope = Literal["leaf", "subtree_integration", "root_integration"]
VerificationLevel = Literal["ui", "api"]
Applicability = Literal["run", "defer", "skip", "fail"]

EXECUTION_SCOPES: tuple[ExecutionScope, ...] = (
    "leaf",
    "subtree_integration",
    "root_integration",
)
VERIFICATION_LEVELS: tuple[VerificationLevel, ...] = ("ui", "api")

APPLICABILITY_POLICY: dict[tuple[ExecutionScope, VerificationLevel], Applicability] = {
    ("leaf", "ui"): "defer",
    ("leaf", "api"): "run",
    ("subtree_integration", "ui"): "defer",
    ("subtree_integration", "api"): "run",
    ("root_integration", "ui"): "run",
    ("root_integration", "api"): "skip",
}

_NODE_KIND_FOR_SCOPE: dict[ExecutionScope, str] = {
    "leaf": "leaf",
    "subtree_integration": "integration",
    "root_integration": "integration",
}

_INFER_FROM_LEAD_SHAPE: dict[tuple[str, bool], ExecutionScope] = {
    ("plan_or_inline", False): "leaf",
    ("plan_or_inline", True): "leaf",
    ("integration", False): "root_integration",
    ("integration", True): "subtree_integration",
}


def applicability_for(
    execution_scope: ExecutionScope,
    verification_level: VerificationLevel,
) -> Applicability:
    """Return the exhaustive scope/level policy decision."""

    return APPLICABILITY_POLICY[(execution_scope, verification_level)]


def infer_execution_scope(*, kind: str, integration_branch: str | None) -> ExecutionScope:
    """Infer execution scope from the existing lead invocation shape."""

    return _INFER_FROM_LEAD_SHAPE[(kind, bool(integration_branch))]


def node_kind_for_scope(execution_scope: ExecutionScope) -> str:
    """Map the explicit scope back to the legacy verifier node_kind string."""

    return _NODE_KIND_FOR_SCOPE[execution_scope]


def validate_policy_exhaustive() -> None:
    expected: set[tuple[ExecutionScope, VerificationLevel]] = {
        (scope, level)
        for scope in EXECUTION_SCOPES
        for level in VERIFICATION_LEVELS
    }
    actual: set[tuple[ExecutionScope, VerificationLevel]] = set(APPLICABILITY_POLICY)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise AssertionError(f"journey applicability policy mismatch missing={missing} extra={extra}")
