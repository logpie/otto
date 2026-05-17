"""Shared path ownership matching helpers."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch


def path_matches_any_ownership_pattern(
    path: str,
    patterns: Iterable[str],
    *,
    allow_literal_prefix: bool = False,
) -> bool:
    """Return whether ``path`` matches any ownership glob/pattern.

    ``allow_literal_prefix`` preserves legacy repair-packet behavior where a
    non-glob allowed path can name a directory prefix. Glob handling is shared
    with the build write-scope checker so recursive ``**`` semantics do not
    drift across gates.
    """
    match_path = str(path or "")
    for raw in patterns:
        pattern = str(raw or "").strip()
        if not pattern:
            continue
        if allow_literal_prefix:
            normalized_path = match_path.strip("/")
            normalized_pattern = pattern.strip("/")
            if not _has_glob_magic(normalized_pattern) and (
                normalized_path == normalized_pattern
                or normalized_path.startswith(normalized_pattern.rstrip("/") + "/")
            ):
                return True
            if _path_matches_pattern(normalized_path, normalized_pattern):
                return True
            continue
        if _path_matches_pattern(match_path, pattern):
            return True
    return False


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if fnmatch(path, pattern):
        return True
    # `**` recursive globs: fnmatch does not handle them; expand to two
    # patterns "x/**/y" -> "x/*/y" + "x/y" + "x/*/*/y" up to 4 levels.
    # Pragmatic v1 - tests cover the common cases.
    if "**" in pattern:
        parts = pattern.split("**")
        # Pattern "a/**/b" matches a/b, a/*/b, a/*/*/b, etc.
        if len(parts) == 2:
            left, right = parts
            left = left.rstrip("/")
            right = right.lstrip("/")
            # ``a/**`` means descendants under ``a``, not ``a`` itself.
            # For ``a/**/b`` keep the usual zero-or-more middle segment
            # behavior so it still matches ``a/b``.
            min_depth = 1 if not right else 0
            for depth in range(min_depth, 6):
                middle = "/".join(["*"] * depth) if depth else ""
                candidate = "/".join(
                    part for part in (left, middle, right) if part
                )
                if fnmatch(path, candidate):
                    return True
    return False


def _has_glob_magic(pattern: str) -> bool:
    return any(char in pattern for char in "*?[")


__all__ = ["path_matches_any_ownership_pattern"]
