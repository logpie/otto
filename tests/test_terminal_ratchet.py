"""Ratchet guard (v5 one-hard-gate keystone, 2026-05-19).

Every `merge_blocked`/`catastrophic` literal in v5_runner.py must be one of:
  (a) inside a chokepoint function (legit HONEST_TERMINAL branch), or
  (b) within 4 lines of `resolve_terminal_outcome` / `# ALLOWED-TERMINAL:`,
      i.e. consciously routed through the chokepoint, or
  (c) a known deferred site in terminal_debt_baseline.json.

New unmanaged terminal literals (not in the baseline) FAIL the test —
the invariant is enforced for all new code. The baseline is the honest,
greppable deferred-tail registry; it only shrinks (Task #5 converts the
Linkboard-path sites; the Codex-led tail clears the rest). Regenerate
intentionally with OTTO_REGEN_TERMINAL_BASELINE=1 (must shrink, never grow).
"""
import ast
import json
import os
import pathlib

RUNNER = pathlib.Path("otto/v5_runner.py")
BASELINE = pathlib.Path("tests/terminal_debt_baseline.json")
TERMINAL = {"merge_blocked", "catastrophic"}
CHOKEPOINT_FUNCS = {
    "resolve_terminal_outcome",
    "_record_task_merge_blocked_reason",
    "_record_structured_merge_failed",
    "_cause_from_origin",
}


def _enclosing_func(tree: ast.AST, lineno: int) -> str:
    best = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= (end or node.lineno):
                best = node.name
    return best


def _unmanaged_sites() -> dict[str, int]:
    src = RUNNER.read_text()
    tree = ast.parse(src)
    lines = src.splitlines()
    counts: dict[str, int] = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, ast.Constant) and c.value in TERMINAL:
                ln = getattr(n, "lineno", c.lineno)
                func = _enclosing_func(tree, ln)
                if func in CHOKEPOINT_FUNCS:
                    continue
                ctx = "\n".join(lines[max(0, ln - 5):ln])
                if ("resolve_terminal_outcome" in ctx
                        or "ALLOWED-TERMINAL:" in ctx):
                    continue
                key = f"{func}:{c.value}"
                counts[key] = counts.get(key, 0) + 1
    return counts


def test_no_new_unmanaged_terminal_literals():
    current = _unmanaged_sites()
    if os.environ.get("OTTO_REGEN_TERMINAL_BASELINE") == "1":
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    baseline = json.loads(BASELINE.read_text()) if BASELINE.is_file() else {}
    # No NEW keys, and no existing key may grow (ratchet: only shrink).
    new_keys = sorted(set(current) - set(baseline))
    grew = sorted(
        k for k in current if current[k] > baseline.get(k, 0)
    )
    assert not new_keys, (
        f"new unmanaged terminal literal(s) — route through the chokepoint "
        f"or add `# ALLOWED-TERMINAL:`: {new_keys}"
    )
    assert not grew, f"deferred-debt site grew (must only shrink): {grew}"
