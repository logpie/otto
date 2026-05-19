"""Regression: P0 deterministic scaffold profiles (plan-scaffold-profiles.md).

Root cause (3 consecutive moving-target clean-boot cascades fgate2/fwconv/
fwadhere: tsc -> ports+bare-python -> python3.14/pydantic-core): the build
agent GUESSES the env-critical scaffold from prose. The protocol fix is that
Otto OWNS a finite set of deterministic, version-pinned scaffold profiles and
seeds them instead of the agent authoring them.

This file locks the P0 *pure* contract (the loader, the guard, the rendered
templates, the hash). Pipeline wiring + missing_toolchain plumbing are
exercised by separate tests as those land.

Codex Plan Gate (thread 019e3df2..., 4 rounds, 17 findings folded) constrains
the shape these tests assert:
  - exact pins, NO carets/ranges, NO committed lockfiles (R3#1);
  - Bash-3 start.sh, FRONTEND_PORT/PORT/API_PORT, --strictPort, /api proxy,
    uv/python3.12, never bare python3 (R2#2 + toolchain);
  - greenfield guard = fs allowlist + project_kind==webapp + UI journey +
    no unsupported-stack token, unless explicit override (Codex#1/R2#4);
  - deterministic stable hash for idempotent/hydrate-first seeding (R4#1);
  - profile-managed .gitignore block ignores package-lock.json/uv.lock (R4#2).
"""

from __future__ import annotations

import json
import re

from otto.scaffold_profiles import (
    PROFILE_WEBAPP_REACT_VITE_FASTAPI_PY312,
    build_scaffold_contract,
    list_profiles,
    load_profile,
    profile_hash,
    render_seed_files,
    select_profile,
)

PID = PROFILE_WEBAPP_REACT_VITE_FASTAPI_PY312


def test_profile_registry_lists_the_webapp_profile() -> None:
    profiles = list_profiles()
    assert PID in profiles
    assert PID == "webapp.react-vite-fastapi.py312"


def test_profile_renders_the_expected_seed_files() -> None:
    files = render_seed_files(PID)
    for rel in (
        "start.sh",
        "frontend/package.json",
        "frontend/tsconfig.json",
        "frontend/tsconfig.node.json",
        "frontend/vite.config.ts",
        "backend/pyproject.toml",
        "backend/.python-version",
    ):
        assert rel in files, f"profile must seed {rel}"
        assert files[rel].strip(), f"{rel} must render non-empty"
    # profile.json / gitignore-block.txt are loader-internal, NOT seed files
    assert "profile.json" not in files
    assert "gitignore-block.txt" not in files


def test_package_json_pins_exact_no_floats_no_lockfile() -> None:
    """R3#1: exact pins, no caret/tilde/range/latest; NO package-lock.json."""
    files = render_seed_files(PID)
    pkg = json.loads(files["frontend/package.json"])
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert deps, "package.json must declare deps"
    exact = re.compile(r"^\d+\.\d+\.\d+$")
    for name, ver in deps.items():
        assert exact.match(ver), f"{name} must be an EXACT pin, got {ver!r}"
    assert "package-lock.json" not in files
    assert "frontend/package-lock.json" not in files
    # node-scoped @types/node present (kills the vite.config TS2580 class)
    assert deps.get("@types/node"), "@types/node must be pinned for vite.config"
    # build runs the project-references tsc (kills the strict-tsc class)
    assert pkg["scripts"]["build"].startswith("tsc -b")


def test_backend_pins_python_312_and_exact_deps_no_uv_lock() -> None:
    files = render_seed_files(PID)
    pyproject = files["backend/pyproject.toml"]
    assert 'requires-python = ">=3.12,<3.13"' in pyproject
    # exact-pinned deps (== ), no floating ranges in the dependency list
    dep_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "==" in dep_block
    assert "^" not in dep_block and ">=" not in dep_block.replace(
        'requires-python = ">=3.12,<3.13"', ""
    )
    assert files["backend/.python-version"].strip() == "3.12"
    assert "uv.lock" not in files and "backend/uv.lock" not in files


def test_start_sh_is_bash3_port_contract_and_uv_python312_never_bare() -> None:
    files = render_seed_files(PID)
    sh = files["start.sh"]
    # Bash-3 compatible: no `wait -n` (macOS /bin/bash is 3.2)
    assert "wait -n" not in sh
    # frontend-identifiable + otto $PORT contract (R2#2)
    assert 'FRONTEND_PORT="${FRONTEND_PORT:-5173}"' in sh
    assert 'PORT="${PORT:-$FRONTEND_PORT}"' in sh
    assert 'API_PORT="${API_PORT:-8000}"' in sh
    # Vite strict (no silent 5173->5174 drift) on $PORT
    assert "--strictPort" in sh
    assert "--port \"$PORT\"" in sh or "--port $PORT" in sh
    # uv first, python3.12 fallback, NEVER bare python3
    assert "uv sync" in sh and "uv run --python 3.12" in sh
    assert "python3.12 -m venv" in sh
    assert not re.search(r"(?<![.\d])python3\b(?!\.12)", sh), (
        "start.sh must never invoke bare python3 (the python3.14 class)"
    )


def test_vite_config_strictport_and_api_proxy_to_api_port() -> None:
    files = render_seed_files(PID)
    vite = files["frontend/vite.config.ts"]
    assert "strictPort: true" in vite
    assert "/api" in vite and "API_PORT" in vite
    assert "127.0.0.1" in vite


def test_profile_hash_is_deterministic_and_content_sensitive() -> None:
    """R4#1: hydrate-first idempotent seeding needs a stable content hash."""
    h1 = profile_hash(PID)
    h2 = profile_hash(load_profile(PID))
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64  # sha256 hex
    # content-sensitive: a mutated copy must hash differently
    prof = load_profile(PID)
    mutated = prof.__class__(
        profile_id=prof.profile_id,
        files={**prof.files, "start.sh": prof.files["start.sh"] + "\n# x"},
        port_contract=prof.port_contract,
        unsupported_stack_tokens=prof.unsupported_stack_tokens,
        gitignore_block=prof.gitignore_block,
    )
    assert profile_hash(mutated) != h1


def test_gitignore_block_ignores_generated_lockfiles() -> None:
    """R4#2: generated package-lock.json/uv.lock must be ignored so the
    verifier never flips to `npm ci` / `uv --locked` in P0."""
    prof = load_profile(PID)
    block = prof.gitignore_block
    assert "package-lock.json" in block
    assert "uv.lock" in block
    assert "node_modules" in block


def test_scaffold_contract_is_record_only_shape() -> None:
    prof = load_profile(PID)
    contract = build_scaffold_contract(prof, head_sha="deadbeef")
    assert contract["profile_id"] == PID
    assert contract["profile_hash"] == profile_hash(prof)
    assert contract["head_sha"] == "deadbeef"
    assert contract["services"]["frontend"]["env"] == "PORT"
    assert contract["services"]["backend"]["env"] == "API_PORT"
    assert sorted(contract["seeded_paths"]) == sorted(prof.files.keys())
    # round-trips as JSON (it is written to a tracked file)
    assert json.loads(json.dumps(contract)) == contract


# --- the greenfield guard (Codex#1 / R2#4) -------------------------------

_GREENFIELD = ["intent.md", "README.md", ".gitignore", "otto.yaml",
               "docs/spec.md", "otto_logs/x.log"]


def test_guard_seeds_a_greenfield_webapp_with_ui_journey() -> None:
    d = select_profile(
        intent_text="Build a React + FastAPI bookmark webapp.",
        project_kind="webapp",
        has_ui_journey=True,
        repo_relpaths=_GREENFIELD,
        scaffold_profile_override=None,
    )
    assert d.profile_id == PID
    assert "greenfield" in d.reason


def test_guard_skips_when_repo_is_not_greenfield() -> None:
    d = select_profile(
        intent_text="Build a webapp.",
        project_kind="webapp",
        has_ui_journey=True,
        repo_relpaths=[*_GREENFIELD, "src/app.py"],
        scaffold_profile_override=None,
    )
    assert d.profile_id is None
    assert "not_greenfield" in d.reason


def test_guard_skips_non_webapp_and_no_ui_journey() -> None:
    assert select_profile(
        intent_text="Build a CLI.",
        project_kind="cli",
        has_ui_journey=False,
        repo_relpaths=_GREENFIELD,
        scaffold_profile_override=None,
    ).profile_id is None
    assert select_profile(
        intent_text="Build a webapp API only.",
        project_kind="webapp",
        has_ui_journey=False,
        repo_relpaths=_GREENFIELD,
        scaffold_profile_override=None,
    ).profile_id is None


def test_guard_skips_unsupported_stack_tokens() -> None:
    for intent in (
        "Build a Next.js app with FastAPI.",
        "A Django webapp.",
        "Use Vue 3 and a Flask backend.",
    ):
        d = select_profile(
            intent_text=intent,
            project_kind="webapp",
            has_ui_journey=True,
            repo_relpaths=_GREENFIELD,
            scaffold_profile_override=None,
        )
        assert d.profile_id is None, f"{intent!r} must skip the React/FastAPI profile"
        assert "unsupported_stack" in d.reason


def test_guard_explicit_override_bypasses_heuristics_but_validates_id() -> None:
    # override wins even on a non-greenfield / non-webapp repo
    d = select_profile(
        intent_text="anything",
        project_kind="cli",
        has_ui_journey=False,
        repo_relpaths=[*_GREENFIELD, "src/main.rs"],
        scaffold_profile_override=PID,
    )
    assert d.profile_id == PID
    assert "override" in d.reason
    # unknown override id is observable, not a silent wrong-profile seed
    bad = select_profile(
        intent_text="x",
        project_kind="webapp",
        has_ui_journey=True,
        repo_relpaths=_GREENFIELD,
        scaffold_profile_override="no.such.profile",
    )
    assert bad.profile_id is None
    assert "override_unknown_profile" in bad.reason
