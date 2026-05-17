"""Capability inventory: read scaffold, surface what's actually there.

The architect writes CHARTER prose declaring conventions. Until now,
nothing checked that the prose matched the actual scaffold — the
architect could say "leaves use Vitest" while shipping a scaffold
with only Playwright. Feature children read the (untrue) prose,
default to whatever IS in the scaffold, and waste time.

This module produces a deterministic *capability inventory* by
walking the actual scaffold:

- For each ``package.json``: scripts, declared deps + devDeps,
  declared package manager (npm/pnpm/yarn detection via lockfile),
  workspaces
- For each ``pyproject.toml``: declared dependencies, optional-deps,
  project scripts, tool tables present (pytest, ruff, mypy, etc.)
- For known config files: presence (vitest.config.*, playwright.config.*,
  tailwind.config.*, etc.) with role label
- Top-level entry points: ``start.sh``, ``main.py``, ``Makefile``

The inventory is *capability-shaped*, not *policy-shaped*: it says
"scripts X, Y exist and `vitest` is in devDependencies" rather than
"therefore leaves should use Vitest." Downstream agents and prompts
consume the inventory and decide policy from it; the inventory itself
doesn't prescribe.

Rendered into a markdown block by ``render_inventory()`` for
injection into CHARTER as the **Detected Infrastructure** managed
section.
"""

from __future__ import annotations

import json
import fnmatch
import re
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

# Configurable to bound walk cost.
_MAX_SUBSYSTEM_DEPTH = 3  # walk up to project_dir/<dir>/<subdir>/manifest
_NOISE_DIRS = frozenset({
    "node_modules", ".venv", "venv", "env", ".git", ".worktrees",
    "dist", "build", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".next", ".vite", ".turbo", "target",
})

# Config files we recognize with a brief role label.
_KNOWN_CONFIGS: dict[str, str] = {
    "vitest.config.ts": "Vitest configured",
    "vitest.config.js": "Vitest configured",
    "vitest.config.mts": "Vitest configured",
    "jest.config.ts": "Jest configured",
    "jest.config.js": "Jest configured",
    "playwright.config.ts": "Playwright configured",
    "playwright.config.js": "Playwright configured",
    "tailwind.config.ts": "Tailwind configured",
    "tailwind.config.js": "Tailwind configured",
    "postcss.config.js": "PostCSS configured",
    "postcss.config.cjs": "PostCSS configured",
    "vite.config.ts": "Vite configured",
    "vite.config.js": "Vite configured",
    "next.config.js": "Next.js configured",
    "next.config.mjs": "Next.js configured",
    "eslint.config.js": "ESLint configured",
    ".eslintrc.json": "ESLint configured (legacy)",
    "tsconfig.json": "TypeScript configured",
    "alembic.ini": "Alembic migrations configured",
    "Dockerfile": "Docker configured",
    "docker-compose.yml": "docker-compose configured",
    "docker-compose.yaml": "docker-compose configured",
    "Makefile": "Makefile present",
}

# Top-level files implying entry points.
_KNOWN_ENTRYPOINTS: dict[str, str] = {
    "start.sh": "shell start script",
    "main.py": "Python entry point at root",
    "manage.py": "Django manage entry",
    "index.ts": "TS entry at root",
    "index.js": "JS entry at root",
}


@dataclass
class PackageJsonEntry:
    """A discovered package.json."""
    path: Path  # path to the package.json file
    rel_dir: str  # directory relative to project_dir (e.g., "frontend")
    scripts: dict[str, str] = field(default_factory=dict)
    dependencies: dict[str, str] = field(default_factory=dict)
    dev_dependencies: dict[str, str] = field(default_factory=dict)
    package_manager: str = "npm"  # "npm" / "pnpm" / "yarn" / "bun"


@dataclass
class PyprojectEntry:
    """A discovered pyproject.toml."""
    path: Path
    rel_dir: str  # e.g., "api"
    dependencies: list[str] = field(default_factory=list)
    optional_deps: dict[str, list[str]] = field(default_factory=dict)
    scripts: dict[str, str] = field(default_factory=dict)
    tool_tables: list[str] = field(default_factory=list)  # ['pytest', 'ruff', ...]


@dataclass
class RequirementsTxtEntry:
    """A discovered requirements.txt (or similar)."""
    path: Path
    rel_dir: str
    dependencies: list[str] = field(default_factory=list)


@dataclass
class IniConfigEntry:
    """A discovered .ini config file (pytest.ini, setup.cfg, etc.)
    that implies a tool is configured."""
    path: Path
    rel_dir: str
    tool: str  # e.g., "pytest"


@dataclass
class CapabilityInventory:
    """All discovered capabilities, grouped."""
    package_jsons: list[PackageJsonEntry] = field(default_factory=list)
    pyprojects: list[PyprojectEntry] = field(default_factory=list)
    requirements_files: list[RequirementsTxtEntry] = field(default_factory=list)
    ini_configs: list[IniConfigEntry] = field(default_factory=list)
    known_configs: list[tuple[str, str]] = field(default_factory=list)  # (rel_path, role)
    entrypoints: list[tuple[str, str]] = field(default_factory=list)   # (rel_path, role)

    def python_tool_available(self, tool: str) -> bool:
        """True iff a Python tool (e.g., 'pytest', 'uvicorn') is reachable
        — declared in any pyproject's deps/optional-deps, listed in any
        requirements.txt, or implied by an ini config (e.g., pytest.ini)."""
        for e in self.pyprojects:
            all_deps = e.dependencies + [
                d for group in e.optional_deps.values() for d in group
            ]
            if any(self._pkg_name(d) == tool for d in all_deps):
                return True
        for r in self.requirements_files:
            if any(self._pkg_name(d) == tool for d in r.dependencies):
                return True
        for ini in self.ini_configs:
            if ini.tool == tool:
                return True
        return False

    @staticmethod
    def _pkg_name(spec: str) -> str:
        """Strip version pin / extras: 'pytest>=7.0' → 'pytest'."""
        s = spec.strip()
        for sep in ("[", ">=", "==", "~=", "<=", "<", ">", ";", " "):
            i = s.find(sep)
            if i != -1:
                s = s[:i]
        return s.strip()


def _detect_pkg_manager(pkg_dir: Path) -> str:
    """Detect the package manager based on lockfile presence."""
    if (pkg_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (pkg_dir / "yarn.lock").exists():
        return "yarn"
    if (pkg_dir / "bun.lockb").exists():
        return "bun"
    return "npm"  # default


def _safe_load_json(p: Path) -> dict[str, Any] | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _safe_load_toml(p: Path) -> dict[str, Any] | None:
    try:
        import tomllib
    except ImportError:
        return None
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, Exception):  # noqa: BLE001 — TOML parse errors vary
        return None


def _is_noise(path: Path, root: Path) -> bool:
    """Skip if any path component is a noise dir."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    return any(part in _NOISE_DIRS for part in rel.parts)


def _find_package_jsons(project_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in project_dir.rglob("package.json"):
        if _is_noise(p, project_dir):
            continue
        # Bound depth (don't walk arbitrarily deep)
        try:
            rel = p.relative_to(project_dir)
        except ValueError:
            continue
        if len(rel.parts) > _MAX_SUBSYSTEM_DEPTH:
            continue
        out.append(p)
    return sorted(out)


def _find_pyprojects(project_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in project_dir.rglob("pyproject.toml"):
        if _is_noise(p, project_dir):
            continue
        try:
            rel = p.relative_to(project_dir)
        except ValueError:
            continue
        if len(rel.parts) > _MAX_SUBSYSTEM_DEPTH:
            continue
        out.append(p)
    return sorted(out)


def _find_requirements_txt(project_dir: Path) -> list[Path]:
    """Find requirements.txt / requirements-dev.txt / etc."""
    out: list[Path] = []
    for name in ("requirements.txt", "requirements-dev.txt",
                 "requirements-test.txt", "dev-requirements.txt"):
        for p in project_dir.rglob(name):
            if _is_noise(p, project_dir):
                continue
            try:
                rel = p.relative_to(project_dir)
            except ValueError:
                continue
            if len(rel.parts) > _MAX_SUBSYSTEM_DEPTH:
                continue
            out.append(p)
    return sorted(out)


# Ini-style config files that imply a Python tool is configured.
_INI_TOOL_HINTS: dict[str, str] = {
    "pytest.ini": "pytest",
    "tox.ini": "tox",
}


def _find_ini_configs(project_dir: Path) -> list[tuple[Path, str]]:
    """Find pytest.ini / tox.ini / etc. that imply a tool is configured."""
    out: list[tuple[Path, str]] = []
    for ini_name, tool in _INI_TOOL_HINTS.items():
        for p in project_dir.rglob(ini_name):
            if _is_noise(p, project_dir):
                continue
            try:
                rel = p.relative_to(project_dir)
            except ValueError:
                continue
            if len(rel.parts) > _MAX_SUBSYSTEM_DEPTH:
                continue
            out.append((p, tool))
    return sorted(out)


def _parse_requirements_lines(text: str) -> list[str]:
    """Parse a requirements.txt-style file into dep specs.

    Strips comments, blank lines, -r references, -e installs.
    Returns the package specs (still with version pins).
    """
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Skip flags: -r, -e, -c, --extra-index-url, etc.
        if line.startswith("-"):
            continue
        # Strip inline comment.
        if "#" in line:
            line = line[:line.index("#")].strip()
        if line:
            out.append(line)
    return out


def build_inventory(project_dir: Path) -> CapabilityInventory:
    """Walk project_dir, return a structured CapabilityInventory.

    Pure read; no side effects.
    """
    inv = CapabilityInventory()

    for pkg_path in _find_package_jsons(project_dir):
        data = _safe_load_json(pkg_path)
        if not isinstance(data, dict):
            continue
        rel_dir = str(pkg_path.parent.relative_to(project_dir))
        entry = PackageJsonEntry(
            path=pkg_path,
            rel_dir=rel_dir if rel_dir != "." else "",
            scripts=dict(data.get("scripts") or {}),
            dependencies=dict(data.get("dependencies") or {}),
            dev_dependencies=dict(data.get("devDependencies") or {}),
            package_manager=_detect_pkg_manager(pkg_path.parent),
        )
        inv.package_jsons.append(entry)

    for pyp_path in _find_pyprojects(project_dir):
        data = _safe_load_toml(pyp_path)
        if not isinstance(data, dict):
            continue
        rel_dir = str(pyp_path.parent.relative_to(project_dir))
        project = (data.get("project") or {}) if isinstance(data.get("project"), dict) else {}
        deps_raw = project.get("dependencies") or []
        deps = [str(d) for d in deps_raw if isinstance(d, str)]
        opt_raw = project.get("optional-dependencies") or {}
        opt_deps = {
            str(k): [str(d) for d in (v or []) if isinstance(d, str)]
            for k, v in opt_raw.items() if isinstance(v, list)
        }
        scripts_raw = project.get("scripts") or {}
        scripts = {str(k): str(v) for k, v in scripts_raw.items() if isinstance(v, str)}
        tool_tables = []
        tool = data.get("tool") or {}
        if isinstance(tool, dict):
            tool_tables = sorted(str(k) for k in tool)
        entry_py = PyprojectEntry(
            path=pyp_path,
            rel_dir=rel_dir if rel_dir != "." else "",
            dependencies=deps,
            optional_deps=opt_deps,
            scripts=scripts,
            tool_tables=tool_tables,
        )
        inv.pyprojects.append(entry_py)

    for req_path in _find_requirements_txt(project_dir):
        try:
            text = req_path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel_dir = str(req_path.parent.relative_to(project_dir))
        inv.requirements_files.append(RequirementsTxtEntry(
            path=req_path,
            rel_dir=rel_dir if rel_dir != "." else "",
            dependencies=_parse_requirements_lines(text),
        ))

    for ini_path, tool in _find_ini_configs(project_dir):
        rel_dir = str(ini_path.parent.relative_to(project_dir))
        inv.ini_configs.append(IniConfigEntry(
            path=ini_path,
            rel_dir=rel_dir if rel_dir != "." else "",
            tool=tool,
        ))

    # Walk for known configs and entrypoints, bounded depth.
    for p in project_dir.rglob("*"):
        if _is_noise(p, project_dir):
            continue
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(project_dir)
        except ValueError:
            continue
        if len(rel.parts) > _MAX_SUBSYSTEM_DEPTH:
            continue
        if p.name in _KNOWN_CONFIGS:
            inv.known_configs.append((str(rel), _KNOWN_CONFIGS[p.name]))
        # Entry points only count at project root (depth 1).
        if len(rel.parts) == 1 and p.name in _KNOWN_ENTRYPOINTS:
            inv.entrypoints.append((str(rel), _KNOWN_ENTRYPOINTS[p.name]))

    return inv


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_BEGIN_MARKER = "<!-- OTTO-DETECTED-INFRASTRUCTURE -->"
_END_MARKER = "<!-- END OTTO-DETECTED-INFRASTRUCTURE -->"


def render_inventory(inv: CapabilityInventory) -> str:
    """Render the inventory as a markdown block.

    Wrapped in BEGIN/END markers so subsequent runs can replace it
    idempotently in CHARTER.md.
    """
    lines: list[str] = [
        _BEGIN_MARKER,
        "## Detected Infrastructure",
        "",
        "*Auto-generated by Otto. This is the actual capability of the "
        "scaffold (read from `package.json` / `pyproject.toml` / config "
        "files). Trust this for operational facts. Agent prose elsewhere "
        "in CHARTER may state intent; this section describes reality.*",
        "",
    ]

    if not (inv.package_jsons or inv.pyprojects or inv.known_configs or inv.entrypoints):
        lines.extend([
            "*(no scaffold detected yet)*",
            "",
            _END_MARKER,
        ])
        return "\n".join(lines)

    # JS / TS manifests
    if inv.package_jsons:
        lines.append("### JavaScript / TypeScript manifests")
        lines.append("")
        for e in inv.package_jsons:
            label = f"`{e.rel_dir}/package.json`" if e.rel_dir else "`package.json`"
            lines.append(f"- {label} (pkg manager: `{e.package_manager}`)")
            if e.scripts:
                lines.append("    - scripts:")
                for name, cmd in sorted(e.scripts.items()):
                    lines.append(f"        - `{name}`: `{cmd}`")
            test_related_deps = sorted({
                k for k in (list(e.dev_dependencies) + list(e.dependencies))
                if any(t in k.lower() for t in (
                    "vitest", "jest", "playwright", "testing-library",
                    "cypress", "@swc/jest", "mocha", "chai",
                ))
            })
            if test_related_deps:
                lines.append(f"    - test-related deps: {', '.join(f'`{d}`' for d in test_related_deps)}")
            # Key runtime deps worth surfacing
            framework_deps = sorted({
                k for k in (list(e.dependencies) + list(e.dev_dependencies))
                if any(t == k.lower() or t in k.lower() for t in (
                    "react", "vue", "svelte", "next", "vite", "express",
                    "fastify", "tailwindcss", "zustand", "redux",
                    "@tanstack/react-query", "react-router-dom",
                ))
            })
            if framework_deps:
                lines.append(f"    - framework deps: {', '.join(f'`{d}`' for d in framework_deps[:8])}")
        lines.append("")

    # Python manifests
    if inv.pyprojects:
        lines.append("### Python manifests")
        lines.append("")
        for e in inv.pyprojects:
            label = f"`{e.rel_dir}/pyproject.toml`" if e.rel_dir else "`pyproject.toml`"
            lines.append(f"- {label}")
            test_related = sorted({
                d.split("[")[0].split(">=")[0].split("==")[0].split("~=")[0].strip()
                for d in e.dependencies
                if any(t in d.lower() for t in ("pytest", "httpx", "respx"))
            })
            optional_test = []
            for group, deps in e.optional_deps.items():
                if any(t in (d.lower() for d in deps) for t in ("pytest",)) or "test" in group.lower():
                    optional_test.append(group)
            if test_related:
                lines.append(f"    - test-related deps: {', '.join(f'`{d}`' for d in test_related)}")
            if optional_test:
                lines.append(f"    - optional-deps groups with tests: {', '.join(f'`{g}`' for g in optional_test)}")
            if e.scripts:
                lines.append("    - scripts:")
                for name, cmd in sorted(e.scripts.items()):
                    lines.append(f"        - `{name}`: `{cmd}`")
            framework = sorted({
                d.split("[")[0].split(">=")[0].split("==")[0].split("~=")[0].strip()
                for d in e.dependencies
                if any(t in d.lower() for t in (
                    "fastapi", "django", "flask", "uvicorn", "starlette",
                    "sqlalchemy", "aiosqlite", "click", "typer",
                ))
            })
            if framework:
                lines.append(f"    - framework deps: {', '.join(f'`{d}`' for d in framework)}")
            if e.tool_tables:
                lines.append(f"    - configured tool sections: {', '.join(f'`{t}`' for t in e.tool_tables)}")
        lines.append("")

    # Known configs (vitest.config.ts, playwright.config.ts, ...)
    if inv.known_configs:
        lines.append("### Configured tools (by config file presence)")
        lines.append("")
        for rel, role in inv.known_configs:
            lines.append(f"- `{rel}` — {role}")
        lines.append("")

    # Entry points (start.sh, main.py, ...)
    if inv.entrypoints:
        lines.append("### Entry points (project root)")
        lines.append("")
        for rel, role in inv.entrypoints:
            lines.append(f"- `{rel}` — {role}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(_END_MARKER)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coherence gate (warning-only): claim-vs-reality check
# ---------------------------------------------------------------------------

# Find "## Agent operating notes" section in CHARTER.
_OPERATING_NOTES_HEADING = re.compile(
    r"^##\s+Agent operating notes\s*$",
    flags=re.MULTILINE,
)
# Find the next heading (or EOF) after the section start.
_NEXT_SECTION = re.compile(r"^##\s+\S", flags=re.MULTILINE)
# Code spans: backticked content. Multiline blocks `` `cmd` `` etc.
_CODE_SPAN = re.compile(r"`([^`\n]+?)`")

# Things that look like a file/dir path (heuristic — contains `/` or ends
# with a known extension or has a `.` in it).
_PATH_LIKE_RE = re.compile(
    r"^(?:[a-zA-Z0-9_\-./]+/[a-zA-Z0-9_\-./]+|[A-Za-z0-9_\-]+\.[a-zA-Z]+)$"
)
# Things that look like a shell command we'd want to verify.
_SHELL_CMD_PREFIXES = ("cd ", "bash ", "npm ", "npx ", "pnpm ", "yarn ", "uv ",
                       "python ", "python3 ", "pytest ", "pip ", "make ")


@dataclass
class CoherenceFinding:
    """One mismatch between operating-notes prose and scaffold reality."""
    kind: str       # "missing_path" | "unknown_script" | "no_test_script"
    reference: str  # the backticked text from the prose
    detail: str     # human-readable explanation


_IA_HEADING = re.compile(
    r"^##\s+Information Architecture Contract\s*$",
    flags=re.MULTILINE,
)
_FOUNDATION_CONTRACTS_HEADING = re.compile(
    r"^##\s+Foundation Contracts\s*$",
    flags=re.MULTILINE,
)
_FENCED_JSON = re.compile(r"```(?:json)?\s*\n(.*?)\n```", flags=re.DOTALL)
_KNOWN_SURFACE_KINDS = frozenset({
    "route",
    "nav",
    "sidebar",
    "topbar",
    "toolbar",
    "empty_state",
    "keyboard",
    "command_palette",
    "modal",
    "form",
    "button",
    "card",
    "list",
    "table",
    "settings",
    "global",
})
CHARTER_PROSE_TARGET_LINES = 300
_REGISTRATION_ISOLATION_POLICIES = frozenset({
    "file_local_auto_discovery",
    "manifest_auto_compose",
    "plugin_auto_discovery",
    "none_needed",
})
_ROUTE_REGISTRATION_TERMS = frozenset({
    "api",
    "blueprint",
    "controller",
    "endpoint",
    "navigation",
    "page",
    "route",
    "router",
    "screen",
    "url",
    "view",
})
_REGISTRY_PATH_RE = re.compile(
    r"(^|/)(api|app|controller|controllers|endpoint|endpoints|index|main|"
    r"manifest|nav|navigation|route|router|routes|url|urls|view|views)"
    r"([./_-]|$)",
    flags=re.IGNORECASE,
)
_CODE_FILE_SUFFIXES = frozenset({
    ".cjs",
    ".go",
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".ts",
    ".tsx",
})
_REGISTRY_CONTENT_ROUTE_TOKENS = (
    "route",
    "router",
    "endpoint",
    "blueprint",
    "urlpatterns",
    "<route",
    "createbrowserrouter",
    "include_router",
    "register_blueprint",
    "app.get",
    "app.post",
)
_REGISTRY_CONTENT_COMPOSE_TOKENS = (
    "register",
    "include",
    "mount",
    "compose",
    "manifest",
    "children",
    "urlpatterns",
    "routes",
    "router",
)
_MAX_REGISTRY_FILE_BYTES = 200_000


def _extract_operating_notes_block(charter_text: str) -> str | None:
    """Return the body of the 'Agent operating notes' section, or None."""
    start_m = _OPERATING_NOTES_HEADING.search(charter_text)
    if not start_m:
        return None
    body_start = start_m.end()
    next_m = _NEXT_SECTION.search(charter_text, pos=body_start)
    body_end = next_m.start() if next_m else len(charter_text)
    return charter_text[body_start:body_end]


def _extract_ia_block(charter_text: str) -> str | None:
    """Return the Information Architecture Contract section body, if present."""
    start_m = _IA_HEADING.search(charter_text)
    if not start_m:
        return None
    body_start = start_m.end()
    next_m = _NEXT_SECTION.search(charter_text, pos=body_start)
    body_end = next_m.start() if next_m else len(charter_text)
    return charter_text[body_start:body_end]


def _extract_foundation_contracts_block(charter_text: str) -> str | None:
    """Return the Foundation Contracts section body, if present."""
    start_m = _FOUNDATION_CONTRACTS_HEADING.search(charter_text)
    if not start_m:
        return None
    body_start = start_m.end()
    next_m = _NEXT_SECTION.search(charter_text, pos=body_start)
    body_end = next_m.start() if next_m else len(charter_text)
    return charter_text[body_start:body_end]


def parse_information_architecture_contract(source: str | Path) -> dict[str, Any] | None:
    """Parse the CHARTER Information Architecture Contract JSON block.

    ``source`` may be raw CHARTER markdown or a path to CHARTER.md. Returns
    ``None`` when the section or valid JSON object is absent.
    """
    if isinstance(source, Path):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            return None
    else:
        text = source
    block = _extract_ia_block(text)
    if block is None:
        return None
    fenced = _FENCED_JSON.search(block)
    json_text = fenced.group(1).strip() if fenced else block.strip()
    if not json_text:
        return None
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_charter_source(source: str | Path) -> tuple[str | None, CoherenceFinding | None]:
    if isinstance(source, Path):
        try:
            return source.read_text(encoding="utf-8"), None
        except OSError as exc:
            return None, CoherenceFinding(
                kind="foundation_contracts_charter_unreadable",
                reference=str(source),
                detail=f"could not read CHARTER.md: {exc}",
            )
    return source, None


def _foundation_contracts_payload(charter_text: str) -> Any:
    ia = parse_information_architecture_contract(charter_text)
    if isinstance(ia, dict) and "foundation_contracts" in ia:
        return ia.get("foundation_contracts")
    block = _extract_foundation_contracts_block(charter_text)
    if block is None:
        return None
    fenced = _FENCED_JSON.search(block)
    json_text = fenced.group(1).strip() if fenced else block.strip()
    if not json_text:
        return None
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError:
        return {"__invalid_json__": True}
    if isinstance(payload, dict) and "foundation_contracts" in payload:
        return payload.get("foundation_contracts")
    return payload


def parse_foundation_contracts(
    source: str | Path,
) -> tuple[list[dict[str, Any]], list[CoherenceFinding]]:
    """Parse machine-readable CHARTER foundation contracts.

    The preferred form is ``CHARTER.IA foundation_contracts``. A sibling
    ``## Foundation Contracts`` JSON block is also accepted so architects can
    keep the ownership contract visually separate while using the same parser
    module as the IA contract.
    """
    text, read_finding = _read_charter_source(source)
    if read_finding is not None:
        return [], [read_finding]
    if text is None:
        return [], []

    payload = _foundation_contracts_payload(text)
    if payload is None:
        return [], []
    if isinstance(payload, dict) and payload.get("__invalid_json__"):
        return [], [CoherenceFinding(
            kind="foundation_contracts_contract_invalid",
            reference="foundation_contracts",
            detail="Foundation Contracts section does not contain valid JSON",
        )]
    if not isinstance(payload, list):
        return [], [CoherenceFinding(
            kind="foundation_contracts_contract_invalid",
            reference="foundation_contracts",
            detail="foundation_contracts must be a list",
        )]

    ia = parse_information_architecture_contract(text) or {}
    registry_paths = _declared_shared_registry_paths(ia)
    contracts: list[dict[str, Any]] = []
    findings: list[CoherenceFinding] = []
    for index, item in enumerate(payload):
        reference = f"foundation_contracts[{index}]"
        if not isinstance(item, dict):
            findings.append(CoherenceFinding(
                kind="foundation_contracts_contract_invalid",
                reference=reference,
                detail="foundation contract entries must be objects",
            ))
            continue
        path = str(item.get("path") or "").strip().lstrip("./")
        owner_task_id = str(item.get("owner_task_id") or "").strip()
        check = str(item.get("check") or "").strip()
        if not path:
            findings.append(CoherenceFinding(
                kind="foundation_contracts_contract_invalid",
                reference=f"{reference}.path",
                detail="foundation contract entries must include path",
            ))
            continue
        if not owner_task_id:
            findings.append(CoherenceFinding(
                kind="foundation_contracts_contract_invalid",
                reference=f"{reference}.owner_task_id",
                detail="foundation contract entries must include owner_task_id",
            ))
            continue
        if check not in {"literal", "semantic"}:
            findings.append(CoherenceFinding(
                kind="foundation_contracts_contract_invalid",
                reference=f"{reference}.check",
                detail="foundation contract check must be literal or semantic",
            ))
            continue
        if check == "semantic" and path in registry_paths:
            findings.append(CoherenceFinding(
                kind="foundation_contracts_registry_semantic_rejected",
                reference=f"{reference}.check",
                detail=(
                    f"{path} is declared in registration_isolation.shared_registry_files; "
                    "route registries must use check='literal'"
                ),
            ))
            continue
        contract: dict[str, Any] = {
            "path": path,
            "owner_task_id": owner_task_id,
            "check": check,
        }
        required_exports = item.get("required_exports")
        if isinstance(required_exports, list):
            contract["required_exports"] = [
                str(value).strip() for value in required_exports if str(value).strip()
            ]
        behavior_probes = item.get("behavior_probes")
        if isinstance(behavior_probes, list):
            contract["behavior_probes"] = [
                str(value).strip() for value in behavior_probes if str(value).strip()
            ]
        contracts.append(contract)
    return contracts, findings


def persist_foundation_contracts_from_charter(
    project_dir: Path,
    *,
    parent_task_id: str,
) -> tuple[list[dict[str, Any]], list[CoherenceFinding]]:
    """Parse CHARTER foundation contracts and store them on the parent task."""
    contracts, findings = parse_foundation_contracts(project_dir / "CHARTER.md")
    if not findings:
        from otto.queue.task_graph import update_task_metadata

        update_task_metadata(project_dir, parent_task_id, foundation_contracts=contracts)
    return contracts, findings


def _feature_ownership_payload(charter_text: str) -> Any:
    ia = parse_information_architecture_contract(charter_text)
    if not isinstance(ia, dict):
        return None
    for key in (
        "feature_owned_paths",
        "feature_ownership",
        "feature_partitions",
        "owned_paths_by_feature",
    ):
        if key in ia:
            return ia.get(key)
    partition = ia.get("ownership_partition")
    if isinstance(partition, dict):
        for key in (
            "feature_owned_paths",
            "feature_ownership",
            "feature_partitions",
            "owned_paths_by_feature",
            "features",
        ):
            if key in partition:
                return partition.get(key)
    return None


# Keys under a feature entry whose value is prose/identity metadata, not
# owned paths. Everything else that resolves to a list of strings IS owned
# paths — the architect freely categorizes paths under self-invented keys
# (owned_paths / paths / may_add, or layer keys backend / frontend / tests,
# or any future grouping). Allowlisting key names is structurally fragile and
# silently drops all-but-the-first match; collect from every path-like list
# instead and only exclude known non-path metadata.
_FEATURE_META_KEYS = frozenset({
    "description",
    "rationale",
    "reason",
    "why",
    "notes",
    "note",
    "summary",
    "purpose",
    "title",
    "owner",
    "task_id",
    "id",
    "feature_task_id",
})


def _collect_path_strings(value: Any) -> list[str]:
    """All path strings reachable from a feature-entry value, regardless of
    how the architect grouped them (flat list, or dict of category->list,
    nested one level deep). Non-path metadata keys are skipped."""
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip().lstrip("./"))
            elif isinstance(item, (list, dict)):
                out.extend(_collect_path_strings(item))
    elif isinstance(value, dict):
        for k, v in value.items():
            if str(k).strip().lower() in _FEATURE_META_KEYS:
                continue
            if isinstance(v, (list, dict)):
                out.extend(_collect_path_strings(v))
    return out


def _feature_ownership_items(payload: Any) -> list[tuple[str, list[str]]]:
    if isinstance(payload, dict):
        items: list[tuple[str, list[str]]] = []
        for task_id, raw in payload.items():
            items.append((str(task_id).strip(), _collect_path_strings(raw)))
        return items
    if isinstance(payload, list):
        items = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            task_id = str(
                entry.get("task_id")
                or entry.get("id")
                or entry.get("feature_task_id")
                or ""
            ).strip()
            items.append((task_id, _collect_path_strings(entry)))
        return items
    return []


def _literal_prefix_before_glob(pattern: str) -> str:
    markers = [idx for marker in "*?[" if (idx := pattern.find(marker)) >= 0]
    if not markers:
        return pattern.rstrip("/")
    return pattern[: min(markers)].rstrip("/")


def _path_matches_leaf_extension(path: str, leaf_globs: list[str]) -> bool:
    if not leaf_globs:
        return True
    path = path.strip().lstrip("./")
    for raw_glob in leaf_globs:
        glob = raw_glob.strip().lstrip("./")
        if not glob:
            continue
        if fnmatch.fnmatch(path, glob):
            return True
        prefix = _literal_prefix_before_glob(glob)
        if prefix and (path == prefix or path.startswith(prefix.rstrip("/") + "/")):
            return True
        path_prefix = _literal_prefix_before_glob(path)
        if path_prefix and fnmatch.fnmatch(path_prefix, glob):
            return True
    return False


def parse_feature_owned_paths_from_charter(
    source: str | Path,
) -> tuple[dict[str, list[str]], list[CoherenceFinding]]:
    """Parse architect-authored feature ownership from CHARTER.IA.

    The preferred IA shape is ``feature_owned_paths`` keyed by child task_id,
    with arrays of file/glob paths. ``ownership_partition.features`` and a few
    legacy-compatible aliases are accepted so prompt wording can evolve
    without changing the runner contract.
    """
    text, read_finding = _read_charter_source(source)
    if read_finding is not None:
        return {}, [read_finding]
    if text is None:
        return {}, []
    ia = parse_information_architecture_contract(text) or {}
    payload = _feature_ownership_payload(text)
    if payload is None:
        return {}, []

    _contracts, contract_findings = parse_foundation_contracts(text)
    shared_registry_paths = _declared_shared_registry_paths(ia)

    owned_by_task: dict[str, list[str]] = {}
    findings: list[CoherenceFinding] = list(contract_findings)
    for task_id, paths in _feature_ownership_items(payload):
        if not task_id:
            findings.append(CoherenceFinding(
                kind="feature_ownership_contract_invalid",
                reference="feature_owned_paths",
                detail="feature ownership entries must include task_id",
            ))
            continue
        clean_paths = sorted(dict.fromkeys(path for path in paths if path))
        if not clean_paths:
            findings.append(CoherenceFinding(
                kind="feature_ownership_contract_invalid",
                reference=f"feature_owned_paths[{task_id}]",
                detail="feature ownership entries must include owned_paths",
            ))
            continue
        for path in clean_paths:
            if path in shared_registry_paths:
                findings.append(CoherenceFinding(
                    kind="feature_ownership_contract_invalid",
                    reference=f"feature_owned_paths[{task_id}]",
                    detail=f"{path} is a shared registry file and cannot be feature-owned",
                ))
            # NOTE: the architect derives feature_owned_paths from the scaffold
            # it actually built; a real feature legitimately owns files outside
            # the (also architect-authored, often narrower) leaf_extension_globs
            # — e.g. components/ui, lib/, store/. The ONLY isolation invariants
            # that matter here are: not a shared registry (checked above) and
            # not a foundation_contract (enforced separately by
            # `_foundation_isolation_feedback`). Do NOT hard-reject the
            # architect's self-consistent partition on a redundant glob list;
            # `_path_matches_leaf_extension` stays available for advisory use.
        owned_by_task[task_id] = clean_paths
    return owned_by_task, findings


def persist_feature_owned_paths_from_charter(
    project_dir: Path,
    *,
    parent_task_id: str,
) -> tuple[dict[str, list[str]], list[CoherenceFinding]]:
    """Persist architect-authored feature owned_paths onto sibling tasks."""
    owned_by_task, findings = parse_feature_owned_paths_from_charter(
        project_dir / "CHARTER.md"
    )
    if findings or not owned_by_task:
        return owned_by_task, findings
    from otto.queue.task_graph import read_graph, update_task_metadata

    tasks = read_graph(project_dir).get("tasks") or {}
    for task_id, owned_paths in owned_by_task.items():
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            findings.append(CoherenceFinding(
                kind="feature_ownership_contract_invalid",
                reference=f"feature_owned_paths[{task_id}]",
                detail="feature ownership references an unknown task_id",
            ))
            continue
        if str(task.get("parent_task_id") or "") != parent_task_id:
            findings.append(CoherenceFinding(
                kind="feature_ownership_contract_invalid",
                reference=f"feature_owned_paths[{task_id}]",
                detail=(
                    f"feature ownership task is not a child of {parent_task_id}"
                ),
            ))
            continue
        if str(task.get("task_role") or "feature") != "feature":
            continue
        update_task_metadata(project_dir, task_id, owned_paths=list(owned_paths))
    return owned_by_task, findings


def _registration_isolation_payload(ia: dict[str, Any]) -> dict[str, Any] | None:
    raw = ia.get("registration_isolation")
    return raw if isinstance(raw, dict) else None


def _declared_shared_registry_paths(ia: dict[str, Any] | None) -> set[str]:
    if not isinstance(ia, dict):
        return set()
    isolation = _registration_isolation_payload(ia)
    if isolation is None:
        return set()
    out: set[str] = set()
    entries = isolation.get("shared_registry_files")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if isinstance(entry, str):
            path = entry.strip()
        elif isinstance(entry, dict):
            path = str(entry.get("path") or "").strip()
        else:
            continue
        if path:
            out.add(path.lstrip("./"))
    return out


def _validate_registration_isolation_contract(
    ia: dict[str, Any],
    *,
    require: bool = False,
) -> list[CoherenceFinding]:
    isolation = _registration_isolation_payload(ia)
    if isolation is None:
        if require:
            return [CoherenceFinding(
                kind="route_registration_isolation_contract_missing",
                reference="registration_isolation",
                detail=(
                    "multi-leaf route decomposition requires CHARTER.IA "
                    "registration_isolation"
                ),
            )]
        return []

    findings: list[CoherenceFinding] = []
    policy = str(isolation.get("policy") or "").strip()
    if policy not in _REGISTRATION_ISOLATION_POLICIES:
        findings.append(CoherenceFinding(
            kind="route_registration_isolation_contract_invalid",
            reference="registration_isolation.policy",
            detail=(
                "registration_isolation.policy must be one of "
                + ", ".join(sorted(_REGISTRATION_ISOLATION_POLICIES))
            ),
        ))

    leaf_globs = isolation.get("leaf_extension_globs")
    if policy != "none_needed":
        if not isinstance(leaf_globs, list) or not any(
            isinstance(item, str) and item.strip() for item in leaf_globs
        ):
            findings.append(CoherenceFinding(
                kind="route_registration_isolation_contract_invalid",
                reference="registration_isolation.leaf_extension_globs",
                detail=(
                    "registration_isolation.leaf_extension_globs must list "
                    "where feature leaves add route modules"
                ),
            ))

    registries = isolation.get("shared_registry_files")
    if registries is not None and not isinstance(registries, list):
        findings.append(CoherenceFinding(
            kind="route_registration_isolation_contract_invalid",
            reference="registration_isolation.shared_registry_files",
            detail="registration_isolation.shared_registry_files must be a list",
        ))
    elif isinstance(registries, list):
        for index, entry in enumerate(registries):
            if isinstance(entry, str):
                if entry.strip():
                    continue
                path = ""
                leaf_edit = False
            elif isinstance(entry, dict):
                path = str(entry.get("path") or "").strip()
                leaf_edit = entry.get("leaf_edit") is True
            else:
                path = ""
                leaf_edit = False
            if not path:
                findings.append(CoherenceFinding(
                    kind="route_registration_isolation_contract_invalid",
                    reference=f"registration_isolation.shared_registry_files[{index}].path",
                    detail="shared registry entries must include a path",
                ))
            if leaf_edit:
                findings.append(CoherenceFinding(
                    kind="route_registration_isolation_contract_invalid",
                    reference=f"registration_isolation.shared_registry_files[{index}].leaf_edit",
                    detail="shared registry files must set leaf_edit false",
                ))

    return findings


def _charter_line_counts(charter_text: str) -> dict[str, int]:
    """Count CHARTER total, prose, and IA JSON lines.

    The v6 cap is aimed at prose duplication, not contract data. The IA
    section remains the source of truth even when it is large.
    """
    total = len(charter_text.splitlines())
    ia_json_lines = 0
    block = _extract_ia_block(charter_text)
    if block is not None:
        fenced = _FENCED_JSON.search(block)
        ia_json_lines = len((fenced.group(0) if fenced else block).splitlines())
    return {
        "total": total,
        "ia_json": ia_json_lines,
        "prose": max(total - ia_json_lines, 0),
    }


def _charter_prose_line_count(charter_text: str) -> int:
    """Count CHARTER lines excluding the IA JSON contract body."""
    return _charter_line_counts(charter_text)["prose"]


def _latest_spec_payload(project_dir: Path) -> dict[str, Any] | None:
    """Best-effort lookup for the root v5 spec when callers do not pass one."""
    sessions = project_dir / "otto_logs" / "sessions"
    if not sessions.exists():
        return None
    candidates = sorted(
        sessions.glob("*/spec/spec.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _coerce_spec(spec: Any) -> dict[str, Any]:
    """Return a JSON-shaped flat spec payload for IA coherence checks."""
    if isinstance(spec, dict):
        return dict(spec)
    if is_dataclass(spec) and not isinstance(spec, type):
        payload = asdict(spec)
        return payload if isinstance(payload, dict) else {}
    return {}


def _spec_project_kind(spec: dict[str, Any]) -> str | None:
    value = spec.get("project_kind")
    return str(value) if value else None


def _spec_primary_action_ids(spec: dict[str, Any]) -> set[str]:
    entities = spec.get("core_entities")
    out: set[str] = set()
    if not isinstance(entities, list):
        return out
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        for action in entity.get("primary_actions") or []:
            if isinstance(action, dict) and action.get("id"):
                out.add(str(action["id"]))
    return out


def _spec_product_page_ids(spec: dict[str, Any]) -> set[str]:
    product_overview = spec.get("product_overview")
    if not isinstance(product_overview, dict):
        return set()
    out: set[str] = set()
    for page in product_overview.get("top_level_pages") or []:
        if isinstance(page, dict) and page.get("id"):
            out.add(str(page["id"]))
    return out


def _spec_sidebar_page_ids(spec: dict[str, Any]) -> set[str]:
    product_overview = spec.get("product_overview")
    if not isinstance(product_overview, dict):
        return set()
    navigation = product_overview.get("primary_navigation")
    if not isinstance(navigation, dict):
        return set()
    return {
        str(page_id)
        for page_id in navigation.get("sidebar") or []
        if str(page_id).strip()
    }


def _known_ia_surface_ids(ia: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for collection in ("routes", "nav_surfaces", "action_surfaces", "settings_sections"):
        for item in ia.get(collection) or []:
            if isinstance(item, dict) and item.get("id"):
                out.add(str(item["id"]))
    for item in ia.get("empty_states") or []:
        if not isinstance(item, dict):
            continue
        entity = str(item.get("entity") or "")
        route = str(item.get("list_route") or "")
        if entity:
            out.add(f"{entity}.empty_state")
        if route:
            out.add(f"{route}.empty_state")
    return out


def _surface_reference_valid(surface: str, known_surface_ids: set[str]) -> bool:
    if not surface:
        return False
    if surface in known_surface_ids or surface in _KNOWN_SURFACE_KINDS:
        return True
    parts = [part for part in surface.split(".") if part]
    if not parts:
        return False
    return parts[0] in _KNOWN_SURFACE_KINDS or parts[-1] in _KNOWN_SURFACE_KINDS


def validate_information_architecture_contract(
    project_dir: Path,
    *,
    spec: Any | None = None,
    project_kind: str | None = None,
) -> list[CoherenceFinding]:
    """Validate CHARTER.md's machine-readable IA contract.

    Missing IA is a webapp coherence failure. Other project kinds skip the
    requirement unless an IA block is present.
    """
    charter = project_dir / "CHARTER.md"
    if spec is None:
        spec = _latest_spec_payload(project_dir)
    spec_payload = _coerce_spec(spec)
    kind = project_kind or _spec_project_kind(spec_payload)

    try:
        charter_text = charter.read_text(encoding="utf-8")
    except OSError:
        if kind == "webapp":
            return [CoherenceFinding(
                kind="missing_ia_contract",
                reference="CHARTER.md",
                detail="webapp coherence requires CHARTER.md with an Information Architecture Contract",
            )]
        return []

    block = _extract_ia_block(charter_text)
    if block is None:
        if kind == "webapp":
            return [CoherenceFinding(
                kind="missing_ia_contract",
                reference="## Information Architecture Contract",
                detail="webapp CHARTER.md is missing the Information Architecture Contract section",
            )]
        return []

    ia = parse_information_architecture_contract(charter_text)
    if ia is None:
        return [CoherenceFinding(
            kind="invalid_ia_contract",
            reference="## Information Architecture Contract",
            detail="Information Architecture Contract section does not contain a valid JSON object",
        )]

    findings: list[CoherenceFinding] = []
    route_ids = {
        str(route.get("id"))
        for route in ia.get("routes") or []
        if isinstance(route, dict) and route.get("id")
    }
    nav_linked_route_ids: set[str] = set()
    for surface in ia.get("nav_surfaces") or []:
        if not isinstance(surface, dict):
            continue
        linked_routes = surface.get("must_link_routes")
        if not isinstance(linked_routes, list):
            continue
        nav_linked_route_ids.update(str(route_id) for route_id in linked_routes if str(route_id).strip())
    known_surfaces = _known_ia_surface_ids(ia)

    for route_id in sorted(nav_linked_route_ids):
        if route_id not in route_ids:
            findings.append(CoherenceFinding(
                kind="ia_unknown_nav_route",
                reference=route_id,
                detail=f"nav_surfaces[].must_link_routes references {route_id!r}, not in routes[].id",
            ))

    for page_id in sorted(_spec_product_page_ids(spec_payload)):
        if page_id not in route_ids:
            findings.append(CoherenceFinding(
                kind="ia_missing_product_page_route",
                reference=page_id,
                detail=(
                    f"product_overview.top_level_pages page {page_id!r} has no matching "
                    "CHARTER.IA routes[].id"
                ),
            ))

    for page_id in sorted(_spec_sidebar_page_ids(spec_payload)):
        if page_id not in nav_linked_route_ids:
            findings.append(CoherenceFinding(
                kind="ia_missing_sidebar_nav_link",
                reference=page_id,
                detail=(
                    f"product_overview.primary_navigation.sidebar page {page_id!r} is not linked "
                    "from CHARTER.IA nav_surfaces[].must_link_routes"
                ),
            ))

    for action in ia.get("action_surfaces") or []:
        if not isinstance(action, dict):
            continue
        action_id = str(action.get("id") or "<missing-action-id>")
        target = str(action.get("target_route") or "")
        if target and target not in route_ids:
            findings.append(CoherenceFinding(
                kind="ia_unknown_target_route",
                reference=action_id,
                detail=f"action_surfaces[{action_id}].target_route {target!r} is not in routes[].id",
            ))
        surfaces = action.get("surfaces")
        if not isinstance(surfaces, list):
            findings.append(CoherenceFinding(
                kind="ia_invalid_surface",
                reference=action_id,
                detail=f"action_surfaces[{action_id}].surfaces must be a list",
            ))
            continue
        for surface in surfaces:
            ref = str(surface)
            if not _surface_reference_valid(ref, known_surfaces):
                findings.append(CoherenceFinding(
                    kind="ia_unknown_surface",
                    reference=ref,
                    detail=(
                        f"action_surfaces[{action_id}] references unknown surface {ref!r}; "
                        "use a known surface kind or declared surface id"
                    ),
                ))

    action_surface_ids = {
        str(action.get("id"))
        for action in ia.get("action_surfaces") or []
        if isinstance(action, dict) and action.get("id")
    }
    for action_id in sorted(_spec_primary_action_ids(spec_payload)):
        if action_id not in action_surface_ids:
            findings.append(CoherenceFinding(
                kind="ia_missing_action_surface",
                reference=action_id,
                detail=(
                    f"spec primary action {action_id!r} has no matching "
                    "CHARTER.IA action_surfaces[].id"
                ),
            ))

    findings.extend(_validate_registration_isolation_contract(ia, require=False))
    _contracts, foundation_findings = parse_foundation_contracts(charter_text)
    findings.extend(foundation_findings)
    return findings


def _text_has_route_registration_term(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(term)}s?\b", lowered) for term in _ROUTE_REGISTRATION_TERMS)


def _task_is_architect(task: dict[str, Any]) -> bool:
    return str(task.get("intent") or "").lstrip().lower().startswith("architect")


def _task_is_route_registration_leaf(task: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(task.get("intent") or ""),
            " ".join(str(item) for item in (task.get("owned_paths") or [])),
            " ".join(str(item) for item in (task.get("action_ids") or [])),
        ]
    )
    return _text_has_route_registration_term(text)


def _is_noise_file(path: Path) -> bool:
    return any(part in _NOISE_DIRS for part in path.parts)


def _owned_pattern_files(project_dir: Path, pattern: str) -> set[str]:
    pattern = pattern.strip().strip("`").lstrip("./")
    if not pattern or "<" in pattern or "{" in pattern:
        return set()
    out: set[str] = set()
    has_glob = any(ch in pattern for ch in "*?[")
    if has_glob:
        try:
            matches = list(project_dir.glob(pattern))
        except ValueError:
            matches = []
        for match in matches:
            if match.is_file() and not _is_noise_file(match):
                out.add(match.relative_to(project_dir).as_posix())
        return out

    candidate = project_dir / pattern
    if candidate.is_file() and not _is_noise_file(candidate):
        return {candidate.relative_to(project_dir).as_posix()}
    if candidate.is_dir() and not _is_noise_file(candidate):
        for match in candidate.rglob("*"):
            if match.is_file() and not _is_noise_file(match):
                out.add(match.relative_to(project_dir).as_posix())
                if len(out) >= 500:
                    break
        return out
    if Path(pattern).suffix:
        return {pattern}
    return set()


def _owned_file_map(
    project_dir: Path,
    leaves: dict[str, dict[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, dict[str, list[str]]]]:
    by_file: dict[str, set[str]] = {}
    patterns_by_file: dict[str, dict[str, list[str]]] = {}
    for task_id, task in leaves.items():
        for raw_pattern in task.get("owned_paths") or []:
            pattern = str(raw_pattern).strip()
            if not pattern:
                continue
            for rel_path in _owned_pattern_files(project_dir, pattern):
                by_file.setdefault(rel_path, set()).add(task_id)
                patterns_by_file.setdefault(rel_path, {}).setdefault(task_id, []).append(pattern)
    return by_file, patterns_by_file


def _read_registry_candidate_text(project_dir: Path, rel_path: str) -> str:
    path = project_dir / rel_path
    try:
        if not path.is_file() or path.stat().st_size > _MAX_REGISTRY_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _registry_candidate_evidence(
    project_dir: Path,
    rel_path: str,
    *,
    declared_registry_paths: set[str],
    route_like_leaf_count: int,
) -> list[str]:
    evidence: list[str] = []
    if rel_path in declared_registry_paths:
        evidence.append("declared in CHARTER.IA registration_isolation.shared_registry_files")
    if _REGISTRY_PATH_RE.search(rel_path):
        evidence.append("path has route/registry naming")
    text = _read_registry_candidate_text(project_dir, rel_path).lower()
    if text:
        has_route_token = any(token in text for token in _REGISTRY_CONTENT_ROUTE_TOKENS)
        has_compose_token = any(token in text for token in _REGISTRY_CONTENT_COMPOSE_TOKENS)
        if has_route_token and has_compose_token:
            evidence.append("content has route registration/composition tokens")
    if (
        not evidence
        and route_like_leaf_count >= 2
        and Path(rel_path).suffix.lower() in _CODE_FILE_SUFFIXES
    ):
        evidence.append("multiple route-like leaves own the same code file")
    return evidence


def check_route_registration_isolation(
    project_dir: Path,
    *,
    graph: dict[str, Any],
    architect_task_id: str,
    parent_task_id: str | None = None,
) -> dict[str, Any] | None:
    """Return a structured blocker when route registration is not isolated.

    This is intentionally stack-generic: it looks at the architect's CHARTER IA
    contract plus the planned leaf owned-path sets. The failure mode is a
    multi-leaf route decomposition where feature leaves would edit the same
    registry/composition file.
    """
    tasks_raw = graph.get("tasks") if isinstance(graph, dict) else None
    if not isinstance(tasks_raw, dict):
        return None
    architect_task = tasks_raw.get(architect_task_id)
    if not isinstance(architect_task, dict):
        return None
    parent_id = parent_task_id or str(architect_task.get("parent_task_id") or "")
    if not parent_id:
        return None

    route_leaves: dict[str, dict[str, Any]] = {}
    for task_id, task in tasks_raw.items():
        if task_id == architect_task_id or not isinstance(task, dict):
            continue
        if str(task.get("parent_task_id") or "") != parent_id:
            continue
        if _task_is_architect(task) or not _task_is_route_registration_leaf(task):
            continue
        route_leaves[str(task_id)] = task
    if len(route_leaves) < 2:
        return None

    ia = parse_information_architecture_contract(project_dir / "CHARTER.md") or {}
    contract_findings = _validate_registration_isolation_contract(ia, require=True)
    declared_registry_paths = _declared_shared_registry_paths(ia)
    by_file, patterns_by_file = _owned_file_map(project_dir, route_leaves)
    shared_files: list[dict[str, Any]] = []
    for rel_path, task_ids in sorted(by_file.items()):
        if len(task_ids) < 2:
            continue
        evidence = _registry_candidate_evidence(
            project_dir,
            rel_path,
            declared_registry_paths=declared_registry_paths,
            route_like_leaf_count=len(route_leaves),
        )
        if not evidence:
            continue
        shared_files.append({
            "path": rel_path,
            "task_ids": sorted(task_ids),
            "owned_patterns": {
                task_id: patterns_by_file.get(rel_path, {}).get(task_id, [])
                for task_id in sorted(task_ids)
            },
            "evidence": evidence,
        })

    if not shared_files and not contract_findings:
        return None

    contract_payload = [
        {"kind": f.kind, "reference": f.reference, "detail": f.detail}
        for f in contract_findings
    ]
    if shared_files:
        message = (
            "architect route registration is not isolated: multiple route leaves "
            "own the same registry/composition file"
        )
    else:
        message = (
            "architect route registration isolation contract is missing or invalid "
            "for a multi-leaf route decomposition"
        )
    return {
        "kind": "shared_registry_not_isolated",
        "step_id": "architect_route_registration_isolation",
        "message": message,
        "architect_task_id": architect_task_id,
        "parent_task_id": parent_id,
        "leaf_task_ids": sorted(route_leaves),
        "shared_files": shared_files,
        "contract_findings": contract_payload,
        "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _looks_like_path(ref: str, inv: CapabilityInventory) -> bool:
    """Heuristic: is this code span referring to a filesystem path?

    Skips path templates with placeholders (``<user_id>``, ``:id``, ``{id}``)
    — those describe naming schemes, not literal paths.
    """
    # Path templates with placeholders aren't checkable literals.
    if "<" in ref and ">" in ref:
        return False
    if "{" in ref and "}" in ref:
        return False
    # Express-style route params (``:slug``) inside what looks like a URL/path:
    # only skip when paired with a leading ``/`` (URL pattern, not a colon in a label).
    if ref.startswith("/") and ":" in ref:
        # e.g., "/users/:id/profile"
        return False
    if "/" in ref and not ref.startswith("--") and not ref.startswith("@"):
        return True
    # Things like `vite.config.ts`, `start.sh`, `Makefile`
    if "." in ref and not ref.startswith(_SHELL_CMD_PREFIXES):
        # extension-y patterns
        base = ref.rsplit("/", 1)[-1]
        if base in _KNOWN_CONFIGS or base in _KNOWN_ENTRYPOINTS:
            return True
        # e.g., `pyproject.toml`, `package.json`
        if base in {"pyproject.toml", "package.json", "tsconfig.json"}:
            return True
    return False


def _path_exists(project_dir: Path, ref: str) -> bool:
    """Check filesystem for the referenced path."""
    # Strip any leading `./` and trailing modifiers like `(from project root)`
    ref = ref.strip()
    if ref.startswith("./"):
        ref = ref[2:]
    p = project_dir / ref
    return p.exists()


def _looks_like_shell_cmd(ref: str) -> bool:
    return any(ref.startswith(prefix) for prefix in _SHELL_CMD_PREFIXES)


def _command_resolves(
    project_dir: Path, ref: str, inv: CapabilityInventory
) -> tuple[bool, str | None]:
    """Try to verify a shell command's primary tool is available.

    Returns (resolves, detail). For ``cd api && uv run pytest``: extract
    `pytest`; check if `pytest` is in any pyproject's deps OR optional-deps.
    For ``npm run X``: check if script `X` exists in any package.json. Etc.

    Conservative: if we can't tell, return (True, None) — don't fire a
    false-positive warning.
    """
    # Strip leading `cd <path> && `
    work = ref
    while work.startswith("cd "):
        idx = work.find("&&")
        if idx == -1:
            break
        work = work[idx + 2:].strip()

    # npm run <script>
    m = re.match(r"^(?:npm|pnpm|yarn|bun)\s+run\s+([a-zA-Z0-9:_\-]+)", work)
    if m:
        script_name = m.group(1)
        for e in inv.package_jsons:
            if script_name in e.scripts:
                return True, None
        return False, f"no `{script_name}` script in any package.json"

    # npx <bin> or `cd ... && npx X`
    m = re.match(r"^npx\s+(--\S+\s+)?([a-zA-Z0-9@/_\-]+)", work)
    if m:
        bin_name = m.group(2)
        # Heuristic: assume npx will resolve if bin appears in any devDep
        # (or its prefix matches).
        for e in inv.package_jsons:
            for dep in {**e.dev_dependencies, **e.dependencies}:
                if dep == bin_name or dep.endswith(f"/{bin_name}") or bin_name in dep:
                    return True, None
        return False, f"npx target `{bin_name}` not in any package.json deps"

    # uv run pytest / pip run / etc.
    m = re.match(r"^uv\s+run\s+([a-zA-Z0-9_\-]+)", work)
    if m:
        tool = m.group(1)
        # Check pyproject deps + requirements.txt + ini-implied tools.
        if inv.python_tool_available(tool):
            return True, None
        return False, (
            f"Python tool `{tool}` not declared in any pyproject, "
            f"requirements.txt, or implied by an ini config"
        )

    # `bash start.sh` — just check the script exists.
    m = re.match(r"^bash\s+([a-zA-Z0-9_/.\-]+)", work)
    if m:
        target = m.group(1)
        return (project_dir / target).exists(), (
            None if (project_dir / target).exists()
            else f"shell script `{target}` doesn't exist"
        )

    # Don't know what this is — don't false-positive.
    return True, None


def check_coherence(
    project_dir: Path,
    inv: CapabilityInventory,
    *,
    spec: Any | None = None,
    project_kind: str | None = None,
) -> list[CoherenceFinding]:
    """Walk operating-notes code spans, verify each resolves.

    Also validates the machine-readable CHARTER IA contract when a webapp
    spec is available. Warning-only at this layer — returns findings but
    doesn't block. Caller decides whether to surface, fail preflight, etc.
    """
    spec_payload = _coerce_spec(spec) if spec is not None else None
    charter = project_dir / "CHARTER.md"
    findings: list[CoherenceFinding] = []
    if not charter.exists():
        findings.extend(validate_information_architecture_contract(
            project_dir, spec=spec_payload, project_kind=project_kind
        ))
        return findings

    try:
        text = charter.read_text(encoding="utf-8")
    except OSError:
        findings.extend(validate_information_architecture_contract(
            project_dir, spec=spec_payload, project_kind=project_kind
        ))
        return findings

    line_counts = _charter_line_counts(text)
    prose_lines = line_counts["prose"]
    if prose_lines > CHARTER_PROSE_TARGET_LINES:
        findings.append(CoherenceFinding(
            kind="charter_prose_over_line_cap",
            reference="CHARTER.md",
            detail=(
                f"CHARTER has {line_counts['total']} total lines: "
                f"{prose_lines} prose lines and {line_counts['ia_json']} IA JSON/fence "
                f"lines. Prose target is <= {CHARTER_PROSE_TARGET_LINES} lines; "
                "IA JSON is uncapped."
            ),
        ))

    notes = _extract_operating_notes_block(text)
    if notes is not None:
        # Track seen to dedupe.
        seen: set[str] = set()
        for span_m in _CODE_SPAN.finditer(notes):
            ref = span_m.group(1).strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)

            if _looks_like_shell_cmd(ref):
                resolves, detail = _command_resolves(project_dir, ref, inv)
                if not resolves:
                    findings.append(CoherenceFinding(
                        kind="unknown_script",
                        reference=ref,
                        detail=detail or "shell command target not found in scaffold",
                    ))
            elif _looks_like_path(ref, inv):
                if not _path_exists(project_dir, ref):
                    findings.append(CoherenceFinding(
                        kind="missing_path",
                        reference=ref,
                        detail=(
                            f"operating notes reference `{ref}` but no such "
                            f"file/dir exists in scaffold"
                        ),
                    ))

    findings.extend(validate_information_architecture_contract(
        project_dir, spec=spec_payload, project_kind=project_kind
    ))

    return findings


def inject_into_charter(project_dir: Path, inventory_md: str) -> bool:
    """Append or replace the Detected Infrastructure block in CHARTER.md.

    Idempotent — running twice on the same project yields the same file.
    Returns True if CHARTER.md was modified, False if no CHARTER exists
    or the block was already current.
    """
    charter = project_dir / "CHARTER.md"
    if not charter.exists():
        return False
    try:
        text = charter.read_text(encoding="utf-8")
    except OSError:
        return False

    # Find existing block (if any).
    pattern = re.compile(
        re.escape(_BEGIN_MARKER) + r".*?" + re.escape(_END_MARKER),
        flags=re.DOTALL,
    )

    if pattern.search(text):
        new_text = pattern.sub(inventory_md.rstrip(), text)
    else:
        # Append at end, with a separating blank line.
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        new_text = text + sep + inventory_md.rstrip() + "\n"

    if new_text == text:
        return False
    try:
        charter.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True
