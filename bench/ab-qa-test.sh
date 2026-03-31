#!/usr/bin/env bash
# A/B/C test: QA performance across three otto variants
#   C = pre-additive-parallelism (8045274) — tasks serialize, per-task QA
#   A = post-parallelism, pre-QA-opt (7ebe88d) — parallel, old qa.py
#   B = post-parallelism, post-QA-opt (a2f9673) — parallel, new qa.py
#
# Uses edge-conflicting-tasks with frozen specs and max_parallel=3
set -euo pipefail

REPO="/Users/yuxuan/work/cc-autonomous"
PROJ_DIR="$REPO/bench/pressure/projects/edge-conflicting-tasks"
FROZEN_TASKS="/tmp/frozen-tasks.yaml"
RESULTS_BASE="/tmp/ab-qa-test-$(date +%Y%m%d-%H%M%S)"

OTTO_A="$REPO/.worktrees/ab-variant-a/.venv/bin/otto"
OTTO_B="$REPO/.worktrees/ab-variant-b/.venv/bin/otto"
OTTO_C="$REPO/.worktrees/ab-variant-c/.venv/bin/otto"

mkdir -p "$RESULTS_BASE"
echo "Results dir: $RESULTS_BASE"
echo "Frozen tasks: $FROZEN_TASKS"

run_one() {
    local VARIANT="$1"
    local RUN_NUM="$2"
    local OTTO_BIN="$3"
    local RUN_LABEL="${VARIANT}${RUN_NUM}"
    local RUN_DIR="$RESULTS_BASE/$RUN_LABEL"
    local WORK_DIR="/tmp/ab-run-${RUN_LABEL}"

    mkdir -p "$RUN_DIR"
    echo ""
    echo "=========================================="
    echo "  Starting $RUN_LABEL at $(date +%H:%M:%S)"
    echo "  Otto: $OTTO_BIN"
    echo "=========================================="

    # Fresh project dir
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"
    cd "$WORK_DIR"
    git init -q
    git config user.email "ab-test@otto.dev"
    git config user.name "AB Test"
    bash "$PROJ_DIR/setup.sh" 2>/dev/null

    # Place frozen specs + config
    cp "$FROZEN_TASKS" tasks.yaml
    cat > otto.yaml << 'YAML'
max_parallel: 3
max_retries: 3
test_command: pytest
verify_timeout: 300
YAML

    # Run
    local RUN_START
    RUN_START=$(date +%s)
    "$OTTO_BIN" run 2>&1 | tee "$RUN_DIR/output.txt" || true
    local RUN_END
    RUN_END=$(date +%s)
    local TOTAL_TIME=$((RUN_END - RUN_START))

    # Copy artifacts
    cp tasks.yaml "$RUN_DIR/tasks.yaml" 2>/dev/null || true
    cp otto.yaml "$RUN_DIR/otto.yaml" 2>/dev/null || true
    cp -r otto_logs "$RUN_DIR/otto_logs" 2>/dev/null || true

    # Record metadata
    cat > "$RUN_DIR/meta.txt" << META
VARIANT: $VARIANT
RUN_NUM: $RUN_NUM
TOTAL_TIME: ${TOTAL_TIME}s
OTTO_BIN: $OTTO_BIN
META

    # Check if planner used parallel
    if [ -f otto_logs/planner.log ]; then
        if grep -q "parallel" otto_logs/planner.log 2>/dev/null; then
            echo "PARALLEL: yes" >> "$RUN_DIR/meta.txt"
        else
            echo "PARALLEL: no" >> "$RUN_DIR/meta.txt"
        fi
    fi

    echo "  Completed $RUN_LABEL in ${TOTAL_TIME}s"
    cd "$REPO"
    rm -rf "$WORK_DIR"
}

# Interleaved execution: A1, B1, C1, A2, B2, C2
for i in 1 2; do
    run_one "A" "$i" "$OTTO_A"
    run_one "B" "$i" "$OTTO_B"
    run_one "C" "$i" "$OTTO_C"
done

echo ""
echo "=========================================="
echo "  All runs complete. Results in: $RESULTS_BASE"
echo "=========================================="

# Extract metrics
"$REPO/.venv/bin/python" << 'PYEOF'
import json, re, os, sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Need PyYAML")

results_base = None
for d in sorted(Path("/tmp").glob("ab-qa-test-*")):
    results_base = d
if not results_base:
    sys.exit("No results found")

print(f"\nResults from: {results_base}\n")

runs = {}
for run_dir in sorted(results_base.iterdir()):
    if not run_dir.is_dir():
        continue
    label = run_dir.name
    m = {}

    # Meta
    meta = (run_dir / "meta.txt").read_text()
    match = re.search(r'TOTAL_TIME: (\d+)s', meta)
    m["total_s"] = int(match.group(1)) if match else 0
    m["parallel"] = "PARALLEL: yes" in meta

    # QA metrics from batch-qa log
    otto_logs = run_dir / "otto_logs"
    batch_dirs = sorted(otto_logs.glob("batch-qa-*"))
    per_task_qa_dirs = [d for d in otto_logs.iterdir() if d.is_dir() and not d.name.startswith("batch-qa") and (d / "qa-agent.log").exists()]

    if batch_dirs:
        qa_log = batch_dirs[-1] / "qa-agent.log"
        if qa_log.exists():
            text = qa_log.read_text()
            match = re.search(r'total: ([\d.]+)s\s+turns: (\d+)\s+cost: \$([\d.]+)', text)
            if match:
                m["qa_time"] = float(match.group(1))
                m["qa_turns"] = int(match.group(2))
                m["qa_cost"] = float(match.group(3))
            m["break_findings"] = text.count("\u26a0")

        regression_sh = batch_dirs[-1] / "qa-proofs" / "regression-check.sh"
        if regression_sh.exists():
            lines = [l for l in regression_sh.read_text().splitlines()
                     if l.strip() and not l.startswith("#") and not l.startswith("set ") and not l.startswith("echo ")]
            m["proof_cmds"] = len(lines)

        proof_report = batch_dirs[-1] / "qa-proofs" / "proof-report.md"
        if proof_report.exists():
            text = proof_report.read_text()
            match = re.search(r'(\d+)/(\d+) must items', text)
            if match:
                m["must_pass"] = int(match.group(1))
                m["must_total"] = int(match.group(2))
    elif per_task_qa_dirs:
        # Per-task QA (variant C)
        total_qa_time = 0
        total_qa_turns = 0
        total_qa_cost = 0
        total_break = 0
        total_proof_cmds = 0
        total_must_pass = 0
        total_must_total = 0
        for d in per_task_qa_dirs:
            qa_log = d / "qa-agent.log"
            if qa_log.exists():
                text = qa_log.read_text()
                match = re.search(r'total: ([\d.]+)s\s+turns: (\d+)\s+cost: \$([\d.]+)', text)
                if match:
                    total_qa_time += float(match.group(1))
                    total_qa_turns += int(match.group(2))
                    total_qa_cost += float(match.group(3))
                total_break += text.count("\u26a0")
            regression_sh = d / "qa-proofs" / "regression-check.sh"
            if regression_sh.exists():
                lines = [l for l in regression_sh.read_text().splitlines()
                         if l.strip() and not l.startswith("#") and not l.startswith("set ") and not l.startswith("echo ")]
                total_proof_cmds += len(lines)
            proof_report = d / "qa-proofs" / "proof-report.md"
            if proof_report.exists():
                text = proof_report.read_text()
                match = re.search(r'(\d+)/(\d+) must items', text)
                if match:
                    total_must_pass += int(match.group(1))
                    total_must_total += int(match.group(2))
        m["qa_time"] = round(total_qa_time, 1)
        m["qa_turns"] = total_qa_turns
        m["qa_cost"] = round(total_qa_cost, 2)
        m["break_findings"] = total_break
        m["proof_cmds"] = total_proof_cmds
        m["must_pass"] = total_must_pass
        m["must_total"] = total_must_total

    # Task results
    tasks_file = run_dir / "tasks.yaml"
    if tasks_file.exists():
        data = yaml.safe_load(tasks_file.read_text())
        tasks = data.get("tasks", [])
        m["tasks_pass"] = sum(1 for t in tasks if t.get("status") == "passed")
        m["tasks_fail"] = sum(1 for t in tasks if t.get("status") == "failed")

    runs[label] = m

# Print table
header = f"{'Run':<6} {'Total':>6} {'QA_t':>6} {'QA_n':>5} {'QA_$':>6} {'Must':>7} {'Proof':>6} {'Break':>6} {'Pass':>5} {'Par':>4}"
print(header)
print("-" * len(header))
for label in sorted(runs.keys()):
    m = runs[label]
    must = f"{m.get('must_pass','?')}/{m.get('must_total','?')}"
    par = "Y" if m.get("parallel") else "N"
    print(f"{label:<6} {m.get('total_s',0):>5}s {m.get('qa_time',0):>5.0f}s {m.get('qa_turns',0):>5} {m.get('qa_cost',0):>5.2f} {must:>7} {m.get('proof_cmds',0):>6} {m.get('break_findings',0):>6} {m.get('tasks_pass',0):>4}/3 {par:>4}")

PYEOF
