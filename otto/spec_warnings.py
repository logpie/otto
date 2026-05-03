"""ValidationWarning — non-blocking signal from the permissive spec parser.

v2.1 design (see docs/intent-to-product-v2-plan.md): the spec parser
NEVER raises on shape problems that can be coerced. Instead it returns
warnings that surface in journal events, the proof packet, and audit
review. Hard reject only on truly unusable input (no slices AND no
intent AND no structure).

A warning is informational: the run continues. The audit and human
reviewer see them and can react. v1's strict-validation cheap-fail
class (R3, R5, R8, R17, R22, R26) goes away entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


WarningCode = Literal[
    "spec.coerce.amendment",       # non-dict amendment entry → wrapped
    "spec.coerce.slice",           # non-dict slice entry → skipped or wrapped
    "spec.coerce.check",           # malformed check payload → coerced/dropped
    "spec.coerce.unknown_kind",    # unknown check.kind → dropped with warning
    "spec.coerce.slice_id",        # slice id slugified
    "spec.coerce.duplicate_id",    # duplicate id auto-suffixed
    "spec.coerce.project_kind",    # unrecognized kind → defaulted
    "spec.coerce.dep_unknown",     # dep refers to non-existent slice → dropped
    "spec.coerce.field",           # generic field-shape coercion
    "spec.shape.missing",          # advisory: recommended field missing
    "check.runtime.malformed",     # check ran but payload was malformed
]


@dataclass(frozen=True)
class ValidationWarning:
    """One non-blocking diagnostic from spec parsing or check execution.

    Attributes:
        code: Stable identifier for the warning category. Use one of
            WarningCode literals; tests assert on these.
        path: Dot-path to the affected field (e.g.,
            "slices[2].checks[0].expression" or "amendments[1]").
        message: Human-readable one-line description.
        coerced_to: If the parser auto-coerced the value, repr of the
            replacement; otherwise None.
    """
    code: str
    path: str
    message: str
    coerced_to: str | None = None


@dataclass
class WarningCollector:
    """Accumulates warnings during a parse or check run.

    Used as a thread-local-equivalent context within parse_spec; tests
    inspect `collector.warnings` after.
    """
    warnings: list[ValidationWarning] = field(default_factory=list)

    def add(
        self,
        code: str,
        path: str,
        message: str,
        coerced_to: str | None = None,
    ) -> None:
        self.warnings.append(
            ValidationWarning(code=code, path=path, message=message, coerced_to=coerced_to)
        )

    def extend(self, other: "WarningCollector") -> None:
        self.warnings.extend(other.warnings)

    def __len__(self) -> int:
        return len(self.warnings)

    def __bool__(self) -> bool:
        return bool(self.warnings)


__all__ = ["ValidationWarning", "WarningCollector", "WarningCode"]
