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
import re
from dataclasses import dataclass, field
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
                lines.append(f"    - scripts:")
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
                lines.append(f"    - scripts:")
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


def _extract_operating_notes_block(charter_text: str) -> str | None:
    """Return the body of the 'Agent operating notes' section, or None."""
    start_m = _OPERATING_NOTES_HEADING.search(charter_text)
    if not start_m:
        return None
    body_start = start_m.end()
    next_m = _NEXT_SECTION.search(charter_text, pos=body_start)
    body_end = next_m.start() if next_m else len(charter_text)
    return charter_text[body_start:body_end]


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


def check_coherence(project_dir: Path, inv: CapabilityInventory) -> list[CoherenceFinding]:
    """Walk operating-notes code spans, verify each resolves.

    Warning-only — returns findings but doesn't block. Caller decides
    whether to surface, fail preflight, etc.
    """
    charter = project_dir / "CHARTER.md"
    findings: list[CoherenceFinding] = []
    if not charter.exists():
        return findings

    try:
        text = charter.read_text(encoding="utf-8")
    except OSError:
        return findings

    notes = _extract_operating_notes_block(text)
    if notes is None:
        return findings

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
