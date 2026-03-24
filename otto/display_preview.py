"""Generate an HTML preview of otto's display output for visual inspection.

Usage:
    python -m otto.display_preview              # simulate a run and open in browser
    python -m otto.display_preview --save out.html  # save without opening

Renders a simulated otto run through TaskDisplay using Rich's record mode,
exports to HTML with all colors/styles intact, then optionally screenshots
via chrome-devtools MCP.
"""

import time
from pathlib import Path

from rich.console import Console

from otto.display import TaskDisplay, rich_escape


def simulate_run(console: Console) -> None:
    """Simulate a realistic otto v4.5 run through TaskDisplay."""
    display = TaskDisplay(console)

    # Header
    console.print()
    console.print("  [bold]1 task[/bold]  [dim](v4.5 PER)[/dim]")
    console.print("    [dim]○[/dim] [bold]#1[/bold]  Add a weather safety index panel  [dim](spec at runtime)[/dim]")
    console.print()
    console.print("─" * 60, style="dim")
    console.print("  Planning...", style="dim")
    console.print("  [dim]Plan: 1 batch(es), 1 tasks[/dim]")
    console.print()
    console.print("  [bold]Batch 1[/bold]  [dim]1 task[/dim]")
    console.print()
    console.print("  ● [bold]Running[/bold]  [dim]#1  abc12345[/dim]")

    # Prepare
    display.update_phase("prepare", "done", time_s=15.0, detail="baseline: 109 tests passing")

    # Spec gen start
    display.update_phase("spec_gen", "running")

    # Coding start
    display.update_phase("coding", "running", detail="bare CC")

    # Tool calls during coding
    display.add_tool(name="Bash", detail="find src -type f -name '*.tsx' | sort")
    display.add_tool(name="Read", detail="src/types/weather.ts")
    display.add_tool(name="Read", detail="src/components/WeatherApp.tsx")
    display.add_tool(name="Read", detail="src/components/WeatherDetails.tsx")
    console.print("      [dim]... explored 8 files[/dim]")

    display.add_tool(name="Write", detail="src/components/SafetyPanel.tsx",
                     data={"name": "Write", "detail": "src/components/SafetyPanel.tsx",
                           "preview_lines": ['"use client";', "",
                                             "import { useMemo } from 'react';"],
                           "total_lines": 120})

    display.add_tool(name="Edit", detail="src/components/WeatherDetails.tsx",
                     data={"name": "Edit", "detail": "src/components/WeatherDetails.tsx",
                           "old_lines": ["import WindCompass from './WindCompass';"],
                           "new_lines": ["import WindCompass from './WindCompass';",
                                         "import SafetyPanel from './SafetyPanel';"],
                           "old_total": 1, "new_total": 2})

    # Large edit (should show summary)
    display.add_tool(name="Edit", detail="src/components/SunCountdown.tsx",
                     data={"name": "Edit", "detail": "src/components/SunCountdown.tsx",
                           "old_lines": ['"use client";', "",
                                         "import { useState } from 'react';"],
                           "new_lines": ['"use client";', "",
                                         "import { useState, useEffect } from 'react';"],
                           "old_total": 140, "new_total": 244})

    display.add_tool(name="Bash", detail="npx jest --no-coverage 2>&1")

    # Coding done
    display.update_phase("coding", "done", time_s=120.0, cost=0.75)

    # Test
    display.update_phase("test", "done", time_s=5.0, detail="109 passed")

    # Awaiting specs
    console.print(f"  [dim]{_ts()}  ⧗ awaiting specs before QA...[/dim]")

    # Spec gen done
    display.update_phase("spec_gen", "done", time_s=80.0, cost=0.25,
                         detail="12 items (8 must, 4 should)")

    # Spec items
    display.add_spec_item("[must] Safety score displayed as 0-100 integer")
    display.add_spec_item("[must] Color-coded: green (75-100), yellow (50-74), orange (25-49), red (0-24)")
    display.add_spec_item("[must] Risk factor text identifies the biggest concern")
    display.flush_spec_summary()

    # QA start
    display.update_phase("qa", "running", detail="tier 2")

    # QA narration (new feature)
    console.print(f"      [dim]Now let me build the app and test visually.[/dim]")
    console.print(f"      [dim]Build passes. Starting dev server.[/dim]")
    console.print(f"      [bold cyan]● Browser:navigate_page[/bold cyan]  [dim]http://localhost:3000[/dim]")
    console.print(f"      [bold cyan]● Browser:take_screenshot[/bold cyan]")
    console.print(f"      [dim]Verified color thresholds match spec.[/dim]")
    console.print(f"      [bold cyan]● Browser:evaluate_script[/bold cyan]")
    console.print(f"      [dim]All 824 tests pass. Writing verdict.[/dim]")

    # QA results — passed items dim, failed items red
    display.add_qa_item_result("✓ [must] Safety score displayed as 0-100 integer", passed=True)
    display.add_qa_item_result("✓ [must] Color-coded safety level with four tiers", passed=True)
    display.add_qa_item_result("✓ [must] Risk factor text identifies biggest concern", passed=True)
    display.add_qa_item_result("✓ [must] Score clamps to 0-100 range", passed=True)
    display.add_qa_item_result("✓ [must] Card renders in weather details area", passed=True)
    display.add_qa_item_result("✓ [must] Handles missing data gracefully", passed=True)
    display.add_qa_item_result("✓ [must] Multiple factors compound to lower score", passed=True)
    display.add_qa_item_result("✓ [must] Panel visible without user interaction", passed=True)
    display.add_qa_item_result("  [should] 4 items noted", passed=True)

    # QA done
    display.update_phase("qa", "done", time_s=90.0, cost=0.67)

    # Passed
    ts = _ts()
    console.print(f"    {ts}  [green]✓ passed[/green]  [dim]5m35s  $1.67[/dim]")
    console.print(f"      [dim]2 files · 12 specs verified[/dim]")
    console.print(f"  Batch 1: 1 passed, 0 failed")
    console.print()
    console.print(f"  [bold]Run complete[/bold]  [dim]5m35s  $1.67[/dim]")
    console.print()
    console.print(f"  [green]✓[/green] [bold]#1[/bold]  Add a weather safety index panel  [dim]5m35s  $1.67[/dim]")
    console.print()
    console.print(f"  [green bold]1/1 tasks passed[/green bold]")
    console.print()

    # --- Simulate a FAILED run too ---
    console.print()
    console.print("─" * 60, style="dim")
    console.print("  [bold dim]FAILED RUN EXAMPLE:[/bold dim]")
    console.print("─" * 60, style="dim")
    console.print()

    display2 = TaskDisplay(console)
    display2.update_phase("coding", "running", detail="bare CC")
    display2.add_tool(name="Write", detail="src/components/ExercisePanel.tsx",
                      data={"name": "Write", "detail": "src/components/ExercisePanel.tsx",
                            "preview_lines": ["export default function ExercisePanel() {"],
                            "total_lines": 80})
    display2.add_tool(name="Bash", detail="npx jest --no-coverage 2>&1")
    display2.update_phase("coding", "done", time_s=180.0, cost=0.85)
    display2.update_phase("test", "done", time_s=20.0, detail="813 passed")
    display2.update_phase("spec_gen", "done", time_s=100.0, cost=0.30,
                          detail="10 items (6 must, 4 should)")

    display2.update_phase("qa", "running", detail="tier 1")
    console.print(f"      [dim]Testing scoring logic with edge cases.[/dim]")
    console.print(f"      [dim]Running: npx jest --testPathPattern='exercise'[/dim]")

    # QA with failures
    display2.add_qa_item_result("✓ [must] Displays 3 exercises ranked by score", passed=True)
    display2.add_qa_item_result("✓ [must] Scores account for wind and temperature", passed=True)
    display2.add_qa_item_result("✓ [must] Panel renders as a card", passed=True)
    display2.add_qa_item_result("✗ [must] Reason references specific weather factor", passed=False,
                                evidence="scoreRunning(15, 35, 10, 50) → reason says 'calm conditions' but wind is 35 km/h")
    display2.add_qa_item_result("✓ [must] Scores clamp to 0-100", passed=True)
    display2.add_qa_item_result("✗ [must] Tests cover double-complete error case", passed=False,
                                evidence="No test exists for this scenario")
    display2.add_qa_item_result("  [should] 3 items noted", passed=True)

    display2.update_phase("qa", "fail", time_s=120.0,
                          error="2/6 must failed: Reason references specific weather factor")

    # Retry
    display2.add_attempt_boundary(attempt=2, reason="qa: 2 must items failed")
    display2.update_phase("coding", "running", detail="attempt 2 — qa failed")
    display2.add_tool(name="Edit", detail="src/components/ExercisePanel.tsx",
                      data={"name": "Edit", "detail": "src/components/ExercisePanel.tsx",
                            "old_lines": ["if (score >= 70) reason = 'Comfortable temps';"],
                            "new_lines": ["if (wind > 25) reason = 'High wind reduces enjoyment';",
                                          "else if (score >= 70) reason = 'Comfortable temps';"],
                            "old_total": 1, "new_total": 2})
    display2.update_phase("coding", "done", time_s=65.0, cost=0.40)
    display2.update_phase("test", "done", time_s=20.0, detail="827 passed")

    display2.update_phase("qa", "running", detail="tier 2")
    display2.add_qa_item_result("✓ [must] All 6 must items pass", passed=True)
    display2.add_qa_item_result("  [should] 3 items noted", passed=True)
    display2.update_phase("qa", "done", time_s=80.0, cost=0.55)

    ts = _ts()
    console.print(f"    {ts}  [green]✓ passed[/green]  [dim]8m10s  $2.10[/dim]")
    console.print(f"      [dim]3 files · 10 specs verified · 2 attempts[/dim]")
    console.print()


def replay_from_jsonl(console: Console, jsonl_path: str) -> None:
    """Replay a real otto run from pilot_results.jsonl through TaskDisplay."""
    import json

    display = TaskDisplay(console)
    events = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not events:
        console.print("[red]No events found in JSONL file[/red]")
        return

    # Extract task info from first event
    task_key = None
    for e in events:
        if e.get("task_key"):
            task_key = e["task_key"]
            break

    console.print()
    console.print(f"  [bold dim]Replay from:[/bold dim] [dim]{jsonl_path}[/dim]")
    if task_key:
        console.print(f"  [bold dim]Task:[/bold dim] [dim]{task_key}[/dim]")
    console.print()

    for evt in events:
        event_type = evt.get("event", "")

        if event_type == "phase":
            name = evt.get("name", "")
            status = evt.get("status", "")
            time_s = evt.get("time_s", 0.0)
            error = evt.get("error", "")
            detail = evt.get("detail", "")
            cost = evt.get("cost", 0)

            display.update_phase(name, status, time_s=time_s,
                                 error=error, detail=detail, cost=cost)

        elif event_type == "agent_tool":
            display.add_tool(data=evt)

        elif event_type == "agent_tool_result":
            display.add_tool_result(data=evt)

        elif event_type == "qa_finding":
            display.add_finding(evt.get("text", ""))

        elif event_type == "qa_status":
            text = evt.get("text", "")
            if text:
                console.print(f"      [dim]{rich_escape(text[:80])}[/dim]")

        elif event_type == "qa_item_result":
            display.add_qa_item_result(
                text=evt.get("text", ""),
                passed=evt.get("passed", True),
                evidence=evt.get("evidence", ""),
            )

        elif event_type == "qa_summary":
            display.set_qa_summary(
                total=evt.get("total", 0),
                passed=evt.get("passed", 0),
                failed=evt.get("failed", 0),
            )

        elif event_type == "spec_item":
            display.add_spec_item(evt.get("text", ""))

        elif event_type == "spec_items_done":
            display.flush_spec_summary()

        elif event_type == "attempt_boundary":
            display.add_attempt_boundary(
                attempt=evt.get("attempt", 0),
                reason=evt.get("reason", ""),
            )


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def generate_preview(output_path: str | None = None, replay_file: str | None = None) -> Path:
    """Generate HTML preview and return the file path."""
    # Use 256color to match typical terminal rendering (not truecolor which
    # shows more color differentiation than most terminals actually display)
    console = Console(record=True, width=100, color_system="256")

    if replay_file:
        replay_from_jsonl(console, replay_file)
    else:
        simulate_run(console)

    html_content = console.export_html(inline_styles=True)

    # Wrap in dark background to match terminal
    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Otto Display Preview</title>
<style>
body {{
    background: #1e1e2e;
    padding: 20px;
    margin: 0;
    font-family: 'JetBrains Mono', 'Menlo', 'Monaco', monospace;
}}
pre {{
    font-size: 13px;
    line-height: 1.5;
}}
</style>
</head>
<body>
{html_content}
</body>
</html>"""

    path = Path(output_path or "/tmp/otto-display-preview.html")
    path.write_text(full_html)
    return path


if __name__ == "__main__":
    import sys
    out = None
    replay = None

    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        if idx + 1 < len(sys.argv):
            out = sys.argv[idx + 1]

    if "--replay" in sys.argv:
        idx = sys.argv.index("--replay")
        if idx + 1 < len(sys.argv):
            replay = sys.argv[idx + 1]

    path = generate_preview(out, replay_file=replay)
    print(f"Preview saved to: {path}")

    # Try to open in browser
    if "--save" not in sys.argv:
        import subprocess
        subprocess.run(["open", str(path)])
