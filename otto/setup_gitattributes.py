"""Phase 1.6: .gitattributes setup for queue/merge bookkeeping drivers.

Two entries are required for `otto queue` + `otto merge` to work safely:

    intent.md merge=union
    otto.yaml merge=ours

The `union` driver appends both sides on merge — exactly what intent.md (an
append-only cumulative log) wants. The `ours` driver keeps target's version
on every merge (also a built-in driver, registered via
``git config merge.ours.driver true``).

If the user's repo already has conflicting rules for these files (e.g.,
``intent.md merge=binary``), setup HARD-FAILS rather than silently letting
the merge path become nondeterministic. Opt-out via
``queue.bookkeeping_files: []`` in otto.yaml.

See plan-parallel.md §3.4 (file format), §4 (decision log), §5 Step 1.6.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

# The exact lines we want present in .gitattributes (newline-separated).
REQUIRED_RULES: list[tuple[str, str, str]] = [
    # (path_pattern, attribute, value)
    ("intent.md", "merge", "union"),
    ("otto.yaml", "merge", "ours"),
]


class GitAttributesConflict(Exception):
    """Raised when user's .gitattributes has a conflicting rule we can't reconcile."""


def _parse_existing(path: Path) -> dict[str, dict[str, str]]:
    """Parse a `.gitattributes` file into ``{pattern: {attr: value}}``.

    Lines that don't match the simple ``pattern attr=value [attr=value...]``
    shape are ignored (we only care about our rules). Comments stripped.
    """
    result: dict[str, dict[str, str]] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern, *attrs = parts
        attr_map: dict[str, str] = {}
        for tok in attrs:
            if "=" in tok:
                k, v = tok.split("=", 1)
                attr_map[k] = v
        if attr_map:
            result.setdefault(pattern, {}).update(attr_map)
    return result


def check_compatibility(
    project_dir: Path,
    *,
    required_rules: Iterable[tuple[str, str, str]] = REQUIRED_RULES,
) -> tuple[bool, list[str]]:
    """Return (ok, messages). Raises nothing — caller decides what to do.

    ok = True iff:
      - all required rules are either already present, OR
      - the file doesn't have any conflicting value for the same (pattern, attr)

    messages contains diagnostic strings (one per conflict).
    """
    path = project_dir / ".gitattributes"
    existing = _parse_existing(path)
    conflicts: list[str] = []
    for pattern, attr, value in required_rules:
        if pattern in existing and attr in existing[pattern]:
            current = existing[pattern][attr]
            if current != value:
                conflicts.append(
                    f"{path}: '{pattern} {attr}={current}' conflicts with required "
                    f"'{pattern} {attr}={value}'. Resolve manually or set "
                    f"`queue.bookkeeping_files: []` in otto.yaml to opt out."
                )
    return (len(conflicts) == 0, conflicts)


def is_setup(project_dir: Path) -> bool:
    """True iff all REQUIRED_RULES are already present in the repo's .gitattributes."""
    path = project_dir / ".gitattributes"
    if not path.exists():
        return False
    existing = _parse_existing(path)
    for pattern, attr, value in REQUIRED_RULES:
        if existing.get(pattern, {}).get(attr) != value:
            return False
    return True


def install(
    project_dir: Path,
    *,
    register_ours_driver: bool = True,
) -> bool:
    """Append the required rules to .gitattributes if not already present.

    Raises GitAttributesConflict if a conflicting rule blocks installation.
    Returns True if any change was made, False if already up to date.

    If `register_ours_driver` is True, also runs
    ``git config merge.ours.driver true`` (idempotent).
    """
    ok, conflicts = check_compatibility(project_dir)
    if not ok:
        raise GitAttributesConflict("\n".join(conflicts))

    path = project_dir / ".gitattributes"
    existing = _parse_existing(path)
    to_append: list[str] = []
    for pattern, attr, value in REQUIRED_RULES:
        if existing.get(pattern, {}).get(attr) != value:
            to_append.append(f"{pattern} {attr}={value}")

    changed = False
    if to_append:
        # Preserve trailing newline behavior
        prefix = ""
        if path.exists():
            current = path.read_text()
            if current and not current.endswith("\n"):
                prefix = "\n"
        else:
            current = ""
        path.write_text(
            current + prefix + "# otto: bookkeeping merge drivers (do not remove)\n"
            + "\n".join(to_append) + "\n"
        )
        changed = True

    if register_ours_driver:
        # Built-in driver registration. Idempotent — no-op if already set.
        subprocess.run(
            ["git", "config", "merge.ours.driver", "true"],
            cwd=project_dir,
            capture_output=True,
        )

    return changed


def assert_setup(project_dir: Path) -> None:
    """Hard-fail (raise GitAttributesConflict) if the required rules are
    missing or conflicting. Used as a precondition by `otto queue run` and
    `otto merge` (Phase 1.6 hard-fail policy).
    """
    ok, conflicts = check_compatibility(project_dir)
    if not ok:
        raise GitAttributesConflict("\n".join(conflicts))
    if not is_setup(project_dir):
        raise GitAttributesConflict(
            f"Missing required `.gitattributes` rules: "
            f"{', '.join(f'{p} {a}={v}' for p, a, v in REQUIRED_RULES)}\n"
            f"Run `otto setup` to install, or set `queue.bookkeeping_files: []` "
            f"in otto.yaml to opt out."
        )
