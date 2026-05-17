"""Tests for the capability inventory + coherence gate.

The inventory reads the scaffold (package.json, pyproject.toml, known
configs) and produces a deterministic capability description. The
coherence gate parses code-span references in CHARTER's Agent operating
notes and verifies each resolves against the scaffold.

Together they close the "policy in prose doesn't match scaffold reality"
class of bug surfaced in today's audits.
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.v5_capability_inventory import (
    build_inventory,
    check_coherence,
    inject_into_charter,
    render_inventory,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# build_inventory
# ---------------------------------------------------------------------------


def test_inventory_empty_project(tmp_path: Path) -> None:
    inv = build_inventory(tmp_path)
    assert inv.package_jsons == []
    assert inv.pyprojects == []
    assert inv.known_configs == []


def test_inventory_finds_webapp_layout(tmp_path: Path) -> None:
    _write(tmp_path / "frontend" / "package.json", json.dumps({
        "name": "frontend",
        "scripts": {"dev": "vite", "test": "vitest run"},
        "devDependencies": {"vitest": "^1", "@playwright/test": "^1"},
        "dependencies": {"react": "^18"},
    }))
    _write(tmp_path / "frontend" / "vitest.config.ts", "// ...")
    _write(tmp_path / "frontend" / "vite.config.ts", "// ...")
    _write(tmp_path / "api" / "pyproject.toml",
           '[project]\nname="api"\ndependencies=["fastapi","pytest"]\n[tool.pytest.ini_options]\n')

    inv = build_inventory(tmp_path)
    assert len(inv.package_jsons) == 1
    pkg = inv.package_jsons[0]
    assert pkg.rel_dir == "frontend"
    assert "test" in pkg.scripts
    assert "vitest" in pkg.dev_dependencies

    assert len(inv.pyprojects) == 1
    py = inv.pyprojects[0]
    assert py.rel_dir == "api"
    assert "pytest" in py.dependencies
    assert "pytest" in py.tool_tables

    # Configs picked up via filename detection
    config_names = {c[0].rsplit("/", 1)[-1] for c in inv.known_configs}
    assert "vitest.config.ts" in config_names
    assert "vite.config.ts" in config_names


def test_inventory_skips_node_modules(tmp_path: Path) -> None:
    """A package.json inside node_modules is NOT a project manifest."""
    _write(tmp_path / "package.json",
           json.dumps({"name": "root", "dependencies": {"x": "1"}}))
    _write(tmp_path / "node_modules" / "x" / "package.json",
           json.dumps({"name": "x", "scripts": {"some": "thing"}}))
    inv = build_inventory(tmp_path)
    assert len(inv.package_jsons) == 1
    assert inv.package_jsons[0].rel_dir == ""  # the root one


def test_inventory_detects_pnpm(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({"name": "x"}))
    _write(tmp_path / "pnpm-lock.yaml", "lockfileVersion: 6")
    inv = build_inventory(tmp_path)
    assert inv.package_jsons[0].package_manager == "pnpm"


def test_inventory_handles_malformed_json(tmp_path: Path) -> None:
    """A broken package.json doesn't crash the inventory."""
    _write(tmp_path / "package.json", "{not valid json")
    _write(tmp_path / "frontend" / "package.json",
           json.dumps({"scripts": {"test": "vitest"}}))
    inv = build_inventory(tmp_path)
    # The malformed one is silently skipped; the valid one is kept.
    assert len(inv.package_jsons) == 1
    assert inv.package_jsons[0].rel_dir == "frontend"


# ---------------------------------------------------------------------------
# render_inventory
# ---------------------------------------------------------------------------


def test_render_includes_markers(tmp_path: Path) -> None:
    inv = build_inventory(tmp_path)
    md = render_inventory(inv)
    assert md.startswith("<!-- OTTO-DETECTED-INFRASTRUCTURE -->")
    assert md.rstrip().endswith("<!-- END OTTO-DETECTED-INFRASTRUCTURE -->")


def test_render_includes_scripts_and_deps(tmp_path: Path) -> None:
    _write(tmp_path / "frontend" / "package.json", json.dumps({
        "scripts": {"build": "vite build", "test": "vitest run"},
        "devDependencies": {"vitest": "^1"},
        "dependencies": {"react": "^18"},
    }))
    inv = build_inventory(tmp_path)
    md = render_inventory(inv)
    assert "`build`: `vite build`" in md
    assert "`test`: `vitest run`" in md
    assert "`vitest`" in md
    assert "`react`" in md


# ---------------------------------------------------------------------------
# inject_into_charter (idempotent block management)
# ---------------------------------------------------------------------------


def test_inject_appends_when_no_block(tmp_path: Path) -> None:
    _write(tmp_path / "CHARTER.md", "# CHARTER\n\nSome text.\n")
    inv = build_inventory(tmp_path)
    md = render_inventory(inv)
    changed = inject_into_charter(tmp_path, md)
    assert changed is True
    result = (tmp_path / "CHARTER.md").read_text()
    assert "Some text." in result
    assert "<!-- OTTO-DETECTED-INFRASTRUCTURE -->" in result


def test_inject_replaces_existing_block(tmp_path: Path) -> None:
    # Pre-existing CHARTER with an outdated managed block.
    existing = (
        "# CHARTER\n\nProse.\n\n"
        "<!-- OTTO-DETECTED-INFRASTRUCTURE -->\n"
        "## Detected Infrastructure\n\nOLD CONTENT\n"
        "<!-- END OTTO-DETECTED-INFRASTRUCTURE -->\n"
    )
    _write(tmp_path / "CHARTER.md", existing)
    _write(tmp_path / "package.json",
           json.dumps({"scripts": {"build": "vite build"}}))
    inv = build_inventory(tmp_path)
    new_md = render_inventory(inv)
    changed = inject_into_charter(tmp_path, new_md)
    assert changed is True
    result = (tmp_path / "CHARTER.md").read_text()
    assert "OLD CONTENT" not in result
    assert "vite build" in result
    # Block should appear exactly once.
    assert result.count("<!-- OTTO-DETECTED-INFRASTRUCTURE -->") == 1


def test_inject_idempotent(tmp_path: Path) -> None:
    """Running inject twice produces the same CHARTER."""
    _write(tmp_path / "CHARTER.md", "# CHARTER\n\nProse.\n")
    _write(tmp_path / "package.json", json.dumps({"name": "x"}))
    inv = build_inventory(tmp_path)
    md = render_inventory(inv)
    inject_into_charter(tmp_path, md)
    snapshot = (tmp_path / "CHARTER.md").read_text()
    inject_into_charter(tmp_path, md)
    assert (tmp_path / "CHARTER.md").read_text() == snapshot


def test_inject_no_charter_returns_false(tmp_path: Path) -> None:
    """No CHARTER.md → no-op, returns False."""
    inv = build_inventory(tmp_path)
    md = render_inventory(inv)
    assert inject_into_charter(tmp_path, md) is False


# ---------------------------------------------------------------------------
# check_coherence (warning-only)
# ---------------------------------------------------------------------------


def _make_charter_with_notes(tmp_path: Path, notes: str) -> None:
    """Write a CHARTER.md with an Agent operating notes section."""
    content = (
        "# CHARTER\n\n"
        "Some preamble.\n\n"
        "## Agent operating notes\n\n"
        + notes
        + "\n\n## Stack\n\nStack section.\n"
    )
    _write(tmp_path / "CHARTER.md", content)


def test_coherence_no_charter_returns_empty(tmp_path: Path) -> None:
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_coherence_no_operating_notes_returns_empty(tmp_path: Path) -> None:
    _write(tmp_path / "CHARTER.md", "# CHARTER\n\nNo operating notes here.\n")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_coherence_warns_when_charter_prose_exceeds_line_cap(tmp_path: Path) -> None:
    ia = {
        "routes": [{"id": "home", "path": "/", "key_text": "Home"}],
        "action_surfaces": [],
        "api_endpoints": [],
        "ws_events": [],
        "data_contracts": [],
    }
    prose = "\n".join(f"- rationale line {idx}" for idx in range(501))
    _write(
        tmp_path / "CHARTER.md",
        "# CHARTER\n\n"
        "## Information Architecture Contract\n\n"
        "```json\n"
        + json.dumps(ia, indent=2)
        + "\n```\n\n"
        "## Rationale\n\n"
        + prose
        + "\n",
    )
    inv = build_inventory(tmp_path)

    findings = check_coherence(tmp_path, inv, project_kind="cli")

    assert any(f.kind == "charter_prose_over_line_cap" for f in findings)


def test_coherence_existing_paths_pass(tmp_path: Path) -> None:
    _write(tmp_path / "frontend" / "src" / "lib" / "api.ts", "// stub")
    _make_charter_with_notes(tmp_path,
        "- Shared types live in `frontend/src/lib/api.ts`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_coherence_missing_path_caught(tmp_path: Path) -> None:
    """CHARTER references `frontend/src/lib/api.ts` but no such file exists."""
    _make_charter_with_notes(tmp_path,
        "- Shared types live in `frontend/src/lib/api.ts`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert len(findings) == 1
    assert findings[0].kind == "missing_path"
    assert "frontend/src/lib/api.ts" in findings[0].reference


def test_coherence_missing_shell_script_caught(tmp_path: Path) -> None:
    """CHARTER says `bash start.sh` but start.sh isn't in the scaffold."""
    _make_charter_with_notes(tmp_path,
        "- Full stack: `bash start.sh`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert len(findings) == 1
    assert findings[0].kind == "unknown_script"
    assert "start.sh" in findings[0].detail


def test_coherence_npm_run_against_real_script(tmp_path: Path) -> None:
    """`npm run test` works when `test` is in package.json scripts."""
    _write(tmp_path / "package.json", json.dumps({
        "scripts": {"test": "vitest run"},
        "devDependencies": {"vitest": "^1"},
    }))
    _make_charter_with_notes(tmp_path,
        "- Unit tests: `npm run test`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_coherence_npm_run_against_missing_script(tmp_path: Path) -> None:
    """CHARTER says `npm run lint` but no such script."""
    _write(tmp_path / "package.json", json.dumps({
        "scripts": {"build": "vite build"},
    }))
    _make_charter_with_notes(tmp_path,
        "- Lint: `npm run lint`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert any(f.kind == "unknown_script" and "lint" in f.detail for f in findings)


def test_coherence_uv_run_against_declared_tool(tmp_path: Path) -> None:
    """`uv run pytest` works when pytest is in pyproject deps."""
    _write(tmp_path / "api" / "pyproject.toml",
           '[project]\nname="api"\ndependencies=["fastapi","pytest"]\n')
    _make_charter_with_notes(tmp_path,
        "- Tests: `cd api && uv run pytest`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_coherence_uv_run_against_missing_tool(tmp_path: Path) -> None:
    """`uv run pytest` flagged when pytest not declared."""
    _write(tmp_path / "pyproject.toml",
           '[project]\nname="x"\ndependencies=["fastapi"]\n')
    _make_charter_with_notes(tmp_path,
        "- Tests: `uv run pytest`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert any(f.kind == "unknown_script" and "pytest" in f.detail for f in findings)


def test_coherence_dedupes_repeated_refs(tmp_path: Path) -> None:
    """If the same backticked ref appears multiple times, only one finding."""
    _make_charter_with_notes(tmp_path,
        "- File at `missing/file.ts`\n- Also at `missing/file.ts`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert len(findings) == 1


def test_coherence_unknown_command_no_false_positive(tmp_path: Path) -> None:
    """Things that aren't shell commands or paths shouldn't fire."""
    _make_charter_with_notes(tmp_path,
        "- The state shape: `{ user, token }`\n"
        "- Color: `#ff8800`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    # These look neither like paths (no /, no extension) nor shell commands
    # → conservative: no findings.
    assert findings == []


# ---------------------------------------------------------------------------
# Improvements from live-run audit (requirements.txt, pytest.ini, templates)
# ---------------------------------------------------------------------------


def test_python_tool_via_requirements_txt(tmp_path: Path) -> None:
    """Python tools declared in requirements.txt count as available
    (not only pyproject.toml)."""
    _write(tmp_path / "api" / "requirements.txt",
           "fastapi>=0.110\npytest>=7.0\nuvicorn[standard]>=0.24\n")
    _make_charter_with_notes(tmp_path,
        "- Backend tests: `cd api && uv run pytest`\n"
        "- Backend dev: `cd api && uv run uvicorn main:app --reload`")
    inv = build_inventory(tmp_path)
    assert inv.python_tool_available("pytest")
    assert inv.python_tool_available("uvicorn")
    assert inv.python_tool_available("fastapi")
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_python_tool_via_pytest_ini(tmp_path: Path) -> None:
    """A pytest.ini file implies pytest is configured even without a deps
    declaration anywhere (common in older projects)."""
    _write(tmp_path / "api" / "pytest.ini", "[pytest]\ntestpaths = tests\n")
    inv = build_inventory(tmp_path)
    assert inv.python_tool_available("pytest")


def test_python_tool_not_available_when_missing(tmp_path: Path) -> None:
    """Sanity: tools genuinely missing return False."""
    _write(tmp_path / "api" / "requirements.txt", "fastapi>=0.110\n")
    inv = build_inventory(tmp_path)
    assert inv.python_tool_available("fastapi") is True
    assert inv.python_tool_available("pytest") is False


def test_coherence_skips_path_templates_angle(tmp_path: Path) -> None:
    """Path templates with ``<placeholder>`` are NOT literal paths."""
    _make_charter_with_notes(tmp_path,
        "- Avatar files: `api/uploads/avatars/<user_id>.<ext>`\n"
        "- Served at: `/uploads/avatars/<user_id>.<ext>`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_coherence_skips_path_templates_brace(tmp_path: Path) -> None:
    """``{id}``-style placeholders also skipped."""
    _make_charter_with_notes(tmp_path,
        "- User profile: `/users/{user_id}/avatar.png`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_coherence_skips_url_route_patterns(tmp_path: Path) -> None:
    """Express/FastAPI-style ``/path/:param`` URL patterns skipped."""
    _make_charter_with_notes(tmp_path,
        "- Token verify endpoint: `/auth/verify/:token`\n"
        "- Issue detail: `/issues/:id`")
    inv = build_inventory(tmp_path)
    findings = check_coherence(tmp_path, inv)
    assert findings == []


def test_inventory_parses_requirements_strips_pins(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt",
           "# header comment\n"
           "\n"
           "fastapi>=0.110\n"
           "pytest~=7.0  # inline comment\n"
           "-r dev-requirements.txt\n"
           "-e .\n"
           "uvicorn[standard]==0.24\n")
    inv = build_inventory(tmp_path)
    assert len(inv.requirements_files) == 1
    deps = inv.requirements_files[0].dependencies
    # Comments / -r / -e lines stripped; package specs preserved with pins.
    assert "fastapi>=0.110" in deps
    assert any(d.startswith("pytest~=") for d in deps)
    assert any(d.startswith("uvicorn[standard]") for d in deps)
    assert not any(d.startswith("-") for d in deps)
