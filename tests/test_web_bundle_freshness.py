"""Tests for the bundle freshness / asset existence checks.

These cover the runtime guard wired into ``otto.web.app.create_app`` —
specifically that we fail loud when:
  * ``index.html`` references an asset that no longer exists,
  * a tracked source file changes after the bundle was built,
  * the build stamp is missing required fields.

And that the documented escape hatches behave as advertised:
  * ``OTTO_WEB_SKIP_FRESHNESS=1`` bypasses the hash compare with a warning.
  * ``OTTO_WEB_DEV=0`` skips the hash compare (prod / wheel install mode).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from otto.web import bundle as bundle_mod
from otto.web.bundle import (
    BUILD_STAMP_PATH,
    BundleBrokenError,
    BundleError,
    BundleStaleError,
    BundleStampMissingError,
    compute_source_hash,
    referenced_static_assets,
    verify_assets_present,
    verify_bundle_freshness,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "otto" / "web" / "static"


def _stamp_present_or_skip() -> None:
    if not BUILD_STAMP_PATH.is_file():
        pytest.skip(
            "build-stamp.json missing; run `npm run web:build` once to populate it."
        )


# Stamp + asset structural tests -----------------------------------------
def test_bundle_stamp_present() -> None:
    _stamp_present_or_skip()
    payload = json.loads(BUILD_STAMP_PATH.read_text(encoding="utf-8"))
    for key in ("source_hash", "built_at", "vite_version", "node_version", "git_commit"):
        assert key in payload, f"build-stamp missing {key!r}: {payload}"
    assert isinstance(payload["source_hash"], str) and len(payload["source_hash"]) == 64


def test_index_html_assets_exist() -> None:
    """Every `/static/...` ref in the shipped index.html resolves to a file."""
    index_html = STATIC_DIR / "index.html"
    assert index_html.is_file()
    refs = referenced_static_assets(index_html)
    # We expect at least the JS bundle to be referenced.
    assert refs, "index.html references no /static/* assets"
    for rel in refs:
        target = STATIC_DIR / rel
        assert target.is_file(), f"index.html references missing asset: /static/{rel}"


# verify_bundle_freshness behavior ---------------------------------------
def test_bundle_stamp_matches_source_hash_in_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _stamp_present_or_skip()
    monkeypatch.setenv("OTTO_WEB_DEV", "1")
    monkeypatch.delenv("OTTO_WEB_SKIP_FRESHNESS", raising=False)
    # Running against the real tree should be a no-op: passes silently.
    verify_bundle_freshness()


def _seed_static_tree(tmp_path: Path, *, source_hash: str | None = None) -> tuple[Path, Path]:
    """Build a minimal fake static dir + matching client/src for tests."""
    static = tmp_path / "static"
    assets = static / "assets"
    assets.mkdir(parents=True)
    asset_name = "index-fake0001.js"
    (assets / asset_name).write_text("/* fake */", encoding="utf-8")
    (static / "index.html").write_text(
        "<!doctype html><html><head>"
        f'<script src="/static/assets/{asset_name}"></script>'
        "</head><body><div id=root></div></body></html>",
        encoding="utf-8",
    )
    src = tmp_path / "client" / "src"
    src.mkdir(parents=True)
    (src / "main.tsx").write_text("export const x = 1;\n", encoding="utf-8")
    if source_hash is None:
        source_hash = compute_source_hash(src_dir=src, toolchain_files=())
    (static / "build-stamp.json").write_text(
        json.dumps(
            {
                "source_hash": source_hash,
                "built_at": "2025-01-01T00:00:00+00:00",
                "vite_version": "test",
                "node_version": "test",
                "git_commit": "deadbeef",
            }
        ),
        encoding="utf-8",
    )
    return static, src


def test_bundle_stamp_mismatch_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static, src = _seed_static_tree(tmp_path)
    # Mutate a tracked source file → hash drifts.
    (src / "main.tsx").write_text("export const x = 2;\n", encoding="utf-8")
    monkeypatch.setattr(bundle_mod, "_TOOLCHAIN_FILES", ())
    monkeypatch.setenv("OTTO_WEB_DEV", "1")
    monkeypatch.delenv("OTTO_WEB_SKIP_FRESHNESS", raising=False)

    with pytest.raises(BundleStaleError) as excinfo:
        verify_bundle_freshness(static_dir=static, src_dir=src)
    msg = str(excinfo.value)
    assert "stale" in msg.lower()
    assert "npm run web:build" in msg


def test_bundle_assets_missing_fails(tmp_path: Path) -> None:
    static, src = _seed_static_tree(tmp_path)
    # Remove the referenced JS asset → broken bundle.
    asset = next((static / "assets").iterdir())
    asset.unlink()
    with pytest.raises(BundleBrokenError) as excinfo:
        verify_bundle_freshness(static_dir=static, src_dir=src)
    assert "/static/assets/" in str(excinfo.value)


def test_bundle_index_html_missing_fails(tmp_path: Path) -> None:
    static, _src = _seed_static_tree(tmp_path)
    (static / "index.html").unlink()
    with pytest.raises(BundleBrokenError):
        verify_assets_present(static_dir=static)


def test_bundle_stamp_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    static, src = _seed_static_tree(tmp_path)
    (static / "build-stamp.json").unlink()
    monkeypatch.setattr(bundle_mod, "_TOOLCHAIN_FILES", ())
    monkeypatch.setenv("OTTO_WEB_DEV", "1")
    monkeypatch.delenv("OTTO_WEB_SKIP_FRESHNESS", raising=False)
    with pytest.raises(BundleStampMissingError):
        verify_bundle_freshness(static_dir=static, src_dir=src)


def test_bundle_stamp_malformed_source_hash_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static, src = _seed_static_tree(tmp_path, source_hash="")
    monkeypatch.setattr(bundle_mod, "_TOOLCHAIN_FILES", ())
    monkeypatch.setenv("OTTO_WEB_DEV", "1")
    monkeypatch.delenv("OTTO_WEB_SKIP_FRESHNESS", raising=False)
    with pytest.raises(BundleError):
        verify_bundle_freshness(static_dir=static, src_dir=src)


def test_skip_freshness_env_var_bypasses_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    static, src = _seed_static_tree(tmp_path)
    (src / "main.tsx").write_text("// drifted\n", encoding="utf-8")
    monkeypatch.setattr(bundle_mod, "_TOOLCHAIN_FILES", ())
    monkeypatch.setenv("OTTO_WEB_DEV", "1")
    monkeypatch.setenv("OTTO_WEB_SKIP_FRESHNESS", "1")
    with caplog.at_level(logging.WARNING, logger="otto.web.bundle"):
        verify_bundle_freshness(static_dir=static, src_dir=src)
    assert any("OTTO_WEB_SKIP_FRESHNESS" in rec.message for rec in caplog.records)


def test_prod_mode_skips_hash_compare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In wheel installs, sources may be absent — never compare hashes."""
    static, src = _seed_static_tree(tmp_path)
    # Drift the source so hash compare *would* fail if it ran.
    (src / "main.tsx").write_text("// changed\n", encoding="utf-8")
    monkeypatch.setattr(bundle_mod, "_TOOLCHAIN_FILES", ())
    monkeypatch.setenv("OTTO_WEB_DEV", "0")  # prod mode
    monkeypatch.delenv("OTTO_WEB_SKIP_FRESHNESS", raising=False)
    # Should pass: only stamp existence + asset existence are required.
    verify_bundle_freshness(static_dir=static, src_dir=src)


def test_referenced_static_assets_picks_js_and_css() -> None:
    text = (
        '<script src="/static/assets/a.js"></script>'
        '<link rel=stylesheet href="/static/assets/b.css">'
        '<script src="/static/assets/a.js"></script>'  # duplicate
    )
    refs = referenced_static_assets_from_text(text)
    assert refs == ["assets/a.js", "assets/b.css"]


def referenced_static_assets_from_text(text: str) -> list[str]:
    """Tiny helper so the regex behaviour is testable without a tmp file."""
    import re
    pattern = re.compile(r'(?:src|href)\s*=\s*["\']/static/([^"\']+)["\']', re.IGNORECASE)
    return list(dict.fromkeys(pattern.findall(text)))
