"""Deterministic edit-scope construction for consolidated conflict resolution.

The conflict agent keeps full tool access, so the orchestrator computes a
strict allowlist before the call and validates against it afterward.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from otto.merge import git_ops

# Conservative by design: N8-class repairs need 1-2 adjacent files, not a
# package-wide rewrite. If the scope expands past this, fail closed.
MAX_SECONDARY_FILES = 12


class EditScopeError(RuntimeError):
    """Raised when the orchestrator cannot build a safe bounded edit scope."""


@dataclass(frozen=True)
class EditScope:
    primary_files: set[str]
    secondary_files: set[str]
    branch_touch_union: set[str]

    @property
    def allowed_files(self) -> set[str]:
        return set(self.primary_files) | set(self.secondary_files)


def collect_branch_touch_union(
    project_dir: Path,
    *,
    target: str,
    branches: list[str],
) -> set[str]:
    """Collect the union of files touched by participating branches."""
    touched: set[str] = set()
    for branch in branches:
        for rel in git_ops.files_in_branch_diff(project_dir, branch, target):
            if _is_repo_local_path(rel):
                touched.add(rel)
    return touched


def build_edit_scope(
    *,
    project_dir: Path,
    conflict_files: set[str],
    branch_touch_union: set[str],
) -> EditScope:
    """Build the deterministic edit scope for the conflict agent.

    secondary_files = branch_touch_union ∩
        (same-package-or-directory ∪ direct-import-neighbors ∪ same-test-package)
    """
    primary_files = {rel for rel in conflict_files if _is_existing_repo_file(project_dir, rel)}
    touched_existing = {
        rel for rel in branch_touch_union
        if _is_existing_repo_file(project_dir, rel)
    }

    structural_neighbors = (
        _same_directory_neighbors(primary_files, touched_existing)
        | _direct_import_neighbors(project_dir, primary_files, touched_existing)
        | _same_test_package_neighbors(primary_files, touched_existing)
    )
    secondary_files = (touched_existing & structural_neighbors) - primary_files

    if len(secondary_files) > MAX_SECONDARY_FILES:
        raise EditScopeError(
            "secondary edit scope too broad "
            f"({len(secondary_files)} files > {MAX_SECONDARY_FILES}): "
            f"{sorted(secondary_files)!r}"
        )

    return EditScope(
        primary_files=primary_files,
        secondary_files=secondary_files,
        branch_touch_union=touched_existing,
    )


def _same_directory_neighbors(primary_files: set[str], candidates: set[str]) -> set[str]:
    parent_dirs = {str(Path(rel).parent) for rel in primary_files}
    return {
        rel for rel in candidates
        if str(Path(rel).parent) in parent_dirs
    }


def _same_test_package_neighbors(primary_files: set[str], candidates: set[str]) -> set[str]:
    test_dirs = {
        str(Path(rel).parent)
        for rel in primary_files
        if rel.startswith("tests/")
    }
    return {
        rel for rel in candidates
        if rel.startswith("tests/") and str(Path(rel).parent) in test_dirs
    }


def _direct_import_neighbors(
    project_dir: Path,
    primary_files: set[str],
    candidates: set[str],
) -> set[str]:
    python_files = _tracked_python_files(project_dir)
    module_index = {
        module_name: rel_path
        for rel_path in python_files
        for module_name in [_module_name_from_path(rel_path)]
        if module_name
    }

    relevant_python = {
        rel for rel in (primary_files | candidates)
        if rel.endswith(".py") and rel in python_files
    }
    imports_by_file = {
        rel: _imported_repo_local_modules(project_dir, rel, module_index)
        for rel in relevant_python
    }

    primary_modules = {
        _module_name_from_path(rel)
        for rel in primary_files
        if rel.endswith(".py")
    } - {None}
    primary_paths = {module_index[module] for module in primary_modules if module in module_index}

    outgoing = {
        module_index[module]
        for rel in primary_paths
        for module in imports_by_file.get(rel, set())
        if module in module_index
    }
    incoming = {
        rel
        for rel, imported in imports_by_file.items()
        if rel in candidates and primary_modules.intersection(imported)
    }
    return outgoing | incoming


def _tracked_python_files(project_dir: Path) -> set[str]:
    result = git_ops.run_git(project_dir, "ls-files", "--", "*.py")
    if not result.ok:
        return set()
    return {
        rel for rel in result.stdout.splitlines()
        if _is_existing_repo_file(project_dir, rel)
    }


def _imported_repo_local_modules(
    project_dir: Path,
    rel_path: str,
    module_index: dict[str, str],
) -> set[str]:
    path = project_dir / rel_path
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()

    current_module = _module_name_from_path(rel_path)
    current_package = _package_name_from_module(current_module, rel_path)
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in module_index:
                    imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import_base(current_package, node.level, node.module)
            if not base:
                continue
            if base in module_index:
                imported.add(base)
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if candidate in module_index:
                    imported.add(candidate)
    return imported


def _resolve_import_base(current_package: str, level: int, module: str | None) -> str | None:
    if level <= 0:
        return module
    package_parts = [part for part in current_package.split(".") if part]
    upward_hops = level - 1
    if upward_hops > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - upward_hops]
    if module:
        base_parts.extend(part for part in module.split(".") if part)
    return ".".join(base_parts) or None


def _module_name_from_path(rel_path: str) -> str | None:
    path = Path(rel_path)
    if path.suffix != ".py":
        return None
    if path.name == "__init__.py":
        return ".".join(path.with_suffix("").parts[:-1]) or None
    return ".".join(path.with_suffix("").parts)


def _package_name_from_module(module_name: str | None, rel_path: str) -> str:
    if not module_name:
        return ""
    if Path(rel_path).name == "__init__.py":
        return module_name
    return module_name.rsplit(".", 1)[0] if "." in module_name else ""


def _is_existing_repo_file(project_dir: Path, rel_path: str) -> bool:
    return _is_repo_local_path(rel_path) and (project_dir / rel_path).is_file()


def _is_repo_local_path(rel_path: str) -> bool:
    path = Path(rel_path)
    return not path.is_absolute() and ".." not in path.parts
