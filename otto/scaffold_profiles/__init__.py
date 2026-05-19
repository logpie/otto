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


# The literal first line of every profile's gitignore-block.txt. Used to
# (a) append the block idempotently and (b) detect the R4#1 crash window
# (block committed into .gitignore but the contract write/commit interrupted).
GITIGNORE_MARKER = "# >>> otto scaffold-profile managed"

SCAFFOLD_CONTRACT_FILENAME = "scaffold-contract.json"


def scaffold_contract_path(project_dir: Path) -> Path:
    """Tracked root path (NOT under .otto/ — Codex#4)."""
    return project_dir / SCAFFOLD_CONTRACT_FILENAME


def read_existing_contract(project_dir: Path) -> dict[str, object] | None:
    p = scaffold_contract_path(project_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _gitignore_has_marker(project_dir: Path) -> bool:
    gi = project_dir / ".gitignore"
    if not gi.is_file():
        return False
    try:
        return GITIGNORE_MARKER in gi.read_text()
    except OSError:
        return False


@dataclass(frozen=True)
class SeedPlan:
    """Hydrate-first decision (R4#1). ``action`` is one of:
    ``hydrate`` (valid contract already present — surface, do NOT reseed/commit),
    ``seed`` (greenfield guard passed — write+commit ``profile_id``),
    ``skip`` (guard says no — observable ``reason``, pipeline untouched),
    ``invalid`` (seed-state corruption / crash window — terminal, never silent).
    """

    action: str
    profile_id: str | None
    reason: str
    contract: dict[str, object] | None


def plan_scaffold_seed(
    *,
    project_dir: Path,
    intent_text: str,
    project_kind: str,
    has_ui_journey: bool,
    scaffold_profile_override: str | None,
    repo_relpaths: Iterable[str],
) -> SeedPlan:
    """Pure seed decision. Hydrate-first: a valid existing contract wins over
    the greenfield guard (the guard applies ONLY to never-seeded repos), and a
    contract that does not verify — or seed files committed with no/!valid
    contract — is a terminal ``invalid`` (R4#1), never a silent skip."""
    existing = read_existing_contract(project_dir)
    if existing is not None:
        pid = existing.get("profile_id")
        if not isinstance(pid, str) or pid not in set(list_profiles()):
            return SeedPlan(
                "invalid", None, "scaffold_seed_state_invalid:unknown_profile", existing
            )
        if existing.get("profile_hash") != profile_hash(pid):
            return SeedPlan(
                "invalid", None, "scaffold_seed_state_invalid:hash_mismatch", existing
            )
        return SeedPlan("hydrate", pid, "hydrate_existing_contract", existing)

    # No contract. If a prior seed committed its .gitignore block but the
    # contract write/commit was interrupted, that is the crash window — fail
    # loud, do not re-run the greenfield guard against the half-seeded tree.
    if _gitignore_has_marker(project_dir):
        return SeedPlan(
            "invalid", None, "scaffold_seed_state_invalid:gitignore_without_contract",
            None,
        )

    decision = select_profile(
        intent_text=intent_text,
        project_kind=project_kind,
        has_ui_journey=has_ui_journey,
        repo_relpaths=repo_relpaths,
        scaffold_profile_override=scaffold_profile_override,
    )
    if decision.profile_id is None:
        return SeedPlan("skip", None, decision.reason, None)
    return SeedPlan("seed", decision.profile_id, decision.reason, None)


@dataclass(frozen=True)
class SeedMaterialization:
    """Result of writing a profile to disk (git is the caller's concern)."""

    profile_id: str
    written_paths: tuple[str, ...]
    gitignore_appended: bool
    contract: dict[str, object]


def materialize_seed(
    *, project_dir: Path, profile_id: str, head_sha: str | None = None
) -> SeedMaterialization:
    """Write the profile's seed files, append the gitignore block (idempotent),
    and write the record-only scaffold-contract.json. Filesystem only — the
    caller stages/commits and enforces the branch/clean-tree invariants."""
    prof = load_profile(profile_id)
    written: list[str] = []
    for rel, content in sorted(prof.files.items()):
        dst = project_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content)
        if rel == "start.sh":
            dst.chmod(0o755)
        written.append(rel)

    appended = False
    if prof.gitignore_block.strip() and not _gitignore_has_marker(project_dir):
        gi = project_dir / ".gitignore"
        prefix = ""
        if gi.is_file():
            cur = gi.read_text()
            if cur and not cur.endswith("\n"):
                prefix = "\n"
        with gi.open("a") as fh:
            fh.write(prefix + prof.gitignore_block)
            if not prof.gitignore_block.endswith("\n"):
                fh.write("\n")
        appended = True

    contract = build_scaffold_contract(prof, head_sha=head_sha)
    scaffold_contract_path(project_dir).write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n"
    )
    return SeedMaterialization(
        profile_id=profile_id,
        written_paths=tuple(written),
        gitignore_appended=appended,
        contract=contract,
    )


def scaffold_surface_note(contract: dict[str, object]) -> str:
    """The authoritative-files note injected into the lead / foundation
    runtime context (R2#1) so the agent never rewrites the env scaffold."""
    pid = contract.get("profile_id")
    paths = contract.get("seeded_paths") or []
    paths_str = ", ".join(str(p) for p in paths) if isinstance(paths, list) else ""
    return (
        f"SEEDED SCAFFOLD (profile {pid}): Otto has already created and "
        f"committed the env-critical scaffold ({paths_str}) plus "
        f"{SCAFFOLD_CONTRACT_FILENAME}. These files are AUTHORITATIVE — do NOT "
        "rewrite, replace, or restructure start.sh, the package/build manifests, "
        "tsconfig/vite config, or the backend pyproject. Build product code "
        "AROUND them; you may add dependencies to the existing manifests."
    )


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
