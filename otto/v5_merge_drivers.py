"""Structured merge drivers for files git can't auto-merge but humans can
trivially union.

When ``merge_branch_into`` hits a conflict on a file matched here, it calls
the registered driver with both sides' content. The driver returns either:
  - (True, merged_text): writes that, stages, conflict resolved
  - (False, None):       fall through to next layer (LLM or surface)

Drivers are intentionally conservative: prefer union/dedupe, never silently
drop fields from either side. Where two sides genuinely disagree on the same
key, prefer "ours" (parent) and let the caller decide if that's right.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("otto.v5_merge_drivers")

# Each driver: (ours_text, theirs_text, base_text_or_None) -> Optional[str]
# Returns merged text on success, None on "can't merge structurally".
MergeDriver = Callable[[str, str, Optional[str]], Optional[str]]


# ---------------------------------------------------------------------------
# package.json
# ---------------------------------------------------------------------------

def merge_package_json(ours: str, theirs: str, base: Optional[str]) -> Optional[str]:
    """Union deps + scripts. On same-key version disagreement, prefer ours."""
    try:
        o = json.loads(ours)
        t = json.loads(theirs)
    except json.JSONDecodeError:
        return None

    merged = dict(o)  # start from ours
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if key in t or key in o:
            o_deps = (o.get(key) or {})
            t_deps = (t.get(key) or {})
            unioned = dict(t_deps)
            unioned.update(o_deps)  # ours wins on overlap
            if unioned:
                merged[key] = unioned

    # scripts: union (ours wins on overlap)
    if "scripts" in t or "scripts" in o:
        o_s = (o.get("scripts") or {})
        t_s = (t.get("scripts") or {})
        scripts = dict(t_s); scripts.update(o_s)
        if scripts:
            merged["scripts"] = scripts

    # any other top-level keys from theirs that ours doesn't have
    for key in t:
        if key not in merged:
            merged[key] = t[key]

    return json.dumps(merged, indent=2) + "\n"


# ---------------------------------------------------------------------------
# requirements.txt
# ---------------------------------------------------------------------------

def merge_requirements_txt(ours: str, theirs: str, base: Optional[str]) -> Optional[str]:
    """Union lines, preserve order from ours then append theirs-only.

    Handles ``pkg==version`` lines: if both sides pin the same package to
    different versions, prefer ours.
    """
    def _pkg_name(line: str) -> str:
        line = line.split("#", 1)[0].strip()
        if not line:
            return ""
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)", line)
        return (m.group(1).lower() if m else line).split("[", 1)[0]

    ours_lines = [ln for ln in ours.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    theirs_lines = [ln for ln in theirs.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]

    seen = {_pkg_name(ln) for ln in ours_lines}
    result = list(ours_lines)
    for ln in theirs_lines:
        if _pkg_name(ln) and _pkg_name(ln) not in seen:
            result.append(ln)
            seen.add(_pkg_name(ln))
    return "\n".join(result) + "\n"


# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------

def merge_gitignore(ours: str, theirs: str, base: Optional[str]) -> Optional[str]:
    """Dedupe-union lines preserving order from ours then theirs-only."""
    ours_lines = ours.splitlines()
    seen = set(ln.strip() for ln in ours_lines)
    result = list(ours_lines)
    for ln in theirs.splitlines():
        if ln.strip() and ln.strip() not in seen:
            result.append(ln)
            seen.add(ln.strip())
    if not result or result[-1] != "":
        result.append("")
    return "\n".join(result)


# ---------------------------------------------------------------------------
# pytest.ini (INI format)
# ---------------------------------------------------------------------------

def merge_pytest_ini(ours: str, theirs: str, base: Optional[str]) -> Optional[str]:
    """Union [pytest] section keys; ours wins on overlap. Other sections
    union by section name."""
    import configparser

    def _parse(text: str) -> configparser.ConfigParser:
        cp = configparser.ConfigParser()
        try:
            cp.read_string(text)
        except configparser.Error:
            return configparser.ConfigParser()  # empty
        return cp

    o = _parse(ours)
    t = _parse(theirs)
    merged = configparser.ConfigParser()
    sections = set(o.sections()) | set(t.sections())
    for s in sections:
        merged[s] = {}
        if t.has_section(s):
            for k, v in t.items(s):
                merged[s][k] = v
        if o.has_section(s):
            for k, v in o.items(s):
                merged[s][k] = v  # ours wins on overlap
    # serialize
    from io import StringIO
    buf = StringIO()
    merged.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# tsconfig.json
# ---------------------------------------------------------------------------

def merge_tsconfig_json(ours: str, theirs: str, base: Optional[str]) -> Optional[str]:
    """Deep-merge compilerOptions; union include/exclude; ours wins on
    same-key conflicts."""
    # tsconfig.json often allows comments — try a tolerant parser path first.
    # If it fails, return None and let LLM fall through.
    def _strip_comments(s: str) -> str:
        # strip // line comments and /* ... */ block comments minimally
        s = re.sub(r"//.*", "", s)
        s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
        return s

    try:
        o = json.loads(_strip_comments(ours))
        t = json.loads(_strip_comments(theirs))
    except json.JSONDecodeError:
        return None

    def _deep_merge(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            out = dict(b); out.update(a)  # ours wins
            for k in set(a) & set(b):
                out[k] = _deep_merge(a[k], b[k])
            return out
        if isinstance(a, list) and isinstance(b, list):
            seen = set(map(str, a))
            return list(a) + [x for x in b if str(x) not in seen]
        return a  # scalar conflict → ours

    merged = _deep_merge(o, t)
    return json.dumps(merged, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Lockfiles: too complex to merge structurally; signal "regenerate"
# ---------------------------------------------------------------------------

def discard_lockfile(ours: str, theirs: str, base: Optional[str]) -> Optional[str]:
    """Return empty string sentinel that callers interpret as 'delete + regen'."""
    return ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Order matters: more specific patterns first.
def merge_decisions_log(ours: str, theirs: str, base: Optional[str]) -> Optional[str]:
    """Union-merge for the Decisions Log (decisions.md / decisions.jsonl).

    Each line is an independent entry; concurrent appends from sibling
    Leads should both land. We dedupe identical lines, preserve order:
    everything from ours, then theirs-only lines in their original order.
    """
    ours_lines = ours.splitlines()
    theirs_lines = theirs.splitlines()
    seen = set(ours_lines)
    out = list(ours_lines)
    for ln in theirs_lines:
        if ln not in seen:
            out.append(ln)
            seen.add(ln)
    return "\n".join(out) + ("\n" if not out or out[-1] != "" else "")


_DRIVERS: tuple[tuple[str, MergeDriver], ...] = (
    ("package.json", merge_package_json),
    ("pyproject.toml", lambda o, t, b: None),  # let LLM handle; toml stdlib varies
    ("pytest.ini", merge_pytest_ini),
    ("tsconfig.json", merge_tsconfig_json),
    ("tsconfig.app.json", merge_tsconfig_json),
    ("tsconfig.node.json", merge_tsconfig_json),
    ("requirements.txt", merge_requirements_txt),
    (".gitignore", merge_gitignore),
    ("decisions.md", merge_decisions_log),
    ("decisions.jsonl", merge_decisions_log),
    ("package-lock.json", discard_lockfile),
    ("yarn.lock", discard_lockfile),
    ("pnpm-lock.yaml", discard_lockfile),
    ("uv.lock", discard_lockfile),
    ("poetry.lock", discard_lockfile),
)


def find_driver(path: str) -> Optional[MergeDriver]:
    """Return a registered driver for ``path``, or None."""
    name = path.rsplit("/", 1)[-1]
    for pattern, driver in _DRIVERS:
        if name == pattern:
            return driver
    return None


def is_discard_signal(merged_content: Optional[str]) -> bool:
    """The ``discard_lockfile`` driver returns ``""`` to mean 'regenerate'.
    Callers should delete the file and let the next build step rebuild it."""
    return merged_content == ""
