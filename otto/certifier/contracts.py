"""Structured certification contracts.

The certifier prompt still asks for human-readable markers for compatibility,
but Mission Control should reason over a typed contract whenever one is
available. This module owns that normalization boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


RESULT_FILENAME = "certification-result.json"
REQUIREMENTS_FILENAME = "proof-requirements.json"

StoryStatus = str
ProductVerdict = str
ProofQuality = str

_VALID_STORY_STATUSES = {"pass", "fail", "warn", "skipped", "flag_for_human"}
_VALID_PRODUCT_VERDICTS = {"pass", "fail", "blocked"}
_VALID_PROOF_QUALITIES = {"complete", "partial", "missing", "invalid", "not_required"}


class CertificationContractError(ValueError):
    """Raised when a structured certifier contract is malformed."""


class EvidenceItem(BaseModel):
    """One concrete evidence item attached to a story."""

    model_config = ConfigDict(extra="allow")

    type: str
    path: str = ""
    command: str = ""
    output: str = ""
    mime: str = ""
    bytes: int | None = None
    hash: str = ""
    assertions: list[str] = Field(default_factory=list)
    covers_user_action: bool | None = None

    @field_validator("type")
    @classmethod
    def _type_required(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("evidence.type is required")
        return value


class StoryCertification(BaseModel):
    """Structured result for one story."""

    model_config = ConfigDict(extra="allow")

    story_id: str
    status: StoryStatus
    claim: str = ""
    observed_steps: list[str] = Field(default_factory=list)
    observed_result: str = ""
    surface: str = ""
    methodology: str = ""
    summary: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @field_validator("story_id")
    @classmethod
    def _story_id_required(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("story_id is required")
        return value

    @field_validator("status")
    @classmethod
    def _status_known(cls, value: str) -> str:
        value = str(value or "").strip().lower()
        if value not in _VALID_STORY_STATUSES:
            raise ValueError(f"unknown story status: {value}")
        return value


class CertificationResult(BaseModel):
    """Machine-readable certifier result."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    product_verdict: ProductVerdict
    proof_quality: ProofQuality = "not_required"
    stories: list[StoryCertification]
    coverage_observed: list[str] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    diagnosis: str = ""
    source: str = "certifier_json"

    @field_validator("product_verdict")
    @classmethod
    def _product_verdict_known(cls, value: str) -> str:
        value = str(value or "").strip().lower()
        if value not in _VALID_PRODUCT_VERDICTS:
            raise ValueError(f"unknown product verdict: {value}")
        return value

    @field_validator("proof_quality")
    @classmethod
    def _proof_quality_known(cls, value: str) -> str:
        value = str(value or "").strip().lower() or "not_required"
        if value not in _VALID_PROOF_QUALITIES:
            raise ValueError(f"unknown proof quality: {value}")
        return value

    @field_validator("stories")
    @classmethod
    def _stories_required(cls, value: list[StoryCertification]) -> list[StoryCertification]:
        if not value:
            raise ValueError("at least one story result is required")
        return value


def requirements_path(report_dir: Path) -> Path:
    return report_dir / REQUIREMENTS_FILENAME


def result_path(report_dir: Path) -> Path:
    return report_dir / RESULT_FILENAME


def write_proof_requirements(report_dir: Path, requirements: dict[str, Any]) -> Path:
    """Persist the pre-certification evidence contract."""
    path = requirements_path(report_dir)
    payload = dict(requirements or {})
    payload.setdefault("schema_version", 1)
    _write_json(path, payload)
    return path


def load_certification_result(report_dir: Path) -> CertificationResult | None:
    path = result_path(report_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return CertificationResult.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise CertificationContractError(f"{RESULT_FILENAME} is invalid: {exc}") from exc


def write_certification_result(report_dir: Path, result: CertificationResult | dict[str, Any]) -> Path:
    path = result_path(report_dir)
    if isinstance(result, CertificationResult):
        payload = result.model_dump(mode="json")
    else:
        payload = CertificationResult.model_validate(result).model_dump(mode="json")
    _write_json(path, payload)
    return path


def story_results_from_contract(result: CertificationResult) -> list[dict[str, Any]]:
    """Convert structured JSON stories to Otto's legacy story row shape."""
    rows: list[dict[str, Any]] = []
    for story in result.stories:
        verdict = {
            "pass": "PASS",
            "fail": "FAIL",
            "warn": "WARN",
            "skipped": "SKIPPED",
            "flag_for_human": "FLAG_FOR_HUMAN",
        }[story.status]
        evidence_text = _evidence_text(story.evidence)
        row: dict[str, Any] = {
            "story_id": story.story_id,
            "verdict": verdict,
            "passed": verdict in {"PASS", "WARN"},
            "summary": story.summary or story.observed_result or story.claim,
            "claim": story.claim or story.summary,
            "observed_result": story.observed_result or story.summary,
        }
        if story.observed_steps:
            row["observed_steps"] = list(story.observed_steps)
        if story.surface:
            row["surface"] = story.surface
        if story.methodology:
            row["methodology"] = story.methodology
            row["interaction_method"] = story.methodology
        if evidence_text:
            row["evidence"] = evidence_text
        if verdict == "WARN":
            row["warn"] = True
        rows.append(row)
    return rows


def contract_from_marker_results(
    *,
    story_results: list[dict[str, Any]],
    verdict_pass: bool,
    proof_quality: str,
    coverage_observed: list[str],
    coverage_gaps: list[str],
    diagnosis: str,
) -> CertificationResult:
    """Normalize legacy marker output into the JSON schema for downstream use."""
    has_failures = any(str(story.get("verdict") or "").upper() == "FAIL" for story in story_results)
    flagged = any(str(story.get("verdict") or "").upper() == "FLAG_FOR_HUMAN" for story in story_results)
    product_verdict = "pass" if verdict_pass and not has_failures and not flagged else "fail"
    stories = []
    for story in story_results:
        verdict = str(story.get("verdict") or ("PASS" if story.get("passed") else "FAIL")).upper()
        status = {
            "PASS": "pass",
            "FAIL": "fail",
            "WARN": "warn",
            "SKIPPED": "skipped",
            "FLAG_FOR_HUMAN": "flag_for_human",
        }.get(verdict, "fail")
        evidence_items = []
        evidence_text = str(story.get("evidence") or "").strip()
        if evidence_text:
            evidence_items.append({"type": "text", "output": evidence_text})
        stories.append(
            {
                "story_id": str(story.get("story_id") or "").strip(),
                "status": status,
                "claim": str(story.get("claim") or story.get("summary") or "").strip(),
                "observed_steps": list(story.get("observed_steps") or []),
                "observed_result": str(story.get("observed_result") or story.get("summary") or "").strip(),
                "surface": str(story.get("surface") or "").strip(),
                "methodology": str(story.get("methodology") or story.get("interaction_method") or "").strip(),
                "summary": str(story.get("summary") or "").strip(),
                "evidence": evidence_items,
            }
        )
    return CertificationResult.model_validate(
        {
            "schema_version": 1,
            "product_verdict": product_verdict,
            "proof_quality": proof_quality,
            "stories": stories,
            "coverage_observed": coverage_observed,
            "coverage_gaps": coverage_gaps,
            "diagnosis": diagnosis,
            "source": "legacy_markers",
        }
    )


def proof_quality_from_demo_evidence(demo: dict[str, Any] | None) -> str:
    data = demo if isinstance(demo, dict) else {}
    status = str(data.get("demo_status") or "").strip().lower()
    required = bool(data.get("demo_required"))
    if status == "strong":
        return "complete"
    if status == "partial":
        return "partial"
    if status == "missing":
        return "missing"
    if status == "not_applicable" or not required:
        return "not_required"
    return "invalid"


def _evidence_text(items: list[EvidenceItem]) -> str:
    lines: list[str] = []
    for item in items:
        if item.command:
            lines.append(f"$ {item.command.lstrip('$ ').strip()}")
        if item.output:
            lines.append(item.output.strip())
        if item.path:
            detail = f"{item.type}: {item.path}"
            if item.bytes is not None:
                detail += f" ({item.bytes} bytes)"
            if item.mime:
                detail += f" {item.mime}"
            lines.append(detail)
        for assertion in item.assertions:
            lines.append(f"- {assertion}")
    return "\n".join(line for line in lines if line).strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
