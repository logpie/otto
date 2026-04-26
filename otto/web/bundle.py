"""Bundle integrity checks for the Mission Control SPA.

Why this module exists
----------------------
``otto web`` serves the pre-built React bundle from ``otto/web/static/``. The
sources live in ``otto/web/client/src/`` and are compiled with Vite. There are
two failure modes we have to defend against in dev/editable installs:

1. **Stale bundle** — a developer edits TSX/CSS sources and forgets to run
   ``npm run web:build``. FastAPI silently keeps serving the old hashed JS,
   so the change "doesn't appear" and the cause is non-obvious.
2. **Broken bundle** — ``otto/web/static/index.html`` references hashed
   asset paths that no longer exist (lost in a bad checkout, missing from a
   wheel, partial build). The shell HTML still loads, the SPA never boots,
   and the user sees a blank page with a console 404.

This module exposes two pure functions used at FastAPI startup:

* :func:`compute_source_hash` — deterministic SHA-256 over every file under
  ``otto/web/client/src/`` plus the package manifests that pin the build
  toolchain (``package.json``, ``otto/web/client/vite.config.ts``,
  ``otto/web/client/tsconfig.json``). Anything that changes the bundle output
  is hashed; anything that does not is skipped (lockfile excluded — it is
  noisy and not part of the source set).
* :func:`verify_bundle_freshness` — raises :class:`BundleStaleError` when the
  hash drifts in dev mode, or :class:`BundleBrokenError` when ``index.html``
  references a missing asset, or returns ``None`` on success.

The Vite build writes ``otto/web/static/build-stamp.json`` via
``scripts/build_stamp.py``; that file carries the source hash and metadata so
the runtime check has something to compare against.

Environment overrides
---------------------
* ``OTTO_WEB_DEV=1`` — force dev mode (default when no stamp ever existed,
  inferred from the install layout otherwise).
* ``OTTO_WEB_DEV=0`` — force prod mode (skip hash compare, only check the
  stamp file is present).
* ``OTTO_WEB_SKIP_FRESHNESS=1`` — bypass the freshness check entirely; logs
  a loud warning so the bypass is not silent. Intended for fast inner-loop
  iteration where the developer accepts staleness.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final


logger = logging.getLogger(__name__)


# Package layout ----------------------------------------------------------
WEB_PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
STATIC_DIR: Final[Path] = WEB_PACKAGE_DIR / "static"
CLIENT_DIR: Final[Path] = WEB_PACKAGE_DIR / "client"
CLIENT_SRC_DIR: Final[Path] = CLIENT_DIR / "src"
BUILD_STAMP_PATH: Final[Path] = STATIC_DIR / "build-stamp.json"

# Files outside ``client/src`` that still influence the bundle output.
# Keeping the set small and explicit makes the hash predictable and the
# failure messages short. We deliberately exclude ``package-lock.json`` —
# it churns on every npm install and is not consumed at build time.
REPO_ROOT: Final[Path] = WEB_PACKAGE_DIR.parent.parent
_TOOLCHAIN_FILES: Final[tuple[Path, ...]] = (
    REPO_ROOT / "package.json",
    CLIENT_DIR / "vite.config.ts",
    CLIENT_DIR / "tsconfig.json",
    CLIENT_DIR / "index.html",
)


# Errors ------------------------------------------------------------------
class BundleError(RuntimeError):
    """Base class for all bundle-integrity failures."""


class BundleStaleError(BundleError):
    """Source files changed since the bundle was built."""


class BundleBrokenError(BundleError):
    """``index.html`` references an asset that doesn't exist on disk."""


class BundleStampMissingError(BundleError):
    """``build-stamp.json`` is absent — bundle was never built."""


# Source hash -------------------------------------------------------------
@dataclass(frozen=True)
class _HashedFile:
    rel: str
    digest: str


def _iter_source_files(src_dir: Path) -> list[Path]:
    """Return every file under ``src_dir`` in deterministic order."""
    if not src_dir.is_dir():
        return []
    return sorted(p for p in src_dir.rglob("*") if p.is_file())


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_source_hash(
    *,
    src_dir: Path = CLIENT_SRC_DIR,
    toolchain_files: tuple[Path, ...] = _TOOLCHAIN_FILES,
) -> str:
    """Compute a deterministic hash of all bundle inputs.

    The hash covers:
    * Every file under ``client/src/`` (recursive).
    * A small fixed set of toolchain files that change the output.

    Missing toolchain files are hashed as the empty string so the absence is
    still observable in the digest.
    """
    overall = hashlib.sha256()
    files: list[_HashedFile] = []
    for path in _iter_source_files(src_dir):
        rel = path.relative_to(src_dir).as_posix()
        files.append(_HashedFile(rel=f"src/{rel}", digest=_hash_file(path)))
    for path in toolchain_files:
        rel = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else path.name
        digest = _hash_file(path) if path.is_file() else hashlib.sha256(b"").hexdigest()
        files.append(_HashedFile(rel=rel, digest=digest))
    for entry in sorted(files, key=lambda f: f.rel):
        overall.update(entry.rel.encode("utf-8"))
        overall.update(b"\0")
        overall.update(entry.digest.encode("ascii"))
        overall.update(b"\n")
    return overall.hexdigest()


# Stamp I/O ---------------------------------------------------------------
def read_build_stamp(stamp_path: Path = BUILD_STAMP_PATH) -> dict[str, object]:
    if not stamp_path.is_file():
        raise BundleStampMissingError(
            f"Web bundle stamp is missing: {stamp_path}. "
            "Run `npm run web:build` and restart `otto web`."
        )
    try:
        return json.loads(stamp_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"Web bundle stamp is malformed JSON: {stamp_path} ({exc})") from exc


# Asset existence ---------------------------------------------------------
_ASSET_REF_RE: Final[re.Pattern[str]] = re.compile(
    r'(?:src|href)\s*=\s*["\']/static/([^"\']+)["\']',
    re.IGNORECASE,
)


def referenced_static_assets(index_html: Path) -> list[str]:
    """Return every ``/static/...`` path referenced from ``index.html``."""
    if not index_html.is_file():
        return []
    text = index_html.read_text(encoding="utf-8", errors="replace")
    return list(dict.fromkeys(_ASSET_REF_RE.findall(text)))


def verify_assets_present(static_dir: Path = STATIC_DIR) -> None:
    """Raise :class:`BundleBrokenError` if a referenced asset is missing."""
    index_html = static_dir / "index.html"
    if not index_html.is_file():
        raise BundleBrokenError(
            f"Web bundle is broken: {index_html} doesn't exist. "
            "Run `npm run web:build` and restart `otto web`."
        )
    missing: list[str] = []
    for rel in referenced_static_assets(index_html):
        if not (static_dir / rel).is_file():
            missing.append(rel)
    if missing:
        joined = ", ".join(f"/static/{p}" for p in missing)
        raise BundleBrokenError(
            f"Web bundle is broken: index.html references {joined} which doesn't exist. "
            "Run `npm run web:build` and restart `otto web`."
        )


# Mode detection ----------------------------------------------------------
def _is_dev_mode() -> bool:
    """Return True when we should compare hashes (sources are present)."""
    override = os.environ.get("OTTO_WEB_DEV")
    if override == "1":
        return True
    if override == "0":
        return False
    # Auto-detect: if the source tree exists alongside the package, we're in
    # an editable / source-checkout install; if not (e.g., installed from a
    # wheel), we're in prod mode.
    return CLIENT_SRC_DIR.is_dir()


# Top-level entrypoint ---------------------------------------------------
def verify_bundle_freshness(
    *,
    static_dir: Path = STATIC_DIR,
    src_dir: Path = CLIENT_SRC_DIR,
) -> None:
    """Raise on stale or broken bundle; return ``None`` on success.

    The two checks run in this order:

    1. **Asset existence** — independent of dev/prod mode. A broken bundle is
       always fatal because the SPA cannot boot.
    2. **Freshness** — compare the on-disk source hash to the stamp's
       ``source_hash``. Skipped in prod mode (the developer cannot edit
       sources inside a wheel) and when ``OTTO_WEB_SKIP_FRESHNESS=1``.
    """
    verify_assets_present(static_dir)

    skip_freshness = os.environ.get("OTTO_WEB_SKIP_FRESHNESS") == "1"
    if skip_freshness:
        logger.warning(
            "OTTO_WEB_SKIP_FRESHNESS=1 is set; skipping web bundle freshness check. "
            "The browser may load a stale UI."
        )
        # Still surface a missing stamp because that signals a never-built tree
        # rather than a deliberate stale bundle.
        try:
            read_build_stamp(static_dir / "build-stamp.json")
        except BundleStampMissingError as exc:
            logger.warning("%s", exc)
        return

    stamp = read_build_stamp(static_dir / "build-stamp.json")

    if not _is_dev_mode():
        # Prod / wheel install: nothing to compare against because sources
        # aren't shipped. Stamp existence (checked above) is the only proof.
        return

    expected_hash = stamp.get("source_hash")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise BundleError(
            "Web bundle stamp is missing 'source_hash'. "
            "Run `npm run web:build` and restart `otto web`."
        )
    actual_hash = compute_source_hash(src_dir=src_dir)
    if actual_hash != expected_hash:
        raise BundleStaleError(
            "Web bundle is stale: source has changed since the bundle was built "
            f"(stamp={expected_hash[:12]}, current={actual_hash[:12]}). "
            "Run `npm run web:build` and restart `otto web`. "
            "(set OTTO_WEB_SKIP_FRESHNESS=1 to bypass during fast iteration)"
        )
