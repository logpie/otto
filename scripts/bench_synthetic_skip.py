"""Deterministic test for SKIPPED-verdict behavior in post-merge cert.

The previous LLM-based bench couldn't reliably engineer the right scenario
because each incremental build's cert only registered stories about THAT
build's contribution — never about pre-existing untouched modules. This
bench bypasses the build phase: we manually scaffold a multi-module repo
with two branches that conflict on ONE file, plus hand-crafted queue
manifests + PoW JSON files containing stories about MULTIPLE files
(some in the merge diff, some not).

Then we run REAL `otto merge`. The merge agent and cert agent are both
real LLM calls — only the upstream "what stories" registration is scripted.
This is exactly the right scope to test whether the merge_context preamble
actually steers the cert agent's behavior.

Success criterion: cert PoW must contain ≥1 STORY_RESULT with
verdict=SKIPPED for stories whose files don't appear in the merge diff.

Usage: OTTO_ALLOW_REAL_COST=1 .venv/bin/python scripts/bench_synthetic_skip.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_runner import BenchResult, OTTO_BIN, RESULTS_DIR, log, proof_of_work_path  # noqa: E402
from bench_costs import merge_cost_from_state_dir  # noqa: E402
from real_cost_guard import require_real_cost_opt_in  # noqa: E402


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


# ---------- Module contents ----------
# Three modules. The two branches will both modify ONLY auth.py (conflict).
# notes.py and search.py exist on main and on each branch unchanged.

AUTH_BASE = '''def login(user, password):
    """Return True for the canonical admin credentials."""
    return user == "admin" and password == "secret"
'''

NOTES_PY = '''def format_note(text, prefix="> "):
    """Pure transform — no I/O."""
    return prefix + text
'''

SEARCH_PY = '''def find(items, query):
    """Return items that contain `query` as a substring."""
    return [x for x in items if query in x]
'''

TEST_AUTH_BASE = '''from auth import login

def test_login_admin():
    assert login("admin", "secret") is True

def test_login_wrong():
    assert login("alice", "x") is False
'''

TEST_NOTES = '''from notes import format_note

def test_format_default():
    assert format_note("hi") == "> hi"

def test_format_custom_prefix():
    assert format_note("x", prefix="# ") == "# x"
'''

TEST_SEARCH = '''from search import find

def test_find_substring():
    assert find(["apple", "banana", "cherry"], "an") == ["banana"]

def test_find_empty():
    assert find([], "x") == []
'''

# Branch B (logout): modifies auth.py to add logout function.
AUTH_B = AUTH_BASE + '''

def logout(user):
    """B's contribution: a logout helper."""
    return f"goodbye, {user}"
'''

TEST_AUTH_B = TEST_AUTH_BASE + '''

def test_logout():
    from auth import logout
    assert logout("alice") == "goodbye, alice"
'''

# Branch C (signup): modifies auth.py to add signup function (CONFLICTS with B).
AUTH_C = AUTH_BASE + '''

def signup(user, password):
    """C's contribution: a signup helper."""
    return {"user": user, "password_set": bool(password)}
'''

TEST_AUTH_C = TEST_AUTH_BASE + '''

def test_signup():
    from auth import signup
    assert signup("alice", "pw") == {"user": "alice", "password_set": True}
'''


def _write_pow(path: Path, stories: list[dict]) -> None:
    """Write a minimal proof-of-work.json with the given stories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pow_data = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcome": "passed",
        "cost_usd": 0.0,
        "duration_s": 0.0,
        "stories": stories,
    }
    path.write_text(json.dumps(pow_data, indent=2))


def _write_manifest(path: Path, *, task_id: str, branch: str, pow_path: Path) -> None:
    """Write a queue manifest pointing at the hand-crafted PoW."""
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "command": "build",
        "argv": ["build", "--fast", f"synthetic-{task_id}"],
        "queue_task_id": task_id,
        "run_id": f"build-synthetic-{task_id}",
        "branch": branch,
        "checkpoint_path": None,
        "proof_of_work_path": str(pow_path),
        "cost_usd": 0.0,
        "duration_s": 0.0,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head_sha": None,
        "resolved_intent": f"synthetic-{task_id}",
        "focus": None,
        "target": None,
        "exit_status": "success",
        "schema_version": 1,
        "extra": {},
    }
    path.write_text(json.dumps(manifest, indent=2))


def _build_synthetic_repo() -> Path:
    """Scaffold the repo state for the test. Returns the repo path."""
    import tempfile
    repo = Path(tempfile.mkdtemp(prefix="bench-synthetic-skip-"))
    log(f"repo: {repo}")

    # Init git
    run(["git", "init", "-q", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "bench@otto"], cwd=repo)
    run(["git", "config", "user.name", "Bench"], cwd=repo)

    # Run otto setup (gets .gitignore + .gitattributes etc.)
    run([str(OTTO_BIN), "setup"], cwd=repo, check=False)

    # Write base modules + tests
    (repo / "auth.py").write_text(AUTH_BASE)
    (repo / "notes.py").write_text(NOTES_PY)
    (repo / "search.py").write_text(SEARCH_PY)
    (repo / "test_auth.py").write_text(TEST_AUTH_BASE)
    (repo / "test_notes.py").write_text(TEST_NOTES)
    (repo / "test_search.py").write_text(TEST_SEARCH)
    (repo / "intent.md").write_text("Synthetic test product: auth + notes + search modules.\n")

    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-q", "-m", "base: auth + notes + search"], cwd=repo)

    # Branch B: only modifies auth.py (and its test)
    run(["git", "checkout", "-q", "-b", "build/logout-synth"], cwd=repo)
    (repo / "auth.py").write_text(AUTH_B)
    (repo / "test_auth.py").write_text(TEST_AUTH_B)
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-q", "-m", "logout: add auth.logout()"], cwd=repo)

    # Branch C: only modifies auth.py (and its test) — different change → conflict
    run(["git", "checkout", "-q", "main"], cwd=repo)
    run(["git", "checkout", "-q", "-b", "build/signup-synth"], cwd=repo)
    (repo / "auth.py").write_text(AUTH_C)
    (repo / "test_auth.py").write_text(TEST_AUTH_C)
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-q", "-m", "signup: add auth.signup()"], cwd=repo)

    # Back to main
    run(["git", "checkout", "-q", "main"], cwd=repo)

    # Hand-craft queue.yml with 2 entries
    queue_yml = """schema_version: 1
tasks:
  - id: logout-synth
    command_argv: [build, --fast, synthetic-logout]
    branch: build/logout-synth
    resolved_intent: synthetic-logout
    resumable: true
  - id: signup-synth
    command_argv: [build, --fast, synthetic-signup]
    branch: build/signup-synth
    resolved_intent: synthetic-signup
    resumable: true
"""
    (repo / ".otto-queue.yml").write_text(queue_yml)

    # Hand-craft state.json marking both as done
    state = {
        "schema_version": 1,
        "tasks": {
            "logout-synth": {
                "status": "done",
                "exit_status": "success",
                "manifest_path": str(repo / "otto_logs" / "queue" / "logout-synth" / "manifest.json"),
            },
            "signup-synth": {
                "status": "done",
                "exit_status": "success",
                "manifest_path": str(repo / "otto_logs" / "queue" / "signup-synth" / "manifest.json"),
            },
        },
    }
    (repo / ".otto-queue-state.json").write_text(json.dumps(state, indent=2))

    # Hand-craft per-task PoW JSON. STORIES SPAN ALL THREE MODULES.
    # The point: when merging logout-synth + signup-synth (which only touch
    # auth.py + test_auth.py), the cert should mark notes/search stories
    # as SKIPPED and only test the auth ones.
    stories_logout = [
        {
            "story_id": "auth-logout-works",
            "name": "logout returns goodbye message",
            "description": "auth.logout('alice') returns 'goodbye, alice'",
            "summary": "logout helper",
            "passed": True,
            "verdict": "PASS",
            "evidence": "",
        },
        {
            "story_id": "notes-format-default",
            "name": "format_note default prefix",
            "description": "notes.format_note('hi') returns '> hi'",
            "summary": "default format",
            "passed": True,
            "verdict": "PASS",
            "evidence": "",
        },
        {
            "story_id": "search-find-substring",
            "name": "find returns matches",
            "description": "search.find(['a','ab','b'], 'a') returns ['a','ab']",
            "summary": "substring search",
            "passed": True,
            "verdict": "PASS",
            "evidence": "",
        },
    ]
    stories_signup = [
        {
            "story_id": "auth-signup-works",
            "name": "signup returns expected dict",
            "description": "auth.signup('alice','pw') returns {user, password_set}",
            "summary": "signup helper",
            "passed": True,
            "verdict": "PASS",
            "evidence": "",
        },
        {
            "story_id": "notes-format-custom-prefix",
            "name": "format_note custom prefix",
            "description": "notes.format_note('x', prefix='# ') returns '# x'",
            "summary": "custom prefix",
            "passed": True,
            "verdict": "PASS",
            "evidence": "",
        },
        {
            "story_id": "search-find-empty",
            "name": "find on empty list",
            "description": "search.find([], 'x') returns []",
            "summary": "empty input",
            "passed": True,
            "verdict": "PASS",
            "evidence": "",
        },
    ]

    pow_logout = repo / "otto_logs" / "queue" / "logout-synth" / "proof-of-work.json"
    pow_signup = repo / "otto_logs" / "queue" / "signup-synth" / "proof-of-work.json"
    _write_pow(pow_logout, stories_logout)
    _write_pow(pow_signup, stories_signup)
    _write_manifest(
        repo / "otto_logs" / "queue" / "logout-synth" / "manifest.json",
        task_id="logout-synth", branch="build/logout-synth", pow_path=pow_logout,
    )
    _write_manifest(
        repo / "otto_logs" / "queue" / "signup-synth" / "manifest.json",
        task_id="signup-synth", branch="build/signup-synth", pow_path=pow_signup,
    )

    return repo


def _sum_cost(repo: Path) -> float:
    return merge_cost_from_state_dir(repo / "otto_logs" / "merge")


def _read_latest_merge_state(repo: Path) -> dict | None:
    merges = sorted((repo / "otto_logs" / "merge").glob("merge-*/state.json"))
    if not merges:
        return None
    try:
        return json.loads(merges[-1].read_text())
    except Exception:
        return None


def main() -> int:
    name = "synthetic-skip"
    require_real_cost_opt_in("synthetic skip benchmark")
    log(f"Running {name}: deterministic test of SKIPPED in post-merge cert")
    t0 = time.time()

    try:
        repo = _build_synthetic_repo()

        log("phase 2: otto merge logout-synth signup-synth (with cert)")
        merge_t0 = time.time()
        r = subprocess.run(
            [str(OTTO_BIN), "merge", "logout-synth", "signup-synth"],
            cwd=repo, capture_output=True, text=True, timeout=3600,
            env={**os.environ},
        )
        merge_seconds = time.time() - merge_t0
        out = (r.stdout or "") + (r.stderr or "")
        log(f"merge done in {merge_seconds:.0f}s, rc={r.returncode}")

        merge_state = _read_latest_merge_state(repo) or {}
        cert_run_id = merge_state.get("cert_run_id")
        cert_passed = merge_state.get("cert_passed")
        log(f"cert_passed={cert_passed}  cert_run_id={cert_run_id}")

        verdict_counts: dict[str, int] = {}
        skipped_stories: list[dict] = []
        all_stories: list[dict] = []
        if cert_run_id:
            pow_file = proof_of_work_path(repo, cert_run_id)
            if pow_file.exists():
                try:
                    pow_data = json.loads(pow_file.read_text())
                    for s in pow_data.get("stories", []):
                        v = s.get("verdict") or ("PASS" if s.get("passed") else "FAIL")
                        verdict_counts[v] = verdict_counts.get(v, 0) + 1
                        all_stories.append(s)
                        if v == "SKIPPED":
                            skipped_stories.append(s)
                except Exception as exc:
                    log(f"could not read PoW: {exc}")
        log(f"verdict counts: {verdict_counts or '(no cert PoW found)'}")
        log(f"SKIPPED count: {len(skipped_stories)}")
        for s in skipped_stories:
            log(f"  - SKIPPED: {s.get('story_id') or s.get('name')} | {s.get('summary', '')}")

        diff_files: list[str] = []
        target_head = merge_state.get("target_head_before") or ""
        if target_head:
            d = subprocess.run(
                ["git", "diff", "--name-only", target_head, "HEAD"],
                cwd=repo, capture_output=True, text=True, check=False,
            )
            diff_files = [f for f in d.stdout.splitlines() if f]
        log(f"merge diff files: {diff_files}")

        # Success criterion: cert ran AND emitted at least one SKIPPED verdict
        # for stories about notes/search (files not in the merge diff).
        bench_passed = (
            r.returncode == 0
            and cert_run_id is not None
            and len(all_stories) > 0
            and len(skipped_stories) >= 1
        )
        if bench_passed:
            log(f"✓ BENCH PASSED — cert emitted {len(skipped_stories)} SKIPPED verdict(s)")
        else:
            log(f"✗ BENCH FAILED — expected ≥1 SKIPPED, got {len(skipped_stories)}")

        merge_cost = _sum_cost(repo)
        res = BenchResult(
            name=name,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            wall_seconds=time.time() - t0,
            total_cost_usd=merge_cost,
            queue_concurrency=0,
            tasks=[],
            merge_outcome="success" if r.returncode == 0 else "failed",
            merge_cost_usd=merge_cost,
            merge_seconds=merge_seconds,
            cert_passed=cert_passed,
            notes=[
                f"BENCH OUTCOME: {'PASSED' if bench_passed else 'FAILED'}",
                f"cert_run_id: {cert_run_id}",
                f"verdict counts: {verdict_counts}",
                f"SKIPPED stories: {[s.get('story_id') for s in skipped_stories]}",
                f"all stories ({len(all_stories)}): {[s.get('story_id') for s in all_stories]}",
                f"merge diff files: {diff_files}",
                f"merge log tail: {out.strip()[-400:]}",
            ],
            repo_path=str(repo),
        )
        out_path = RESULTS_DIR / f"{name}.json"
        res.write(out_path)
        log(f"wrote {out_path}")
        print()
        print(res.short_summary())
        return 0 if bench_passed else 1
    except Exception as exc:
        log(f"bench crashed: {exc}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
