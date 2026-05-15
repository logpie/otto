"""Run small Otto v5 field-test scenarios and write a markdown matrix.

This script intentionally uses Otto as a black box. It does not add runner
rules, classifiers, or product-specific patches. Live mode requires
``OTTO_ALLOW_REAL_COST=1`` because it launches real provider-backed
``otto v5 run`` calls.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from real_cost_guard import require_real_cost_opt_in  # noqa: E402

FIELD_TEST_ROOT = REPO_ROOT / "bench" / "field-tests"
DEFAULT_RUNS_ROOT = FIELD_TEST_ROOT / "runs"
DEFAULT_BASE_PORT = 19000
DEFAULT_PORT_STRIDE = 100
DEFAULT_BOOT_TIMEOUT_S = 90
OTTO_BIN = Path(os.environ.get("OTTO_BIN") or REPO_ROOT / ".venv" / "bin" / "otto")
if not OTTO_BIN.exists():
    OTTO_BIN = Path("otto")

_META_RE = re.compile(r"^\s*[-*]\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
VALID_TIERS = {"auto", "solo", "lead", "modular"}


@dataclass(frozen=True)
class Scenario:
    name: str
    path: Path
    intent: str
    expected_shape: str
    success_criteria: str
    kind: str
    budget_seconds: int
    max_parallel: int
    tier: str
    boot_smoke: bool
    smoke_path: str
    smoke_port_var: str


@dataclass(frozen=True)
class PortAllocation:
    index: int
    start: int
    end: int

    @property
    def port(self) -> int:
        return self.start

    @property
    def api_port(self) -> int:
        return self.start + 1

    def env(self) -> dict[str, str]:
        return {
            "PORT": str(self.port),
            "FRONTEND_PORT": str(self.port),
            "VITE_PORT": str(self.port),
            "API_PORT": str(self.api_port),
            "BACKEND_PORT": str(self.api_port),
            "FIELD_TEST_PORT_START": str(self.start),
            "FIELD_TEST_PORT_END": str(self.end),
        }


@dataclass
class BootResult:
    status: str = "skipped"
    detail: str = ""
    url: str = ""
    log_path: str = ""


@dataclass
class ScenarioResult:
    name: str
    status: str
    dry_run: bool
    tier: str = ""
    project_dir: str = ""
    log_path: str = ""
    wall_seconds: float = 0.0
    agent_seconds: float = 0.0
    cost_usd: float = 0.0
    final_verdict: str = "not_run"
    tree_nodes: int = 0
    tree_depth: int = 0
    shape: str = "not_run"
    cli_exit_code: int | None = None
    cli_timeout: bool = False
    port_start: int = 0
    port_end: int = 0
    boot: BootResult = field(default_factory=BootResult)
    bugs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def parse_bool(value: str, *, default: bool = False) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def parse_success_metadata(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        match = _META_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower().replace("-", "_")
        metadata[key] = match.group(2).strip()
    return metadata


def load_scenario(path: Path) -> Scenario:
    intent_path = path / "intent.md"
    expected_path = path / "expected_shape.md"
    success_path = path / "success_criteria.md"
    missing = [p.name for p in (intent_path, expected_path, success_path) if not p.exists()]
    if missing:
        raise ValueError(f"{path.name} missing required file(s): {', '.join(missing)}")

    intent = intent_path.read_text(encoding="utf-8")
    expected = expected_path.read_text(encoding="utf-8")
    success = success_path.read_text(encoding="utf-8")
    meta = parse_success_metadata(success)
    kind = meta.get("kind", "web").strip().lower() or "web"
    budget_seconds = _positive_int(meta.get("budget_seconds"), default=1200)
    max_parallel = _positive_int(meta.get("max_parallel"), default=3)
    tier = meta.get("tier", "auto").strip().lower() or "auto"
    if tier not in VALID_TIERS:
        allowed = ", ".join(sorted(VALID_TIERS))
        raise ValueError(f"{path.name} has invalid tier {tier!r}; expected one of: {allowed}")
    boot_smoke = parse_bool(meta.get("boot_smoke", "true"), default=True)
    smoke_path = meta.get("smoke_path", "/").strip() or "/"
    if not smoke_path.startswith("/"):
        smoke_path = "/" + smoke_path
    smoke_port_var = meta.get("smoke_port_var", "PORT").strip() or "PORT"

    return Scenario(
        name=path.name,
        path=path,
        intent=intent,
        expected_shape=expected,
        success_criteria=success,
        kind=kind,
        budget_seconds=budget_seconds,
        max_parallel=max_parallel,
        tier=tier,
        boot_smoke=boot_smoke,
        smoke_path=smoke_path,
        smoke_port_var=smoke_port_var,
    )


def _positive_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def discover_scenarios(root: Path = FIELD_TEST_ROOT) -> list[Scenario]:
    scenarios: list[Scenario] = []
    if not root.exists():
        return scenarios
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name == "runs":
            continue
        if not (child / "intent.md").exists():
            continue
        scenarios.append(load_scenario(child))
    return scenarios


def select_scenarios(scenarios: list[Scenario], query: str | None) -> list[Scenario]:
    if not query:
        return scenarios
    matches = [
        s for s in scenarios
        if s.name == query or s.name.startswith(query) or query in s.name
    ]
    if not matches:
        names = ", ".join(s.name for s in scenarios)
        raise SystemExit(f"Unknown scenario {query!r}. Available: {names}")
    if len(matches) > 1:
        names = ", ".join(s.name for s in matches)
        raise SystemExit(f"Scenario {query!r} is ambiguous: {names}")
    return matches


def allocate_ports(count: int, *, base_port: int, stride: int) -> list[PortAllocation]:
    if stride < 10:
        raise SystemExit("--port-stride must be at least 10")
    return [
        PortAllocation(index=i, start=base_port + i * stride, end=base_port + (i + 1) * stride - 1)
        for i in range(count)
    ]


def setup_project(scenario: Scenario, run_root: Path, ports: PortAllocation) -> Path:
    project_dir = run_root / scenario.name
    project_dir.mkdir(parents=True, exist_ok=False)

    (project_dir / "README.md").write_text(
        f"# {scenario.name}\n\n"
        "Fresh project generated by `scripts/run_field_tests.py`.\n",
        encoding="utf-8",
    )
    (project_dir / "intent.md").write_text(scenario.intent.rstrip() + "\n", encoding="utf-8")
    (project_dir / "FIELD_TEST.md").write_text(
        _field_test_context(scenario, ports),
        encoding="utf-8",
    )
    (project_dir / "otto.yaml").write_text(
        "default_branch: main\n"
        "provider: claude\n"
        f"run_budget_seconds: {scenario.budget_seconds}\n"
        "max_turns_per_call: 200\n"
        "test_command: null\n",
        encoding="utf-8",
    )

    _git(project_dir, "init", "-q", "-b", "main")
    _git(project_dir, "config", "user.email", "field-tests@otto.local")
    _git(project_dir, "config", "user.name", "Otto Field Tests")
    _git(project_dir, "add", ".")
    _git(project_dir, "commit", "-q", "-m", "field-test seed")
    return project_dir


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _field_test_context(scenario: Scenario, ports: PortAllocation) -> str:
    return (
        f"# Field Test Context\n\n"
        f"Scenario: `{scenario.name}`\n"
        f"Generated at: {utc_now_iso()}\n\n"
        "## Expected Shape\n\n"
        f"{scenario.expected_shape.rstrip()}\n\n"
        "## Success Criteria\n\n"
        f"{scenario.success_criteria.rstrip()}\n\n"
        "## Runtime Ports\n\n"
        f"- PORT / FRONTEND_PORT / VITE_PORT: `{ports.port}`\n"
        f"- API_PORT / BACKEND_PORT: `{ports.api_port}`\n"
        f"- FIELD_TEST_PORT_START: `{ports.start}`\n"
        f"- FIELD_TEST_PORT_END: `{ports.end}`\n\n"
        "If this is a web/static product, `start.sh` must serve the user-facing "
        "HTTP surface on `$PORT`. Use the other allocated ports only for "
        "internal services. Do not hard-code 3000, 5173, 8000, or 8080 when "
        "an environment variable is available.\n"
    )


def runtime_intent(scenario: Scenario, ports: PortAllocation) -> str:
    return (
        scenario.intent.rstrip()
        + "\n\n---\n\n"
        + "## Otto Field-Test Runtime Context\n\n"
        + "Use the success criteria below as the acceptance target for this run. "
        + "Build real product output, include runnable local checks, and keep the "
        + "implementation intentionally small.\n\n"
        + scenario.success_criteria.rstrip()
        + "\n\n"
        + "## Allocated Ports\n\n"
        + f"- PORT / FRONTEND_PORT / VITE_PORT: {ports.port}\n"
        + f"- API_PORT / BACKEND_PORT: {ports.api_port}\n"
        + f"- FIELD_TEST_PORT_START: {ports.start}\n"
        + f"- FIELD_TEST_PORT_END: {ports.end}\n\n"
        + "For web/static products, include root `start.sh` and make `$PORT` the "
        + "user-facing HTTP port. For CLI products, do not start a server.\n"
    )


def command_preview(scenario: Scenario) -> str:
    return (
        f"{OTTO_BIN} v5 run <intent> --provider claude "
        f"--budget {scenario.budget_seconds} "
        f"--max-parallel {scenario.max_parallel} --tier {scenario.tier}"
    )


def otto_command(scenario: Scenario, ports: PortAllocation) -> list[str]:
    return [
        str(OTTO_BIN),
        "v5",
        "run",
        runtime_intent(scenario, ports),
        "--provider",
        "claude",
        "--budget",
        str(scenario.budget_seconds),
        "--max-parallel",
        str(scenario.max_parallel),
        "--tier",
        scenario.tier,
    ]


def run_scenario(
    scenario: Scenario,
    *,
    run_root: Path,
    ports: PortAllocation,
    boot_smoke_enabled: bool,
    boot_timeout_s: int,
    dry_run: bool,
) -> ScenarioResult:
    result = ScenarioResult(
        name=scenario.name,
        status=("dry_run" if dry_run else "running"),
        dry_run=dry_run,
        tier=scenario.tier,
        port_start=ports.start,
        port_end=ports.end,
        notes=[f"command: {command_preview(scenario)}"],
    )
    if dry_run:
        result.status = "not_run"
        result.final_verdict = "not_run"
        result.shape = "not_run"
        result.boot = BootResult(status="skipped", detail="dry run")
        result.notes.append("dry run only; no Otto process launched")
        return result

    project_dir = setup_project(scenario, run_root, ports)
    result.project_dir = str(project_dir)
    log_path = project_dir / "field-test-otto.log"
    result.log_path = str(log_path)
    env = os.environ.copy()
    env.update(ports.env())
    env["FIELD_TEST_SCENARIO"] = scenario.name
    env["FIELD_TEST_PROJECT_DIR"] = str(project_dir)
    env.setdefault("PYTHONUNBUFFERED", "1")

    start = time.monotonic()
    timeout_s = scenario.budget_seconds + 300
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# field-test otto run\n# _written_at: {utc_now_iso()}\n")
        log.write(f"# command: {command_preview(scenario)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            otto_command(scenario, ports),
            cwd=str(project_dir),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            result.cli_exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            result.cli_timeout = True
            _terminate_process_group(proc)
            result.cli_exit_code = proc.wait(timeout=15)
    result.wall_seconds = time.monotonic() - start

    metrics = collect_metrics(project_dir)
    result.agent_seconds = metrics["agent_seconds"]
    result.cost_usd = metrics["cost_usd"]
    result.final_verdict = metrics["final_verdict"]
    result.tree_nodes = metrics["tree_nodes"]
    result.tree_depth = metrics["tree_depth"]
    result.shape = metrics["shape"]
    result.bugs.extend(metrics["bugs"])
    result.notes.extend(metrics["notes"])

    if result.cli_timeout:
        result.bugs.append(f"otto command timed out after {timeout_s}s")
    if result.cli_exit_code not in (0, None):
        result.bugs.append(f"otto command exited {result.cli_exit_code}")

    if boot_smoke_enabled and scenario.boot_smoke:
        result.boot = run_boot_smoke(
            project_dir=project_dir,
            scenario=scenario,
            env=env,
            boot_timeout_s=boot_timeout_s,
        )
    else:
        detail = "disabled" if not boot_smoke_enabled else "scenario metadata"
        result.boot = BootResult(status="skipped", detail=detail)

    if result.boot.status == "fail":
        result.bugs.append(f"boot-smoke failed: {result.boot.detail}")

    result.status = "pass" if _result_is_clean(result) else "fail"
    write_result_json(project_dir, result)
    return result


def _result_is_clean(result: ScenarioResult) -> bool:
    return (
        not result.cli_timeout
        and result.cli_exit_code == 0
        and result.final_verdict == "pass"
        and result.boot.status in {"pass", "skipped"}
        and not result.bugs
    )


def collect_metrics(project_dir: Path) -> dict[str, Any]:
    graph = _read_json(project_dir / "otto_logs" / "cross-sessions" / "task_graph.json")
    summaries = _read_summaries(project_dir)
    tasks = graph.get("tasks") if isinstance(graph.get("tasks"), dict) else {}
    tree_nodes = len(tasks)
    tree_depth = _tree_depth(tasks)
    final_verdict = _root_verdict(tasks, summaries)
    shape = _shape_summary(tasks)
    agent_seconds = sum(_float(s.get("duration_s")) for s in summaries)
    summary_cost_usd = sum(_float(s.get("cost_usd")) for s in summaries)
    graph_cost_usd = _graph_cost(tasks)
    cost_usd = max(summary_cost_usd, graph_cost_usd)

    bugs: list[str] = []
    notes: list[str] = []
    if not graph:
        bugs.append("missing task_graph.json")
    if tree_nodes == 0 and summaries:
        bugs.append("no task graph nodes recorded")
    if final_verdict not in {"pass", "not_run"}:
        bugs.append(f"final verdict {final_verdict}")

    for task_id, task in sorted(tasks.items()):
        verdict = str(task.get("verdict") or "unknown")
        if verdict in {"catastrophic", "merge_blocked", "unverified", "partial"}:
            bugs.append(f"{task_id}: {verdict}")

    for summary in summaries:
        verdict = str(summary.get("verdict") or "")
        task_id = str(summary.get("task_id") or "?")
        failure = str(summary.get("failure_reason") or "").strip()
        if failure:
            bugs.append(f"{task_id}: {failure[:180]}")
        verify = summary.get("verify_result")
        if verdict in {"partial", "unverified", "merge_blocked", "catastrophic"} and isinstance(verify, dict):
            verify_summary = str(verify.get("summary") or "").strip()
            if verify_summary:
                bugs.append(f"{task_id}: {verify_summary[:180]}")

    notes.append(f"summaries={len(summaries)}")
    return {
        "agent_seconds": agent_seconds,
        "cost_usd": cost_usd,
        "final_verdict": final_verdict,
        "tree_nodes": tree_nodes,
        "tree_depth": tree_depth,
        "shape": shape,
        "bugs": _dedupe(bugs)[:8],
        "notes": notes,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_summaries(project_dir: Path) -> list[dict[str, Any]]:
    sessions = project_dir / "otto_logs" / "sessions"
    if not sessions.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for path in sorted(sessions.rglob("summary.json")):
        payload = _read_json(path)
        if payload:
            payload["_path"] = str(path)
            summaries.append(payload)
    return summaries


def _tree_depth(tasks: dict[str, Any]) -> int:
    if not tasks:
        return 0

    def children(task_id: str) -> list[str]:
        task = tasks.get(task_id) or {}
        ids = task.get("child_task_ids") or []
        return [str(i) for i in ids if str(i) in tasks]

    roots = ["root"] if "root" in tasks else [
        str(tid) for tid, task in tasks.items() if not task.get("parent_task_id")
    ]
    seen: set[str] = set()

    def depth(task_id: str) -> int:
        if task_id in seen:
            return 1
        seen.add(task_id)
        child_depths = [depth(child_id) for child_id in children(task_id)]
        seen.discard(task_id)
        return 1 + max(child_depths, default=0)

    return max((depth(root) for root in roots), default=0)


def _root_verdict(tasks: dict[str, Any], summaries: list[dict[str, Any]]) -> str:
    root = tasks.get("root")
    if isinstance(root, dict) and root.get("verdict"):
        return str(root["verdict"])
    for summary in reversed(summaries):
        if summary.get("task_id") == "root" and summary.get("verdict"):
            return str(summary["verdict"])
    if summaries:
        return str(summaries[-1].get("verdict") or "unknown")
    return "unknown"


def _shape_summary(tasks: dict[str, Any]) -> str:
    if not tasks:
        return "no graph"
    root = tasks.get("root") or {}
    root_decomp = str(root.get("decomposition") or "unknown")
    root_children = list(root.get("child_task_ids") or [])
    depth = _tree_depth(tasks)
    if len(tasks) == 1:
        return f"root {root_decomp}"
    if depth <= 2:
        return f"root {root_decomp} -> {len(root_children)} children"
    return f"nested depth {depth}, {len(tasks)} nodes"


def _graph_cost(tasks: dict[str, Any]) -> float:
    return sum(_float(task.get("cost_usd")) for task in tasks.values())


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = " ".join(str(value).split())
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def run_boot_smoke(
    *,
    project_dir: Path,
    scenario: Scenario,
    env: dict[str, str],
    boot_timeout_s: int,
) -> BootResult:
    if scenario.kind == "cli":
        return BootResult(status="skipped", detail="cli scenario")
    start_sh = project_dir / "start.sh"
    log_path = project_dir / "field-test-boot-smoke.log"
    if not start_sh.exists():
        return BootResult(status="fail", detail="missing start.sh", log_path=str(log_path))

    port_value = env.get(scenario.smoke_port_var) or env.get("PORT") or ""
    try:
        port = int(port_value)
    except ValueError:
        return BootResult(
            status="fail",
            detail=f"invalid smoke port {scenario.smoke_port_var}={port_value!r}",
            log_path=str(log_path),
        )
    url = f"http://127.0.0.1:{port}{scenario.smoke_path}"
    proc: subprocess.Popen[str] | None = None
    pgid: int | None = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"# field-test boot smoke\n# _written_at: {utc_now_iso()}\n")
            log.write(f"# url: {url}\n\n")
            log.flush()
            proc = subprocess.Popen(
                ["bash", "./start.sh"],
                cwd=str(project_dir),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
            ok, detail = _wait_for_http(url, timeout_s=boot_timeout_s)
            if ok:
                return BootResult(status="pass", detail=detail, url=url, log_path=str(log_path))
            exit_code = proc.poll()
            if exit_code is not None:
                detail = f"{detail}; start.sh exited {exit_code}"
            tail = _tail_text(log_path, max_chars=1000)
            if tail:
                detail = f"{detail}; log tail: {tail}"
            return BootResult(status="fail", detail=detail[:1200], url=url, log_path=str(log_path))
    finally:
        if proc is not None:
            _terminate_process_group(proc, pgid=pgid)


def _wait_for_http(url: str, *, timeout_s: int) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "otto-field-test/1"})
            with urllib.request.urlopen(request, timeout=2) as response:
                status = int(getattr(response, "status", response.getcode()))
                if 200 <= status < 400:
                    return True, f"HTTP {status}"
                last_error = f"HTTP {status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(1.0)
    return False, f"no successful response within {timeout_s}s ({last_error})"


def _terminate_process_group(proc: subprocess.Popen[Any], *, pgid: int | None = None) -> None:
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _tail_text(path: Path, *, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " ".join(text[-max_chars:].split())


def write_result_json(project_dir: Path, result: ScenarioResult) -> None:
    payload = asdict(result)
    payload["_written_at"] = utc_now_iso()
    path = project_dir / "field-test-result.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_report(
    *,
    results: list[ScenarioResult],
    scenarios: list[Scenario],
    run_id: str,
    run_root: Path,
    dry_run: bool,
) -> str:
    scenario_map = {s.name: s for s in scenarios}
    lines: list[str] = []
    lines.append(f"# Otto Field-Test Results - {run_id}")
    lines.append("")
    lines.append(f"_written_at: {utc_now_iso()}")
    lines.append(f"dry_run: `{str(dry_run).lower()}`")
    lines.append(f"run_root: `{run_root}`")
    lines.append("")
    if dry_run:
        lines.append("No live Otto runs were executed. Values below are command previews only.")
        lines.append("")
    lines.append("## Matrix")
    lines.append("")
    lines.append(
        "| Scenario | Tier | Expected | Shape | Nodes | Depth | Wall | Agent | Cost | Verdict | Boot | Bugs / notes |"
    )
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---|---|---|")
    for result in results:
        scenario = scenario_map[result.name]
        expected = _one_line(_drop_heading(scenario.expected_shape), limit=80)
        bugs = "; ".join(result.bugs or result.notes[:2] or ["-"])
        boot = result.boot.status
        if result.boot.detail:
            boot = f"{boot}: {_one_line(result.boot.detail, limit=40)}"
        lines.append(
            f"| `{result.name}` | `{result.tier or scenario.tier}` | {expected} | {result.shape} | "
            f"{result.tree_nodes} | {result.tree_depth} | "
            f"{fmt_seconds(result.wall_seconds)} | {fmt_seconds(result.agent_seconds)} | "
            f"{fmt_cost(result.cost_usd, dry_run=result.dry_run)} | "
            f"{result.final_verdict} | {boot} | {_one_line(bugs, limit=140)} |"
        )

    lines.append("")
    lines.append("## Details")
    for result in results:
        lines.append("")
        lines.append(f"### {result.name}")
        lines.append("")
        lines.append(f"- status: `{result.status}`")
        lines.append(f"- tier: `{result.tier or scenario_map[result.name].tier}`")
        lines.append(f"- project_dir: `{result.project_dir or '-'}`")
        lines.append(f"- otto_log: `{result.log_path or '-'}`")
        lines.append(f"- port_range: `{result.port_start}-{result.port_end}`")
        lines.append(f"- boot_url: `{result.boot.url or '-'}`")
        lines.append(f"- boot_log: `{result.boot.log_path or '-'}`")
        for note in result.notes:
            lines.append(f"- note: {note}")
        if result.bugs:
            lines.append("- bugs:")
            for bug in result.bugs:
                lines.append(f"  - {bug}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _drop_heading(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("#")).strip()


def _one_line(text: str, *, limit: int) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact or "-"
    return compact[: max(0, limit - 3)].rstrip() + "..."


def fmt_seconds(seconds: float) -> str:
    if seconds <= 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f}m"


def fmt_cost(cost: float, *, dry_run: bool) -> str:
    if dry_run:
        return "-"
    return f"${cost:.2f}"


def write_report(path: Path, report: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def run_many(
    scenarios: list[Scenario],
    *,
    run_root: Path,
    ports: list[PortAllocation],
    parallel: int,
    boot_smoke_enabled: bool,
    boot_timeout_s: int,
    dry_run: bool,
) -> list[ScenarioResult]:
    indexed = list(zip(scenarios, ports, strict=True))
    results: dict[str, ScenarioResult] = {}
    if parallel <= 1 or len(indexed) <= 1:
        for scenario, allocation in indexed:
            results[scenario.name] = run_scenario(
                scenario,
                run_root=run_root,
                ports=allocation,
                boot_smoke_enabled=boot_smoke_enabled,
                boot_timeout_s=boot_timeout_s,
                dry_run=dry_run,
            )
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            future_map = {
                pool.submit(
                    run_scenario,
                    scenario,
                    run_root=run_root,
                    ports=allocation,
                    boot_smoke_enabled=boot_smoke_enabled,
                    boot_timeout_s=boot_timeout_s,
                    dry_run=dry_run,
                ): scenario
                for scenario, allocation in indexed
            }
            for future in concurrent.futures.as_completed(future_map):
                scenario = future_map[future]
                try:
                    results[scenario.name] = future.result()
                except Exception as exc:  # noqa: BLE001 - keep matrix complete.
                    results[scenario.name] = ScenarioResult(
                        name=scenario.name,
                        status="error",
                        dry_run=dry_run,
                        tier=scenario.tier,
                        bugs=[f"driver error: {type(exc).__name__}: {exc}"],
                    )
    return [results[s.name] for s in scenarios]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", help="Run one scenario by name or unique substring.")
    parser.add_argument("--parallel", type=int, default=1, help="Concurrent scenarios to run.")
    parser.add_argument("--dry-run", action="store_true", help="Write a preview report only.")
    parser.add_argument("--report-path", type=Path, help="Report output path.")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    parser.add_argument("--port-stride", type=int, default=DEFAULT_PORT_STRIDE)
    parser.add_argument("--boot-timeout-s", type=int, default=DEFAULT_BOOT_TIMEOUT_S)
    parser.add_argument(
        "--no-boot-smoke",
        action="store_true",
        help="Do not run post-Otto start.sh smoke checks.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    scenarios = discover_scenarios()
    selected = select_scenarios(scenarios, args.scenario)
    if not selected:
        raise SystemExit(f"No scenarios found under {FIELD_TEST_ROOT}")

    if args.parallel < 1:
        raise SystemExit("--parallel must be >= 1")
    if not args.dry_run:
        require_real_cost_opt_in("Otto field tests")

    run_id = utc_stamp()
    run_root = args.runs_root / run_id
    if not args.dry_run:
        run_root.mkdir(parents=True, exist_ok=False)
    ports = allocate_ports(
        len(selected),
        base_port=args.base_port,
        stride=args.port_stride,
    )
    results = run_many(
        selected,
        run_root=run_root,
        ports=ports,
        parallel=args.parallel,
        boot_smoke_enabled=not args.no_boot_smoke,
        boot_timeout_s=args.boot_timeout_s,
        dry_run=args.dry_run,
    )
    report = render_report(
        results=results,
        scenarios=selected,
        run_id=run_id,
        run_root=run_root,
        dry_run=args.dry_run,
    )
    report_path = args.report_path or FIELD_TEST_ROOT / f"results-{run_id}.md"
    write_report(report_path, report)
    print(f"Wrote {report_path}")

    if args.dry_run:
        return 0
    return 0 if all(r.status == "pass" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
