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
    materialize_seed,
    plan_scaffold_seed,
    profile_hash,
    read_existing_contract,
    render_seed_files,
    scaffold_contract_path,
    scaffold_surface_note,
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
        "frontend/index.html",
        "frontend/src/main.tsx",
        "frontend/src/App.tsx",
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


# --- the seed planner + materializer (R4#1 hydrate-first) ----------------


def test_plan_seed_greenfield_then_materialize_then_hydrate(tmp_path) -> None:
    proj = tmp_path
    (proj / "intent.md").write_text("Build a React + FastAPI webapp.")
    (proj / ".gitignore").write_text("otto_logs/\n")

    p1 = plan_scaffold_seed(
        project_dir=proj,
        intent_text="Build a React + FastAPI webapp.",
        project_kind="webapp",
        has_ui_journey=True,
        scaffold_profile_override=None,
        repo_relpaths=["intent.md", ".gitignore"],
    )
    assert p1.action == "seed" and p1.profile_id == PID

    mat = materialize_seed(project_dir=proj, profile_id=p1.profile_id, head_sha="abc123")
    assert (proj / "start.sh").is_file()
    assert (proj / "frontend/package.json").is_file()
    assert (proj / "backend/.python-version").read_text().strip() == "3.12"
    assert mat.gitignore_appended is True
    # generated-lockfile ignore is now in .gitignore (R4#2)
    assert "package-lock.json" in (proj / ".gitignore").read_text()
    contract = read_existing_contract(proj)
    assert contract is not None and contract["profile_id"] == PID
    assert contract["head_sha"] == "abc123"
    assert scaffold_contract_path(proj).is_file()

    # Re-plan on the now-seeded repo: hydrate (NOT reseed) even though the
    # tree is no longer greenfield (R4#1: guard only never-seeded repos).
    p2 = plan_scaffold_seed(
        project_dir=proj,
        intent_text="Build a React + FastAPI webapp.",
        project_kind="webapp",
        has_ui_journey=True,
        scaffold_profile_override=None,
        repo_relpaths=["intent.md", ".gitignore", "start.sh", "frontend/package.json"],
    )
    assert p2.action == "hydrate"
    assert p2.profile_id == PID
    assert p2.contract == contract


def test_materialize_gitignore_append_is_idempotent() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        (proj / ".gitignore").write_text("node_modules\n")
        materialize_seed(project_dir=proj, profile_id=PID)
        once = (proj / ".gitignore").read_text()
        # second materialize must NOT duplicate the managed block
        materialize_seed(project_dir=proj, profile_id=PID)
        twice = (proj / ".gitignore").read_text()
        assert once.count("otto scaffold-profile managed") == twice.count(
            "otto scaffold-profile managed"
        )


def test_plan_seed_invalid_on_hash_mismatch(tmp_path) -> None:
    scaffold_contract_path(tmp_path).write_text(
        json.dumps({"profile_id": PID, "profile_hash": "deadbeef"})
    )
    p = plan_scaffold_seed(
        project_dir=tmp_path,
        intent_text="x",
        project_kind="webapp",
        has_ui_journey=True,
        scaffold_profile_override=None,
        repo_relpaths=[],
    )
    assert p.action == "invalid"
    assert "scaffold_seed_state_invalid" in p.reason


def test_plan_seed_invalid_on_gitignore_block_without_contract(tmp_path) -> None:
    """R4#1 crash window: block committed, contract write interrupted."""
    (tmp_path / ".gitignore").write_text(
        "# >>> otto scaffold-profile managed (P0) >>>\nuv.lock\n"
    )
    p = plan_scaffold_seed(
        project_dir=tmp_path,
        intent_text="x",
        project_kind="webapp",
        has_ui_journey=True,
        scaffold_profile_override=None,
        repo_relpaths=[".gitignore"],
    )
    assert p.action == "invalid"
    assert "gitignore_without_contract" in p.reason


def test_plan_seed_skip_is_observable_not_invalid(tmp_path) -> None:
    p = plan_scaffold_seed(
        project_dir=tmp_path,
        intent_text="A CLI tool.",
        project_kind="cli",
        has_ui_journey=False,
        scaffold_profile_override=None,
        repo_relpaths=[],
    )
    assert p.action == "skip"
    assert p.reason and p.profile_id is None


def test_surface_note_marks_scaffold_authoritative() -> None:
    note = scaffold_surface_note(build_scaffold_contract(load_profile(PID)))
    assert "AUTHORITATIVE" in note
    assert "do NOT" in note
    assert PID in note
    assert "start.sh" in note


# --- the build-clean frontend entry skeleton -----------------------------
#
# Root cause (FRESH Linkboard linkboard-p0fix2-205454, 2026-05-18, terminal
# `Verdict: merge_blocked` $0 2405s — foundation-clean-boot probe e6cd82173
# fired, caught a GENUINE `npm run build` failure, bounded repair root-caused
# it but ran out of the 40min hard-budget): the P0 frontend profile was
# CONFIG-ONLY (package.json/tsconfig*/vite.config.ts) with NO `index.html`,
# NO `src/main.tsx`, NO `src/App.tsx`. So the foundation agent had to invent
# the entire Vite/React entry from scratch and reached for a
# ``React.lazy(() => import("./features/..."))`` router referencing feature
# pages that do not exist at foundation-build time -> rollup
# ``ModuleLoader.resolveDynamicImport`` cannot resolve them -> ``npm run
# build`` exit 1. A config-only frontend that cannot ``npm run build``
# standalone re-introduces the exact "agent guesses the env/build-critical
# scaffold" moving target the P0 profile exists to eliminate, one layer up.
#
# Fix (consistent-by-construction with P0's seed-the-invariant thesis, NOT a
# prose patch): the profile now also seeds a minimal build-clean
# eager-import entry skeleton. ``index.html`` (Vite entry) and
# ``src/main.tsx`` (React-18 root mount) are pure invariants; ``src/App.tsx``
# is the minimal product-owned starting point the agent EXTENDS with eager
# static imports. The seeded src must itself contain ZERO dynamic
# ``import(`` / lazy code-splitting so the foundation frontend
# ``npm run build``s clean by construction.

_FRONTEND_ENTRY = ("frontend/index.html", "frontend/src/main.tsx",
                    "frontend/src/App.tsx")


def test_profile_seeds_build_clean_frontend_entry() -> None:
    files = render_seed_files(PID)
    for rel in _FRONTEND_ENTRY:
        assert rel in files and files[rel].strip(), (
            f"profile must seed a non-empty {rel} (was config-only -> agent "
            f"guessed an unbuildable React.lazy router)"
        )
    html = files["frontend/index.html"]
    assert '<div id="root"></div>' in html or 'id="root"' in html, (
        f"index.html must mount #root; got {html!r}"
    )
    assert "/src/main.tsx" in html, (
        "index.html must load the seeded Vite entry /src/main.tsx"
    )
    main = files["frontend/src/main.tsx"]
    assert "createRoot" in main and 'from "react-dom/client"' in main, (
        "main.tsx must be the React-18 createRoot mount"
    )
    assert 'from "./App"' in main, "main.tsx must render the seeded App"
    app = files["frontend/src/App.tsx"]
    assert "export default" in app, "App.tsx must default-export the entry component"


def test_seeded_frontend_src_has_no_dynamic_imports() -> None:
    """The exact rollup ``resolveDynamicImport`` killer: the seeded frontend
    entry must contain NO dynamic ``import( )`` call, NO ``React.lazy``, and
    NO bare ``lazy(`` so the foundation frontend builds clean by
    construction. (Same discipline as the seeded start.sh: the whole file is
    scanned, so even the guidance comment must stay free of the forbidden
    tokens — only eager static ``import X from "..."`` is allowed.)"""
    files = render_seed_files(PID)
    dyn = re.compile(r"\bimport\s*\(")
    lazy = re.compile(r"\bReact\.lazy\b|\blazy\s*\(")
    for rel in ("frontend/src/main.tsx", "frontend/src/App.tsx"):
        src = files[rel]
        assert not dyn.search(src), (
            f"{rel} contains a dynamic import() — rollup cannot resolve it at "
            f"`npm run build` (the linkboard-p0fix2 terminal block)"
        )
        assert not lazy.search(src), (
            f"{rel} references React.lazy/lazy() — forbidden in the seeded "
            f"entry (caused the foundation-clean-boot rollup failure)"
        )
        # the entry must use ordinary eager static imports
        assert re.search(r'^\s*import\s+[^(]+\sfrom\s+"', src, re.M), (
            f"{rel} must use eager static `import X from \"...\"` statements"
        )


def test_surface_note_forbids_dynamic_import_and_marks_entry_authoritative() -> None:
    """Consistent-by-construction: the surfaced authoritative-files note must
    also tell the agent the frontend entry is seeded, that index.html /
    main.tsx are invariants, and that feature pages are added as EAGER
    imports — never lazily loading a not-yet-existing module (the exact
    foundation-clean-boot rollup root cause)."""
    note = scaffold_surface_note(build_scaffold_contract(load_profile(PID)))
    assert "index.html" in note and "main.tsx" in note, (
        "note must name the seeded frontend entry invariants"
    )
    assert "App.tsx" in note, "note must point the agent at the extend-point"
    low = note.lower()
    assert "eager" in low, "note must require eager static imports"
    assert "react.lazy" in low or "lazy(" in low or "dynamic import" in low, (
        "note must explicitly forbid lazy/dynamic-importing a missing module"
    )


def test_seeded_frontend_npm_build_is_clean() -> None:
    """The real generated-scaffold assertion (not doc-presence): materialize
    the profile and run the seeded ``npm run build``; it must exit 0.

    Opt-in / environment-gated so the fast offline suite stays fast: needs
    ``npm`` on PATH and ``OTTO_SCAFFOLD_BUILD_E2E=1`` (it does a real
    ``npm install`` + ``tsc -b && vite build``). The FRESH Linkboard run is
    the campaign-level e2e proof; this is the local guard."""
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    import pytest

    if not os.environ.get("OTTO_SCAFFOLD_BUILD_E2E"):
        pytest.skip("set OTTO_SCAFFOLD_BUILD_E2E=1 to run the real npm build")
    if shutil.which("npm") is None:
        pytest.skip("npm not on PATH")

    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        materialize_seed(project_dir=proj, profile_id=PID)
        fe = proj / "frontend"
        inst = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=fe, capture_output=True, text=True, timeout=600,
        )
        assert inst.returncode == 0, f"npm install failed:\n{inst.stderr[-2000:]}"
        bld = subprocess.run(
            ["npm", "run", "build"],
            cwd=fe, capture_output=True, text=True, timeout=600,
        )
        assert bld.returncode == 0, (
            f"seeded frontend `npm run build` failed (the rollup "
            f"resolveDynamicImport class must be impossible by "
            f"construction):\n{bld.stdout[-2000:]}\n{bld.stderr[-2000:]}"
        )
