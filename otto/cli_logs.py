"""Otto CLI — log inspection, show, diff, and history commands."""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import click

from otto.display import console, format_cost, format_duration, rich_escape
from otto.tasks import load_tasks, spec_binding, spec_is_verifiable, spec_text
from otto.theme import error_console


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


# ---------------------------------------------------------------------------
# Log parsing helpers
# ---------------------------------------------------------------------------

def _load_progress_events(log_dir: Path) -> list[dict]:
    """Load progress events from pilot_results.jsonl that match this task key."""
    results_file = log_dir.parent / "pilot_results.jsonl"
    task_key = log_dir.name
    events = []
    if not results_file.exists():
        return events
    try:
        for line in results_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("task_key") == task_key:
                    events.append(data)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return events


def _extract_phase_timings(events: list[dict]) -> dict[str, float]:
    """Extract per-phase timings from progress events."""
    timings: dict[str, float] = {}
    for evt in events:
        if evt.get("event") == "phase" and evt.get("status") in ("done", "fail"):
            name = evt.get("name", "")
            time_s = evt.get("time_s", 0.0)
            if name and time_s:
                timings[name] = timings.get(name, 0) + time_s
    return timings


def _parse_qa_report(log_dir: Path, events: list[dict]) -> dict:
    """Parse QA report from file or progress events.

    Returns dict with keys: exists, passed, total, summary_lines.
    """
    result = {"exists": False, "passed": 0, "total": 0, "summary_lines": []}

    qa_file = log_dir / "qa-report.md"
    if qa_file.exists():
        result["exists"] = True
        content = qa_file.read_text()
        lines = content.strip().splitlines()
        passed = 0
        total = 0
        summary = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
                total += 1
                passed += 1
            elif stripped.startswith("- [ ]"):
                total += 1
            if stripped.lower().startswith("layer") or stripped.lower().startswith("## layer"):
                summary.append(stripped.lstrip("#").strip())
            if "verdict" in stripped.lower() or "result" in stripped.lower():
                summary.append(stripped.lstrip("#").strip())
        result["passed"] = passed
        result["total"] = total
        result["summary_lines"] = summary[:5]
        return result

    for evt in reversed(events):
        if evt.get("tool") == "run_task_with_qa":
            qa_text = evt.get("qa_report", "")
            if qa_text:
                result["exists"] = True
                passed = 0
                total = 0
                summary = []
                for line in qa_text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
                        total += 1
                        passed += 1
                    elif stripped.startswith("- [ ]"):
                        total += 1
                    if "PASS" in stripped.upper() and ("/" in stripped or "of" in stripped):
                        summary.append(stripped)
                    if stripped.lower().startswith("layer") or stripped.lower().startswith("## layer"):
                        summary.append(stripped.lstrip("#").strip())
                result["passed"] = passed
                result["total"] = total
                result["summary_lines"] = summary[:5]
            break

    return result


def _get_diff_stat(task_id: int, project_dir: Path) -> list[str]:
    """Get diff --stat lines for a task's commit."""
    import subprocess
    result = subprocess.run(
        ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
        capture_output=True, text=True, cwd=project_dir,
    )
    if not result.stdout.strip():
        return []
    sha = result.stdout.strip().splitlines()[0].split()[0]
    stat = subprocess.run(
        ["git", "diff", "--stat", f"{sha}~1", sha],
        capture_output=True, text=True, cwd=project_dir,
    )
    if stat.returncode != 0:
        return []
    return [l.strip() for l in stat.stdout.strip().splitlines() if l.strip()]


def _get_agent_log_highlights(log_dir: Path) -> tuple[list[str], list[str]]:
    """Get first and last few lines of the most recent agent log."""
    logs = sorted(log_dir.glob("attempt-*-agent.log"), reverse=True)
    if not logs:
        return ([], [])
    try:
        content = logs[0].read_text()
    except OSError:
        return ([], [])
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return ([], [])
    first = lines[:3]
    last = lines[-3:] if len(lines) > 6 else []
    return (first, last)


def _get_verify_summary(log_dir: Path) -> str:
    """Get a one-line verify summary from the most recent verify log."""
    verify_file = log_dir / "verify.log"
    if verify_file.exists():
        content = verify_file.read_text().strip()
        if content == "PASSED":
            return "[success]PASSED[/success]"

    logs = sorted(log_dir.glob("attempt-*-verify.log"), reverse=True)
    if not logs:
        return ""
    try:
        content = logs[0].read_text()
    except OSError:
        return ""

    passed = content.count(": PASS")
    failed = content.count(": FAIL")
    total = passed + failed
    if total == 0:
        return ""

    test_count = ""
    m = re.search(r"(\d+)\s+passed", content)
    if m:
        test_count = f" ({m.group(1)} tests)"

    if failed == 0:
        return f"[success]PASSED[/success][dim]{test_count}[/dim]"
    return f"[error]FAILED[/error][dim] ({passed}/{total} tiers){test_count}[/dim]"


def _tail_logs(log_dir: Path, task_id: int) -> None:
    """Tail the most recent agent log and progress events in real-time."""
    console.print(f"[bold]Tailing logs for task #{task_id}[/bold]  [dim](Ctrl+C to stop)[/dim]\n")

    results_file = log_dir.parent / "pilot_results.jsonl"
    results_pos = results_file.stat().st_size if results_file.exists() else 0

    agent_pos = 0
    last_agent_log = None

    try:
        while True:
            agent_logs = sorted(log_dir.glob("attempt-*-agent.log"))
            if agent_logs and agent_logs[-1] != last_agent_log:
                last_agent_log = agent_logs[-1]
                agent_pos = 0

            if last_agent_log and last_agent_log.exists():
                try:
                    with open(last_agent_log) as f:
                        f.seek(agent_pos)
                        new = f.read()
                        agent_pos = f.tell()
                    if new:
                        for line in new.splitlines():
                            if line.strip():
                                console.print(f"  [dim]{rich_escape(line[:120])}[/dim]")
                except OSError:
                    pass

            if results_file.exists():
                try:
                    with open(results_file) as f:
                        f.seek(results_pos)
                        new_lines = f.readlines()
                        results_pos = f.tell()
                    task_key = log_dir.name
                    for rline in new_lines:
                        rline = rline.strip()
                        if not rline:
                            continue
                        try:
                            data = json.loads(rline)
                            if data.get("task_key") != task_key:
                                continue
                            evt = data.get("event", "")
                            if evt == "phase":
                                name = data.get("name", "")
                                phase_status = data.get("status", "")
                                time_s = data.get("time_s", 0)
                                if phase_status == "running":
                                    console.print(f"  [info]{rich_escape(name)}[/info] started")
                                elif phase_status == "done":
                                    console.print(f"  [success]{rich_escape(name)}[/success] done  [dim]{format_duration(time_s)}[/dim]")
                                elif phase_status == "fail":
                                    err = rich_escape(data.get("error", "")[:60])
                                    console.print(f"  [error]{rich_escape(name)}[/error] failed  [dim]{err}[/dim]")
                            elif evt == "agent_tool":
                                name = data.get("name", "")
                                detail = data.get("detail", "")[:60]
                                console.print(f"    [dim]{rich_escape(name)}  {rich_escape(detail)}[/dim]")
                        except json.JSONDecodeError:
                            pass
                except OSError:
                    pass

            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print(f"\n[dim]Stopped.[/dim]")


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def register_log_commands(main: click.Group) -> None:
    """Register logs, show, and diff commands on the main CLI group."""

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_id", type=int)
    @click.option("--raw", is_flag=True, help="Dump all log files without formatting")
    @click.option("-f", "--follow", is_flag=True, help="Tail logs in real-time (useful during runs)")
    def logs(task_id, raw, follow):
        """Show structured logs for a task."""
        tasks_path = Path.cwd() / "tasks.yaml"
        tasks = load_tasks(tasks_path)
        target = None
        for t in tasks:
            if t.get("id") == task_id:
                target = t
                break
        if not target:
            error_console.print(f"Task #{task_id} not found", style="error")
            sys.exit(1)

        log_dir = Path.cwd() / "otto_logs" / target["key"]
        if not log_dir.exists():
            console.print(f"[dim]No logs for task #{task_id}[/dim]")
            return

        if follow:
            _tail_logs(log_dir, task_id)
            return

        if raw:
            for log_file in sorted(log_dir.iterdir()):
                if log_file.is_file():
                    rule = "\u2501" * 40
                    console.print(f"\n[bold]{rule}[/bold]")
                    console.print(f"[bold]  {rich_escape(log_file.name)}[/bold]")
                    console.print(f"[bold]{rule}[/bold]")
                    try:
                        content = log_file.read_text()
                        console.print(content)
                    except (OSError, UnicodeDecodeError):
                        console.print(f"  [dim](binary or unreadable)[/dim]")
            return

        # Structured mode
        console.print(f"\n[bold]Logs for Task #{task_id}[/bold]  [dim]({rich_escape(target['key'])})[/dim]")

        # 1. Verify logs
        verify_logs = sorted(log_dir.glob("attempt-*-verify.log"))
        if verify_logs:
            console.print(f"\n[bold]  Verification[/bold]")
            for vlog in verify_logs:
                attempt = vlog.stem.split("-")[1] if "-" in vlog.stem else "?"
                try:
                    content = vlog.read_text()
                    passed = content.count(": PASS")
                    failed = content.count(": FAIL")
                    test_count = ""
                    m = re.search(r"(\d+)\s+passed", content)
                    if m:
                        test_count = f" ({m.group(1)} tests)"
                    if failed == 0 and passed > 0:
                        console.print(f"    [success]Attempt {attempt}: PASS[/success][dim]{test_count}[/dim]")
                    elif failed > 0:
                        console.print(f"    [error]Attempt {attempt}: FAIL[/error][dim] ({passed}/{passed+failed} tiers){test_count}[/dim]")
                        for line in content.splitlines():
                            if ": FAIL" in line:
                                console.print(f"      [error]{rich_escape(line.strip()[:100])}[/error]")
                    else:
                        console.print(f"    [dim]Attempt {attempt}: {rich_escape(content[:80])}[/dim]")
                except OSError:
                    console.print(f"    [dim]Attempt {attempt}: (unreadable)[/dim]")

        verify_file = log_dir / "verify.log"
        if verify_file.exists() and not verify_logs:
            try:
                content = verify_file.read_text().strip()
                if content == "PASSED":
                    console.print(f"\n[bold]  Verification[/bold]")
                    console.print(f"    [success]PASSED[/success]")
            except OSError:
                pass

        # 2. Agent logs
        agent_logs = sorted(log_dir.glob("attempt-*-agent.log"))
        if agent_logs:
            console.print(f"\n[bold]  Agent Activity[/bold]")
            for alog in agent_logs:
                attempt = alog.stem.split("-")[1] if "-" in alog.stem else "?"
                try:
                    content = alog.read_text()
                    lines = content.splitlines()
                    tool_lines = [l for l in lines if l.strip().startswith("\u25cf") or l.strip().startswith("*")]
                    if tool_lines:
                        console.print(f"    [dim]Attempt {attempt} \u2014 {len(tool_lines)} tool calls:[/dim]")
                        for tl in tool_lines[:10]:
                            console.print(f"      [dim]{rich_escape(tl.strip()[:90])}[/dim]")
                        if len(tool_lines) > 10:
                            console.print(f"      [dim]... ({len(tool_lines) - 10} more)[/dim]")
                    else:
                        console.print(f"    [dim]Attempt {attempt} \u2014 {len(lines)} lines:[/dim]")
                        for l in lines[:3]:
                            console.print(f"      [dim]{rich_escape(l[:90])}[/dim]")
                        if len(lines) > 6:
                            console.print(f"      [dim]...[/dim]")
                            for l in lines[-3:]:
                                console.print(f"      [dim]{rich_escape(l[:90])}[/dim]")
                except OSError:
                    console.print(f"    [dim]Attempt {attempt}: (unreadable)[/dim]")

        # 3. QA report
        qa_file = log_dir / "qa-report.md"
        if qa_file.exists():
            console.print(f"\n[bold]  QA Report[/bold]")
            try:
                content = qa_file.read_text()
                lines = content.strip().splitlines()
                for line in lines[:30]:
                    console.print(f"    {line}")
                if len(lines) > 30:
                    console.print(f"    [dim]... ({len(lines) - 30} more lines)[/dim]")
            except OSError:
                console.print(f"    [dim](unreadable)[/dim]")

        # 4. Pilot debug log
        debug_log = log_dir.parent / "pilot_debug.log"
        if debug_log.exists():
            try:
                content = debug_log.read_text()
                if content.strip():
                    console.print(f"\n[bold]  Pilot Debug[/bold]  [dim](use --raw for full output)[/dim]")
                    lines = content.strip().splitlines()
                    console.print(f"    [dim]{len(lines)} lines \u2014 last 5:[/dim]")
                    for line in lines[-5:]:
                        console.print(f"    [dim]{rich_escape(line[:100])}[/dim]")
            except OSError:
                pass

        console.print()

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_id", type=int)
    def diff(task_id):
        """Show the git diff for a task's commit."""
        import subprocess
        result = subprocess.run(
            ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
            capture_output=True, text=True,
        )
        commits = result.stdout.strip().splitlines()
        if not commits:
            error_console.print(f"No commit found for task #{task_id}", style="error")
            sys.exit(1)
        sha = commits[0].split()[0]
        subprocess.run(["git", "show", sha])

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.argument("task_id", type=int)
    def show(task_id):
        """Show rich details for a task including timing, QA, and diff."""
        import subprocess
        from rich.panel import Panel

        tasks_path = Path.cwd() / "tasks.yaml"
        project_dir = Path.cwd()
        tasks = load_tasks(tasks_path)
        for t in tasks:
            if t.get("id") == task_id:
                key = t.get("key", "?")
                task_status = t.get("status", "?")
                log_dir = project_dir / "otto_logs" / key

                status_styles = {
                    "passed": "success", "failed": "error", "blocked": "error",
                    "running": "info", "pending": "dim",
                }
                status_style = status_styles.get(task_status, "")
                status_styled = f"[{status_style}]{task_status}[/{status_style}]" if status_style else task_status

                att = t.get("attempts", 0)
                cost = t.get("cost_usd", 0.0)
                cost_str = format_cost(cost) if cost else "n/a"
                dur = t.get("duration_s", 0.0)
                dur_str = format_duration(dur) if dur else "n/a"

                console.print(Panel(
                    f"Status: {status_styled}  [dim]|[/dim]  Attempts: {att}  [dim]|[/dim]  Cost: {cost_str}\n"
                    f"Time: {dur_str}",
                    title=f"[bold]Task #{task_id}[/bold]  {rich_escape(t['prompt'][:50])}",
                    border_style="dim",
                    expand=False,
                ))

                if dur:
                    events = _load_progress_events(log_dir) if log_dir.exists() else []
                    timings = _extract_phase_timings(events)
                    if timings:
                        phase_order = ["prepare", "coding", "test", "qa", "merge"]
                        parts = []
                        for p in phase_order:
                            pt = timings.get(p, 0.0)
                            parts.append(f"{format_duration(pt)} {p}")
                        console.print(f"  [dim]Phases:[/dim]   {dur_str}  [dim]({' + '.join(parts)})[/dim]")

                deps = t.get("depends_on") or []
                if deps:
                    console.print(f"  [dim]Deps:[/dim]     {', '.join(f'#{d}' for d in deps)}")

                console.print(f"\n  [dim]Prompt:[/dim] {rich_escape(t['prompt'])}")

                spec = t.get("spec", [])
                if spec:
                    must_count = sum(1 for i in spec if spec_binding(i) == "must")
                    should_count = len(spec) - must_count
                    label = f"{must_count} must"
                    if should_count:
                        label += f", {should_count} should"
                    console.print(f"\n  [dim]Spec ({len(spec)} criteria \u2014 {label}):[/dim]")
                    for i, item in enumerate(spec, 1):
                        text = spec_text(item)
                        binding = spec_binding(item)
                        verifiable = spec_is_verifiable(item)
                        marker = "" if verifiable else " \u25c8"
                        if binding == "must":
                            tag = f"[success]\\[must{marker}][/success]"
                        else:
                            tag = f"[info]\\[should{marker}][/info]"
                        console.print(f"    {i}. {tag} {rich_escape(text)}")

                diff_lines = _get_diff_stat(task_id, project_dir)
                if diff_lines:
                    console.print(f"\n  [dim]Files changed:[/dim]")
                    for dl in diff_lines:
                        if "|" in dl:
                            console.print(f"    [dim]{rich_escape(dl)}[/dim]")
                        elif "changed" in dl or "insertion" in dl or "deletion" in dl:
                            console.print(f"    [dim]{rich_escape(dl)}[/dim]")

                if log_dir.exists():
                    events = _load_progress_events(log_dir)
                    qa = _parse_qa_report(log_dir, events)
                    if qa["exists"]:
                        if qa["total"] > 0:
                            qa_style = "success" if qa["passed"] == qa["total"] else "error"
                            console.print(f"\n  [dim]QA:[/dim] [{qa_style}]{qa['passed']}/{qa['total']} specs passed[/{qa_style}]")
                        for line in qa["summary_lines"]:
                            console.print(f"    [dim]{rich_escape(line)}[/dim]")

                    verify_str = _get_verify_summary(log_dir)
                    if verify_str:
                        console.print(f"\n  [dim]Verify:[/dim] {verify_str}")

                    first_lines, last_lines = _get_agent_log_highlights(log_dir)
                    if first_lines:
                        console.print(f"\n  [dim]Agent log (latest attempt):[/dim]")
                        for line in first_lines:
                            console.print(f"    [dim]{rich_escape(line[:90])}[/dim]")
                        if last_lines:
                            console.print(f"    [dim]...[/dim]")
                            for line in last_lines:
                                console.print(f"    [dim]{rich_escape(line[:90])}[/dim]")

                if t.get("feedback"):
                    console.print(f"\n  [dim]Feedback:[/dim] {rich_escape(t['feedback'])}")

                if t.get("review_ref"):
                    console.print(f"\n  [dim]Review ref:[/dim] {rich_escape(t['review_ref'])}")

                if t.get("error"):
                    console.print(f"\n  [error]Error:[/error] {rich_escape(t['error'])}")

                if task_status == "failed" and log_dir.exists():
                    verify_logs = sorted(log_dir.glob("attempt-*-verify.log"), reverse=True)
                    if verify_logs:
                        try:
                            verify_content = verify_logs[0].read_text()
                            fail_lines = [
                                l for l in verify_content.splitlines()
                                if any(kw in l.upper() for kw in ["FAIL", "ERROR", "ASSERT"])
                            ]
                            if fail_lines:
                                console.print(f"\n  [dim]Last verify errors:[/dim]")
                                for fl in fail_lines[-5:]:
                                    console.print(f"    [error]{rich_escape(fl[:100])}[/error]")
                        except OSError:
                            pass

                result = subprocess.run(
                    ["git", "log", "--oneline", "--all", f"--grep=(#{task_id})"],
                    capture_output=True, text=True, cwd=project_dir,
                )
                if result.stdout.strip():
                    console.print(f"\n  [dim]Commit:[/dim] {rich_escape(result.stdout.strip().splitlines()[0])}")

                test_file = project_dir / "tests" / f"test_otto_{key}.py"
                if test_file.exists():
                    console.print(f"  [dim]Test file:[/dim] {rich_escape(str(test_file.relative_to(project_dir)))}")

                if log_dir.exists():
                    console.print(f"  [dim]Logs:[/dim]     {rich_escape(str(log_dir.relative_to(project_dir)))}/")

                console.print()
                return
        error_console.print(f"Task #{task_id} not found", style="error")
        sys.exit(1)

    HISTORY_FILE = "otto_logs/run-history.jsonl"

    @main.command(context_settings=CONTEXT_SETTINGS)
    @click.option("-n", "--limit", "limit_", default=20, help="Number of runs to show")
    def history(limit_):
        """Show past run history."""
        from rich.table import Table
        from rich.text import Text

        project_dir = Path.cwd()
        history_path = project_dir / HISTORY_FILE

        if not history_path.exists():
            console.print(f"[dim]No run history found. History is recorded after each 'otto run'.[/dim]")
            return

        entries = []
        try:
            for line in history_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            error_console.print(f"Error reading history file", style="error")
            sys.exit(1)

        if not entries:
            console.print(f"[dim]No run history found.[/dim]")
            return

        entries.reverse()
        entries = entries[:limit_]

        table = Table(show_header=True, box=None, pad_edge=False, show_edge=False, expand=False)
        table.add_column("Date", width=20)
        table.add_column("Tasks", width=6, justify="right")
        table.add_column("Pass", width=5, justify="right")
        table.add_column("Fail", width=5, justify="right")
        table.add_column("Cost", width=8, justify="right", style="dim")
        table.add_column("Time", width=8, justify="right", style="dim")
        table.add_column("Detail", ratio=1, no_wrap=True)

        for entry in entries:
            ts = entry.get("timestamp", "?")
            try:
                dt = datetime.fromisoformat(ts)
                ts_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                ts_str = str(ts)[:16]

            total = entry.get("tasks_total", 0)
            passed = entry.get("tasks_passed", 0)
            failed = entry.get("tasks_failed", 0)
            cost = entry.get("cost_usd", 0.0)
            time_s = entry.get("time_s", 0.0)

            tasks_str = f"{passed + failed}/{total}" if total else "0"
            pass_style = "success" if passed > 0 else "dim"
            fail_style = "error" if failed > 0 else "dim"
            cost_str = format_cost(cost) if cost > 0 else ""
            time_str = format_duration(time_s) if time_s > 0 else ""

            fail_detail = ""
            if failed > 0 and entry.get("failure_summary"):
                fail_detail = f"[error]({rich_escape(entry['failure_summary'][:50])})[/error]"

            pass_text = Text(str(passed), style=pass_style)
            fail_text = Text(str(failed), style=fail_style)

            table.add_row(
                ts_str,
                tasks_str,
                pass_text,
                fail_text,
                cost_str,
                time_str,
                fail_detail,
            )

        console.print()
        console.print(table)
        console.print()
