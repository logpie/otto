#!/usr/bin/env python3
"""i2p benchmark: otto build vs bare CC.

Usage:
    python bench/i2p-bench/run.py                  # all intents, both paths
    python bench/i2p-bench/run.py --otto           # otto only
    python bench/i2p-bench/run.py --bare           # bare CC only
    python bench/i2p-bench/run.py --intent cli-simple  # single intent
    python bench/i2p-bench/run.py --certify-only /path/to/project  # just certify an existing project
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
    # Create otto.yaml
    from otto.config import create_config
    create_config(project_dir)
    return project_dir


def run_otto(intent_id: str, intent_text: str) -> dict:
    """Run otto build (v3 agentic) and return results."""
    from otto.config import load_config
    from otto.pipeline import build_agentic_v3

    project_dir = _create_project(f"otto-{intent_id}")
    config = load_config(project_dir / "otto.yaml")

    start = time.time()
    try:
        result = asyncio.run(build_agentic_v3(intent_text, project_dir, config))
        passed = result.passed
        cost = result.total_cost
        stories_passed = result.tasks_passed
        stories_failed = result.tasks_failed
        journeys = result.journeys
    except Exception as e:
        print(f"    otto error: {e}")
        passed = False
        cost = 0.0
        stories_passed = 0
        stories_failed = 0
        journeys = []
    elapsed = time.time() - start

    # Parse PoW for additional data
    pow_data = {}
    pow_path = project_dir / "otto_logs" / "certifier" / "proof-of-work.json"
    if pow_path.exists():
        try:
            pow_data = json.loads(pow_path.read_text())
        except Exception:
            pass

    stories_tested = stories_passed + stories_failed

    return {
        "runner": "otto",
        "intent_id": intent_id,
        "passed": passed,
        "stories_tested": stories_tested,
        "stories_passed": stories_passed,
        "cost_usd": round(cost, 2),
        "duration_s": round(elapsed, 1),
        "certify_rounds": pow_data.get("certify_rounds", 1),
        "stories": pow_data.get("stories", journeys),
        "project_dir": str(project_dir),
    }


def run_bare_cc(intent_id: str, intent_text: str) -> dict:
    """Run bare Claude Code and return results."""
    project_dir = _create_project(f"bare-{intent_id}")

    # Bare CC prompt — same intent, no otto structure
    prompt = (
        f"Build this product from scratch in this directory. "
        f"Write tests and make them pass. Commit when done.\n\n{intent_text}"
    )

    start = time.time()
    from otto.agent import ClaudeAgentOptions, _subprocess_env, run_agent_query

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=str(project_dir),
        system_prompt={"type": "preset", "preset": "claude_code"},
        env=_subprocess_env(),
        setting_sources=["project"],
    )

    text, cost, result_msg = asyncio.run(run_agent_query(prompt, options))
    build_elapsed = time.time() - start

    # Now run the SAME certifier against the bare CC product
    certify_start = time.time()
    from otto.certifier import run_agentic_certifier
    report = asyncio.run(run_agentic_certifier(
        intent=intent_text,
        project_dir=project_dir,
    ))
    certify_elapsed = time.time() - certify_start

    # Parse stories from report
    story_results = getattr(report, "_story_results", [])

    return {
        "runner": "bare-cc",
        "intent_id": intent_id,
        "passed": report.outcome.value == "passed",
        "stories_tested": len(story_results),
        "stories_passed": sum(1 for s in story_results if s.get("passed")),
        "cost_usd": round(float(cost or 0) + report.cost_usd, 2),
        "build_cost": round(float(cost or 0), 2),
        "certify_cost": round(report.cost_usd, 2),
        "duration_s": round(build_elapsed + certify_elapsed, 1),
        "build_duration_s": round(build_elapsed, 1),
        "certify_duration_s": round(certify_elapsed, 1),
        "stories": story_results,
        "project_dir": str(project_dir),
    }


def certify_existing(project_dir: str, intent_text: str) -> dict:
    """Run certifier on an existing project."""
    from otto.certifier import run_agentic_certifier

    start = time.time()
    report = asyncio.run(run_agentic_certifier(
        intent=intent_text,
        project_dir=Path(project_dir),
    ))
    elapsed = time.time() - start

    story_results = getattr(report, "_story_results", [])
    return {
        "runner": "certify-only",
        "passed": report.outcome.value == "passed",
        "stories_tested": len(story_results),
        "stories_passed": sum(1 for s in story_results if s.get("passed")),
        "cost_usd": round(report.cost_usd, 2),
        "duration_s": round(elapsed, 1),
        "stories": story_results,
    }


def print_comparison(results: list[dict]) -> None:
    """Print a comparison table."""
    print("\n" + "=" * 80)
    print("BENCHMARK RESULTS")
    print("=" * 80)

    # Group by intent
    by_intent: dict[str, dict[str, dict]] = {}
    for r in results:
        iid = r["intent_id"]
        runner = r["runner"]
        if iid not in by_intent:
            by_intent[iid] = {}
        by_intent[iid][runner] = r

    # Header
    print(f"\n{'Intent':<20} {'Runner':<10} {'Pass?':<6} {'Stories':<10} {'Cost':>8} {'Time':>8}")
    print("-" * 70)

    for iid, runners in sorted(by_intent.items()):
        for runner_name, r in sorted(runners.items()):
            passed = "PASS" if r["passed"] else "FAIL"
            stories = f"{r['stories_passed']}/{r['stories_tested']}"
            cost = f"${r['cost_usd']:.2f}"
            time_s = f"{r['duration_s']:.0f}s"
            print(f"{iid:<20} {runner_name:<10} {passed:<6} {stories:<10} {cost:>8} {time_s:>8}")
        print()

    # Summary
    otto_results = [r for r in results if r["runner"] == "otto"]
    bare_results = [r for r in results if r["runner"] == "bare-cc"]

    if otto_results and bare_results:
        print("-" * 70)
        otto_pass = sum(1 for r in otto_results if r["passed"])
        bare_pass = sum(1 for r in bare_results if r["passed"])
        otto_cost = sum(r["cost_usd"] for r in otto_results)
        bare_cost = sum(r["cost_usd"] for r in bare_results)
        print(f"{'TOTAL':<20} {'otto':<10} {otto_pass}/{len(otto_results):<5} {'':10} ${otto_cost:>7.2f}")
        print(f"{'TOTAL':<20} {'bare-cc':<10} {bare_pass}/{len(bare_results):<5} {'':10} ${bare_cost:>7.2f}")


def main():
    parser = argparse.ArgumentParser(description="i2p benchmark: otto vs bare CC")
    parser.add_argument("--otto", action="store_true", help="Run otto only")
    parser.add_argument("--bare", action="store_true", help="Run bare CC only")
    parser.add_argument("--intent", help="Run single intent by ID")
    parser.add_argument("--certify-only", help="Certify existing project dir")
    parser.add_argument("--certify-intent", help="Intent text for --certify-only")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.certify_only:
        if not args.certify_intent:
            print("--certify-only requires --certify-intent")
            sys.exit(1)
        result = certify_existing(args.certify_only, args.certify_intent)
        print(json.dumps(result, indent=2, default=str))
        return

    # Select intents
    intents = INTENTS
    if args.intent:
        intents = [i for i in INTENTS if i["id"] == args.intent]
        if not intents:
            print(f"Unknown intent: {args.intent}. Available: {[i['id'] for i in INTENTS]}")
            sys.exit(1)

    # Select runners
    run_otto_flag = not args.bare
    run_bare_flag = not args.otto

    all_results = []
    for intent in intents:
        print(f"\n{'='*60}")
        print(f"Intent: {intent['id']} ({intent['name']})")
        print(f"{'='*60}")

        if run_otto_flag:
            print(f"\n  Running otto...")
            r = run_otto(intent["id"], intent["intent"])
            all_results.append(r)
            print(f"  otto: {'PASS' if r['passed'] else 'FAIL'} "
                  f"({r['stories_passed']}/{r['stories_tested']} stories, "
                  f"${r['cost_usd']:.2f}, {r['duration_s']:.0f}s)")

        if run_bare_flag:
            print(f"\n  Running bare CC...")
            r = run_bare_cc(intent["id"], intent["intent"])
            all_results.append(r)
            print(f"  bare: {'PASS' if r['passed'] else 'FAIL'} "
                  f"({r['stories_passed']}/{r['stories_tested']} stories, "
                  f"${r['cost_usd']:.2f}, {r['duration_s']:.0f}s)")

    # Save results
    ts = time.strftime("%Y%m%d-%H%M%S")
    results_file = RESULTS_DIR / f"bench-{ts}.json"
    results_file.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nResults saved to {results_file}")

    # Print comparison
    print_comparison(all_results)


if __name__ == "__main__":
    main()
