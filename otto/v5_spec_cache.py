"""Exact-key cache for v5 flat spec compilation."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from otto.observability import sha256_text
from otto.observability import iso_timestamp


@dataclass(frozen=True)
class SpecCacheHit:
    key_hash: str
    cache_dir: Path
    spec_path: Path
    metadata: dict[str, Any]


def cache_key_payload(
    *,
    intent_hash: str,
    prompt_hash: str,
    provider: str,
    model: str,
    schema_version: int,
    otto_version: str,
) -> dict[str, Any]:
    return {
        "intent_hash": str(intent_hash),
        "prompt_hash": str(prompt_hash),
        "provider": str(provider),
        "model": str(model),
        "schema_version": int(schema_version),
        "otto_version": str(otto_version),
    }


def cache_key_hash(key_payload: dict[str, Any]) -> str:
    return sha256_text(json.dumps(key_payload, sort_keys=True, separators=(",", ":")))


def spec_cache_root(project_dir: Path) -> Path:
    from otto import paths as _paths
    return _paths.cross_sessions_dir(project_dir) / "spec-cache"


def lookup_spec_cache(project_dir: Path, key_payload: dict[str, Any]) -> SpecCacheHit | None:
    key_hash = cache_key_hash(key_payload)
    cache_dir = spec_cache_root(project_dir) / key_hash
    spec_path = cache_dir / "spec.json"
    metadata_path = cache_dir / "metadata.json"
    if not spec_path.exists() or not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        spec_payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or not isinstance(spec_payload, dict):
        return None
    if metadata.get("key") != key_payload:
        return None
    return SpecCacheHit(
        key_hash=key_hash,
        cache_dir=cache_dir,
        spec_path=spec_path,
        metadata=metadata,
    )


def store_spec_cache(
    *,
    project_dir: Path,
    key_payload: dict[str, Any],
    spec_path: Path,
) -> SpecCacheHit | None:
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
        spec_payload = json.loads(spec_text)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(spec_payload, dict):
        return None

    key_hash = cache_key_hash(key_payload)
    cache_dir = spec_cache_root(project_dir) / key_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_spec = cache_dir / "spec.json"
    shutil.copyfile(spec_path, cached_spec)
    metadata = {
        "schema_version": 1,
        "_written_at": iso_timestamp(),
        "key": key_payload,
        "key_hash": key_hash,
        "spec_sha256": sha256_text(spec_text),
    }
    (cache_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return SpecCacheHit(
        key_hash=key_hash,
        cache_dir=cache_dir,
        spec_path=cached_spec,
        metadata=metadata,
    )
