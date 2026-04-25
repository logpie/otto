"""Stress F13 across diverse project shapes. Each scenario is synthetic but
realistic, designed to expose different bug classes.

Scenarios:
  S1 — Web app (HTML + JS + CSS): tests F13 on web stack files
  S2 — Multi-branch (4 branches): tests F13 scaling beyond 2 branches
  S3 — Rename + modify conflict: tests git's harder edge cases
  S4 — Add/add conflict (both branches added the same new file): tests another git edge
  S5 — Large single file (3000+ lines): tests if F13 handles big-file conflicts

Usage: OTTO_ALLOW_REAL_COST=1 .venv/bin/python scripts/bench_f13_diverse.py [S1|S2|S3|S4|S5|all]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from dataclasses import dataclass, asdict, field

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"
RESULTS_DIR = REPO_ROOT / "bench-results"

from real_cost_guard import require_real_cost_opt_in  # noqa: E402
from bench_costs import merge_cost_from_state_dir  # noqa: E402


@dataclass
class ScenarioResult:
    name: str
    n_branches: int
    n_files_with_conflicts: int
    expected_regions: int
    wall_seconds: float
    cost_usd: float
    rc: int
    tool_counts: dict[str, int]
    markers_remain: bool
    notes: list[str] = field(default_factory=list)
    repo: str = ""
    merge_log_tail: list[str] = field(default_factory=list)


def hr(label: str) -> None:
    pad = max(0, 76 - len(label))
    print(f"\n══════ {label} {'═' * pad}", flush=True)


def log(msg: str) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def setup_repo(prefix: str, files: dict[str, str]) -> Path:
    """Create base repo with given files (path → content)."""
    base = Path(tempfile.mkdtemp(prefix=prefix))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "s@s"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "S"], cwd=base, check=True)
    for path, content in files.items():
        p = base / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    # Common .gitignore (avoids F14)
    (base / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n*.pyo\n.pytest_cache/\n"
        "node_modules/\n*.log\ndist/\nbuild/\n.cache/\n"
    )
    # Otto config; consolidated merge is the default merge path.
    (base / "otto.yaml").write_text("default_branch: main\n")
    subprocess.run(["git", "add", "."], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=base, check=True)
    return base


def make_branch(repo: Path, name: str, files: dict[str, str], commit_msg: str) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", name], cwd=repo, check=True)
    for path, content in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", commit_msg], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)


def count_regions(repo: Path, branches: list[str]) -> int:
    """Quickly count conflict markers expected when merging all branches."""
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
                subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
                subprocess.run(["git", "commit", "-q", "-m", "markers"], cwd=r, capture_output=True)
        total = 0
        for f in r.rglob("*"):
            if f.is_file() and ".git" not in f.parts:
                try:
                    text = f.read_text()
                    total += sum(1 for line in text.splitlines() if line.startswith("<<<<<<<"))
                except Exception:
                    pass
        return total
    finally:
        _sh.rmtree(test_dir, ignore_errors=True)


def run_consolidated(repo: Path, branches: list[str], name: str) -> ScenarioResult:
    expected = count_regions(repo, branches)
    log(f"expected ~{expected} conflict regions")
    log(f"running consolidated merge of {branches}")
    t0 = time.time()
    result = subprocess.run(
        [str(OTTO_BIN), "merge", *branches, "--no-certify"],
        cwd=repo, capture_output=True, text=True, timeout=3600,
        env={**os.environ},
    )
    wall = time.time() - t0
    out = (result.stdout or "") + (result.stderr or "")
    log(f"merge done in {wall:.0f}s, rc={result.returncode}")

    cost = merge_cost_from_state_dir(repo / "otto_logs" / "merge")

    log_path = repo / "otto_logs" / "merge" / "conflict-agent-agentic.log"
    tool_counts: dict[str, int] = {}
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            m = re.match(r"\[\s*\d+\.\d+s\]\s*●\s*(\S+)", line)
            if m:
                tool_counts[m.group(1)] = tool_counts.get(m.group(1), 0) + 1

    # Detect remaining markers anywhere
    markers_remain = False
    files_with_conflicts = 0
    for f in repo.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            try:
                text = f.read_text()
                if any(line.startswith("<<<<<<<") for line in text.splitlines()):
                    markers_remain = True
                    files_with_conflicts += 1
            except Exception:
                pass

    return ScenarioResult(
        name=name,
        n_branches=len(branches),
        n_files_with_conflicts=files_with_conflicts,
        expected_regions=expected,
        wall_seconds=wall,
        cost_usd=cost,
        rc=result.returncode,
        tool_counts=tool_counts,
        markers_remain=markers_remain,
        repo=str(repo),
        merge_log_tail=out.strip().split("\n")[-7:],
    )


# ---------------------------------------------------------------- scenarios

def scenario_s1_web_app() -> ScenarioResult:
    """S1: Tiny web app, 2 branches each adding a feature touching HTML+JS+CSS."""
    hr("S1 — web app (HTML + JS + CSS)")
    base_html = '''<!DOCTYPE html>
<html>
<head>
  <title>Notes</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <h1>Notes</h1>
  <input id="note-input" placeholder="new note">
  <button onclick="addNote()">Add</button>
  <ul id="notes"></ul>
  <script src="app.js"></script>
</body>
</html>
'''
    base_js = '''const notes = [];

function addNote() {
  const text = document.getElementById('note-input').value;
  if (!text) return;
  notes.push({text, done: false});
  render();
  document.getElementById('note-input').value = '';
}

function render() {
  const ul = document.getElementById('notes');
  ul.innerHTML = '';
  notes.forEach((n, i) => {
    const li = document.createElement('li');
    li.textContent = n.text;
    if (n.done) li.style.textDecoration = 'line-through';
    li.onclick = () => { notes[i].done = !notes[i].done; render(); };
    ul.appendChild(li);
  });
}
'''
    base_css = '''body { font-family: sans-serif; max-width: 600px; margin: 2rem auto; }
h1 { color: #333; }
input { padding: 0.5rem; }
button { padding: 0.5rem 1rem; }
ul { padding: 0; }
li { list-style: none; padding: 0.5rem; cursor: pointer; }
'''
    repo = setup_repo("bench-s1-web-", {
        "index.html": base_html, "app.js": base_js, "style.css": base_css,
    })

    # Branch A: add categories (color-coded badges)
    branch_a_html = base_html.replace(
        '<input id="note-input" placeholder="new note">',
        '<input id="note-input" placeholder="new note">\n  <select id="cat"><option>work</option><option>personal</option></select>'
    )
    branch_a_js = '''const notes = [];

function addNote() {
  const text = document.getElementById('note-input').value;
  const cat = document.getElementById('cat').value;
  if (!text) return;
  notes.push({text, done: false, cat});
  render();
  document.getElementById('note-input').value = '';
}

function render() {
  const ul = document.getElementById('notes');
  ul.innerHTML = '';
  notes.forEach((n, i) => {
    const li = document.createElement('li');
    li.textContent = n.text;
    li.classList.add('cat-' + n.cat);
    if (n.done) li.style.textDecoration = 'line-through';
    li.onclick = () => { notes[i].done = !notes[i].done; render(); };
    ul.appendChild(li);
  });
}
'''
    branch_a_css = base_css + '\n.cat-work { background: #e8f0fe; }\n.cat-personal { background: #fef0e8; }\n'
    make_branch(repo, "feat/categories", {
        "index.html": branch_a_html, "app.js": branch_a_js, "style.css": branch_a_css,
    }, "feat: note categories")

    # Branch B: add localStorage persistence + dark mode
    branch_b_html = base_html.replace(
        '<h1>Notes</h1>',
        '<h1>Notes</h1>\n  <button onclick="toggleDark()">🌙</button>'
    )
    branch_b_js = '''const notes = JSON.parse(localStorage.getItem('notes') || '[]');

function save() { localStorage.setItem('notes', JSON.stringify(notes)); }

function addNote() {
  const text = document.getElementById('note-input').value;
  if (!text) return;
  notes.push({text, done: false});
  save();
  render();
  document.getElementById('note-input').value = '';
}

function render() {
  const ul = document.getElementById('notes');
  ul.innerHTML = '';
  notes.forEach((n, i) => {
    const li = document.createElement('li');
    li.textContent = n.text;
    if (n.done) li.style.textDecoration = 'line-through';
    li.onclick = () => { notes[i].done = !notes[i].done; save(); render(); };
    ul.appendChild(li);
  });
}

function toggleDark() {
  document.body.classList.toggle('dark');
}

if (localStorage.getItem('dark') === 'true') document.body.classList.add('dark');
'''
    branch_b_css = base_css + '\nbody.dark { background: #222; color: #eee; }\nbody.dark li { background: #333; }\n'
    make_branch(repo, "feat/persistence-darkmode", {
        "index.html": branch_b_html, "app.js": branch_b_js, "style.css": branch_b_css,
    }, "feat: localStorage + dark mode")

    return run_consolidated(repo, ["feat/categories", "feat/persistence-darkmode"], "S1-web-app")


def scenario_s2_four_branches() -> ScenarioResult:
    """S2: 4 branches all touching the same file. Tests scaling beyond 2-3 branches."""
    hr("S2 — 4 branches, single file")
    base = '''def hello():
    return "hello"


def goodbye():
    return "goodbye"


def main():
    print(hello())
    print(goodbye())


if __name__ == "__main__":
    main()
'''
    repo = setup_repo("bench-s2-four-", {"app.py": base})

    # Each branch adds a different greeting and modifies main()
    for greeting, fn_name in [("buenas", "buenas"), ("ohayo", "ohayo"), ("ciao", "ciao"), ("hola", "hola")]:
        branch_content = base.replace(
            "def main():",
            f'def {fn_name}():\n    return "{greeting}"\n\n\ndef main():'
        ).replace(
            "    print(hello())\n    print(goodbye())",
            f"    print(hello())\n    print({fn_name}())\n    print(goodbye())"
        )
        make_branch(repo, f"feat/{fn_name}", {"app.py": branch_content}, f"feat: {greeting}")

    branches = [f"feat/{fn}" for fn in ["buenas", "ohayo", "ciao", "hola"]]
    return run_consolidated(repo, branches, "S2-four-branches")


def scenario_s3_rename_modify() -> ScenarioResult:
    """S3: One branch renames a file; another modifies it. Tricky git case."""
    hr("S3 — rename + modify")
    base = '''def utility():
    return 42


def use_it():
    return utility() + 1
'''
    repo = setup_repo("bench-s3-rename-", {"utils.py": base})

    # Branch A: rename utils.py to helpers.py (no content change inside the function)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/rename"], cwd=repo, check=True)
    subprocess.run(["git", "mv", "utils.py", "helpers.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "rename: utils.py -> helpers.py"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

    # Branch B: modify utils.py (change utility() body)
    branch_b = '''def utility():
    """Always returns 100 now (was 42)."""
    return 100


def use_it():
    return utility() * 2
'''
    make_branch(repo, "feat/modify", {"utils.py": branch_b}, "feat: utility=100, double")

    return run_consolidated(repo, ["feat/rename", "feat/modify"], "S3-rename-modify")


def scenario_s4_add_add() -> ScenarioResult:
    """S4: Both branches added the same new file with different content."""
    hr("S4 — add/add (both branches created same file)")
    base = '''# Tiny project
'''
    repo = setup_repo("bench-s4-addadd-", {"README.md": base})

    branch_a_config = '''{
  "name": "myapp",
  "version": "1.0.0",
  "scripts": {
    "start": "node app.js",
    "test": "jest"
  }
}
'''
    branch_b_config = '''{
  "name": "myapp",
  "version": "1.0.0",
  "scripts": {
    "start": "node server.js",
    "build": "webpack"
  },
  "dependencies": {
    "express": "^4.0.0"
  }
}
'''
    make_branch(repo, "feat/test-config", {"package.json": branch_a_config}, "add package.json with test")
    make_branch(repo, "feat/build-config", {"package.json": branch_b_config}, "add package.json with build")

    return run_consolidated(repo, ["feat/test-config", "feat/build-config"], "S4-add-add")


def scenario_s5_large_file() -> ScenarioResult:
    """S5: Large single file (~2000 lines), 2 branches each adding 5 functions."""
    hr("S5 — large file (~2000 lines)")
    # Generate a large base: 200 placeholder functions
    funcs = []
    for i in range(200):
        funcs.append(f'''def func_{i}(x):
    """Placeholder function {i}."""
    return x * {i + 1}
''')
    base = "\n".join(funcs) + "\n\nif __name__ == '__main__':\n    print(func_0(5))\n"
    repo = setup_repo("bench-s5-large-", {"big.py": base})

    # Branch A: add 5 funcs, modify funcs 50, 100
    branch_a_lines = base.replace(
        'def func_50(x):\n    """Placeholder function 50."""\n    return x * 51',
        'def func_50(x):\n    """Now does 51x + 1."""\n    return x * 51 + 1'
    ).replace(
        'def func_100(x):\n    """Placeholder function 100."""\n    return x * 101',
        'def func_100(x):\n    """Now does 101x squared."""\n    return (x * 101) ** 2'
    )
    branch_a_extra = "\n".join(
        f'def feature_a_{i}(x):\n    """Branch A feature {i}."""\n    return x + {i}\n'
        for i in range(5)
    )
    branch_a_content = branch_a_lines.replace(
        "if __name__ == '__main__':",
        branch_a_extra + "\n\nif __name__ == '__main__':"
    )
    make_branch(repo, "feat/branch-a", {"big.py": branch_a_content}, "feat: branch A — modify + 5 funcs")

    # Branch B: same files modified differently + add 5 funcs
    branch_b_lines = base.replace(
        'def func_50(x):\n    """Placeholder function 50."""\n    return x * 51',
        'def func_50(x):\n    """Now does 51x - 1."""\n    return x * 51 - 1'
    ).replace(
        'def func_100(x):\n    """Placeholder function 100."""\n    return x * 101',
        'def func_100(x):\n    """Now does 101 + x."""\n    return 101 + x'
    )
    branch_b_extra = "\n".join(
        f'def feature_b_{i}(x):\n    """Branch B feature {i}."""\n    return x * {i + 100}\n'
        for i in range(5)
    )
    branch_b_content = branch_b_lines.replace(
        "if __name__ == '__main__':",
        branch_b_extra + "\n\nif __name__ == '__main__':"
    )
    make_branch(repo, "feat/branch-b", {"big.py": branch_b_content}, "feat: branch B — modify + 5 funcs")

    return run_consolidated(repo, ["feat/branch-a", "feat/branch-b"], "S5-large-file")


SCENARIOS = {
    "S1": scenario_s1_web_app,
    "S2": scenario_s2_four_branches,
    "S3": scenario_s3_rename_modify,
    "S4": scenario_s4_add_add,
    "S5": scenario_s5_large_file,
}


def main() -> int:
    try:
        require_real_cost_opt_in("F13 diverse benchmark")
    except SystemExit as exc:
        return int(exc.code or 2)
    args = sys.argv[1:] or ["all"]
    selected = []
    for arg in args:
        if arg.lower() == "all":
            selected = sorted(SCENARIOS.keys())
        elif arg.upper() in SCENARIOS:
            selected.append(arg.upper())
        else:
            print(f"unknown scenario: {arg!r}", file=sys.stderr)
            return 2
    results = []
    had_exception = False
    for name in selected:
        try:
            res = SCENARIOS[name]()
            results.append(res)
        except Exception as exc:
            had_exception = True
            log(f"  {name} FAILED with exception: {exc}")
            traceback.print_exc()

    # Summary
    hr("SUMMARY")
    print(f"{'Scenario':30s}  {'Branches':>8s}  {'Regions':>8s}  {'Wall':>8s}  {'Cost':>7s}  {'rc':>3s}  {'Markers':>8s}")
    for r in results:
        print(f"{r.name:30s}  {r.n_branches:>8}  {r.expected_regions:>8}  {r.wall_seconds:>7.0f}s  ${r.cost_usd:>5.2f}  {r.rc:>3}  {str(r.markers_remain):>8s}")

    # Save
    out_path = RESULTS_DIR / f"F13-diverse-{int(time.time())}.json"
    out_path.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\nSaved to {out_path}")
    if had_exception or not results:
        return 1
    return 1 if any(r.rc != 0 or r.markers_remain for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
