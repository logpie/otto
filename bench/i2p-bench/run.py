#!/usr/bin/env python3
"""i2p benchmark: otto build vs bare CC.

Fair comparison: both products tested against the SAME fixed stories.
Stories are defined per intent, not generated per-run.

Usage:
    python bench/i2p-bench/run.py                      # all intents, both paths
    python bench/i2p-bench/run.py --otto               # otto only
    python bench/i2p-bench/run.py --bare               # bare CC only
    python bench/i2p-bench/run.py --intent cli-simple  # single intent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Add otto to path
OTTO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(OTTO_ROOT))

from intents import INTENTS

RESULTS_DIR = Path(__file__).parent / "results"

# Fixed-story certifier prompt — tests exact stories, no planning
FIXED_CERTIFIER_PROMPT = """\
You are a QA tester. Test this product against the exact stories below.
Do NOT plan your own stories — test ONLY what is listed.

Product intent: {intent}

Stories to test:
{stories_text}

For EACH story:
1. Run the commands described. Use curl for HTTP, CLI commands for CLI, Python for libraries.
2. Verify the expected behavior.
3. Report PASS or FAIL with the actual commands you ran and their output.

Rules:
- Make REAL requests. Never simulate.
- For failures: report WHAT is wrong (symptom + actual output). No fix suggestions.
- For web apps: also use agent-browser for visual checks if applicable.
- Install dependencies and start the app first if needed.

End with EXACT markers:
STORIES_TESTED: {story_count}
STORIES_PASSED: (count)
STORY_RESULT: (story-id) | PASS or FAIL | (one-line summary)
...
VERDICT: PASS or FAIL
"""


def _format_stories(stories: list[dict]) -> str:
    lines = []
    for s in stories:
        lines.append(f"- **{s['id']}**: {s['test']}")
    return "\n".join(lines)


def _create_project(name: str) -> Path:
    """Create a fresh git repo for a benchmark run."""
    project_dir = RESULTS_DIR / name
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=project_dir, check=True,
    )
    from otto.config import create_config
    create_config(project_dir)
    return project_dir


def _run_fixed_certifier(
    intent_text: str,
    stories: list[dict],
    project_dir: Path,
) -> dict:
    """Run certifier with fixed stories against a product. Returns parsed results."""
    from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query

    stories_text = _format_stories(stories)
    story_ids = [s["id"] for s in stories]
    prompt = FIXED_CERTIFIER_PROMPT.format(
        intent=intent_text,
        stories_text=stories_text,
        story_count=len(stories),
    )

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
        env=_subprocess_env(),
        setting_sources=["project"],
    )

    start = time.time()
    text, cost, _ = asyncio.run(run_agent_query(prompt, options))
    elapsed = time.time() - start

    # Parse STORY_RESULT markers
    story_results = []
    verdict_pass = False
    if text:
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("STORY_RESULT:"):
                parts = stripped[len("STORY_RESULT:"):].strip().split("|")
                if len(parts) >= 2:
                    sid = parts[0].strip()
                    passed = "PASS" in parts[1].upper()
                    summary = parts[2].strip() if len(parts) > 2 else ""
                    story_results.append({"story_id": sid, "passed": passed, "summary": summary})
            elif stripped.startswith("VERDICT:"):
                verdict_pass = "PASS" in stripped.upper()

    tested = len(story_results)
    passed_count = sum(1 for s in story_results if s["passed"])

    return {
        "passed": verdict_pass and all(s["passed"] for s in story_results),
        "stories_tested": tested,
        "stories_passed": passed_count,
        "stories_expected": len(stories),
        "cost_usd": round(float(cost or 0), 2),
        "duration_s": round(elapsed, 1),
        "story_results": story_results,
        "missing_stories": [sid for sid in story_ids if sid not in {s["story_id"] for s in story_results}],
    }


def run_otto(intent: dict) -> dict:
    """Run otto build then certify with fixed stories."""
    from otto.config import load_config
    from otto.pipeline import build_agentic_v3

    intent_id = intent["id"]
    intent_text = intent["intent"]
    stories = intent["stories"]
    project_dir = _create_project(f"otto-{intent_id}")
    config = load_config(project_dir / "otto.yaml")

    # Build with otto
    build_start = time.time()
    try:
        result = asyncio.run(build_agentic_v3(intent_text, project_dir, config))
        build_cost = result.total_cost
    except Exception as e:
        print(f"    otto build error: {e}")
        return {
            "runner": "otto", "intent_id": intent_id,
            "passed": False, "stories_tested": 0, "stories_passed": 0,
            "cost_usd": 0, "duration_s": 0, "project_dir": str(project_dir),
            "error": str(e),
        }
    build_elapsed = time.time() - build_start

    # Certify with fixed stories (same stories as bare CC will get)
    certify = _run_fixed_certifier(intent_text, stories, project_dir)

    return {
        "runner": "otto",
        "intent_id": intent_id,
        "passed": certify["passed"],
        "stories_tested": certify["stories_tested"],
        "stories_passed": certify["stories_passed"],
        "stories_expected": certify["stories_expected"],
        "missing_stories": certify["missing_stories"],
        "build_cost": round(build_cost, 2),
        "certify_cost": certify["cost_usd"],
        "cost_usd": round(build_cost + certify["cost_usd"], 2),
        "build_duration_s": round(build_elapsed, 1),
        "certify_duration_s": certify["duration_s"],
        "duration_s": round(build_elapsed + certify["duration_s"], 1),
        "story_results": certify["story_results"],
        "project_dir": str(project_dir),
    }


def run_bare_cc(intent: dict) -> dict:
    """Run bare CC then certify with fixed stories."""
    from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query

    intent_id = intent["id"]
    intent_text = intent["intent"]
    stories = intent["stories"]
    project_dir = _create_project(f"bare-{intent_id}")

    prompt = (
        f"Build this product from scratch in this directory. "
        f"Write tests and make them pass. Commit when done.\n\n{intent_text}"
    )

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
        env=_subprocess_env(),
        setting_sources=["project"],
    )

    build_start = time.time()
    _, build_cost, _ = asyncio.run(run_agent_query(prompt, options))
    build_elapsed = time.time() - build_start
    build_cost = float(build_cost or 0)

    # Certify with same fixed stories
    certify = _run_fixed_certifier(intent_text, stories, project_dir)

    return {
        "runner": "bare-cc",
        "intent_id": intent_id,
        "passed": certify["passed"],
        "stories_tested": certify["stories_tested"],
        "stories_passed": certify["stories_passed"],
        "stories_expected": certify["stories_expected"],
        "missing_stories": certify["missing_stories"],
        "build_cost": round(build_cost, 2),
        "certify_cost": certify["cost_usd"],
        "cost_usd": round(build_cost + certify["cost_usd"], 2),
        "build_duration_s": round(build_elapsed, 1),
        "certify_duration_s": certify["duration_s"],
        "duration_s": round(build_elapsed + certify["duration_s"], 1),
        "story_results": certify["story_results"],
        "project_dir": str(project_dir),
    }


def print_comparison(results: list[dict]) -> None:
    """Print a comparison table."""
    print("\n" + "=" * 90)
    print("BENCHMARK RESULTS")
    print("=" * 90)

    by_intent: dict[str, dict[str, dict]] = {}
    for r in results:
        iid = r["intent_id"]
        runner = r["runner"]
        if iid not in by_intent:
            by_intent[iid] = {}
        by_intent[iid][runner] = r

    print(f"\n{'Intent':<20} {'Runner':<10} {'Pass?':<6} {'Stories':<12} {'Build$':>8} {'Cert$':>8} {'Total$':>8} {'Time':>8}")
    print("-" * 90)

    for iid, runners in sorted(by_intent.items()):
        for rname, r in sorted(runners.items()):
            p = "PASS" if r["passed"] else "FAIL"
            stories = f"{r['stories_passed']}/{r['stories_expected']}"
            bc = f"${r.get('build_cost', 0):.2f}"
            cc = f"${r.get('certify_cost', 0):.2f}"
            tc = f"${r['cost_usd']:.2f}"
            t = f"{r['duration_s']:.0f}s"
            print(f"{iid:<20} {rname:<10} {p:<6} {stories:<12} {bc:>8} {cc:>8} {tc:>8} {t:>8}")

            # Show per-story results for failures
            if not r["passed"]:
                for sr in r.get("story_results", []):
                    if not sr["passed"]:
                        print(f"{'':>20} {'':>10} {'FAIL':<6}   {sr['story_id']}: {sr.get('summary', '')[:50]}")
        print()

    # Summary
    otto_r = [r for r in results if r["runner"] == "otto"]
    bare_r = [r for r in results if r["runner"] == "bare-cc"]
    if otto_r and bare_r:
        print("-" * 90)
        op = sum(1 for r in otto_r if r["passed"])
        bp = sum(1 for r in bare_r if r["passed"])
        oc = sum(r["cost_usd"] for r in otto_r)
        bc = sum(r["cost_usd"] for r in bare_r)
        os_pass = sum(r["stories_passed"] for r in otto_r)
        os_total = sum(r["stories_expected"] for r in otto_r)
        bs_pass = sum(r["stories_passed"] for r in bare_r)
        bs_total = sum(r["stories_expected"] for r in bare_r)
        print(f"{'TOTAL':<20} {'otto':<10} {op}/{len(otto_r):<5} {os_pass}/{os_total:<11} {'':>8} {'':>8} ${oc:>7.2f}")
        print(f"{'TOTAL':<20} {'bare-cc':<10} {bp}/{len(bare_r):<5} {bs_pass}/{bs_total:<11} {'':>8} {'':>8} ${bc:>7.2f}")


def main():
    parser = argparse.ArgumentParser(description="i2p benchmark: otto vs bare CC")
    parser.add_argument("--otto", action="store_true", help="Run otto only")
    parser.add_argument("--bare", action="store_true", help="Run bare CC only")
    parser.add_argument("--intent", help="Run single intent by ID")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    intents = INTENTS
    if args.intent:
        intents = [i for i in INTENTS if i["id"] == args.intent]
        if not intents:
            print(f"Unknown intent: {args.intent}. Available: {[i['id'] for i in INTENTS]}")
            sys.exit(1)

    run_otto_flag = not args.bare
    run_bare_flag = not args.otto

    all_results = []
    for intent in intents:
        print(f"\n{'='*60}")
        print(f"Intent: {intent['id']} ({intent['name']}) — {len(intent['stories'])} stories")
        print(f"{'='*60}")

        if run_otto_flag:
            print(f"\n  Running otto build + fixed certify...")
            r = run_otto(intent)
            all_results.append(r)
            print(f"  otto: {'PASS' if r['passed'] else 'FAIL'} "
                  f"({r['stories_passed']}/{r['stories_expected']} stories, "
                  f"${r['cost_usd']:.2f}, {r['duration_s']:.0f}s)")

        if run_bare_flag:
            print(f"\n  Running bare CC build + fixed certify...")
            r = run_bare_cc(intent)
            all_results.append(r)
            print(f"  bare: {'PASS' if r['passed'] else 'FAIL'} "
                  f"({r['stories_passed']}/{r['stories_expected']} stories, "
                  f"${r['cost_usd']:.2f}, {r['duration_s']:.0f}s)")

    # Save
    ts = time.strftime("%Y%m%d-%H%M%S")
    results_file = RESULTS_DIR / f"bench-{ts}.json"
    results_file.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nResults saved to {results_file}")

    print_comparison(all_results)


if __name__ == "__main__":
    main()
