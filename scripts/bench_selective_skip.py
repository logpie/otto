"""E2E bench for selective story skipping in post-merge cert.

The merge_context preamble's whole point is to let the cert agent SKIP
stories whose feature lives in files the merge didn't touch. This bench
constructs a scenario where SKIPPED should fire:

  Phase 1: build BASE — multi-module product (auth.py + notes.py).
           Cert registers stories for both modules. Auto-merge to main.

  Phase 2: queue 2 incremental builds (B + C), each adding a different
           function to auth.py only (notes.py untouched).
           Both certs register auth + notes stories (cert tests the full
           product, not just the changed bit).

  Phase 3: merge B + C → conflict on auth.py → resolved by agent.
           Merge diff: auth.py + maybe its test file. notes.py NOT touched.

  Phase 4: post-merge cert sees the union of B + C's stories. Stories about
           auth.py → in merge diff → cert tests them (PASS/FAIL).
           Stories about notes.py → NOT in merge diff → cert should emit
           SKIPPED with reason "no overlap with merge diff".

We assert that at least 1 story comes back as SKIPPED. If 0 SKIPPED, the
preamble isn't influencing the cert agent (or the bench's intent design
failed to produce a notes story — re-tune intents).

Usage: .venv/bin/python scripts/bench_selective_skip.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_runner import (
    BenchResult,
    OTTO_BIN,
    RESULTS_DIR,
    log,
    make_repo,
    otto_run,
    queue_state,
    start_watcher,
    stop_watcher,
    wait_for_all_done,
)


INTENT_BASE = (
    "Build a Python package with TWO files (PURE FUNCTIONS — NO file I/O, "
    "NO disk writes, NO subprocess): "
    "(1) `auth.py` with a function `login(user, password)` that returns True "
    "if user=='admin' AND password=='secret', else False. "
    "(2) `notes.py` with a function `format_note(text, prefix='> ')` that "
    "returns prefix + text (e.g. format_note('hi') returns '> hi'). "
    "Add `test_auth.py` (asserts login('admin','secret') is True and "
    "login('x','y') is False) and `test_notes.py` (asserts format_note('hi') "
    "== '> hi' and format_note('x', prefix='# ') == '# x'). "
    "All 4 files at the repo root. Tests must NOT create files or directories."
)

INTENT_B = (
    "Modify ONLY `auth.py`: add a function `logout(user)` that returns the "
    "string 'goodbye, ' + user. Add a test in `test_auth.py` for it. "
    "Do NOT modify notes.py or test_notes.py."
)

INTENT_C = (
    "Modify ONLY `auth.py`: add a function `signup(user, password)` that "
    "returns the dict {'user': user, 'password_set': bool(password)}. Add "
    "a test in `test_auth.py` for it. Do NOT modify notes.py or test_notes.py."
)


def _sum_cost(repo: Path) -> float:
    cost_re = re.compile(r"cost \$(\d+(?:\.\d+)?)")
    total = 0.0
    for state_file in (repo / "otto_logs" / "merge").glob("merge-*/state.json"):
        try:
            d = json.loads(state_file.read_text())
        except Exception:
            continue
        seen: set[str] = set()
        for o in d.get("outcomes", []):
            note = o.get("note") or ""
            if not note or note in seen:
                continue
            m = cost_re.search(note)
            if m:
                total += float(m.group(1))
                seen.add(note)
    return total


def _read_latest_merge_state(repo: Path) -> dict | None:
    merges = sorted((repo / "otto_logs" / "merge").glob("merge-*/state.json"))
    if not merges:
        return None
    try:
        return json.loads(merges[-1].read_text())
    except Exception:
        return None


def _wait_for_done(repo: Path, label: str) -> dict:
    log(f"watcher dispatching: {label}")
    w = start_watcher(repo, concurrent=2)
    try:
        statuses = wait_for_all_done(repo, timeout=900, interval=15)
        log(f"phase done: {statuses}")
        return statuses
    finally:
        stop_watcher(w)


def main() -> int:
    name = "selective-skip"
    log(f"Running {name}: validate cert SKIPPED on merge_context preamble")
    repo = make_repo("bench-selective-skip-")
    log(f"repo: {repo}")
    t0 = time.time()

    try:
        # ---- Phase 1: build base (multi-module) ----
        log("phase 1: build base (auth.py + notes.py)")
        otto_run(repo, "queue", "build", "--as", "base", "--", "--fast", INTENT_BASE)
        _wait_for_done(repo, "base build")

        # Merge base into main so subsequent builds branch off a tree containing notes.py.
        log("phase 1b: merge base into main")
        m1 = subprocess.run(
            [str(OTTO_BIN), "merge", "base", "--no-certify"],
            cwd=repo, capture_output=True, text=True, timeout=600,
            env={**os.environ},
        )
        log(f"base merge: rc={m1.returncode}")
        if m1.returncode != 0:
            log(f"merge stderr: {m1.stderr[-500:]}")
            return 1

        # ---- Phase 2: queue 2 incremental builds, both modify auth.py only ----
        log("phase 2: queue 2 incremental builds (both touch auth.py only)")
        otto_run(repo, "queue", "build", "--as", "logout", "--", "--fast", INTENT_B)
        otto_run(repo, "queue", "build", "--as", "signup", "--", "--fast", INTENT_C)
        _wait_for_done(repo, "logout + signup builds")

        # ---- Phase 3: merge B + C with cert ----
        state = queue_state(repo)
        done_ids: list[str] = []
        from otto.queue.schema import load_queue
        for t in load_queue(repo):
            ts = state.get("tasks", {}).get(t.id, {})
            if t.id != "base" and ts.get("status") == "done" and t.branch:
                done_ids.append(t.id)
        log(f"merging task ids: {done_ids}")
        if len(done_ids) < 2:
            log(f"expected 2 incremental builds done, got {done_ids} — bench inconclusive")
            return 1

        log("phase 3: otto merge logout signup (with cert — exercises merge_context preamble)")
        merge_t0 = time.time()
        r = subprocess.run(
            [str(OTTO_BIN), "merge", *done_ids],
            cwd=repo, capture_output=True, text=True, timeout=3600,
            env={**os.environ},
        )
        merge_seconds = time.time() - merge_t0
        out = (r.stdout or "") + (r.stderr or "")
        log(f"merge done in {merge_seconds:.0f}s, rc={r.returncode}")

        # ---- Phase 4: inspect cert verdicts ----
        merge_state = _read_latest_merge_state(repo) or {}
        cert_run_id = merge_state.get("cert_run_id")
        cert_passed = merge_state.get("cert_passed")
        log(f"cert_passed={cert_passed}  cert_run_id={cert_run_id}")

        verdict_counts: dict[str, int] = {}
        skipped_stories: list[dict] = []
        all_stories: list[dict] = []
        if cert_run_id:
            pow_file = repo / "otto_logs" / "certifier" / cert_run_id / "proof-of-work.json"
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

        # Diff that the cert agent saw — for evidence in the result file
        diff_files = []
        try:
            d = subprocess.run(
                ["git", "diff", "--name-only", merge_state.get("target_head_before", ""),
                 "HEAD"],
                cwd=repo, capture_output=True, text=True, check=False,
            )
            diff_files = [f for f in d.stdout.splitlines() if f]
        except Exception:
            pass

        # ---- Verdict on the bench itself ----
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
            log(f"  rc={r.returncode}, cert_run_id={cert_run_id}, total stories={len(all_stories)}")

        merge_cost = _sum_cost(repo)
        total_cost = merge_cost
        for tid, ts in state.get("tasks", {}).items():
            total_cost += float(ts.get("cost_usd") or 0.0)

        res = BenchResult(
            name=name,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            wall_seconds=time.time() - t0,
            total_cost_usd=total_cost,
            queue_concurrency=2,
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
                f"merged task ids: {done_ids}",
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
