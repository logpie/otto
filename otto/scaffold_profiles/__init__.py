"""Otto-owned deterministic scaffold profiles (P0 — plan-scaffold-profiles.md).

The build agent must stop GUESSING env-critical scaffold (3 consecutive
moving-target clean-boot cascades: tsc -> ports/bare-python -> python3.14).
Otto owns a finite set of version-pinned profiles and seeds them deterministically
*before* decomposition; the agent only fills product code around them.

This module is the PURE half (loader, guard, hash, contract). It has zero otto
runtime imports so it stays trivially unit-testable. Pipeline wiring (seed
phase in v5_runner) and ``missing_toolchain`` status plumbing live elsewhere
and consume this module's outputs.

Layout per profile (``otto/scaffold_profiles/<profile_id>/``):
  profile.json          metadata: services/port contract, unsupported tokens
  gitignore-block.txt   profile-managed .gitignore block (R4#2)
  files/                the literal seed tree (relpaths preserved)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROFILE_WEBAPP_REACT_VITE_FASTAPI_PY312 = "webapp.react-vite-fastapi.py312"

_PROFILES_DIR = Path(__file__).parent

# Greenfield allowlist (Codex#1 / R2#4): a repo is "empty-ish" only if every
# tracked path is one of these (or lives under an allowed dir). Anything
# code-like means brownfield -> do NOT seed.
_ALLOWED_TOP = {
    ".gitignore",
    "intent.md",
    "otto.yaml",
    "CLAUDE.md",
}
_ALLOWED_DIRS = ("docs/", "otto_logs/", ".git/")


@dataclass(frozen=True)
class ScaffoldProfile:
    """A loaded profile: the literal seed files + its contract metadata."""

    profile_id: str
    files: dict[str, str]
    port_contract: dict[str, object]
    unsupported_stack_tokens: tuple[str, ...]
    gitignore_block: str


@dataclass(frozen=True)
class SeedDecision:
    """Guard result. ``profile_id is None`` means skip; ``reason`` is always
    set so the skip/seed choice is observable (logged, never silent)."""

    profile_id: str | None
    reason: str


def list_profiles() -> list[str]:
    """All profile ids (directories that carry a profile.json)."""
    out: list[str] = []
    for child in sorted(_PROFILES_DIR.iterdir()):
        if child.is_dir() and (child / "profile.json").is_file():
            out.append(child.name)
    return out


def _profile_dir(profile_id: str) -> Path:
    d = _PROFILES_DIR / profile_id
    if not (d.is_dir() and (d / "profile.json").is_file()):
        raise KeyError(f"unknown scaffold profile: {profile_id!r}")
    return d


def load_profile(profile_id: str) -> ScaffoldProfile:
    d = _profile_dir(profile_id)
    meta = json.loads((d / "profile.json").read_text())
    gitignore_block = ""
    gi = d / "gitignore-block.txt"
    if gi.is_file():
        gitignore_block = gi.read_text()

    files: dict[str, str] = {}
    files_root = d / "files"
    if files_root.is_dir():
        for p in sorted(files_root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(files_root).as_posix()
                files[rel] = p.read_text()

    return ScaffoldProfile(
        profile_id=profile_id,
        files=files,
        port_contract=meta.get("services", {}),
        unsupported_stack_tokens=tuple(meta.get("unsupported_stack_tokens", [])),
        gitignore_block=gitignore_block,
    )


def render_seed_files(profile_id: str) -> dict[str, str]:
    """The exact relpath->content map the seeder writes (no internal files)."""
    return dict(load_profile(profile_id).files)


def profile_hash(profile: ScaffoldProfile | str) -> str:
    """Deterministic, content-sensitive sha256 over the seed files +
    gitignore block + port contract. Drives R4#1 hydrate-first idempotency."""
    prof = load_profile(profile) if isinstance(profile, str) else profile
    h = hashlib.sha256()
    for rel in sorted(prof.files):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(prof.files[rel].encode())
        h.update(b"\0")
    h.update(prof.gitignore_block.encode())
    h.update(b"\0")
    h.update(
        json.dumps(prof.port_contract, sort_keys=True, separators=(",", ":")).encode()
    )
    return h.hexdigest()


def build_scaffold_contract(
    profile: ScaffoldProfile, *, head_sha: str | None = None
) -> dict[str, object]:
    """The record-only ``scaffold-contract.json`` payload (tracked root file,
    NOT under .otto/ — Codex#4). P0 writes it for record; the existing regex
    verifier is untouched."""
    return {
        "profile_id": profile.profile_id,
        "profile_hash": profile_hash(profile),
        "head_sha": head_sha,
        "services": dict(profile.port_contract),
        "seeded_paths": sorted(profile.files.keys()),
    }


def _is_greenfield(repo_relpaths: Iterable[str]) -> tuple[bool, str]:
    for raw in repo_relpaths:
        rel = raw.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        if not rel:
            continue
        if rel in _ALLOWED_TOP:
            continue
        if rel.upper().startswith("README"):
            continue
        if any(rel.startswith(d) for d in _ALLOWED_DIRS):
            continue
        return False, f"not_greenfield:{rel}"
    return True, "greenfield"


def _intent_has_unsupported_token(
    intent_text: str, tokens: Iterable[str]
) -> str | None:
    low = intent_text.lower()
    for tok in tokens:
        t = tok.lower()
        # boundary-aware so 'vue' does not match 'revue'
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low):
            return tok
    return None


def select_profile(
    *,
    intent_text: str,
    project_kind: str,
    has_ui_journey: bool,
    repo_relpaths: Iterable[str],
    scaffold_profile_override: str | None,
) -> SeedDecision:
    """The greenfield guard. Returns the profile to seed, or a skip with an
    observable reason. Explicit override bypasses the heuristics but is still
    validated against the registry (no silent wrong-profile seed)."""
    known = set(list_profiles())

    if scaffold_profile_override:
        if scaffold_profile_override in known:
            return SeedDecision(scaffold_profile_override, "explicit_override")
        return SeedDecision(
            None, f"override_unknown_profile:{scaffold_profile_override}"
        )

    ok, why = _is_greenfield(repo_relpaths)
    if not ok:
        return SeedDecision(None, why)

    if project_kind != "webapp":
        return SeedDecision(None, f"project_kind_not_webapp:{project_kind}")

    if not has_ui_journey:
        return SeedDecision(None, "no_ui_journey")

    pid = PROFILE_WEBAPP_REACT_VITE_FASTAPI_PY312
    if pid not in known:  # pragma: no cover - registry integrity
        return SeedDecision(None, "profile_registry_missing")
    tok = _intent_has_unsupported_token(
        intent_text, load_profile(pid).unsupported_stack_tokens
    )
    if tok:
        return SeedDecision(None, f"unsupported_stack:{tok}")

    return SeedDecision(pid, "greenfield_webapp")
