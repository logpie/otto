"""Otto CLI — benchmark subcommands."""

import asyncio
import sys
from pathlib import Path

import click

from otto.display import console, rich_escape
from otto.theme import error_console


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _get_bench_dir() -> Path:
    """Locate the bench/ directory (in the otto repo root)."""
    otto_root = Path(__file__).parent.parent
    bench_dir = otto_root / "bench"
    if not bench_dir.exists():
        error_console.print("Error: bench/ directory not found.", style="error")
        sys.exit(2)
    return bench_dir


def _get_runner(name: str):
    """Import and instantiate a runner by name."""
    import importlib.util

    bench_dir = _get_bench_dir()
    runner_files = {
        "otto": "otto_runner.py",
        "bare-cc": "bare_cc_runner.py",
        "ralph": "ralph_runner.py",
        "self-test": "self_test_runner.py",
    }
    runner_classes = {
        "otto": "OttoRunner",
        "bare-cc": "BareClaudeRunner",
        "ralph": "RalphRunner",
        "self-test": "SelfTestRunner",
    }

    if name not in runner_files:
        error_console.print(f"Unknown runner: {name}. Available: {', '.join(runner_files)}", style="error")
        sys.exit(2)

    runner_path = bench_dir / "runners" / runner_files[name]
    if not runner_path.exists():
        error_console.print(f"Runner file not found: {rich_escape(str(runner_path))}", style="error")
        sys.exit(2)

    spec = importlib.util.spec_from_file_location(f"bench_runner_{name}", runner_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = getattr(mod, runner_classes[name])
    return cls()


def register_bench_commands(main: click.Group) -> None:
    """Register all bench subcommands on the main CLI group."""

    @main.group(context_settings=CONTEXT_SETTINGS)
    def bench():
        """Benchmark system — measure and compare pipeline effectiveness."""
        pass

    @bench.command("run", context_settings=CONTEXT_SETTINGS)
    @click.option("--task", "task_names", multiple=True, help="Run specific task(s) by name")
    @click.option("--runner", "runner_name", default="otto", help="Runner to use (otto, bare-cc, ralph, self-test)")
    @click.option("--label", default="", help="Label for this run (for comparison)")
    @click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), help="Filter by difficulty")
    @click.option("--suite", "suite_file", default="suite.yaml", help="Suite file (default: suite.yaml)")
    def bench_run(task_names, runner_name, label, difficulty, suite_file):
        """Run benchmark tasks and save results."""
        from otto.bench import filter_tasks, load_suite, run_bench, save_results

        bench_dir = _get_bench_dir()
        suite_path = bench_dir / suite_file

        if not suite_path.exists():
            error_console.print(f"Error: {rich_escape(str(suite_path))} not found.", style="error")
            sys.exit(2)

        tasks = load_suite(suite_path)
        if not tasks:
            error_console.print("No tasks found in suite.", style="error")
            sys.exit(2)

        tasks = filter_tasks(
            tasks,
            difficulty=difficulty,
            names=list(task_names) if task_names else None,
        )
        if not tasks:
            error_console.print("No tasks match the given filters.", style="error")
            sys.exit(2)

        runner = _get_runner(runner_name)
        console.print(f"[bold]Otto Bench[/bold] \u2014 {len(tasks)} tasks, runner: [info]{rich_escape(runner_name)}[/info]")
        if label:
            console.print(f"  Label: {rich_escape(label)}")
        console.print()

        run = asyncio.run(run_bench(bench_dir, tasks, runner, label=label))

        results_dir = bench_dir / "results"
        out_path = save_results(run, results_dir)

        s = run.summary
        rule = "\u2501" * 50
        console.print(f"\n{rule}")
        console.print(f"[bold]Results:[/bold] {rich_escape(out_path.name)}")
        console.print(f"  Success: [success]{s['passed']}[/success]/{s['total']}  ({s['success_rate'] * 100:.1f}%)")
        console.print(f"  Cost:    ${s['total_cost']:.2f}  (${s['cost_per_success']:.2f}/success)")
        console.print(f"  Time:    {s['total_time_s']:.0f}s  ({s['time_per_success_s']:.0f}s/success)")
        if s["mean_mutation_score"] > 0:
            console.print(f"  Mutation: {s['mean_mutation_score']:.3f}")

    @bench.command("compare", context_settings=CONTEXT_SETTINGS)
    @click.argument("baseline")
    @click.argument("current")
    def bench_compare(baseline, current):
        """Compare two benchmark runs.

        Arguments are result filenames or labels. Searches bench/results/ for matches.
        """
        from otto.bench import compare_runs, load_results, list_results

        bench_dir = _get_bench_dir()
        results_dir = bench_dir / "results"

        def _find_result(query: str) -> Path:
            exact = results_dir / query
            if exact.exists():
                return exact
            with_ext = results_dir / f"{query}.json"
            if with_ext.exists():
                return with_ext
            for name, run in list_results(results_dir):
                if run.label == query or run.run_id == query:
                    return results_dir / name
            error_console.print(f"Result not found: {rich_escape(query)}", style="error")
            sys.exit(2)

        baseline_path = _find_result(baseline)
        current_path = _find_result(current)

        baseline_run = load_results(baseline_path)
        current_run = load_results(current_path)

        console.print(compare_runs(baseline_run, current_run))

    @bench.command("history", context_settings=CONTEXT_SETTINGS)
    @click.option("--limit", "-n", default=20, help="Number of runs to show")
    def bench_history(limit):
        """Show recent benchmark run history."""
        from rich.table import Table
        from otto.bench import list_results

        bench_dir = _get_bench_dir()
        results_dir = bench_dir / "results"

        runs = list_results(results_dir)
        if not runs:
            console.print(f"[dim]No benchmark results found.[/dim]")
            return

        table = Table(show_header=True, box=None, pad_edge=False, show_edge=False, expand=False)
        table.add_column("Run ID", width=24)
        table.add_column("Runner", width=10)
        table.add_column("Label", width=20)
        table.add_column("Pass", width=6, justify="right")
        table.add_column("Cost", width=8, justify="right", style="dim")
        table.add_column("Time", width=8, justify="right", style="dim")

        for name, run in runs[:limit]:
            s = run.summary
            sr = s["success_rate"] * 100
            table.add_row(
                run.run_id,
                run.runner,
                run.label,
                f"{s['passed']}/{s['total']} {sr:.0f}%",
                f"${s['total_cost']:.2f}",
                f"{s['total_time_s']:.0f}s",
            )

        console.print(table)
