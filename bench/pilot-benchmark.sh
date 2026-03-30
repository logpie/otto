#!/usr/bin/env bash
# Gate pilot A/B benchmark — runs multi-task projects with and without pilot.
# Usage: ./bench/pilot-benchmark.sh [project_name]
#
# Runs each project TWICE:
#   1. pilot=true  (gate pilot enabled — default)
#   2. pilot=false (fallback to replan — baseline)
#
# Compares: pass rate, cost, retry success, pilot.log decisions.
# Results: /tmp/otto-pilot-benchmark/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECTS_DIR="$REPO_DIR/bench/pressure/projects"
RESULTS_DIR="/tmp/otto-pilot-benchmark"
TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
RUN_DIR="$RESULTS_DIR/$TIMESTAMP"

OTTO_BIN="${OTTO_BIN:-}"
if [[ -z "$OTTO_BIN" ]]; then
    repo_otto="$REPO_DIR/.venv/bin/otto"
    if [[ -x "$repo_otto" ]]; then
        OTTO_BIN="$repo_otto"
    else
        OTTO_BIN="otto"
    fi
fi
[[ "$OTTO_BIN" == */* ]] && OTTO_BIN="$(cd "$(dirname "$OTTO_BIN")" && pwd)/$(basename "$OTTO_BIN")"

# Default projects: multi-task only (pilot only activates with batch failures)
DEFAULT_PROJECTS=(
    edge-conflicting-tasks
    multi-blog-engine
    multi-expense-tracker
)

FILTER="${1:-}"
PROJECT_NAMES=()
if [[ -n "$FILTER" ]]; then
    PROJECT_NAMES+=("$FILTER")
else
    PROJECT_NAMES=("${DEFAULT_PROJECTS[@]}")
fi

mkdir -p "$RUN_DIR"

echo "============================================"
echo "  Gate Pilot A/B Benchmark — $TIMESTAMP"
echo "  Projects: ${PROJECT_NAMES[*]}"
echo "  Results: $RUN_DIR"
echo "============================================"
echo ""

run_project() {
    local proj="$1"
    local pilot_flag="$2"   # "true" or "false"
    local label="$3"        # "pilot" or "baseline"
    local proj_dir="$PROJECTS_DIR/$proj"
    local result_dir="$RUN_DIR/$proj/$label"
    local work_dir="/tmp/pb-$proj-$label"

    mkdir -p "$result_dir"
    rm -rf "$work_dir"
    mkdir -p "$work_dir"

    echo "  [$label] Setting up..."

    # Setup
    (
        cd "$work_dir"
        git init -q
        git config user.email "benchmark@otto.dev"
        git config user.name "Benchmark"
        bash "$proj_dir/setup.sh"
    ) > "$result_dir/setup.log" 2>&1

    # Write otto.yaml with pilot flag
    cat > "$work_dir/otto.yaml" << EOF
test_command: auto
max_retries: 2
max_parallel: 1
pilot: $pilot_flag
EOF

    # Add initial commit
    (cd "$work_dir" && git add -A && git commit -q -m "initial setup")

    # Add tasks
    (
        cd "$work_dir"
        while IFS= read -r line; do
            line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
            [[ -z "$line" || "$line" == \#* ]] && continue
            env CLAUDECODE= "$OTTO_BIN" add "$line"
        done < "$proj_dir/tasks.txt"
    ) > "$result_dir/add.log" 2>&1

    # Run otto
    echo "  [$label] Running otto (pilot=$pilot_flag)..."
    local start_time=$(date +%s)
    set +e
    (
        cd "$work_dir"
        env CLAUDECODE= "$OTTO_BIN" run 2>&1
    ) | tee "$result_dir/output.txt"
    local exit_code=${PIPESTATUS[0]}
    set -e
    local end_time=$(date +%s)
    local duration=$((end_time - start_time))

    # Copy artifacts
    cp "$work_dir/tasks.yaml" "$result_dir/" 2>/dev/null || true
    cp "$work_dir/otto.yaml" "$result_dir/" 2>/dev/null || true
    cp -r "$work_dir/otto_logs" "$result_dir/" 2>/dev/null || true

    # Parse results
    local passed=0 failed=0 cost="0.00"
    if [[ -f "$work_dir/tasks.yaml" ]]; then
        passed=$(grep -c 'status: passed' "$work_dir/tasks.yaml" 2>/dev/null || true)
        failed=$(grep -c 'status: failed' "$work_dir/tasks.yaml" 2>/dev/null || true)
        cost=$(grep 'cost_usd:' "$work_dir/tasks.yaml" 2>/dev/null \
            | sed 's/.*cost_usd:[[:space:]]*//' \
            | awk '{s += $1} END {printf "%.2f", s}' 2>/dev/null || echo "0.00")
    fi

    # Independent verification
    local verify="n/a"
    if [[ -f "$proj_dir/verify.sh" && $passed -gt 0 ]]; then
        if (cd "$work_dir" && bash "$proj_dir/verify.sh") > "$result_dir/verify.log" 2>&1; then
            verify="PASS"
        else
            verify="FAIL"
        fi
    fi

    # Write summary
    cat > "$result_dir/summary.json" << EOF
{
  "project": "$proj",
  "mode": "$label",
  "pilot": $pilot_flag,
  "passed": $passed,
  "failed": $failed,
  "cost_usd": $cost,
  "duration_s": $duration,
  "verify": "$verify",
  "exit_code": $exit_code
}
EOF

    echo "  [$label] Done: ${passed}p/${failed}f, \$$cost, ${duration}s, verify=$verify"

    # Check for pilot.log
    if [[ "$label" == "pilot" && -f "$work_dir/otto_logs/pilot.log" ]]; then
        echo "  [$label] Pilot log available at $result_dir/otto_logs/pilot.log"
    fi

    # Cleanup workdir
    rm -rf "$work_dir"
}

for proj in "${PROJECT_NAMES[@]}"; do
    echo ""
    echo "────────────────────────────────────────────"
    echo "  Project: $proj"
    echo "────────────────────────────────────────────"

    # Run baseline first (no pilot), then pilot
    run_project "$proj" "false" "baseline"
    echo ""
    run_project "$proj" "true" "pilot"
done

# Summary comparison
echo ""
echo "============================================"
echo "  COMPARISON SUMMARY"
echo "============================================"
echo ""
printf "%-30s %-10s %-6s %-6s %-8s %-6s %-8s\n" "Project" "Mode" "Pass" "Fail" "Cost" "Time" "Verify"
printf "%-30s %-10s %-6s %-6s %-8s %-6s %-8s\n" "-------" "----" "----" "----" "----" "----" "------"

for proj in "${PROJECT_NAMES[@]}"; do
    for label in baseline pilot; do
        summary="$RUN_DIR/$proj/$label/summary.json"
        if [[ -f "$summary" ]]; then
            passed=$(python3 -c "import json; print(json.load(open('$summary'))['passed'])")
            failed=$(python3 -c "import json; print(json.load(open('$summary'))['failed'])")
            cost=$(python3 -c "import json; print(json.load(open('$summary'))['cost_usd'])")
            duration=$(python3 -c "import json; print(json.load(open('$summary'))['duration_s'])")
            verify=$(python3 -c "import json; print(json.load(open('$summary'))['verify'])")
            printf "%-30s %-10s %-6s %-6s \$%-7s %-6s %-8s\n" "$proj" "$label" "$passed" "$failed" "$cost" "${duration}s" "$verify"
        fi
    done
done

echo ""
echo "Full results at: $RUN_DIR"
echo ""
echo "To review pilot decisions:"
echo "  cat $RUN_DIR/*/pilot/otto_logs/pilot.log"
echo ""
echo "To compare orchestrator logs:"
echo "  diff $RUN_DIR/<project>/baseline/otto_logs/orchestrator.log \\"
echo "       $RUN_DIR/<project>/pilot/otto_logs/orchestrator.log"
