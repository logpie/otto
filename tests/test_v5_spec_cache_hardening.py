from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from otto import __version__ as OTTO_VERSION
from otto.observability import sha256_text
from otto.spec_compile_flat import (
    SCHEMA_VERSION,
    StructuredSpecValidationError,
    _PROMPT_TEMPLATE,
    compile_flat_spec,
)
from otto.v5_spec_cache import cache_key_payload, lookup_spec_cache, store_spec_cache


def _legacy_v2_payload(intent: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "intent": intent,
        "intent_hash": sha256_text(intent),
        "project_kind": "webapp",
        "behavior_journeys": [
            {"id": "legacy", "description": "User opens the app and sees the old flow."}
        ],
    }


def test_spec_cache_key_misses_when_prompt_or_schema_changes(tmp_path: Path) -> None:
    intent = "build an issue tracker"
    key = cache_key_payload(
        intent_hash=sha256_text(intent),
        prompt_hash="prompt-a",
        provider="claude",
        model="sonnet",
        schema_version=3,
        otto_version=OTTO_VERSION,
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_legacy_v2_payload(intent)), encoding="utf-8")

    stored = store_spec_cache(project_dir=tmp_path, key_payload=key, spec_path=spec_path)

    assert stored is not None
    assert lookup_spec_cache(tmp_path, key) is not None
    assert lookup_spec_cache(tmp_path, {**key, "prompt_hash": "prompt-b"}) is None
    assert lookup_spec_cache(tmp_path, {**key, "schema_version": 4}) is None


@pytest.mark.asyncio
async def test_compile_cache_hit_rejects_unmigratable_legacy_webapp_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = "build an issue tracker"
    provider = "claude"
    model = "claude-sonnet-test"
    key = cache_key_payload(
        intent_hash=sha256_text(intent),
        prompt_hash=sha256_text(_PROMPT_TEMPLATE.format(intent=intent)),
        provider=provider,
        model=model,
        schema_version=SCHEMA_VERSION,
        otto_version=OTTO_VERSION,
    )
    seed_spec = tmp_path / "legacy-spec.json"
    seed_spec.write_text(json.dumps(_legacy_v2_payload(intent)), encoding="utf-8")
    assert store_spec_cache(project_dir=tmp_path, key_payload=key, spec_path=seed_spec)

    monkeypatch.setattr(
        "otto.spec_compile_flat.make_agent_options",
        lambda *_args, **_kwargs: SimpleNamespace(
            max_turns=1,
            provider=provider,
            model=model,
        ),
    )

    async def fail_compile(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("compile provider should not run on exact cache hit")

    monkeypatch.setattr("otto.spec_compile_flat._run_compile", fail_compile)

    session_dir = tmp_path / "otto_logs" / "sessions" / "s-cache-hit"
    with pytest.raises(StructuredSpecValidationError, match="entry_route"):
        await compile_flat_spec(
            project_dir=tmp_path,
            session_dir=session_dir,
            intent=intent,
            config={},
        )
