"""Spec-review routes — STUB (legacy pipeline removed).

The legacy editable-spec amendment chain is gone with the legacy
pipeline. v5's intent-to-product spec is single-pass: there is no
post-approval edit/amend flow, so every endpoint here now returns
HTTP 410 Gone.

The module shape is preserved (``install_spec_review_routes``) so
``otto/web/app.py`` can keep mounting it without conditional imports.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger("otto.web.spec_review")

_GONE_DETAIL = (
    "spec review removed with legacy pipeline; "
    "v5 i2p spec is single-pass"
)


def _gone() -> HTTPException:
    return HTTPException(status_code=410, detail=_GONE_DETAIL)


def install_spec_review_routes(
    app: FastAPI,
    *,
    project_dir: Path | None = None,
    project_dir_provider: Callable[[], Path | None] | None = None,
) -> None:
    """Mount stubbed `/api/specs/*` routes returning HTTP 410."""
    if project_dir_provider is None and project_dir is None:
        raise ValueError(
            "install_spec_review_routes needs project_dir or project_dir_provider"
        )

    router = APIRouter(prefix="/api/specs", tags=["spec-review"])

    @router.get("/{session_id}/markdown")
    def get_markdown(session_id: str) -> JSONResponse:  # noqa: ARG001
        raise _gone()

    @router.post("/{session_id}/edit")
    def edit_spec(session_id: str) -> JSONResponse:  # noqa: ARG001
        raise _gone()

    @router.post("/{session_id}/approve")
    def approve_spec(session_id: str) -> JSONResponse:  # noqa: ARG001
        raise _gone()

    @router.get("/{session_id}/versions")
    def list_versions(session_id: str) -> JSONResponse:  # noqa: ARG001
        raise _gone()

    @router.get("/{session_id}/diff")
    def diff_versions(session_id: str) -> JSONResponse:  # noqa: ARG001
        raise _gone()

    app.include_router(router)


__all__ = ["install_spec_review_routes"]
