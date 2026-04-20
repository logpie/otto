"""Small synthetic conflict to measure F13 scaling vs P6's 41 regions / 18min.

Sets up a minimal single-file 2-branch conflict with ~5-8 conflict regions,
runs F13 (consolidated agent-mode) salvage, measures wall + cost.

If F13 takes ~3 min, scaling is roughly proportional (~30s/region).
If F13 takes ~10 min, there's an overhead floor that doesn't scale down.

Usage: .venv/bin/python scripts/bench_small_conflict.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"


# Base: a small calculator with 4 functions
BASE_PY = '''"""Tiny calculator."""

def add(a, b):
    """Return a + b."""
    return a + b


def sub(a, b):
    """Return a - b."""
    return a - b


def mul(a, b):
    """Return a * b."""
    return a * b


def div(a, b):
    """Return a / b. Raises ZeroDivisionError if b is 0."""
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 4:
        print("usage: calc.py <op> <a> <b>")
        sys.exit(1)
    op, a, b = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    fn = {"add": add, "sub": sub, "mul": mul, "div": div}.get(op)
    if not fn:
        print(f"unknown op: {op}")
        sys.exit(1)
    print(fn(a, b))
'''


# Branch A: adds power + mod, plus changes div's docstring + makes it int when possible
BRANCH_A_PY = '''"""Tiny calculator with power and mod."""

def add(a, b):
    """Return a + b (commutative)."""
    return a + b


def sub(a, b):
    """Return a - b."""
    return a - b


def mul(a, b):
    """Return a * b."""
    return a * b


def div(a, b):
    """Return a / b as int if both inputs are ints, else float. Raises ZeroDivisionError."""
    if b == 0:
        raise ZeroDivisionError("division by zero")
    result = a / b
    if isinstance(a, int) and isinstance(b, int) and result == int(result):
        return int(result)
    return result


def power(a, b):
    """Return a ** b."""
    return a ** b


def mod(a, b):
    """Return a % b. Raises ZeroDivisionError if b is 0."""
    if b == 0:
        raise ZeroDivisionError("mod by zero")
    return a % b


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 4:
        print("usage: calc.py <op> <a> <b>")
        sys.exit(1)
    op, a, b = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    fn = {"add": add, "sub": sub, "mul": mul, "div": div, "power": power, "mod": mod}.get(op)
    if not fn:
        print(f"unknown op: {op}")
        sys.exit(1)
    print(fn(a, b))
'''


# Branch B: adds sqrt + abs, makes div return float always (different signature change)
BRANCH_B_PY = '''"""Tiny calculator with sqrt and abs."""

def add(a, b):
    """Return a + b (associative)."""
    return a + b


def sub(a, b):
    """Return a - b."""
    return a - b


def mul(a, b):
    """Return a * b."""
    return a * b


def div(a, b):
    """Return a / b as float. Raises ZeroDivisionError if b is 0."""
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return float(a) / float(b)


def sqrt(a):
    """Return the square root of a. Raises ValueError for negatives."""
    if a < 0:
        raise ValueError("sqrt of negative")
    return a ** 0.5


def abs_val(a):
    """Return |a|."""
    return -a if a < 0 else a


if __name__ == "__main__":
    import sys
    if len(sys.argv) not in (3, 4):
        print("usage: calc.py <op> <a> [b]")
        sys.exit(1)
    op = sys.argv[1]
    a = float(sys.argv[2])
    fn_unary = {"sqrt": sqrt, "abs_val": abs_val}.get(op)
    if fn_unary:
        print(fn_unary(a))
        sys.exit(0)
    if len(sys.argv) != 4:
        print("usage: calc.py <op> <a> <b>")
        sys.exit(1)
    b = float(sys.argv[3])
    fn = {"add": add, "sub": sub, "mul": mul, "div": div}.get(op)
    if not fn:
        print(f"unknown op: {op}")
        sys.exit(1)
    print(fn(a, b))
'''


def setup_repo() -> Path:
    base = Path(tempfile.mkdtemp(prefix="bench-small-"))
    print(f"  setup at {base}")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "s@s"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "S"], cwd=base, check=True)

    (base / "calc.py").write_text(BASE_PY)
    (base / "README.md").write_text("# calc\n\npython calc.py add 1 2\n")
    # Standard Python .gitignore so agent's test runs don't trigger
    # the orchestrator's "untracked files" validator.
    (base / ".gitignore").write_text("__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\n")
    subprocess.run(["git", "add", "."], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=base, check=True)

    # Branch A
    subprocess.run(["git", "checkout", "-q", "-b", "feat/power-mod"], cwd=base, check=True)
    (base / "calc.py").write_text(BRANCH_A_PY)
    subprocess.run(["git", "add", "."], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: power + mod"], cwd=base, check=True)

    # Branch B
    subprocess.run(["git", "checkout", "-q", "main"], cwd=base, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/sqrt-abs"], cwd=base, check=True)
    (base / "calc.py").write_text(BRANCH_B_PY)
    subprocess.run(["git", "add", "."], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: sqrt + abs"], cwd=base, check=True)

    subprocess.run(["git", "checkout", "-q", "main"], cwd=base, check=True)

    # Set merge_mode: consolidated
    (base / "otto.yaml").write_text("default_branch: main\n\nqueue:\n  merge_mode: consolidated\n")
    subprocess.run(["git", "add", "otto.yaml"], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "configure consolidated merge"], cwd=base, check=True)

    return base


def count_conflict_regions(repo: Path, branches: list[str]) -> int:
    """Apply merges in test mode to count regions, then abort."""
    import shutil as _sh
    test_dir = Path(tempfile.mkdtemp(prefix="count-"))
    try:
        _sh.copytree(repo, test_dir / "r", symlinks=True)
        r = test_dir / "r"
        for b in branches:
            result = subprocess.run(
                ["git", "merge", "--no-ff", b, "-m", f"merge {b}"],
                cwd=r, capture_output=True,
            )
            if result.returncode != 0:
                # Stage markers + commit, continue
                subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
                subprocess.run(["git", "commit", "-q", "-m", f"markers from {b}", "-i", "--allow-empty"], cwd=r, capture_output=True)
        # Count markers
        total = 0
        for f in (r / "calc.py", r / "README.md"):
            if f.exists():
                total += sum(1 for line in f.read_text().splitlines() if line.startswith("<<<<<<<"))
        return total
    finally:
        _sh.rmtree(test_dir, ignore_errors=True)


def main() -> int:
    print("Building small synthetic conflict scenario...")
    repo = setup_repo()
    branches = ["feat/power-mod", "feat/sqrt-abs"]

    # Count expected regions
    n_regions = count_conflict_regions(repo, branches)
    print(f"  expected conflict regions across all merges: ~{n_regions}")

    # Run consolidated merge
    print(f"\nRunning F13 consolidated merge on {len(branches)} branches...")
    t0 = time.time()
    result = subprocess.run(
        [str(OTTO_BIN), "merge", *branches, "--no-certify"],
        cwd=repo, capture_output=True, text=True, timeout=3600,
        env={**os.environ},
    )
    wall = time.time() - t0
    out = (result.stdout or "") + (result.stderr or "")
    print(f"  done in {wall:.1f}s, rc={result.returncode}")

    # Parse cost
    cost = 0.0
    for sf in (repo / "otto_logs" / "merge").glob("merge-*/state.json"):
        try:
            d = json.loads(sf.read_text())
            for o in d.get("outcomes", []):
                note = o.get("note") or ""
                if "cost $" in note:
                    bit = note.split("cost $")[1].split(",")[0].split(")")[0]
                    cost += float(bit)
        except Exception:
            pass

    # Tool counts from log
    log = repo / "otto_logs" / "merge" / "conflict-agent-agentic.log"
    tool_counts = {}
    if log.exists():
        for line in log.read_text().splitlines():
            m = re.match(r"\[\s*\d+\.\d+s\]\s*●\s*(\S+)", line)
            if m:
                tool_counts[m.group(1)] = tool_counts.get(m.group(1), 0) + 1

    # Verify result
    final_calc = (repo / "calc.py").read_text() if (repo / "calc.py").exists() else ""
    has_markers = "<<<<<<<" in final_calc

    res = {
        "name": "F13-small-synthetic",
        "scenario": "calc.py + README.md, 2 branches each adding 2 functions and changing existing ones",
        "n_branches": len(branches),
        "expected_conflict_regions": n_regions,
        "wall_seconds": wall,
        "cost_usd": cost,
        "rc": result.returncode,
        "tool_counts": tool_counts,
        "markers_remain": has_markers,
        "final_calc_lines": len(final_calc.splitlines()),
        "repo": str(repo),
        "merge_log_tail": out.strip().split("\n")[-5:] if out else [],
    }
    print()
    print(json.dumps(res, indent=2))

    out_path = REPO_ROOT / "bench-results" / "F13-small-synthetic.json"
    out_path.write_text(json.dumps(res, indent=2))
    print(f"\nSaved to {out_path}")

    return 0 if result.returncode == 0 and not has_markers else 1


if __name__ == "__main__":
    sys.exit(main())
