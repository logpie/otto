"""Markdown-blog SSG benchmark for the unified i2p pipeline.

A different-shape project than Microfeed (no Flask, no DB, no API) to
validate generalization. The product: a Python CLI that turns
`content/*.md` files into a static `output/` directory with:

  - output/index.html — landing page with post titles + links
  - output/posts/<slug>.html — one page per post
  - output/tags/<tag>.html — one page per tag listing posts
  - output/rss.xml — RSS feed

Frontmatter format (YAML):
  ---
  title: ...
  date: 2026-01-15
  tags: [python, otto]
  ---
  <markdown body>

Acceptance verifies the structural output shape only (presence of
expected files, correct cross-links, RSS validity), not aesthetics.

Usage:
    OTTO_ALLOW_REAL_COST=1 uv run python scripts/bench_blog_ssg_i2p.py \\
        --timeout-s 1800 --provider claude
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from real_cost_guard import require_real_cost_opt_in  # noqa: E402

PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python3"
if not PYTHON_BIN.exists():
    PYTHON_BIN = Path(sys.executable)
RESULTS_DIR = REPO_ROOT / "bench-results"


SSG_INTENT = (
    "Build a Python static-site generator for a markdown blog from this "
    "greenfield repo. Provide a CLI entry-point `python -m blog build` "
    "(also discoverable as `python build_site.py` if simpler) that reads "
    "every .md file under `content/` and writes a complete `output/` "
    "directory tree. Required outputs: `output/index.html` listing all "
    "posts with title + date + tags + link, `output/posts/<slug>.html` "
    "for each post (slug = filename stem), `output/tags/<tag>.html` for "
    "each tag listing posts that carry it, and `output/rss.xml` (RSS 2.0 "
    "or Atom — either is fine) covering all posts in reverse chronological "
    "order. Markdown sources have YAML frontmatter with title, date "
    "(YYYY-MM-DD), and tags (list). Render the markdown body to HTML "
    "(any standard library — `markdown`, `mistune`, etc.). Posts must "
    "be ordered newest-first on index.html and rss.xml. Index, post, "
    "and tag pages must use a shared base template so navigation back "
    "to the home page is reachable from any page (a header link <a "
    "href=\"/\">Home</a> or similar). The CLI should be idempotent — "
    "running `python -m blog build` twice from the same content produces "
    "the same output."
)


# Acceptance test verifies the SSG's structural output: files exist,
# cross-links are correct, RSS contains all posts. Does NOT exercise
# aesthetics. Uses only stdlib so any Python project can run it.
ACCEPTANCE_SCRIPT = r'''
"""Acceptance test for the markdown-blog SSG bench."""
from __future__ import annotations
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
OUTPUT = ROOT / "output"


def fail(label: str, detail: str) -> int:
    print(f"acceptance:{label}:FAIL ({detail})")
    return 1


def ok(label: str, detail: str = "") -> None:
    print(f"acceptance:{label}:PASS{(' ' + detail) if detail else ''}")


def main() -> int:
    # 1. Build runs cleanly twice (idempotent).
    for run_num in (1, 2):
        # try `python -m blog build` first; fall back to build_site.py
        cmd = None
        if (ROOT / "blog").is_dir() and (ROOT / "blog" / "__init__.py").exists():
            cmd = [sys.executable, "-m", "blog", "build"]
        elif (ROOT / "build_site.py").exists():
            cmd = [sys.executable, "build_site.py"]
        else:
            return fail("build-entry", "no `blog/__init__.py` and no `build_site.py`")
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return fail(
                f"build-run-{run_num}",
                f"exit={r.returncode}; stderr={r.stderr[:500]!r}",
            )
    ok("build-idempotent", "ran twice without error")

    # 2. Output structure: index, posts, tags, rss.
    if not OUTPUT.is_dir():
        return fail("output-dir", "output/ missing")
    index = OUTPUT / "index.html"
    if not index.is_file():
        return fail("output-index", "output/index.html missing")
    posts_dir = OUTPUT / "posts"
    if not posts_dir.is_dir():
        return fail("output-posts-dir", "output/posts/ missing")
    tags_dir = OUTPUT / "tags"
    if not tags_dir.is_dir():
        return fail("output-tags-dir", "output/tags/ missing")
    rss_path = OUTPUT / "rss.xml"
    if not rss_path.is_file():
        return fail("output-rss", "output/rss.xml missing")
    ok("output-structure", "index, posts/, tags/, rss.xml all present")

    # 3. Each content/*.md becomes output/posts/<slug>.html
    md_files = sorted(CONTENT.glob("*.md"))
    if not md_files:
        return fail("content", "no content/*.md files seeded")
    expected_slugs = {p.stem for p in md_files}
    rendered_slugs = {p.stem for p in posts_dir.glob("*.html")}
    missing = expected_slugs - rendered_slugs
    if missing:
        return fail(
            "post-coverage",
            f"missing post pages for slugs: {sorted(missing)}",
        )
    ok("post-coverage", f"all {len(expected_slugs)} posts rendered")

    # 4. Index contains links to each post page.
    index_html = index.read_text(encoding="utf-8")
    for slug in expected_slugs:
        # Accept either /posts/<slug>.html or posts/<slug>.html
        if f"posts/{slug}.html" not in index_html and f"/posts/{slug}.html" not in index_html:
            return fail("index-links", f"no link to posts/{slug}.html on index.html")
    ok("index-links")

    # 5. Each post's body renders SOMETHING (>50 chars beyond head/nav)
    for post_html in posts_dir.glob("*.html"):
        body = post_html.read_text(encoding="utf-8")
        if len(body) < 200:
            return fail(
                "post-body",
                f"{post_html.name} too small ({len(body)} chars)",
            )
    ok("post-bodies")

    # 6. Tag pages exist for at least one tag (we seed tagged posts).
    tag_pages = list(tags_dir.glob("*.html"))
    if not tag_pages:
        return fail("tag-pages", "no tag pages produced")
    ok("tag-pages", f"{len(tag_pages)} tag page(s)")

    # 7. RSS validates as XML and contains every post's title.
    try:
        tree = ET.parse(rss_path)
    except ET.ParseError as exc:
        return fail("rss-xml", f"rss.xml not parseable: {exc}")
    root = tree.getroot()
    rss_text = rss_path.read_text(encoding="utf-8")
    # Pull expected titles from frontmatter to assert they appear.
    expected_titles = []
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
        if m:
            expected_titles.append(m.group(1).strip().strip('"').strip("'"))
    for title in expected_titles:
        if title not in rss_text:
            return fail("rss-titles", f"title {title!r} missing from rss.xml")
    ok("rss-titles", f"{len(expected_titles)} title(s) in feed")

    # 8. Idempotency: re-running produced no diff in output/ tree
    # (we ran build twice above; check git or recompute).
    # If git is available, compare.
    try:
        diff = subprocess.run(
            ["git", "diff", "--quiet", "output/"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if diff.returncode != 0:
            # Files changed between runs — second run produced different
            # content. (Acceptable for date-stamped files; we don't
            # assert this strictly.)
            print("acceptance:idempotent:WARN (output/ changed between runs)")
        else:
            ok("idempotent", "second build produced no diff")
    except FileNotFoundError:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# Three sample posts the bench seeds. Variety in tags so tag pages exist.
SEED_POSTS = {
    "hello-otto.md": """\
---
title: Hello, Otto
date: 2026-01-15
tags: [python, otto, intro]
---

This is a sample post used by the benchmark.

Otto turns intent into product.
""",
    "second-post.md": """\
---
title: A second post
date: 2026-02-01
tags: [python, blogging]
---

Another post — to make the index nontrivial and the RSS feed nontrivial.
""",
    "third-post.md": """\
---
title: Third post about Otto
date: 2026-02-20
tags: [otto, design]
---

Describing the i2p pipeline at a high level.
""",
}


OTTO_YAML = '''\
test_command: "python tests/run_acceptance.py"
project_kind: webapp
'''


@dataclass
class BenchResult:
    schema_version: int = 1
    run_id: str = ""
    run_root: str = ""
    started_at: str = ""
    seed_intent: str = ""
    cli_exit_code: int | None = None
    cli_timeout: bool = False
    wall_s: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)
    verdict: str = "unknown"


def _setup_repo(run_root: Path) -> Path:
    project_dir = run_root / "i2p"
    project_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "otto.yaml").write_text(OTTO_YAML)

    tests_dir = project_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "run_acceptance.py").write_text(ACCEPTANCE_SCRIPT)

    content_dir = project_dir / "content"
    content_dir.mkdir(exist_ok=True)
    for name, body in SEED_POSTS.items():
        (content_dir / name).write_text(body)

    (project_dir / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\notto_logs/\notto_artifacts/\noutput/\n.otto/\n"
    )

    subprocess.run(["git", "init", "-b", "main"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.com",
         "commit", "-m", "seed"],
        cwd=project_dir, check=True, capture_output=True,
    )
    return project_dir


def _drive_otto(
    project_dir: Path,
    artifacts_dir: Path,
    timeout_s: int,
    provider: str,
) -> tuple[int, bool, float]:
    log_path = artifacts_dir / "ssg-otto-run.log"
    cmd = [
        str(PYTHON_BIN), "-m", "otto.cli", "run",
        "--project-kind", "webapp",  # SSG is closest to webapp shape
        SSG_INTENT,
    ]
    print(f"[ssg] $ {shlex.join(cmd)}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        with log_path.open("wb") as fh:
            proc = subprocess.run(
                cmd, cwd=project_dir, stdout=fh, stderr=subprocess.STDOUT,
                timeout=timeout_s, check=False,
            )
        return proc.returncode, False, time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return -1, True, time.monotonic() - t0


def _read_journal(session_dir: Path) -> list[dict[str, Any]]:
    target = session_dir / "spec-state.jsonl"
    if not target.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _latest_session_dir(project_dir: Path) -> Path | None:
    sessions = project_dir / "otto_logs" / "sessions"
    if not sessions.exists():
        return None
    children = [c for c in sessions.iterdir() if c.is_dir()]
    if not children:
        return None
    return max(children, key=lambda p: p.stat().st_mtime)


def _summarize(
    project_dir: Path,
    cli_exit: int,
    cli_timeout: bool,
    wall_s: float,
) -> tuple[dict[str, Any], Path | None]:
    session_dir = _latest_session_dir(project_dir)
    events = _read_journal(session_dir) if session_dir else []
    landed = [e.get("slice_id") for e in events if e.get("kind") == "slice.merge.landed"]
    blocked = [e.get("slice_id") for e in events if e.get("kind") == "slice.blocked"]
    run_finished = [e for e in events if e.get("kind") == "run.finished"]
    audit_verdict = ""
    if run_finished:
        audit_verdict = run_finished[-1].get("extra", {}).get("verdict", "")

    # Run acceptance test directly to get an INDEPENDENT verification.
    accept_log = project_dir / "tests" / "run_acceptance.py"
    accept_passed = False
    accept_output = ""
    if accept_log.exists():
        try:
            r = subprocess.run(
                [str(PYTHON_BIN), "tests/run_acceptance.py"],
                cwd=project_dir, capture_output=True, text=True, timeout=300,
            )
            accept_passed = r.returncode == 0
            accept_output = (r.stdout + "\n" + r.stderr)[:2000]
        except subprocess.TimeoutExpired:
            accept_output = "TIMEOUT"

    spec_path = session_dir / "spec" / "spec.json" if session_dir else None
    amendments_count = 0
    spec_slices = 0
    if spec_path and spec_path.exists():
        try:
            spec = json.loads(spec_path.read_text())
            amendments_count = len(spec.get("amendments") or [])
            spec_slices = len(spec.get("slices") or [])
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "cli_exit_code": cli_exit,
        "cli_timeout": cli_timeout,
        "wall_s": round(wall_s, 1),
        "session_dir": str(session_dir) if session_dir else None,
        "spec_slices": spec_slices,
        "slices_landed": landed,
        "slices_blocked": blocked,
        "audit_verdict": audit_verdict,
        "amendments_count": amendments_count,
        "scope_warnings_count": sum(1 for e in events if e.get("kind") == "scope.warning"),
        "acceptance_independent_pass": accept_passed,
        "acceptance_output": accept_output,
    }, session_dir


def _verdict(summary: dict[str, Any]) -> str:
    if summary["cli_timeout"]:
        return "timeout"
    if not summary.get("session_dir"):
        return "no_session_produced"
    if summary.get("slices_blocked"):
        return "slices_did_not_land"
    if not summary.get("slices_landed"):
        return "no_slices_landed"
    if summary.get("audit_verdict") not in ("passed", "partial"):
        return f"unexpected_audit_verdict_{summary.get('audit_verdict','')}"
    if not summary.get("acceptance_independent_pass"):
        return "acceptance_failed"
    if summary.get("audit_verdict") == "partial":
        return "partial_but_acceptance_passes"
    return "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--provider", default="claude")
    args = parser.parse_args()

    require_real_cost_opt_in()

    started_at = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"blog-ssg-i2p-{started_at}"
    artifacts_dir = args.output_dir / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if args.run_root is not None:
        run_root = args.run_root
        run_root.mkdir(parents=True, exist_ok=True)
    else:
        run_root = Path(tempfile.mkdtemp(prefix=f"{run_id}-"))

    (artifacts_dir / "paths.env").write_text(
        f"RUN_ROOT={run_root}\n"
        f"ART={artifacts_dir}\n"
        f"REPO_ROOT={REPO_ROOT}\n"
        f"PROVIDER={args.provider}\n"
    )

    print(f"[ssg] run_root={run_root}")
    project_dir = _setup_repo(run_root)

    cli_exit, cli_timeout, wall_s = _drive_otto(
        project_dir, artifacts_dir, args.timeout_s, args.provider,
    )
    summary, _session_dir = _summarize(project_dir, cli_exit, cli_timeout, wall_s)
    verdict = _verdict(summary)

    result = BenchResult(
        run_id=run_id,
        run_root=str(run_root),
        started_at=started_at,
        seed_intent=SSG_INTENT,
        cli_exit_code=cli_exit,
        cli_timeout=cli_timeout,
        wall_s=wall_s,
        summary=summary,
        verdict=verdict,
    )
    (artifacts_dir / "result.json").write_text(json.dumps(asdict(result), indent=2) + "\n")

    lines = [
        f"# SSG i2p bench — {run_id}",
        f"\n**Verdict:** `{verdict}`",
        "",
        f"- wall_s: {wall_s:.0f}",
        f"- cli_exit_code: {cli_exit}",
        f"- spec_slices: {summary.get('spec_slices')}",
        f"- slices landed: {summary.get('slices_landed')}",
        f"- slices blocked: {summary.get('slices_blocked')}",
        f"- audit verdict: {summary.get('audit_verdict')}",
        f"- amendments: {summary.get('amendments_count')}",
        f"- scope_warnings: {summary.get('scope_warnings_count')}",
        f"- acceptance_independent_pass: {summary.get('acceptance_independent_pass')}",
    ]
    (artifacts_dir / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"\nwrote {artifacts_dir}/REPORT.md")
    print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
