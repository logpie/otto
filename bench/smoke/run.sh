#!/usr/bin/env bash
# Otto smoke test suite — validates core functionality across project types.
# Usage: ./bench/smoke/run.sh [project_name]  (omit name to run all)
# Results written to bench/smoke/results/YYYY-MM-DD-HHMMSS/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_DIR="$SCRIPT_DIR/projects"
RESULTS_BASE="$SCRIPT_DIR/results"

# Otto binary: env var > fallback to repo .venv/bin/otto > PATH
OTTO_BIN="${OTTO_BIN:-}"
if [[ -z "$OTTO_BIN" ]]; then
    repo_otto="$(cd "$SCRIPT_DIR/../.." && pwd)/.venv/bin/otto"
    if [[ -x "$repo_otto" ]]; then
        OTTO_BIN="$repo_otto"
    else
        OTTO_BIN="otto"
    fi
fi

# Verify otto exists
if ! command -v "$OTTO_BIN" &>/dev/null && [[ ! -x "$OTTO_BIN" ]]; then
    echo "ERROR: otto not found at '$OTTO_BIN'. Set OTTO_BIN or install otto." >&2
    exit 1
fi
echo "Using otto: $OTTO_BIN"

# Timestamp for this run
TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
RESULTS_DIR="$RESULTS_BASE/$TIMESTAMP"
mkdir -p "$RESULTS_DIR"

# Git metadata for results
OTTO_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
OTTO_BRANCH="$(cd "$OTTO_REPO" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
OTTO_COMMIT="$(cd "$OTTO_REPO" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")"

# Collect project list
FILTER="${1:-}"
PROJECT_NAMES=()
if [[ -n "$FILTER" ]]; then
    if [[ -d "$PROJECTS_DIR/$FILTER" ]]; then
        PROJECT_NAMES+=("$FILTER")
    else
        echo "ERROR: project '$FILTER' not found in $PROJECTS_DIR" >&2
        exit 1
    fi
else
    for dir in "$PROJECTS_DIR"/*/; do
        PROJECT_NAMES+=("$(basename "$dir")")
    done
fi

# Sort for deterministic order
IFS=$'\n' PROJECT_NAMES=($(sort <<<"${PROJECT_NAMES[*]}")); unset IFS

echo "============================================"
echo "  Otto Smoke Tests — $TIMESTAMP"
echo "  Branch: $OTTO_BRANCH ($OTTO_COMMIT)"
echo "  Projects: ${PROJECT_NAMES[*]}"
echo "============================================"
echo ""

# ─── Per-project results stored in temp files ───
# Each project writes: status tasks passed failed time cost
META_DIR="$(mktemp -d)"
trap 'rm -rf "$META_DIR"' EXIT

ANY_FAILED=0

for proj in "${PROJECT_NAMES[@]}"; do
    proj_dir="$PROJECTS_DIR/$proj"
    proj_results="$RESULTS_DIR/$proj"
    mkdir -p "$proj_results"

    echo "────────────────────────────────────────────"
    echo "  Project: $proj"
    echo "────────────────────────────────────────────"

    # Count tasks
    task_count=0
    while IFS= read -r line; do
        line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [[ -z "$line" || "$line" == \#* ]] && continue
        task_count=$((task_count + 1))
    done < "$proj_dir/tasks.txt"
    echo "  Tasks: $task_count"

    # Create temp dir for this project
    WORK_DIR="$(mktemp -d)"
    echo "  Workdir: $WORK_DIR"

    # ─── Setup ───
    setup_ok=1
    (
        cd "$WORK_DIR"
        git init -q
        git config user.email "smoke@otto.dev"
        git config user.name "Smoke Test"
        bash "$proj_dir/setup.sh"
    ) || setup_ok=0

    if [[ $setup_ok -eq 0 ]]; then
        echo "  ERROR: setup.sh failed"
        echo "ERROR $task_count 0 $task_count 0 0.00" > "$META_DIR/$proj"
        ANY_FAILED=1
        rm -rf "$WORK_DIR"
        continue
    fi

    # ─── Add tasks ───
    echo "  Adding tasks..."
    add_ok=1
    (
        cd "$WORK_DIR"
        while IFS= read -r line; do
            line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
            [[ -z "$line" || "$line" == \#* ]] && continue
            env CLAUDECODE= "$OTTO_BIN" add "$line"
        done < "$proj_dir/tasks.txt"
    ) || add_ok=0

    if [[ $add_ok -eq 0 ]]; then
        echo "  ERROR: task add failed"
        echo "ERROR $task_count 0 $task_count 0 0.00" > "$META_DIR/$proj"
        ANY_FAILED=1
        rm -rf "$WORK_DIR"
        continue
    fi

    # ─── Run otto ───
    echo "  Running otto..."
    RUN_START=$(date +%s)
    set +e
    (
        cd "$WORK_DIR"
        env CLAUDECODE= "$OTTO_BIN" run 2>&1
    ) | tee "$proj_results/otto_output.txt"
    OTTO_EXIT=${PIPESTATUS[0]}
    set -e
    RUN_END=$(date +%s)
    RUN_TIME=$((RUN_END - RUN_START))

    # ─── Parse results from tasks.yaml ───
    passed=0
    failed=0
    total_cost="0.00"

    if [[ -f "$WORK_DIR/tasks.yaml" ]]; then
        cp "$WORK_DIR/tasks.yaml" "$proj_results/tasks.yaml"

        # Count passed/failed by looking at status fields
        passed=$(grep -c 'status: passed' "$WORK_DIR/tasks.yaml" 2>/dev/null || true)
        failed_count=$(grep -c 'status: failed' "$WORK_DIR/tasks.yaml" 2>/dev/null || true)
        blocked_count=$(grep -c 'status: blocked' "$WORK_DIR/tasks.yaml" 2>/dev/null || true)
        failed=$((failed_count + blocked_count))

        # Parse cost_usd values and sum them
        total_cost=$(grep 'cost_usd:' "$WORK_DIR/tasks.yaml" 2>/dev/null \
            | sed 's/.*cost_usd:[[:space:]]*//' \
            | awk '{s += $1} END {printf "%.2f", s}' 2>/dev/null || echo "0.00")
    fi

    # Determine status
    if [[ $passed -eq $task_count && $task_count -gt 0 ]]; then
        status="PASS"
    else
        status="FAIL"
        ANY_FAILED=1
    fi

    # Store results
    echo "$status $task_count $passed $failed $RUN_TIME $total_cost" > "$META_DIR/$proj"

    echo ""
    echo "  Result: $status  ($passed/$task_count passed, ${RUN_TIME}s, \$$total_cost)"
    echo ""

    # ─── Cleanup ───
    rm -rf "$WORK_DIR"
done

# ─── Summary table ───
echo ""
echo "============================================"
echo "  RESULTS SUMMARY"
echo "============================================"
printf "  %-15s %6s %6s %6s %8s %8s\n" "Project" "Status" "Pass" "Fail" "Time(s)" "Cost"
printf "  %-15s %6s %6s %6s %8s %8s\n" "---------------" "------" "------" "------" "--------" "--------"

TOTAL_TASKS=0
TOTAL_PASSED=0
TOTAL_FAILED=0
TOTAL_TIME=0
TOTAL_COST="0.00"

for proj in "${PROJECT_NAMES[@]}"; do
    read -r status tasks passed failed time_s cost < "$META_DIR/$proj"

    printf "  %-15s %6s %6s %6s %8s %8s\n" "$proj" "$status" "$passed" "$failed" "${time_s}s" "\$$cost"

    TOTAL_TASKS=$((TOTAL_TASKS + tasks))
    TOTAL_PASSED=$((TOTAL_PASSED + passed))
    TOTAL_FAILED=$((TOTAL_FAILED + failed))
    TOTAL_TIME=$((TOTAL_TIME + time_s))
    TOTAL_COST=$(echo "$TOTAL_COST $cost" | awk '{printf "%.2f", $1 + $2}')
done

printf "  %-15s %6s %6s %6s %8s %8s\n" "---------------" "------" "------" "------" "--------" "--------"

if [[ $TOTAL_TASKS -gt 0 ]]; then
    PASS_RATE=$(echo "$TOTAL_PASSED $TOTAL_TASKS" | awk '{printf "%.1f", ($1/$2)*100}')
else
    PASS_RATE="0.0"
fi

printf "  %-15s %5s%% %6s %6s %8s %8s\n" "TOTAL" "$PASS_RATE" "$TOTAL_PASSED" "$TOTAL_FAILED" "${TOTAL_TIME}s" "\$$TOTAL_COST"
echo ""

# ─── Write results.json ───
RESULTS_JSON="$RESULTS_DIR/results.json"

# Build projects JSON using a temp file for cleaner construction
proj_json_file="$(mktemp)"
echo "{" > "$proj_json_file"
first=1
for proj in "${PROJECT_NAMES[@]}"; do
    read -r status tasks passed failed time_s cost < "$META_DIR/$proj"
    [[ $first -eq 0 ]] && echo "," >> "$proj_json_file"
    first=0
    cat >> "$proj_json_file" << PROJEOF
    "$proj": {
      "tasks": $tasks,
      "passed": $passed,
      "failed": $failed,
      "time_seconds": $time_s,
      "cost_usd": $cost,
      "status": "$status"
    }
PROJEOF
done
echo "}" >> "$proj_json_file"

cat > "$RESULTS_JSON" << ENDJSON
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%S)",
  "otto_branch": "$OTTO_BRANCH",
  "otto_commit": "$OTTO_COMMIT",
  "projects": $(cat "$proj_json_file"),
  "summary": {
    "total_tasks": $TOTAL_TASKS,
    "total_passed": $TOTAL_PASSED,
    "total_failed": $TOTAL_FAILED,
    "total_time_seconds": $TOTAL_TIME,
    "total_cost_usd": $TOTAL_COST,
    "pass_rate": $(echo "$PASS_RATE" | awk '{printf "%.2f", $1/100}')
  }
}
ENDJSON
rm -f "$proj_json_file"

echo "Results: $RESULTS_JSON"
echo ""

if [[ $ANY_FAILED -ne 0 ]]; then
    echo "FAILED — some projects did not pass."
    exit 1
else
    echo "ALL PASSED."
    exit 0
fi
