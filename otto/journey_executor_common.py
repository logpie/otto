"""Shared helpers for controller-run behavior journey executors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from otto.observability import iso_timestamp


def journey_id(journey: dict[str, Any]) -> str:
    return str(journey.get("id") or "<unnamed>").strip() or "<unnamed>"


def artifact_run_name(started: str) -> str:
    return started.replace(":", "").replace("-", "").replace("T", "-").replace("Z", "")


def executor_result_payload(
    journey: dict[str, Any],
    *,
    source: str,
    status: str,
    detail: str,
    proof_usable: bool,
    artifact_paths: list[Path],
) -> dict[str, Any]:
    return {
        "id": journey_id(journey),
        "status": status,
        "source": source,
        "proof_usable": proof_usable,
        "detail": detail,
        "artifact_paths": [str(path) for path in artifact_paths],
        "_written_at": iso_timestamp(),
    }


def executor_pass_result(
    journey: dict[str, Any],
    *,
    source: str,
    detail: str,
    artifact_paths: list[Path],
) -> dict[str, Any]:
    return executor_result_payload(
        journey,
        source=source,
        status="pass",
        detail=detail,
        proof_usable=True,
        artifact_paths=artifact_paths,
    )


def executor_non_pass_result(
    journey: dict[str, Any],
    *,
    source: str,
    status: str,
    detail: str,
    proof_usable: bool,
    artifact_paths: list[Path],
) -> dict[str, Any]:
    return executor_result_payload(
        journey,
        source=source,
        status=status,
        detail=detail,
        proof_usable=proof_usable,
        artifact_paths=artifact_paths,
    )
