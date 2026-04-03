#!/usr/bin/env python3
"""Generate an HTML timing/breakdown report from benchmark result directories.

Usage:
  .venv/bin/python tools/bench_breakdown_report.py \
      --runs retest-blog-parallel,retest-edge-parallel \
      --output /tmp/bench-breakdown.html
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - tool still works without tasks.yaml parsing
    yaml = None


RESULTS_ROOT = Path("bench/pressure/results")
QA_LINE_RE = re.compile(r"^\[\s*([0-9]+(?:\.[0-9])?)s\] ● Bash\s+(.*)$")
ORCH_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)$")
PHASE_COLORS = {
    "prepare": "#7c8ea3",
    "spec_gen": "#5b8c5a",
    "coding": "#2c6db4",
    "test": "#7a52b3",
    "qa": "#b46a2c",
    "merge": "#8a3b5f",
}


@dataclass
class QaBreakdown:
    name: str
    total_s: float
    buckets: dict[str, float]
    top_steps: list[dict[str, Any]]
    proof_of_work: bool | None = None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--results-root", type=Path, default=RESULTS_ROOT)
    p.add_argument("--runs", required=True, help="Comma-separated run labels under results root")
    p.add_argument("--output", type=Path, default=Path("/tmp/bench-breakdown.html"))
    return p.parse_args()


def _bucket_for_command(cmd: str) -> str:
    lower = cmd.lower()
    if any(token in lower for token in ("cat <<", "cat >", "verdict", "summary.json")):
        return "verdict_write"
    if "npm install" in lower or "pnpm install" in lower or "pip install" in lower:
        return "install"
    if any(token in lower for token in ("pytest", "npm test", "npx jest", "vitest", "cargo test", "go test")):
        return "test_run"
    if any(token in lower for token in ("sed -n", "rg -n", "rg --files", "pwd", "git status", "cat ", "ls -")):
        return "source_read"
    if any(token in lower for token in ("http", "fetch(", "createapp", "localhost", "analyticsengine", "blogservice")):
        return "integration_probe"
    if "break" in lower:
        return "break_probe"
    if any(token in lower for token in ("python - <<", "node <<", "node - <<")):
        return "direct_api"
    return "other"


def parse_qa_agent_breakdown(path: Path) -> QaBreakdown:
    buckets = {
        "source_read": 0.0,
        "test_run": 0.0,
        "direct_api": 0.0,
        "integration_probe": 0.0,
        "break_probe": 0.0,
        "install": 0.0,
        "verdict_write": 0.0,
        "other": 0.0,
    }
    steps: list[dict[str, Any]] = []
    last_ts = 0.0
    total_s = 0.0
    for line in path.read_text().splitlines():
        match = QA_LINE_RE.match(line)
        if not match:
            continue
        ts = float(match.group(1))
        cmd = match.group(2)
        delta = max(0.0, ts - last_ts)
        last_ts = ts
        total_s = ts
        bucket = _bucket_for_command(cmd)
        buckets[bucket] += delta
        steps.append({
            "ts": ts,
            "delta": delta,
            "bucket": bucket,
            "command": cmd,
        })
    top_steps = sorted(steps, key=lambda step: step["delta"], reverse=True)[:8]
    return QaBreakdown(
        name=path.parent.name,
        total_s=total_s,
        buckets=buckets,
        top_steps=top_steps,
    )


def parse_qa_profile(path: Path) -> QaBreakdown:
    data = json.loads(path.read_text())
    steps = data.get("steps", []) or []
    top_steps = []
    for step in sorted(steps, key=lambda item: float(item.get("delta", 0.0) or 0.0), reverse=True)[:8]:
        top_steps.append({
            "ts": float(step.get("ts", 0.0) or 0.0),
            "delta": float(step.get("delta", 0.0) or 0.0),
            "bucket": str(step.get("bucket", "") or ""),
            "command": str(step.get("command", "") or ""),
            "label": str(step.get("label", "") or ""),
        })
    return QaBreakdown(
        name=path.parent.name,
        total_s=float(data.get("total_s", 0.0) or 0.0),
        buckets={k: float(v or 0.0) for k, v in (data.get("bucket_totals", {}) or {}).items()},
        top_steps=top_steps,
        proof_of_work=data.get("proof_of_work"),
    )


def parse_orchestrator_timeline(path: Path) -> list[dict[str, str]]:
    timeline: list[dict[str, str]] = []
    for line in path.read_text().splitlines():
        match = ORCH_LINE_RE.match(line)
        if not match:
            continue
        timeline.append({"timestamp": match.group(1), "message": match.group(2)})
    return timeline


def _load_yaml(path: Path) -> Any:
    if yaml is None or not path.exists():
        return None
    return yaml.safe_load(path.read_text())


def load_project_report(run_dir: Path, project_dir: Path) -> dict[str, Any]:
    result_path = project_dir / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    tasks_data = _load_yaml(project_dir / "tasks.yaml") or {}
    task_specs = {
        task.get("key", ""): len(task.get("spec") or [])
        for task in tasks_data.get("tasks", [])
        if isinstance(task, dict)
    }

    otto_logs = project_dir / "otto_logs"
    task_summaries = []
    qa_breakdowns = []
    timeline = []
    if otto_logs.exists():
        for summary_path in sorted(otto_logs.glob("*/task-summary.json")):
            data = json.loads(summary_path.read_text())
            task_summaries.append({
                "name": summary_path.parent.name,
                "attempts": data.get("attempts", 0),
                "status": data.get("status", ""),
                "duration_s": data.get("total_duration_s", 0.0),
                "phase_timings": data.get("phase_timings", {}),
                "spec_count": task_specs.get(summary_path.parent.name, None),
            })
        qa_dirs = sorted(otto_logs.glob("batch-qa-*"))
        for qa_dir in qa_dirs:
            profile_path = qa_dir / "qa-profile.json"
            if profile_path.exists():
                qa_breakdowns.append(parse_qa_profile(profile_path).__dict__)
                continue
            qa_path = qa_dir / "qa-agent.log"
            if qa_path.exists():
                qa_breakdowns.append(parse_qa_agent_breakdown(qa_path).__dict__)
        orch_path = otto_logs / "orchestrator.log"
        if orch_path.exists():
            timeline = parse_orchestrator_timeline(orch_path)

    return {
        "run_label": run_dir.name,
        "project": project_dir.name,
        "result": result,
        "task_summaries": task_summaries,
        "qa_breakdowns": qa_breakdowns,
        "timeline": timeline,
    }


def _aggregate_project_profile(project: dict[str, Any]) -> dict[str, Any]:
    phase_totals = {phase: 0.0 for phase in PHASE_COLORS}
    task_hotspots: list[dict[str, Any]] = []
    phase_hotspots: list[dict[str, Any]] = []
    for task in project.get("task_summaries", []):
        task_hotspots.append({
            "task": task["name"],
            "duration_s": float(task.get("duration_s", 0.0) or 0.0),
            "attempts": task.get("attempts", 0),
            "status": task.get("status", ""),
        })
        for phase, value in (task.get("phase_timings", {}) or {}).items():
            if phase in phase_totals:
                value_f = float(value or 0.0)
                phase_totals[phase] += value_f
                if value_f:
                    phase_hotspots.append({
                        "task": task["name"],
                        "phase": phase,
                        "duration_s": value_f,
                    })

    qa_bucket_totals = {
        "source_read": 0.0,
        "test_run": 0.0,
        "direct_api": 0.0,
        "integration_probe": 0.0,
        "break_probe": 0.0,
        "install": 0.0,
        "verdict_write": 0.0,
        "other": 0.0,
    }
    qa_step_hotspots: list[dict[str, Any]] = []
    for qa in project.get("qa_breakdowns", []):
        for bucket, value in (qa.get("buckets", {}) or {}).items():
            qa_bucket_totals[bucket] = qa_bucket_totals.get(bucket, 0.0) + float(value or 0.0)
        for step in qa.get("top_steps", []):
            qa_step_hotspots.append({
                "qa": qa.get("name", ""),
                "bucket": step.get("bucket", ""),
                "duration_s": float(step.get("delta", 0.0) or 0.0),
                "command": step.get("command", ""),
                "label": step.get("label", "") or step.get("command", ""),
            })

    return {
        "phase_totals": phase_totals,
        "task_hotspots": sorted(task_hotspots, key=lambda row: row["duration_s"], reverse=True),
        "phase_hotspots": sorted(phase_hotspots, key=lambda row: row["duration_s"], reverse=True),
        "qa_bucket_totals": qa_bucket_totals,
        "qa_step_hotspots": sorted(qa_step_hotspots, key=lambda row: row["duration_s"], reverse=True),
    }


def collect_runs(results_root: Path, labels: list[str]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for label in labels:
        run_dir = results_root / label
        summary_path = run_dir / "summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        projects = []
        for child in sorted(run_dir.iterdir()):
            if not child.is_dir():
                continue
            if (child / "result.json").exists():
                projects.append(load_project_report(run_dir, child))
        runs.append({
            "label": label,
            "summary": summary.get("summary", {}),
            "projects": projects,
        })
    return runs


def _json_for_html(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _build_html(runs: list[dict[str, Any]]) -> str:
    data_json = _json_for_html(runs)
    colors_json = _json_for_html(PHASE_COLORS)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Bench Breakdown</title>
  <style>
    :root {{
      --bg: #0f1720;
      --panel: #17212b;
      --panel-2: #1d2a36;
      --text: #e7eef5;
      --muted: #9cb0c3;
      --accent: #7fd1b9;
      --border: #2b3b49;
    }}
    body {{
      margin: 0;
      padding: 24px;
      background: linear-gradient(180deg, #0d141a 0%, #0f1720 55%, #111b24 100%);
      color: var(--text);
      font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    h1, h2, h3 {{ margin: 0 0 12px; }}
    .controls, .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: 0 10px 30px rgba(0,0,0,.18);
    }}
    .controls {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      align-items: end;
    }}
    label {{ display: grid; gap: 6px; color: var(--muted); }}
    select {{
      background: var(--panel-2);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .card {{
      background: var(--panel-2);
      border-radius: 12px;
      padding: 12px;
      border: 1px solid var(--border);
    }}
    .card .label {{ color: var(--muted); font-size: 12px; }}
    .card .value {{ font-size: 24px; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      text-align: left;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    .stack {{
      display: flex;
      width: 100%;
      height: 18px;
      overflow: hidden;
      border-radius: 999px;
      background: #0c1117;
      border: 1px solid var(--border);
    }}
    .seg {{ height: 100%; min-width: 2px; }}
    .legend {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
    .swatch {{ width: 10px; height: 10px; border-radius: 999px; display: inline-block; }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
      word-break: break-word;
      background: #0f151d;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
    }}
    .top-steps li {{ margin-bottom: 6px; }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
  <h1>Benchmark Breakdown Report</h1>
  <div class="controls">
    <label>Run
      <select id="run-select"></select>
    </label>
    <label>Project
      <select id="project-select"></select>
    </label>
  </div>

  <div class="panel">
    <h2>Run Summary</h2>
    <div class="cards" id="summary-cards"></div>
  </div>

  <div class="panel">
    <h2>Where Time Went</h2>
    <div id="profile-summary"></div>
  </div>

  <div class="panel">
    <h2>Task Phase Timing</h2>
    <div id="task-table"></div>
  </div>

  <div class="panel">
    <h2>Batch QA Breakdown</h2>
    <div id="qa-breakdowns"></div>
  </div>

  <div class="panel">
    <h2>Orchestrator Timeline</h2>
    <div class="mono" id="timeline"></div>
  </div>

  <script>
    const RUNS = {data_json};
    const PHASE_COLORS = {colors_json};

    const runSelect = document.getElementById('run-select');
    const projectSelect = document.getElementById('project-select');

    function fmtSeconds(value) {{
      return `${{Number(value || 0).toFixed(1)}}s`;
    }}

    function populateRuns() {{
      runSelect.innerHTML = RUNS.map((run, idx) => `<option value="${{idx}}">${{run.label}}</option>`).join('');
    }}

    function populateProjects() {{
      const run = RUNS[runSelect.value || 0];
      projectSelect.innerHTML = run.projects.map((proj, idx) => `<option value="${{idx}}">${{proj.project}}</option>`).join('');
    }}

    function renderSummary(run, project) {{
      const summary = run.summary || {{}};
      const result = project.result || {{}};
      const cards = [
        ['Runner Pass', result.runner_pass || ''],
        ['Verify Pass', result.verify_pass || ''],
        ['Runtime', `${{result.time_s || 0}}s`],
        ['Attempts', result.attempts || 0],
      ];
      document.getElementById('summary-cards').innerHTML = cards.map(([label, value]) => `
        <div class="card">
          <div class="label">${{label}}</div>
          <div class="value">${{value}}</div>
        </div>
      `).join('');
    }}

    function renderProfile(project) {{
      const profile = project.profile || {{}};
      const phaseTotals = profile.phase_totals || {{}};
      const qaTotals = profile.qa_bucket_totals || {{}};
      const runtime = Number((project.result || {{}}).time_s || 0) || 1;
      const qaModes = [...new Set((project.qa_breakdowns || []).map((qa) => qa.proof_of_work).filter((value) => value !== null && value !== undefined))];

      const phaseRows = Object.entries(phaseTotals).map(([name, value]) => {{
        const width = Math.max(0, (Number(value || 0) / runtime) * 100);
        return `
          <tr>
            <td>${{name}}</td>
            <td>${{fmtSeconds(value)}}</td>
            <td>${{(width).toFixed(1)}}%</td>
            <td><div class="stack"><div class="seg" style="width:${{width}}%;background:${{PHASE_COLORS[name]}}"></div></div></td>
          </tr>
        `;
      }}).join('');

      const qaRows = Object.entries(qaTotals).map(([name, value]) => {{
        const width = Math.max(0, (Number(value || 0) / runtime) * 100);
        return `
          <tr>
            <td>${{name}}</td>
            <td>${{fmtSeconds(value)}}</td>
            <td>${{(width).toFixed(1)}}%</td>
            <td><div class="stack"><div class="seg" style="width:${{width}}%;background:#7fd1b9"></div></div></td>
          </tr>
        `;
      }}).join('');

      const topTasks = (profile.task_hotspots || []).slice(0, 8).map((row) => `
        <tr><td class="mono">${{row.task}}</td><td>${{fmtSeconds(row.duration_s)}}</td><td>${{row.attempts}}</td><td>${{row.status}}</td></tr>
      `).join('');
      const topPhases = (profile.phase_hotspots || []).slice(0, 8).map((row) => `
        <tr><td class="mono">${{row.task}}</td><td>${{row.phase}}</td><td>${{fmtSeconds(row.duration_s)}}</td></tr>
      `).join('');
      const topQaSteps = (profile.qa_step_hotspots || []).slice(0, 8).map((row) => `
        <tr><td class="mono">${{row.qa}}</td><td>${{row.bucket}}</td><td>${{fmtSeconds(row.duration_s)}}</td><td class="mono">${{row.label || row.command}}</td></tr>
      `).join('');

      document.getElementById('profile-summary').innerHTML = `
        <div class="cards" style="margin-bottom:12px">
          <div class="card"><div class="label">Runtime</div><div class="value">${{runtime.toFixed(0)}}s</div></div>
          <div class="card"><div class="label">Coding Total</div><div class="value">${{fmtSeconds(phaseTotals.coding || 0)}}</div></div>
          <div class="card"><div class="label">QA Total</div><div class="value">${{fmtSeconds(phaseTotals.qa || 0)}}</div></div>
          <div class="card"><div class="label">Batch QA Direct Proofs</div><div class="value">${{fmtSeconds(qaTotals.direct_api || 0)}}</div></div>
          <div class="card"><div class="label">Proof Of Work</div><div class="value">${{qaModes.length ? (qaModes.every(Boolean) ? 'on' : qaModes.every((v) => !v) ? 'off' : 'mixed') : 'n/a'}}</div></div>
        </div>
        <h3>Task Phase Totals vs Runtime</h3>
        <table>
          <thead><tr><th>Phase</th><th>Time</th><th>% runtime</th><th>Bar</th></tr></thead>
          <tbody>${{phaseRows}}</tbody>
        </table>
        <h3 style="margin-top:14px">Batch QA Buckets vs Runtime</h3>
        <table>
          <thead><tr><th>Bucket</th><th>Time</th><th>% runtime</th><th>Bar</th></tr></thead>
          <tbody>${{qaRows}}</tbody>
        </table>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px">
          <div>
            <h3>Top Task Hotspots</h3>
            <table>
              <thead><tr><th>Task</th><th>Time</th><th>Attempts</th><th>Status</th></tr></thead>
              <tbody>${{topTasks}}</tbody>
            </table>
          </div>
          <div>
            <h3>Top Phase Hotspots</h3>
            <table>
              <thead><tr><th>Task</th><th>Phase</th><th>Time</th></tr></thead>
              <tbody>${{topPhases}}</tbody>
            </table>
          </div>
        </div>
        <h3 style="margin-top:14px">Top Batch QA Steps</h3>
        <table>
          <thead><tr><th>QA Run</th><th>Bucket</th><th>Time</th><th>Command</th></tr></thead>
          <tbody>${{topQaSteps}}</tbody>
        </table>
      `;
    }}

    function renderTaskTable(project) {{
      const rows = project.task_summaries || [];
      const legend = Object.entries(PHASE_COLORS).map(([name, color]) => `<span><i class="swatch" style="background:${{color}}"></i>${{name}}</span>`).join('');
      const table = rows.map((row) => {{
        const phases = row.phase_timings || {{}};
        const total = Object.values(phases).reduce((a, b) => a + Number(b || 0), 0) || 1;
        const segs = Object.entries(PHASE_COLORS).map(([phase, color]) => {{
          const value = Number(phases[phase] || 0);
          if (!value) return '';
          return `<div class="seg" style="width:${{(value / total) * 100}}%;background:${{color}}" title="${{phase}} ${{value.toFixed(1)}}s"></div>`;
        }}).join('');
        return `
          <tr>
            <td class="mono">${{row.name}}</td>
            <td>${{row.attempts}}</td>
            <td>${{fmtSeconds(row.duration_s)}}</td>
            <td><div class="stack">${{segs}}</div></td>
          </tr>
        `;
      }}).join('');
      document.getElementById('task-table').innerHTML = `
        <table>
          <thead><tr><th>Task</th><th>Attempts</th><th>Total</th><th>Phases</th></tr></thead>
          <tbody>${{table}}</tbody>
        </table>
        <div class="legend">${{legend}}</div>
      `;
    }}

    function renderQa(project) {{
      const blocks = (project.qa_breakdowns || []).map((qa) => {{
        const bucketRows = Object.entries(qa.buckets || {{}}).map(([name, value]) => `
          <tr><td>${{name}}</td><td>${{fmtSeconds(value)}}</td></tr>
        `).join('');
        const topSteps = (qa.top_steps || []).map((step) => `<li><span class="muted">${{fmtSeconds(step.delta)}}</span> <span class="mono">${{step.label || step.command}}</span></li>`).join('');
        return `
          <div class="panel" style="margin:12px 0 0;padding:12px">
            <h3>${{qa.name}} <span class="muted">(${{
              fmtSeconds(qa.total_s)
            }})</span> <span class="muted">(proof_of_work: ${{qa.proof_of_work === true ? 'on' : qa.proof_of_work === false ? 'off' : 'unknown'}})</span></h3>
            <table>
              <thead><tr><th>Bucket</th><th>Time</th></tr></thead>
              <tbody>${{bucketRows}}</tbody>
            </table>
            <div class="muted" style="margin-top:10px">Top steps</div>
            <ol class="top-steps">${{topSteps}}</ol>
          </div>
        `;
      }}).join('');
      document.getElementById('qa-breakdowns').innerHTML = blocks || '<div class="muted">No batch QA logs found.</div>';
    }}

    function renderTimeline(project) {{
      const lines = (project.timeline || []).map((entry) => `${{entry.timestamp}}  ${{entry.message}}`).join('\\n');
      document.getElementById('timeline').textContent = lines || 'No orchestrator timeline found.';
    }}

    function render() {{
      const run = RUNS[runSelect.value || 0];
      const project = run.projects[projectSelect.value || 0];
      renderSummary(run, project);
      renderProfile(project);
      renderTaskTable(project);
      renderQa(project);
      renderTimeline(project);
    }}

    runSelect.addEventListener('change', () => {{ populateProjects(); render(); }});
    projectSelect.addEventListener('change', render);
    populateRuns();
    populateProjects();
    render();
  </script>
</body>
</html>
"""


def main() -> int:
    args = _parse_args()
    labels = [label.strip() for label in args.runs.split(",") if label.strip()]
    runs = collect_runs(args.results_root, labels)
    for run in runs:
        for project in run["projects"]:
            project["profile"] = _aggregate_project_profile(project)
    html = _build_html(runs)
    args.output.write_text(html, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
