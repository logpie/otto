from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_script(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_e2e_runner_all_excludes_real_cost_scenarios() -> None:
    runner = _load_script("tests._e2e_runner_script", SCRIPTS_DIR / "e2e_runner.py")

    assert set(runner.select_scenarios(["all"])).isdisjoint(runner.REAL_SCENARIOS)
    assert set(runner.select_scenarios(["real"])) == runner.REAL_SCENARIOS
    assert runner.select_scenarios(["C1"]) == ["C1"]


def test_e2e_scenario_catalog_matches_runner_surface() -> None:
    runner = _load_script("tests._e2e_runner_catalog_script", SCRIPTS_DIR / "e2e_runner.py")
    catalog = (REPO_ROOT / "e2e-scenarios.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^### ([ABC]\d+[A-Z]?)\.", catalog, flags=re.MULTILINE))

    assert documented == set(runner.SCENARIOS)


def test_e2e_runner_direct_real_scenario_requires_opt_in(monkeypatch) -> None:
    runner = _load_script("tests._e2e_runner_guard_script", SCRIPTS_DIR / "e2e_runner.py")
    monkeypatch.delenv("OTTO_ALLOW_REAL_COST", raising=False)
    monkeypatch.setattr(sys, "argv", ["e2e_runner.py", "C1"])
    guard_calls: list[str] = []

    def fake_require_real_cost_opt_in(activity: str) -> None:
        guard_calls.append(activity)
        raise SystemExit(2)

    monkeypatch.setattr(runner, "require_real_cost_opt_in", fake_require_real_cost_opt_in)
    monkeypatch.setitem(
        runner.SCENARIOS,
        "C1",
        lambda _results: (_ for _ in ()).throw(AssertionError("real scenario executed")),
    )

    assert runner.main() == 2
    assert guard_calls == ["real E2E scenario(s)"]


def test_benchmark_merge_cost_parser_dedupes_repeated_outcome_notes(tmp_path: Path) -> None:
    bench_costs = _load_script("tests._bench_costs_script", SCRIPTS_DIR / "bench_costs.py")
    state_dir = tmp_path / "otto_logs" / "merge" / "merge-1"
    state_dir.mkdir(parents=True)
    state_dir.joinpath("state.json").write_text(
        json.dumps({
            "outcomes": [
                {"branch": "a", "note": "resolved by agent (cost $1.25, 3 files)"},
                {"branch": "b", "note": "resolved by agent (cost $1.25, 3 files)"},
                {"branch": "c", "note": "second agent pass (cost $0.75)"},
            ]
        }),
        encoding="utf-8",
    )

    assert bench_costs.merge_cost_from_state_dir(tmp_path / "otto_logs" / "merge") == 2.0


def test_e2e_runner_child_liveness_delegates_to_queue_runner(monkeypatch) -> None:
    runner = _load_script("tests._e2e_runner_liveness_script", SCRIPTS_DIR / "e2e_runner.py")
    import otto.queue.runner as queue_runner

    calls: list[dict[str, object]] = []

    def fake_child_is_alive(child: dict[str, object]) -> bool:
        calls.append(child)
        return child.get("pid") == 123

    monkeypatch.setattr(queue_runner, "child_is_alive", fake_child_is_alive)

    assert runner._child_process_is_alive({"pid": 123}) is True
    assert runner._child_process_is_alive({"pid": 456}) is False
    assert calls == [{"pid": 123}, {"pid": 456}]


def test_bench_flask_fixture_queue_branches_uses_queue_definition(tmp_path: Path) -> None:
    from otto.queue.schema import QueueTask, append_task
    from tests._helpers import init_repo

    script = _load_script("tests._bench_flask_fixture_script", SCRIPTS_DIR / "bench_flask_fixture.py")
    repo = init_repo(tmp_path)
    append_task(repo, QueueTask(
        id="tags",
        command_argv=["improve", "feature", "tags"],
        branch="improve/sentinel-tags-branch",
        worktree=".worktrees/tags",
    ))
    append_task(repo, QueueTask(
        id="priority",
        command_argv=["improve", "feature", "priority"],
        branch="improve/sentinel-priority-branch",
        worktree=".worktrees/priority",
    ))

    assert script.queue_branches(repo) == {
        "tags": "improve/sentinel-tags-branch",
        "priority": "improve/sentinel-priority-branch",
    }
    assert not re.search(r"20\d\d-\d\d-\d\d", (SCRIPTS_DIR / "bench_flask_fixture.py").read_text(encoding="utf-8"))


def test_fixture_restore_scripts_do_not_hardcode_dated_branch_refs() -> None:
    for name in ("restore_flask_fixture.sh", "restore_p6_fixture.sh"):
        script = SCRIPTS_DIR / name
        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert syntax.returncode == 0, syntax.stderr
        usage = subprocess.run(["bash", str(script)], capture_output=True, text=True)
        assert usage.returncode == 2
        assert "Usage:" in usage.stderr
        text = script.read_text(encoding="utf-8")
        assert not re.search(r"20\d\d-\d\d-\d\d", text)


def test_real_otto_run_disables_fake_otto_override(monkeypatch) -> None:
    runner = _load_script("tests._e2e_runner_real_override_script", SCRIPTS_DIR / "e2e_runner.py")

    class FakeRepo:
        def run(self, *argv, **kwargs):  # type: ignore[no-untyped-def]
            return argv, kwargs

    monkeypatch.setenv("OTTO_BIN", "fake-from-parent")
    argv, kwargs = runner.real_otto_run(FakeRepo(), "queue", "ls")

    assert argv[0] == str(runner.OTTO_BIN)
    assert argv[1:] == ("queue", "ls")
    assert kwargs["fake_otto"] is False
    assert "OTTO_BIN" not in kwargs["env"]


def _ast_arg_text(arg: ast.AST) -> str:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return ast.unparse(arg)


def _skip_option_value(tokens: list[str], index: int, options_with_values: set[str]) -> int:
    token = tokens[index]
    if token in options_with_values and index + 1 < len(tokens):
        return index + 2
    return index + 1


def _positionals_before_passthrough(tokens: list[str], *, start: int) -> list[str]:
    end = tokens.index("--") if "--" in tokens else len(tokens)
    positionals: list[str] = []
    index = start
    while index < end:
        token = tokens[index]
        if token in {"--as", "--after"}:
            index = _skip_option_value(tokens, index, {"--as", "--after"})
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    return positionals


def _passthrough_positionals(tokens: list[str]) -> list[str]:
    if "--" not in tokens:
        return []
    passthrough = tokens[tokens.index("--") + 1:]
    positionals: list[str] = []
    index = 0
    options_with_values = {"-n", "--rounds", "--budget", "--max-turns", "--model", "--provider", "--effort"}
    while index < len(passthrough):
        token = passthrough[index]
        if token in options_with_values:
            index = _skip_option_value(passthrough, index, options_with_values)
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    return positionals


def test_script_queue_enqueue_calls_put_positionals_before_passthrough_separator() -> None:
    checked_files = [
        SCRIPTS_DIR / "bench_build_merge_with_cert.py",
        SCRIPTS_DIR / "bench_flask_fixture.py",
        SCRIPTS_DIR / "bench_runner.py",
        SCRIPTS_DIR / "e2e_merge_sanity.py",
        SCRIPTS_DIR / "e2e_runner.py",
    ]
    queue_calls: list[tuple[Path, int, list[str]]] = []
    for path in checked_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = getattr(node.func, "id", None)
            attr_name = getattr(node.func, "attr", None)
            if func_name not in {"otto_run", "real_otto_run"} and attr_name != "otto":
                continue
            args = [_ast_arg_text(arg) for arg in node.args]
            try:
                queue_idx = args.index("queue")
            except ValueError:
                continue
            if len(args) <= queue_idx + 2 or args[queue_idx + 1] not in {"build", "improve", "certify"}:
                continue
            queue_calls.append((path, node.lineno, args[queue_idx:]))

    assert queue_calls
    for path, lineno, args in queue_calls:
        command = args[1]
        if command == "build":
            assert _positionals_before_passthrough(args, start=2), (
                f"{path}:{lineno} puts passthrough separator before the queue build intent: {args}"
            )
        if command == "improve":
            positionals = _positionals_before_passthrough(args, start=2)
            assert positionals and positionals[0] in {"bugs", "feature", "target"}, (
                f"{path}:{lineno} has malformed queue improve syntax: {args}"
            )
        assert not _passthrough_positionals(args), (
            f"{path}:{lineno} puts queue-level positional(s) after passthrough separator: {args}"
        )


def test_e2e_merge_sanity_requires_real_cost_opt_in(monkeypatch) -> None:
    script = _load_script("tests._e2e_merge_sanity_script", SCRIPTS_DIR / "e2e_merge_sanity.py")
    monkeypatch.delenv("OTTO_ALLOW_REAL_COST", raising=False)
    guard_calls: list[str] = []

    def fake_require_real_cost_opt_in(activity: str) -> None:
        guard_calls.append(activity)
        raise SystemExit(2)

    monkeypatch.setattr(script, "require_real_cost_opt_in", fake_require_real_cost_opt_in)
    monkeypatch.setattr(script, "phase1_single_task", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("phase1 reached")))
    monkeypatch.setattr(script, "phase2_parallel_merge", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("phase2 reached")))

    assert script.main() == 2
    assert guard_calls == ["E2E merge sanity"]
