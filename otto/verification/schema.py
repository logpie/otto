"""Shared verification plan schema.

Verification plans are intentionally simple JSON. They give operators and
agents the same contract: what will be checked, what may be skipped, and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Literal

from otto.observability import write_json_atomic

VerificationPolicy = Literal["smart", "fast", "full", "skip"]
VerificationStatus = Literal[
    "pending",
    "running",
    "pass",
    "fail",
    "warn",
    "skipped",
    "flag_for_human",
]

VERIFICATION_POLICIES: tuple[VerificationPolicy, ...] = ("smart", "fast", "full", "skip")


@dataclass(frozen=True)
class VerificationCheck:
    id: str
    label: str
    action: str = "CHECK"
    status: VerificationStatus = "pending"
    reason: str = ""
    source: str = ""
    evidence: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "action": self.action,
            "status": self.status,
            "reason": self.reason,
            "source": self.source,
            "evidence": list(self.evidence),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VerificationPlan:
    scope: str
    target: str
    policy: VerificationPolicy = "smart"
    risk_level: str = ""
    verification_level: str = ""
    allow_skip: bool = True
    reasons: list[str] = field(default_factory=list)
    checks: list[VerificationCheck] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "target": self.target,
            "policy": self.policy,
            "risk_level": self.risk_level,
            "verification_level": self.verification_level,
            "allow_skip": self.allow_skip,
            "reasons": list(self.reasons),
            "checks": [check.to_dict() for check in self.checks],
            "metadata": dict(self.metadata),
        }


def normalize_verification_policy(value: str | None, *, default: VerificationPolicy = "smart") -> VerificationPolicy:
    normalized = str(value or default).strip().lower().replace("_", "-")
    aliases = {
        "none": "skip",
        "off": "skip",
        "no-certify": "skip",
        "no-cert": "skip",
        "quick": "fast",
        "targeted": "smart",
        "thorough": "full",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in VERIFICATION_POLICIES:
        raise ValueError(
            f"unknown verification policy {value!r}; expected one of "
            f"{', '.join(VERIFICATION_POLICIES)}"
        )
    return normalized  # type: ignore[return-value]


def verification_plan_from_dict(data: dict[str, Any]) -> VerificationPlan:
    checks: list[VerificationCheck] = []
    for raw in data.get("checks") or []:
        if not isinstance(raw, dict):
            continue
        checks.append(
            VerificationCheck(
                id=str(raw.get("id") or raw.get("story_id") or raw.get("label") or "check"),
                label=str(raw.get("label") or raw.get("story_id") or raw.get("id") or "check"),
                action=str(raw.get("action") or "CHECK"),
                status=str(raw.get("status") or "pending"),  # type: ignore[arg-type]
                reason=str(raw.get("reason") or ""),
                source=str(raw.get("source") or raw.get("source_branch") or ""),
                evidence=[str(item) for item in (raw.get("evidence") or []) if item],
                metadata=dict(raw.get("metadata") or {}),
            )
        )
    return VerificationPlan(
        schema_version=int(data.get("schema_version") or 1),
        scope=str(data.get("scope") or ""),
        target=str(data.get("target") or ""),
        policy=normalize_verification_policy(str(data.get("policy") or "smart")),
        risk_level=str(data.get("risk_level") or ""),
        verification_level=str(data.get("verification_level") or ""),
        allow_skip=bool(data.get("allow_skip", True)),
        reasons=[str(item) for item in (data.get("reasons") or []) if item],
        checks=checks,
        metadata=dict(data.get("metadata") or {}),
    )


def write_verification_plan(path: Path, plan: VerificationPlan | dict[str, Any]) -> Path:
    payload = plan.to_dict() if isinstance(plan, VerificationPlan) else dict(plan)
    write_json_atomic(Path(path), payload)
    return Path(path)


def read_verification_plan(path: Path) -> VerificationPlan | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return verification_plan_from_dict(data)


def format_verification_plan(plan: VerificationPlan | dict[str, Any]) -> str:
    if isinstance(plan, dict):
        plan = verification_plan_from_dict(plan)
    lines: list[str] = [
        "## Verification Plan",
        "",
        f"- Scope: `{plan.scope}`",
        f"- Target: `{plan.target or '-'}`",
        f"- Policy: `{plan.policy}`",
        f"- Risk level: `{plan.risk_level or '-'}`",
        f"- Verification level: `{plan.verification_level or '-'}`",
        f"- Skips allowed: `{'yes' if plan.allow_skip else 'no'}`",
    ]
    if plan.reasons:
        lines += ["", "Reasons:"]
        lines.extend(f"- {reason}" for reason in plan.reasons)
    if plan.checks:
        lines += ["", "Checks:"]
        for check in plan.checks:
            source = f" from `{check.source}`" if check.source else ""
            reason = f" - {check.reason}" if check.reason else ""
            lines.append(f"- `{check.id}`{source}: {check.action}{reason}")
    return "\n".join(lines).rstrip() + "\n"
