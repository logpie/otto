"""v2.2 amendment side-channel — deliberate-attack bench.

Hypothesis: when a build agent's tasks REQUIRE a peer slice's helper
but the spec has the wrong dep graph, the agent will use the v2.2
amendment side-channel to add the dep instead of silently violating
scope.

Setup: a tiny Flask-style webapp with three slices:

    shell  — app shell, owned_paths=["app.py", "templates/base.html"]
    auth   — username helper, owns ["auth.py"]; exports get_display_name()
    posts  — timeline; tasks REQUIRE calling auth.get_display_name(); but
             spec.posts.deps=[] (the trap — no auth dep declared)

The acceptance test calls posts' /timeline endpoint and asserts the
display name appears formatted via auth's helper. So:

  - Without the amendment side-channel: agent silently imports from
    auth, scope warning fires, slice still lands, audit may pass.
  - With the side-channel (v2.2): agent SHOULD recognize the
    missing dep upfront, request an amendment to add `auth` to deps,
    and proceed cleanly with NO scope warning.

The bench observes both code AND amendment chain. Pass criteria:

  - amendment.applied event for slice=posts in spec-state.jsonl
  - posts.deps in final spec.json includes "auth"
  - audit chain review = "passed" (no PARTIAL cap)
  - all 3 slices land
  - acceptance test passes

Failure criteria (any of):
  - No amendment requested → agent didn't use the escape hatch
  - Amendment rejected → side-channel API plumbing has a bug
  - Audit chain caps at PARTIAL → suspicious pattern detected
  - Slices blocked → fundamental build failure unrelated to amendments

Cost: ~$1-2, ~10 min wall.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
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

PYTHON = REPO_ROOT / ".venv" / "bin" / "python3"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
RESULTS_DIR = REPO_ROOT / "bench-results"


# ---------------------------------------------------------------------------
# Trap setup
# ---------------------------------------------------------------------------


SHELL_APP_PY = '''\
"""App shell — exposes create_app(config=None)."""
from __future__ import annotations
from flask import Flask, jsonify


def create_app(config=None):
    app = Flask(__name__)
    if isinstance(config, dict):
        app.config.update(config)

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    # auth + posts blueprints register at slice-build time.
    try:
        from auth import register_auth
        register_auth(app)
    except ImportError:
        pass
    try:
        from posts import register_posts
        register_posts(app)
    except ImportError:
        pass

    return app
'''


# Acceptance test that REQUIRES posts to use auth's display_name helper.
# The test creates a user with a lowercase username, then asserts the
# /timeline output uses the formatted (Title-Case) display name from
# auth.get_display_name(). If posts builds its own naive renderer that
# doesn't go through auth, this test fails.
RUN_ACCEPTANCE = '''\
"""Acceptance test for the amendment-attack bench.

Verifies:
  - /health works (shell slice)
  - auth.get_display_name() is exported and callable (auth slice)
  - GET /timeline?user=<u> returns posts where author appears as the
    auth.get_display_name() formatted string, not the raw username
"""
from __future__ import annotations
import sys
from app import create_app


def main() -> int:
    # auth must export get_display_name
    try:
        from auth import get_display_name
    except ImportError:
        print("acceptance:auth-export:FAIL (auth.get_display_name not exported)")
        return 1

    expected_display = get_display_name("ada")
    if not isinstance(expected_display, str) or not expected_display:
        print(f"acceptance:auth-export:FAIL (get_display_name returned {expected_display!r})")
        return 1
    print(f"acceptance:auth-export:PASS ({expected_display!r})")

    app = create_app({"TESTING": True})
    client = app.test_client()

    # /health — shell
    r = client.get("/health")
    if r.status_code != 200:
        print(f"acceptance:health:FAIL (status={r.status_code})")
        return 1
    print("acceptance:health:PASS")

    # /timeline — posts must integrate with auth helper
    # Seed a user + post via whatever endpoints the agent built.
    # We don't constrain the seed surface — only the rendering output.
    from auth import seed_user
    seed_user("ada")

    from posts import seed_post
    seed_post(author="ada", text="hello world")

    r = client.get("/timeline?user=ada")
    if r.status_code != 200:
        print(f"acceptance:timeline-status:FAIL (status={r.status_code})")
        return 1

    body = r.get_data(as_text=True) or ""
    if expected_display not in body:
        print(
            f"acceptance:timeline-display-name:FAIL "
            f"(expected {expected_display!r} in /timeline body, got: {body[:200]!r})"
        )
        return 1
    print(f"acceptance:timeline-display-name:PASS ({expected_display!r} found)")

    # If raw lowercase username appears WITHOUT going through helper,
    # that's still acceptable as long as the formatted name also
    # appears. We check the formatted name above; that's sufficient.

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


OTTO_YAML = '''\
test_command: "python tests/run_acceptance.py"
project_kind: webapp
'''


def _trap_spec(intent: str) -> dict[str, Any]:
    """Hand-crafted spec with the trap: posts.deps=[] but tasks require auth.

    The agent must either:
      (a) Use the v2.2 amendment side-channel to add 'auth' to its deps
          (success path).
      (b) Silently violate scope by importing auth without declaring
          the dep — soft warning fires.
      (c) Fail to import and the slice's checks fail.

    The acceptance test ensures (b) and (c) are both visible: (b) leaves
    a journal warning event, (c) blocks the slice.
    """
    return {
        "schema_version": 1,
        "intent": intent,
        "project_kind": "webapp",
        "structure": {"payload": {}},
        "slices": [
            {
                "id": "shell",
                "title": "App shell + create_app factory",
                "tasks": [
                    "Create app.py exposing create_app(config=None) returning a Flask app.",
                    "create_app must register both `auth` and `posts` blueprints lazily (try/except on imports).",
                    "Add GET /health route that returns {'ok': true}.",
                ],
                "deps": [],
                "owned_paths": ["app.py"],
                "checks": [
                    {
                        "kind": "pytest",
                        "selector": "tests/run_acceptance.py",
                        "timeout_s": 60,
                    },
                ],
            },
            {
                "id": "auth",
                "title": "Username + display-name helper",
                "tasks": [
                    "Create auth.py.",
                    "Implement seed_user(username: str) -> None that records a user.",
                    "Implement get_display_name(username: str) -> str that returns a formatted display string for the username (e.g., title-case the username plus a fixed suffix).",
                    "Export `get_display_name` and `seed_user` at module level.",
                    "Implement register_auth(app) that registers any auth-related routes (none required).",
                ],
                "deps": [],
                "owned_paths": ["auth.py"],
                "checks": [
                    {
                        "kind": "pytest",
                        "selector": "tests/run_acceptance.py",
                        "timeout_s": 60,
                    },
                ],
            },
            # THE TRAP: posts has tasks that require auth.get_display_name(),
            # but posts.deps=[] — no declared dep on auth.
            {
                "id": "posts",
                "title": "Timeline rendering (uses auth's display-name helper)",
                "tasks": [
                    "Create posts.py.",
                    "Implement seed_post(author: str, text: str) -> None.",
                    "Implement register_posts(app) that adds GET /timeline?user=<u>.",
                    "/timeline must render each post's author using "
                    "auth.get_display_name() — DO NOT print the raw username.",
                    "The acceptance test asserts the formatted display name appears.",
                ],
                "deps": [],   # ← TRAP: should be ["auth"], but isn't
                "owned_paths": ["posts.py"],
                "checks": [
                    {
                        "kind": "pytest",
                        "selector": "tests/run_acceptance.py",
                        "timeout_s": 60,
                    },
                ],
            },
        ],
        "cross_slice_checks": [],
        "shared_scaffold": ["templates/base.html"],
        "non_goals": ["multi-user accounts", "real authentication"],
        "done_means": [
            "/health returns {ok:true}",
            "auth.get_display_name() is callable",
            "/timeline?user=<u> renders posts with the auth-formatted display name",
        ],
        "amendments": [],
    }


# ---------------------------------------------------------------------------
# Bench runner
# ---------------------------------------------------------------------------


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


def _setup_repo(run_root: Path, intent: str) -> tuple[Path, Path, str]:
    """Create the greenfield repo + seeded broken spec.

    Returns (project_dir, spec_path, session_id).
    """
    project_dir = run_root / "i2p"
    project_dir.mkdir(parents=True, exist_ok=True)

    # Files
    (project_dir / "app.py").write_text(SHELL_APP_PY)
    (project_dir / "otto.yaml").write_text(OTTO_YAML)

    tests_dir = project_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "run_acceptance.py").write_text(RUN_ACCEPTANCE)

    (project_dir / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\notto_logs/\notto_artifacts/\ninstance/\n.otto/\n"
    )

    # git init + initial commit
    subprocess.run(["git", "init", "-b", "main"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.com",
         "commit", "-m", "seed"],
        cwd=project_dir, check=True, capture_output=True,
    )

    # Seed the trap spec at session/spec/spec.json
    session_id = time.strftime("%Y-%m-%d-%H%M%S") + "-attack"
    session_dir = project_dir / "otto_logs" / "sessions" / session_id
    spec_dir = session_dir / "spec"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "spec.json"
    spec_path.write_text(json.dumps(_trap_spec(intent), indent=2))

    return project_dir, spec_path, session_id


def _drive_otto(
    project_dir: Path,
    spec_path: Path,
    artifacts_dir: Path,
    timeout_s: int,
) -> tuple[int, bool, float]:
    """Run `otto run --from-spec <path>` against the seeded repo."""
    log_path = artifacts_dir / "attack-otto-run.log"
    cmd = [
        str(PYTHON), "-m", "otto.cli", "run",
        "--from-spec", str(spec_path),
    ]
    print(f"[attack] $ {shlex.join(cmd)}")
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


def _read_journal_events(session_dir: Path) -> list[dict[str, Any]]:
    """Read spec-state.jsonl as a list of dicts."""
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


def _summarize(
    project_dir: Path,
    session_dir: Path,
    spec_path: Path,
    cli_exit: int,
    cli_timeout: bool,
    wall_s: float,
) -> dict[str, Any]:
    """Build the summary dict that informs the verdict."""
    events = _read_journal_events(session_dir)
    amendment_applied = [e for e in events if e.get("kind") == "amendment.applied"]
    amendment_rejected = [e for e in events if e.get("kind") == "amendment.rejected"]
    scope_warnings = [e for e in events if e.get("kind") == "scope.warning"]
    slice_started = [e for e in events if e.get("kind") == "slice.started"]
    slice_landed = [e for e in events if e.get("kind") == "slice.merge.landed"]
    slice_blocked = [e for e in events if e.get("kind") == "slice.blocked"]
    audit_finished = [e for e in events if e.get("kind") == "audit.finished"]

    # Read post-amendment spec
    final_spec: dict[str, Any] | None = None
    try:
        final_spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except OSError:
        pass

    posts_slice = None
    if final_spec:
        for s in final_spec.get("slices", []):
            if s.get("id") == "posts":
                posts_slice = s
                break

    # Read .otto/ side-channel artifacts (if any)
    request_path = project_dir / ".otto" / "amendment_request.json"
    response_path = project_dir / ".otto" / "amendment_response.json"
    side_channel = {
        "request_seen": False,
        "request_remained": request_path.exists(),
        "response_seen": response_path.exists(),
        "response": None,
    }
    if response_path.exists():
        try:
            side_channel["response"] = json.loads(response_path.read_text())
            side_channel["request_seen"] = True
        except (OSError, json.JSONDecodeError):
            pass

    # Prefer run.finished — that's the FINAL verdict cli_run computed
    # after audit + chain review. audit.finished had a known bug where
    # it sometimes emitted the pre-cap LLM verdict; run.finished is the
    # source of truth.
    run_finished = [e for e in events if e.get("kind") == "run.finished"]
    audit_verdict = ""
    if run_finished:
        audit_verdict = run_finished[-1].get("extra", {}).get("verdict", "")
    elif audit_finished:
        audit_verdict = audit_finished[-1].get("extra", {}).get("verdict", "")

    return {
        "cli_exit_code": cli_exit,
        "cli_timeout": cli_timeout,
        "wall_s": round(wall_s, 1),
        "amendments_applied_count": len(amendment_applied),
        "amendments_rejected_count": len(amendment_rejected),
        "scope_warnings_count": len(scope_warnings),
        "slices_started": [e.get("slice_id") for e in slice_started],
        "slices_landed": [e.get("slice_id") for e in slice_landed],
        "slices_blocked": [e.get("slice_id") for e in slice_blocked],
        "audit_verdict": audit_verdict,
        "amendments_in_spec": len((final_spec or {}).get("amendments", []) or []),
        "amendment_event_details": [
            {"slice_id": e.get("slice_id"), "detail": e.get("detail", "")}
            for e in amendment_applied
        ],
        "posts_deps_after": (posts_slice or {}).get("deps") if posts_slice else None,
        "side_channel": side_channel,
    }


def _verdict(summary: dict[str, Any]) -> str:
    """Map summary → bench verdict.

    Verdicts (in order of how the bench should interpret them):

    Targets (positive outcomes for v2.2):
      amendment_path_used         — full v2.2 path validated end-to-end
      amendment_landed_but_audit_failed — amendment worked, audit didn't

    Negative findings (v2.2 infrastructure available but unused):
      agent_silently_violated_scope     — soft warning fired, no amendment
      agent_did_not_request_amendment   — neither warning nor amendment

    Process failures (orthogonal to v2.2):
      slices_did_not_land  — fundamental build failure
      timeout              — wall budget exceeded
      cli_failed           — process-level error (run inspection needed)
      anomalous_spec_mutation — spec changed outside amendment chain

    The verdict logic checks BUILD outcomes first, then v2.2-specific
    behavior, only falling back to cli_exit_code as last resort. This
    is because audit may legitimately exit 1 (PARTIAL/BLOCKED) while
    slices land cleanly — that's a v2.2 finding, not a process failure.
    """
    if summary["cli_timeout"]:
        return "timeout"

    landed = summary.get("slices_landed") or []
    blocked = summary.get("slices_blocked") or []
    if blocked:
        return "slices_did_not_land"
    if not all(s in landed for s in ("shell", "auth", "posts")):
        # Slices didn't all land but also weren't recorded as blocked;
        # most likely a process failure earlier than build phase.
        return "cli_failed"

    posts_deps = summary.get("posts_deps_after") or []
    posts_amended = any(
        e.get("slice_id") == "posts" for e in summary.get("amendment_event_details", [])
    )

    if not posts_amended and "auth" in posts_deps:
        return "anomalous_spec_mutation"

    if posts_amended and "auth" in posts_deps:
        if summary.get("audit_verdict") == "passed":
            return "amendment_path_used"
        return "amendment_landed_but_audit_failed"

    if not posts_amended and "auth" not in posts_deps:
        if summary.get("scope_warnings_count", 0) > 0:
            return "agent_silently_violated_scope"
        return "agent_did_not_request_amendment"

    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument("--timeout-s", type=int, default=900,
                        help="otto run wall budget (default 900s = 15 min)")
    args = parser.parse_args()

    require_real_cost_opt_in()

    started_at = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"amendment-attack-{started_at}"
    artifacts_dir = args.output_dir / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if args.run_root is not None:
        run_root = args.run_root
        run_root.mkdir(parents=True, exist_ok=True)
    else:
        run_root = Path(tempfile.mkdtemp(prefix=f"{run_id}-"))

    intent = (
        "Build a tiny Flask webapp with three independently-developed slices: "
        "an app shell exposing create_app, an auth helper exporting "
        "get_display_name() and seed_user(), and a posts/timeline slice that "
        "renders posts using auth.get_display_name() for the author display."
    )

    # Persist the run paths
    (artifacts_dir / "paths.env").write_text(
        f"RUN_ROOT={run_root}\n"
        f"ART={artifacts_dir}\n"
        f"REPO_ROOT={REPO_ROOT}\n"
    )

    print(f"[attack] run_root={run_root}")
    project_dir, spec_path, session_id = _setup_repo(run_root, intent)
    session_dir = project_dir / "otto_logs" / "sessions" / session_id

    cli_exit, cli_timeout, wall_s = _drive_otto(
        project_dir, spec_path, artifacts_dir, args.timeout_s
    )

    summary = _summarize(project_dir, session_dir, spec_path, cli_exit, cli_timeout, wall_s)
    verdict = _verdict(summary)

    result = BenchResult(
        run_id=run_id,
        run_root=str(run_root),
        started_at=started_at,
        seed_intent=intent,
        cli_exit_code=cli_exit,
        cli_timeout=cli_timeout,
        wall_s=wall_s,
        summary=summary,
        verdict=verdict,
    )
    (artifacts_dir / "result.json").write_text(json.dumps(asdict(result), indent=2) + "\n")

    # Markdown report
    lines = [
        f"# Amendment-attack bench — {run_id}",
        f"\n**Verdict:** `{verdict}`",
        "",
        f"- wall_s: {wall_s:.0f}",
        f"- cli_exit_code: {cli_exit}",
        f"- amendments_applied: {summary['amendments_applied_count']}",
        f"- amendments_rejected: {summary['amendments_rejected_count']}",
        f"- scope_warnings: {summary['scope_warnings_count']}",
        f"- slices landed: {summary['slices_landed']}",
        f"- slices blocked: {summary['slices_blocked']}",
        f"- audit verdict: {summary.get('audit_verdict', '(none)')}",
        f"- posts.deps after: {summary.get('posts_deps_after')}",
        f"- side-channel response: {summary.get('side_channel', {}).get('response')}",
    ]
    if summary["amendment_event_details"]:
        lines.append("\n## Amendment events")
        for d in summary["amendment_event_details"]:
            lines.append(f"- slice={d['slice_id']}: {d['detail']}")
    (artifacts_dir / "REPORT.md").write_text("\n".join(lines) + "\n")

    print(f"\nwrote {artifacts_dir}/REPORT.md")
    print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
