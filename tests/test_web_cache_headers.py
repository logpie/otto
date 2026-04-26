"""Tests for ``Cache-Control`` headers on the Mission Control web surface.

The SPA shell (`/` and `/static/index.html`) must be ``no-store`` because it
embeds the current hashed asset filenames; the hashed assets themselves are
content-addressed and safe to cache forever (`immutable`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from otto.web.app import create_app
from otto.web.bundle import BUILD_STAMP_PATH, referenced_static_assets


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "otto" / "web" / "static"


def _stamp_or_skip() -> None:
    if not BUILD_STAMP_PATH.is_file():
        pytest.skip(
            "build-stamp.json missing; run `npm run web:build` once to populate it."
        )


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    _stamp_or_skip()
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# t\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    app = create_app(repo)
    return TestClient(app)


def test_index_html_cache_no_store_at_root(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"


def test_index_html_cache_no_store_via_static(client: TestClient) -> None:
    resp = client.get("/static/index.html")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"


def test_static_assets_cache_immutable(client: TestClient) -> None:
    refs = referenced_static_assets(STATIC_DIR / "index.html")
    asset_refs = [r for r in refs if r.startswith("assets/")]
    assert asset_refs, "expected at least one /static/assets/ reference in index.html"
    for rel in asset_refs:
        resp = client.get(f"/static/{rel}")
        assert resp.status_code == 200, f"missing /static/{rel}"
        assert (
            resp.headers.get("cache-control")
            == "public, max-age=31536000, immutable"
        ), f"unexpected cache-control for /static/{rel}: {resp.headers.get('cache-control')!r}"


def test_build_stamp_endpoint_no_store(client: TestClient) -> None:
    """build-stamp.json is metadata, not a hashed asset → don't cache."""
    resp = client.get("/static/build-stamp.json")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
