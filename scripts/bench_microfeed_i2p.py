"""Real-cost Microfeed benchmark for the unified i2p pipeline.

Drives the new `otto run` (compile → build → merge → audit → render)
against the Microfeed greenfield intent and runs the same private
hidden + browser evaluators as the legacy bench
(scripts/bench_microfeed_real_webapp.py from codex-i2p) so the verdict
is comparable.

Compare result.json to the codex-i2p baselines:
    bench-results/microfeed-realweb-20260503-033646  (codex-i2p Plan/Slices)
    bench-results/microfeed-realweb-20260503-020705  (mono baseline)

Parity criteria (per docs/intent-to-product-design.md, Step 11):
    * hidden_result == "pass"
    * browser_result == "pass"
    * 0 slices blocked
    * wall_s <= 1500  (≤ 25 min ceiling; mono baseline is ~1436s)
    * audit_verdict == "passed"
    * App shell visually comparable to mono (human-checked from
      proof-packet.html screenshot grid).

Usage:
    OTTO_ALLOW_REAL_COST=1 uv run python scripts/bench_microfeed_i2p.py \\
        --timeout-s 3600 --provider claude
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

import bench_evaluator as be  # noqa: E402

PYTHON = REPO_ROOT / ".venv" / "bin" / "python3"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
RESULTS_DIR = REPO_ROOT / "bench-results"


MICROFEED_INTENT = (
    "Build Microfeed as a Flask + SQLite webapp from this greenfield repo. "
    "Expose create_app(config=None), server-rendered HTML pages, and deterministic "
    "JSON/CSV endpoints. Required routes: GET /, GET /timeline/<username>, "
    "GET /search?q=..., GET /api/users/<username>/following, GET "
    "/api/users/<username>/followers, POST /api/reset, POST /api/users, "
    "POST /api/login-as, POST /api/follow, POST /api/unfollow, POST /api/posts, "
    "GET /api/timeline/<username>, GET /api/search?q=..., and GET "
    "/api/export/<username>.csv. The product must support normalized signup/login, "
    "duplicate user rejection, follow/unfollow with idempotent duplicate follows and "
    "self-follow rejection, nonblank tagged post creation, reverse-chronological "
    "timeline for self plus followed users, case-insensitive text and #tag search, "
    "and CSV export of the user's visible timeline with id, author, text, tags rows. "
    "Use a local SQLite database or deterministic in-memory test database. Include "
    "a usable browser UI with visible controls or links for creating users, following "
    "and unfollowing, creating tagged posts, searching, viewing timelines, and reaching "
    "CSV export without manually calling JSON endpoints."
)


@dataclass
class I2pSummary:
    mode: str = "i2p"
    repo_path: str = ""
    session_id: str = ""
    project_kind: str = "webapp"
    provider: str = "claude"
    wall_s: float = 0.0
    cli_exit_code: int = -1
    cli_timeout: bool = False
    spec_slices: int = 0
    slices_landed: int = 0
    slices_blocked: int = 0
    audit_verdict: str = "unknown"
    audit_narrative: str = ""
    cost_usd: float = 0.0
    proof_packet_path: str = ""
    # Audit-final-quality (audit-final-quality check). 0 = not assessed.
    quality_score: int = 0
    quality_findings: list[str] = field(default_factory=list)
    # bench_evaluator framework results (static-only here — HTTP-flavor
    # evaluators need server-boot scaffolding, separate work).
    evaluator_aggregate: str = "skipped"
    evaluator_summary: dict = field(default_factory=dict)
    evaluator_findings: list[dict] = field(default_factory=list)
    hidden_result: str = "not_run"
    browser_result: str = "not_run"
    visible_result: str = "not_run"
    notes: list[str] = field(default_factory=list)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument("--timeout-s", type=int, default=3600,
                        help="Max wall budget for `otto run` itself.")
    parser.add_argument("--provider", default="claude",
                        choices=["claude", "codex"],
                        help="LLM provider for compile/build/audit agents.")
    args = parser.parse_args(argv)

    require_real_cost_opt_in("real Microfeed i2p benchmark")

    run_id = time.strftime("microfeed-i2p-%Y%m%d-%H%M%S", time.gmtime())
    run_root = args.run_root or Path(tempfile.mkdtemp(prefix=f"{run_id}-"))
    art = args.output_dir / run_id
    art.mkdir(parents=True, exist_ok=True)
    _write(
        art / "paths.env",
        f"RUN_ROOT={run_root}\nART={art}\nREPO_ROOT={REPO_ROOT}\nPROVIDER={args.provider}\n",
    )

    repo = run_root / "i2p"
    summary = _run_i2p(
        repo, art, run_root, timeout_s=args.timeout_s, provider=args.provider
    )

    report = {
        "schema_version": 2,
        "i2p_path": True,
        "run_id": run_id,
        "run_root": str(run_root),
        "started_at": run_id.removeprefix("microfeed-i2p-"),
        "goal": MICROFEED_INTENT,
        "summary": asdict(summary),
        "verdict": _verdict(summary),
    }
    _write(art / "result.json", json.dumps(report, indent=2) + "\n")
    _write(art / "REPORT.md", _format_report(report))

    print(f"\nwrote {art / 'REPORT.md'}")
    print(f"verdict: {report['verdict']}")
    return 0 if report["verdict"] in {"i2p_passed"} else 1


def _run_i2p(
    repo: Path,
    art: Path,
    run_root: Path,
    *,
    timeout_s: int,
    provider: str,
) -> I2pSummary:
    summary = I2pSummary(
        repo_path=str(repo),
        provider=provider,
    )
    _setup_repo(repo, provider=provider)
    private = _setup_private_evaluator(art)

    # Drive `otto run` end-to-end. The CLI now drives compile + build +
    # merge + audit + render in one call.
    print(f"\n[i2p] driving otto run on {repo}")
    started = time.monotonic()
    cli_log = art / "i2p-01-otto-run.log"
    cli_result = _run_otto(
        repo,
        REPO_ROOT,
        ["run", MICROFEED_INTENT, "--project-kind", "webapp"],
        cli_log,
        timeout=timeout_s,
        check=False,
    )
    summary.wall_s = round(time.monotonic() - started, 2)
    summary.cli_exit_code = cli_result.returncode
    summary.cli_timeout = bool(cli_result.returncode == 124 or "Timeout" in (cli_result.stderr or ""))

    # Locate the most recent i2p session under the repo's otto_logs.
    session_dir = _latest_session_dir(repo)
    if session_dir is not None:
        summary.session_id = session_dir.name
        # Read spec.json
        spec_path = session_dir / "spec" / "spec.json"
        if spec_path.exists():
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                summary.spec_slices = len(spec.get("slices") or [])
                summary.project_kind = str(spec.get("project_kind") or "webapp")
            except Exception:
                summary.notes.append("spec.json present but unreadable")
        # Read proof-packet.json
        packet_path = session_dir / "proof-packet.json"
        if packet_path.exists():
            try:
                packet = json.loads(packet_path.read_text(encoding="utf-8"))
                summary.audit_verdict = str(packet.get("verdict") or "unknown")
                summary.audit_narrative = str(packet.get("audit_narrative") or "")[:600]
                summary.cost_usd = float(packet.get("cost_usd") or 0.0)
                summary.slices_landed = len(packet.get("landed_slice_ids") or [])
                summary.slices_blocked = len(packet.get("blocked_slice_ids") or [])
                summary.proof_packet_path = str(session_dir / "proof-packet.html")
                summary.quality_score = int(packet.get("quality_score") or 0)
                summary.quality_findings = [
                    str(f) for f in (packet.get("quality_findings") or [])[:20]
                ]
            except Exception:
                summary.notes.append("proof-packet.json present but unreadable")
        else:
            summary.notes.append("proof-packet.json was not produced")
    else:
        summary.notes.append("no i2p session found under otto_logs/sessions/")

    # Run bench_evaluator framework's static evaluators.
    eval_results = be.run_evaluators(
        be.EvaluatorContext(
            project_dir=repo, python=PYTHON, project_kind="webapp", timeout_s=120,
        ),
        [be.eval_contract_test, be.eval_code_health],
    )
    summary.evaluator_aggregate = be.aggregate_status(eval_results)
    summary.evaluator_summary = {r.name: r.status for r in eval_results}
    summary.evaluator_findings = [
        {"evaluator": r.name, "status": r.status, "summary": r.summary,
         "findings": [{"severity": f.severity, "message": f.message,
                       "evidence": f.evidence[:200]} for f in r.findings[:5]]}
        for r in eval_results
    ]

    # Run public visible acceptance (the same harness the legacy bench used,
    # seeded into the repo via _setup_repo). This validates the integrated
    # worktree's behavior outside of i2p's own checks.
    visible = _run_command(
        repo,
        [str(PYTHON), "tests/run_acceptance.py"],
        art / "i2p-02-visible-acceptance.log",
        timeout=180,
    )
    summary.visible_result = "pass" if visible.returncode == 0 else "fail"

    # Run private hidden + browser evaluators. These are kept outside the
    # builder's worktree (under art/private-evaluator/) to prevent the
    # build agent from reading them and gaming the eval.
    hidden = _run_command(
        repo,
        [str(PYTHON), str(private["hidden"]), "--repo", str(repo)],
        art / "i2p-03-private-hidden.log",
        timeout=120,
    )
    browser = _run_command(
        repo,
        [
            str(PYTHON),
            str(private["browser"]),
            "--repo",
            str(repo),
            "--artifacts",
            str(private["artifacts"]),
        ],
        art / "i2p-04-private-browser.log",
        timeout=300,
    )
    summary.hidden_result = "pass" if hidden.returncode == 0 else "fail"
    summary.browser_result = "pass" if browser.returncode == 0 else "fail"

    summary.notes.append(
        f"i2p: drove `otto run` with provider={provider}; "
        f"public acceptance + private hidden + private browser evaluators"
    )
    summary.notes.append(f"private evaluator stayed outside builder worktree: {private['root']}")
    return summary


def _verdict(summary: I2pSummary) -> str:
    must_functional = (
        summary.audit_verdict == "passed"
        and summary.hidden_result == "pass"
        and summary.browser_result == "pass"
        and summary.slices_blocked == 0
    )
    if not must_functional:
        return "i2p_failed"
    # Audit-final-quality (added per /loop check). Score 0 = audit didn't
    # assess; treat as "no quality signal" not failure. Score 1-2 means
    # the audit actively flagged quality issues — that's a separate-from-
    # functional fail dimension.
    if 0 < summary.quality_score < 3:
        return "i2p_quality_low"
    return "i2p_passed"


# ---------------------------------------------------------------------------
# Repo + evaluator setup (vendored from codex-i2p bench)
# ---------------------------------------------------------------------------


def _setup_repo(repo: Path, *, provider: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=repo)
    _run(["git", "config", "user.email", "bench@otto"], cwd=repo)
    _run(["git", "config", "user.name", "Bench"], cwd=repo)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=repo)
    _write(repo / "README.md", "# Microfeed\n\nGreenfield Flask webapp benchmark for the i2p pipeline.\n")
    _write(
        repo / ".gitignore",
        textwrap.dedent(
            """\
            otto_logs/
            __pycache__/
            *.pyc
            .pytest_cache/
            .venv/
            otto_artifacts/
            """
        ),
    )
    _write(
        repo / "otto.yaml",
        textwrap.dedent(
            f"""\
            provider: {provider}
            default_branch: main
            test_command: {json.dumps(f"{shlex.quote(str(PYTHON))} tests/run_acceptance.py")}
            """
        ),
    )
    _write(repo / "tests" / "run_acceptance.py", ACCEPTANCE_SCRIPT)
    # Seed a browser-test contract too. Without it, agents have no in-repo
    # signal that the home page must expose specific form selectors —
    # they read the prompt's "browser UI" guidance but consistently
    # don't implement it. The seeded test_command-style script gives
    # build agents a deterministic check to satisfy.
    _write(repo / "tests" / "run_browser_journey.py", BROWSER_SCRIPT)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-q", "-m", "seed acceptance contract", "--no-verify"], cwd=repo)


def _setup_private_evaluator(art: Path) -> dict[str, Path]:
    root = art / "private-evaluator"
    artifacts = root / "artifacts"
    hidden = root / "private_hidden_verifier.py"
    browser = root / "private_browser_evaluator.py"
    _write(hidden, PRIVATE_HIDDEN_SCRIPT)
    _write(browser, PRIVATE_BROWSER_SCRIPT)
    artifacts.mkdir(parents=True, exist_ok=True)
    return {"root": root, "artifacts": artifacts, "hidden": hidden, "browser": browser}


def _run_otto(
    repo: Path,
    code_root: Path,
    args: list[str],
    log_path: Path,
    *,
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(code_root) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("OTTO_BIN", None)
    result = _run(
        [str(PYTHON), "-m", "otto.cli", *args],
        cwd=repo, env=env, timeout=timeout, check=False,
    )
    _write(log_path, _command_log([str(PYTHON), "-m", "otto.cli", *args], repo, result))
    if check and result.returncode != 0:
        raise RuntimeError(f"otto command failed: {args}; see {log_path}")
    return result


def _run_command(
    repo: Path,
    argv: list[str],
    log_path: Path,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    result = _run(argv, cwd=repo, timeout=timeout, check=False)
    _write(log_path, _command_log(argv, repo, result))
    return result


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _command_log(argv: list[str], cwd: Path, result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"$ {' '.join(shlex.quote(a) for a in argv)}\n"
        f"cwd={cwd}\nexit_code={result.returncode}\n\n"
        f"STDOUT:\n{result.stdout or ''}\n"
        f"STDERR:\n{result.stderr or ''}"
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _latest_session_dir(repo: Path) -> Path | None:
    sessions_root = repo / "otto_logs" / "sessions"
    if not sessions_root.exists():
        return None
    candidates = [d for d in sessions_root.iterdir() if d.is_dir() and (d / "spec" / "spec.json").exists()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.name)[-1]


def _format_report(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines: list[str] = []
    lines.append(f"# Microfeed i2p bench — {report['run_id']}")
    lines.append("")
    lines.append(f"**Verdict:** `{report['verdict']}`")
    lines.append("")
    lines.append(f"- provider: `{s['provider']}`")
    lines.append(f"- session_id: `{s.get('session_id', '')}`")
    lines.append(f"- wall_s: {s['wall_s']:.1f} (ceiling target: 1500.0)")
    lines.append(f"- cost_usd: {s['cost_usd']:.2f}")
    lines.append(f"- spec slices: {s['spec_slices']}")
    lines.append(f"- slices landed: {s['slices_landed']}")
    lines.append(f"- slices blocked: {s['slices_blocked']}")
    lines.append(f"- audit verdict: {s['audit_verdict']}")
    lines.append(f"- quality_score: {s.get('quality_score', 0)}/5")
    lines.append(f"- evaluator_aggregate: {s.get('evaluator_aggregate', 'skipped')}")
    lines.append(
        f"- evaluator_summary: " + ", ".join(
            f"{k}={v}" for k, v in (s.get('evaluator_summary') or {}).items()
        )
    )
    lines.append("")
    if s.get("quality_findings"):
        lines.append("## Quality findings")
        for f in s["quality_findings"]:
            lines.append(f"- {f}")
        lines.append("")
    lines.append("## Evaluators")
    lines.append("")
    lines.append(f"- visible (public acceptance): **{s['visible_result']}**")
    lines.append(f"- hidden (private): **{s['hidden_result']}**")
    lines.append(f"- browser (private): **{s['browser_result']}**")
    lines.append("")
    if s.get("audit_narrative"):
        lines.append("## Audit narrative")
        lines.append("")
        lines.append("> " + s["audit_narrative"].replace("\n", "\n> "))
        lines.append("")
    lines.append("## Notes")
    for note in s.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    if s.get("proof_packet_path"):
        lines.append(f"Proof packet: {s['proof_packet_path']}")
    lines.append("")
    lines.append("## Parity criteria (Step 11)")
    lines.append("")
    parity = [
        ("hidden == pass", s["hidden_result"] == "pass"),
        ("browser == pass", s["browser_result"] == "pass"),
        ("0 slices blocked", s["slices_blocked"] == 0),
        ("wall_s <= 1500", s["wall_s"] <= 1500),
        ("audit verdict == passed", s["audit_verdict"] == "passed"),
        ("quality_score >= 3", s.get("quality_score", 0) >= 3),
    ]
    for label, ok in parity:
        lines.append(f"- {'✅' if ok else '❌'} {label}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Vendored evaluator scripts (identical to codex-i2p bench so verdicts compare)
# ---------------------------------------------------------------------------


ACCEPTANCE_SCRIPT = r'''
from __future__ import annotations

import csv
import importlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_app():
    errors = []
    for module_name in ("microfeed", "app", "run"):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name}: {exc}")
            continue
        create_app = getattr(module, "create_app", None)
        if callable(create_app):
            for arg in ({"TESTING": True}, None):
                try:
                    app = create_app(arg) if arg is not None else create_app()
                    app.config.update(TESTING=True)
                    return app
                except TypeError:
                    continue
        app = getattr(module, "app", None)
        if app is not None:
            app.config.update(TESTING=True)
            return app
    raise AssertionError("could not load Flask app; tried " + "; ".join(errors))


def client():
    app = load_app()
    return app.test_client()


def post_json(c, path: str, payload: dict[str, Any], expected: int = 200):
    resp = c.post(path, data=json.dumps(payload), content_type="application/json")
    assert resp.status_code == expected, (path, resp.status_code, resp.get_data(as_text=True))
    if resp.data:
        return resp.get_json() or {}
    return {}


def get_json(c, path: str, expected: int = 200):
    resp = c.get(path)
    assert resp.status_code == expected, (path, resp.status_code, resp.get_data(as_text=True))
    return resp.get_json() or {}


def reset(c) -> None:
    resp = c.post("/api/reset")
    assert resp.status_code in {200, 204}, (resp.status_code, resp.get_data(as_text=True))


def seed_accounts(c) -> None:
    reset(c)
    ada = post_json(c, "/api/users", {"username": "  Ada  ", "display_name": "  Ada Lovelace  "}, 201)
    assert ada["username"] == "ada"
    assert ada["display_name"] == "Ada Lovelace"
    login = post_json(c, "/api/login-as", {"username": " ADA "})
    assert login["username"] == "ada"
    dup = c.post("/api/users", json={"username": "ada", "display_name": "Ada Again"})
    assert dup.status_code == 400
    post_json(c, "/api/users", {"username": "grace", "display_name": "Grace Hopper"}, 201)
    post_json(c, "/api/users", {"username": "linus", "display_name": "Linus Torvalds"}, 201)


def check_accounts() -> None:
    with client() as c:
        seed_accounts(c)
        assert b"Microfeed" in c.get("/").data


def check_social() -> None:
    with client() as c:
        seed_accounts(c)
        post_json(c, "/api/follow", {"follower": "ada", "target": "grace"})
        post_json(c, "/api/follow", {"follower": "ada", "target": "linus"})
        post_json(c, "/api/follow", {"follower": "ada", "target": "grace"})
        assert get_json(c, "/api/users/ada/following")["following"] == ["grace", "linus"]
        assert get_json(c, "/api/users/grace/followers")["followers"] == ["ada"]
        assert c.post("/api/follow", json={"follower": "ada", "target": "ada"}).status_code == 400
        post_json(c, "/api/unfollow", {"follower": "ada", "target": "grace"})
        assert get_json(c, "/api/users/ada/following")["following"] == ["linus"]


def check_posts_search() -> None:
    with client() as c:
        seed_accounts(c)
        post_json(c, "/api/follow", {"follower": "ada", "target": "grace"})
        post_json(c, "/api/posts", {"author": "grace", "text": "Compilers are kind", "tags": ["compiler", "UX"]}, 201)
        post_json(c, "/api/posts", {"author": "linus", "text": "Hidden distributed version control", "tags": ["git"]}, 201)
        post_json(c, "/api/posts", {"author": "ada", "text": "Analytical engine notes", "tags": ["math"]}, 201)
        timeline = get_json(c, "/api/timeline/ada")["posts"]
        assert [post["author"] for post in timeline] == ["ada", "grace"]
        assert [post["text"] for post in get_json(c, "/api/search?q=compiler")["posts"]] == ["Compilers are kind"]
        assert [post["text"] for post in get_json(c, "/api/search?q=%23math")["posts"]] == ["Analytical engine notes"]
        assert c.get("/timeline/ada").status_code == 200
        assert c.get("/search?q=compiler").status_code == 200


def check_export() -> None:
    with client() as c:
        seed_accounts(c)
        post_json(c, "/api/follow", {"follower": "ada", "target": "grace"})
        post_json(c, "/api/posts", {"author": "grace", "text": "comma, friendly", "tags": ["math", "notes"]}, 201)
        post_json(c, "/api/posts", {"author": "ada", "text": "Ada own post", "tags": ["self"]}, 201)
        rows = list(csv.reader(io.StringIO(c.get("/api/export/ada.csv").get_data(as_text=True))))
        assert rows[0] == ["id", "author", "text", "tags"]
        assert [row[1] for row in rows[1:]] == ["ada", "grace"]


CHECKS = {
    "accounts": check_accounts,
    "social": check_social,
    "posts": check_posts_search,
    "timeline": check_posts_search,
    "search": check_posts_search,
    "export": check_export,
    "routes": check_export,
}


def main() -> int:
    for name in ("accounts", "social", "posts", "export"):
        CHECKS[name]()
        print(f"acceptance:{name}:pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


PRIVATE_HIDDEN_SCRIPT = r'''
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from pathlib import Path


def prepare_repo(repo: Path) -> None:
    repo = repo.resolve()
    sys.path.insert(0, str(repo / "tests"))
    sys.path.insert(0, str(repo))
    os.chdir(repo)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args()
    prepare_repo(args.repo)

    from run_acceptance import client, get_json, post_json, seed_accounts

    with client() as c:
        seed_accounts(c)
        post_json(c, "/api/follow", {"follower": "ada", "target": "grace"})
        post_json(c, "/api/follow", {"follower": "ada", "target": "linus"})
        assert c.post("/api/posts", json={"author": "ada", "text": "   ", "tags": ["blank"]}).status_code == 400
        post_json(c, "/api/posts", {"author": "grace", "text": "Compiler UX", "tags": ["compiler"]}, 201)
        post_json(c, "/api/posts", {"author": "linus", "text": "Git internals", "tags": ["git"]}, 201)
        post_json(c, "/api/posts", {"author": "ada", "text": "Analytical notes", "tags": ["math"]}, 201)
        timeline = get_json(c, "/api/timeline/ada")["posts"]
        assert [post["author"] for post in timeline] == ["ada", "linus", "grace"]
        rows = list(csv.reader(io.StringIO(c.get("/api/export/ada.csv").get_data(as_text=True))))
        assert [row[1] for row in rows[1:]] == ["ada", "linus", "grace"]
        assert [post["author"] for post in get_json(c, "/api/search?q=%23GIT")["posts"]] == ["linus"]
        post_json(c, "/api/unfollow", {"follower": "ada", "target": "linus"})
        assert [post["author"] for post in get_json(c, "/api/timeline/ada")["posts"]] == ["ada", "grace"]
    print("hidden:pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


PRIVATE_BROWSER_SCRIPT = r'''
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from urllib import request

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


def prepare_repo(repo: Path) -> None:
    repo = repo.resolve()
    sys.path.insert(0, str(repo / "tests"))
    sys.path.insert(0, str(repo))
    os.chdir(repo)


def post_json(base: str, path: str, payload: dict) -> None:
    req = request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=5) as response:
        if response.status >= 400:
            raise AssertionError((path, response.status, response.read()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    prepare_repo(args.repo)

    from run_acceptance import load_app

    app = load_app()
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    out = args.artifacts
    videos = out / "videos"
    out.mkdir(parents=True, exist_ok=True)
    videos.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(record_video_dir=str(videos), viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.goto(base + "/", wait_until="networkidle")
            body = page.text_content("body") or ""
            assert "Microfeed" in body
            assert page.locator("form").count() >= 2, "home must expose multiple usable UI forms"
            assert page.locator("input[name='username'], input[placeholder*=user i]").count() >= 1, "missing user creation control"
            assert page.locator("input[name='text'], textarea[name='text'], textarea[placeholder*=post i]").count() >= 1, "missing post creation control"
            assert page.locator("input[name='q'], input[type='search']").count() >= 1, "missing search control"
            page.screenshot(path=str(out / "home-ui-controls.png"), full_page=True)

            try:
                post_json(base, "/api/reset", {})
            except Exception:
                request.urlopen(request.Request(base + "/api/reset", method="POST"), timeout=5).read()
            for username, display in [("ada", "Ada Lovelace"), ("grace", "Grace Hopper")]:
                post_json(base, "/api/users", {"username": username, "display_name": display})
            post_json(base, "/api/follow", {"follower": "ada", "target": "grace"})
            post_json(base, "/api/posts", {"author": "grace", "text": "Compiler UX", "tags": ["compiler"]})
            post_json(base, "/api/posts", {"author": "ada", "text": "Analytical notes", "tags": ["math"]})

            page.goto(base + "/timeline/ada", wait_until="networkidle")
            timeline = page.text_content("body") or ""
            assert "Analytical notes" in timeline and "Compiler UX" in timeline
            assert page.locator("a[href*='export'], a[href$='.csv']").count() >= 1, "timeline must expose export link"
            page.screenshot(path=str(out / "timeline-usable.png"), full_page=True)

            page.goto(base + "/search?q=compiler", wait_until="networkidle")
            assert "Compiler UX" in (page.text_content("body") or "")
            page.screenshot(path=str(out / "search-usable.png"), full_page=True)
            context.close()
            browser.close()
    finally:
        server.shutdown()
    print(f"private-browser:pass artifacts={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


BROWSER_SCRIPT = r'''
from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib import request

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from run_acceptance import load_app


def post_json(base: str, path: str, payload: dict) -> None:
    req = request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=5) as response:
        if response.status >= 400:
            raise AssertionError((path, response.status, response.read()))


def main() -> int:
    app = load_app()
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    out = Path("otto_artifacts/browser")
    videos = out / "videos"
    out.mkdir(parents=True, exist_ok=True)
    videos.mkdir(parents=True, exist_ok=True)
    try:
        post_json(base, "/api/reset", {})
    except Exception:
        request.urlopen(request.Request(base + "/api/reset", method="POST"), timeout=5).read()
    for username, display in [("ada", "Ada Lovelace"), ("grace", "Grace Hopper"), ("linus", "Linus Torvalds")]:
        post_json(base, "/api/users", {"username": username, "display_name": display})
    post_json(base, "/api/follow", {"follower": "ada", "target": "grace"})
    post_json(base, "/api/posts", {"author": "grace", "text": "Compiler UX", "tags": ["compiler"]})
    post_json(base, "/api/posts", {"author": "ada", "text": "Analytical notes", "tags": ["math"]})
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(record_video_dir=str(videos), viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.goto(base + "/", wait_until="networkidle")
        home = page.text_content("body") or ""
        assert "Microfeed" in home
        assert page.locator("form").count() >= 2, "home must expose real workflow forms"
        assert page.locator("input[name='username'], input[placeholder*=user i]").count() >= 1, "home missing user creation control"
        assert page.locator("input[name='target'], input[placeholder*=follow i]").count() >= 1, "home missing follow/unfollow control"
        assert page.locator("input[name='text'], textarea[name='text'], textarea[placeholder*=post i]").count() >= 1, "home missing post creation control"
        assert page.locator("input[name='q'], input[type='search']").count() >= 1, "home missing search control"
        page.screenshot(path=str(out / "home.png"), full_page=True)
        page.goto(base + "/timeline/ada", wait_until="networkidle")
        body = page.text_content("body")
        assert "Analytical notes" in body and "Compiler UX" in body
        assert page.locator("a[href*='export'], a[href$='.csv']").count() >= 1, "timeline missing CSV export link"
        page.screenshot(path=str(out / "timeline.png"), full_page=True)
        page.goto(base + "/search?q=compiler", wait_until="networkidle")
        assert "Compiler UX" in page.text_content("body")
        page.screenshot(path=str(out / "search.png"), full_page=True)
        with request.urlopen(base + "/api/export/ada.csv", timeout=5) as response:
            export_text = response.read().decode()
        assert "author" in export_text and "Compiler UX" in export_text
        context.close()
        browser.close()
    server.shutdown()
    print(f"browser:pass artifacts={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


if __name__ == "__main__":
    raise SystemExit(main())
