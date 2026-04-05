#!/usr/bin/env python3
"""Certifier benchmark — run on any machine, report time/cost/pass-fail.
Usage: python scripts/bench-certifier.py [smoke|parallel|browser|all]
"""
import subprocess, sys, time, json
from pathlib import Path

PROJECT = Path("bench/certifier-stress-test/task-manager")
INTENT = "task manager with user auth, CRUD tasks with title/description/status/due date, user isolation, filter by status, sort by due date"

def kill_stale():
    subprocess.run(["pkill", "-f", "task-manager"], capture_output=True)
    subprocess.run(["pkill", "-f", "next dev"], capture_output=True)
    time.sleep(2)

def run_certifier(name, config, skip_ids=None):
    from otto.certifier import run_unified_certifier
    kill_stale()
    print(f"\n{'='*60}\n{name}\n{'='*60}")
    start = time.monotonic()
    report = run_unified_certifier(
        intent=INTENT, project_dir=PROJECT, config=config,
        skip_story_ids=skip_ids)
    d = time.monotonic() - start

    tier4 = next((t for t in report.tiers if t.tier == 4), None)
    cr = tier4._cert_result if tier4 and hasattr(tier4, "_cert_result") else None
    stories_passed = cr.stories_passed if cr else 0
    stories_total = cr.stories_tested if cr else 0

    result = {
        "name": name,
        "outcome": report.outcome.value,
        "time_s": round(d, 1),
        "cost_usd": round(report.cost_usd, 2),
        "stories": f"{stories_passed}/{stories_total}",
    }
    print(f"Outcome: {result['outcome']}, {result['time_s']}s, ${result['cost_usd']}")
    if cr:
        for r in cr.results:
            s = "PASS" if r.passed else "FAIL"
            print(f"  [{s}] {r.story_title} ({r.duration_s:.0f}s, ${r.cost_usd:.3f})")

    # Check evidence
    import glob
    evidence_dirs = glob.glob(str(PROJECT / "otto_logs" / "certifier" / "evidence-*"))
    if evidence_dirs:
        for ed in evidence_dirs:
            files = list(Path(ed).iterdir())
            result["evidence_files"] = len(files)
            print(f"  Evidence: {len(files)} files in {Path(ed).name}")

    pow_html = PROJECT / "otto_logs" / "certifier" / "proof-of-work.html"
    if pow_html.exists():
        result["pow_html"] = True
        print(f"  PoW report: {pow_html.stat().st_size:,} bytes")

    kill_stale()
    return result

def load_stories():
    from otto.certifier.stories import load_or_compile_stories
    story_set, _, _, _ = load_or_compile_stories(PROJECT, INTENT, config={})
    return story_set

def smoke():
    stories = load_stories()
    skip = {s.id for s in stories.stories[1:]}
    return run_certifier("Smoke (1 story, no browser)", {
        "certifier_parallel_stories": 1,
        "certifier_skip_break": True,
        "certifier_app_start_timeout": 90,
    }, skip_ids=skip)

def parallel():
    return run_certifier("Parallel (7 stories, parallel=3)", {
        "certifier_parallel_stories": 3,
        "certifier_skip_break": True,
        "certifier_app_start_timeout": 90,
    })

def browser():
    stories = load_stories()
    skip = {s.id for s in stories.stories[1:]}
    return run_certifier("Browser (1 story, browser+video)", {
        "certifier_parallel_stories": 1,
        "certifier_skip_break": True,
        "certifier_browser": True,
        "certifier_app_start_timeout": 90,
    }, skip_ids=skip)

if __name__ == "__main__":
    import platform
    mode = sys.argv[1] if len(sys.argv) > 1 else "smoke"

    print(f"Machine: {platform.node()}")
    print(f"Platform: {platform.system()} {platform.machine()}")
    print(f"Project: {PROJECT}")

    results = []
    if mode in ("smoke", "all"):
        results.append(smoke())
    if mode in ("parallel", "all"):
        results.append(parallel())
    if mode in ("browser", "all"):
        results.append(browser())

    if not results and mode not in ("smoke", "parallel", "browser", "all"):
        print(f"Usage: {sys.argv[0]} [smoke|parallel|browser|all]")
        sys.exit(1)

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY — {platform.node()}")
    print(f"{'='*60}")
    print(f"{'Test':<35} {'Time':>8} {'Cost':>8} {'Result':>8}")
    for r in results:
        print(f"{r['name']:<35} {r['time_s']:>6.0f}s ${r['cost_usd']:>6.2f} {r['stories']:>8}")

    # Save results
    out_path = PROJECT / "otto_logs" / f"bench-{platform.node()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "machine": platform.node(),
        "platform": f"{platform.system()} {platform.machine()}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "results": results,
    }, indent=2))
    print(f"\nSaved to {out_path}")
