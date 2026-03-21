#!/usr/bin/env bash
# Otto pressure test runner — runs canonical project set sequentially.
# Usage: ./bench/pressure/run.sh [project_name]  (omit name to run all)
# Results written to /tmp/otto-pressure-results/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_DIR="$SCRIPT_DIR/projects"
RESULTS_DIR="/tmp/otto-pressure-results"
BUGS_FILE="/tmp/otto-pressure-bugs.md"

# Otto binary: env var > repo .venv/bin/otto > PATH
OTTO_BIN="${OTTO_BIN:-}"
if [[ -z "$OTTO_BIN" ]]; then
    repo_otto="$(cd "$SCRIPT_DIR/../.." && pwd)/.venv/bin/otto"
    if [[ -x "$repo_otto" ]]; then
        OTTO_BIN="$repo_otto"
    else
        OTTO_BIN="otto"
    fi
fi

# Resolve to absolute path so it works after cd to /tmp workdirs
OTTO_BIN="$(cd "$(dirname "$OTTO_BIN")" && pwd)/$(basename "$OTTO_BIN")" 2>/dev/null || true

if ! command -v "$OTTO_BIN" &>/dev/null && [[ ! -x "$OTTO_BIN" ]]; then
    echo "ERROR: otto not found at '$OTTO_BIN'. Set OTTO_BIN or install otto." >&2
    exit 1
fi
echo "Using otto: $OTTO_BIN"

# Setup results dir
mkdir -p "$RESULTS_DIR"
TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
RUN_DIR="$RESULTS_DIR/$TIMESTAMP"
mkdir -p "$RUN_DIR"

# Init bugs file
if [[ ! -f "$BUGS_FILE" ]]; then
    cat > "$BUGS_FILE" << 'EOF'
# Otto Pressure Test — Bug Log
# Document bugs here during the run. Fix after all projects complete.

EOF
fi

# Git metadata
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
IFS=$'\n' PROJECT_NAMES=($(sort <<<"${PROJECT_NAMES[*]}")); unset IFS

TOTAL=${#PROJECT_NAMES[@]}
echo "============================================"
echo "  Otto Pressure Test — $TIMESTAMP"
echo "  Branch: $OTTO_BRANCH ($OTTO_COMMIT)"
echo "  Projects: $TOTAL"
echo "  Results: $RUN_DIR"
echo "============================================"
echo ""

# Per-project results
META_DIR="$(mktemp -d)"
trap 'rm -rf "$META_DIR"' EXIT

IDX=0
for proj in "${PROJECT_NAMES[@]}"; do
    IDX=$((IDX + 1))
    proj_dir="$PROJECTS_DIR/$proj"
    proj_results="$RUN_DIR/$proj"
    mkdir -p "$proj_results"
    WORK_DIR="/tmp/pt-$proj"

    echo "────────────────────────────────────────────"
    echo "  [$IDX/$TOTAL] $proj"
    echo "  Started: $(date '+%H:%M:%S')"
    echo "────────────────────────────────────────────"

    # Count tasks
    task_count=0
    while IFS= read -r line; do
        line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [[ -z "$line" || "$line" == \#* ]] && continue
        task_count=$((task_count + 1))
    done < "$proj_dir/tasks.txt"
    echo "  Tasks: $task_count"

    # Clean and setup workdir
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"

    setup_ok=1
    (
        cd "$WORK_DIR"
        git init -q
        git config user.email "pressure@otto.dev"
        git config user.name "Pressure Test"
        bash "$proj_dir/setup.sh"
    ) > "$proj_results/setup.log" 2>&1 || setup_ok=0

    if [[ $setup_ok -eq 0 ]]; then
        echo "  ERROR: setup.sh failed (see $proj_results/setup.log)"
        echo "SETUP_FAIL $task_count 0 $task_count 0 0.00" > "$META_DIR/$proj"
        continue
    fi

    # Add tasks
    add_ok=1
    (
        cd "$WORK_DIR"
        while IFS= read -r line; do
            line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
            [[ -z "$line" || "$line" == \#* ]] && continue
            env CLAUDECODE= "$OTTO_BIN" add "$line"
        done < "$proj_dir/tasks.txt"
    ) > "$proj_results/add.log" 2>&1 || add_ok=0

    if [[ $add_ok -eq 0 ]]; then
        echo "  ERROR: task add failed (see $proj_results/add.log)"
        echo "ADD_FAIL $task_count 0 $task_count 0 0.00" > "$META_DIR/$proj"
        continue
    fi

    # Run otto
    echo "  Running otto..."
    RUN_START=$(date +%s)
    set +e
    (
        cd "$WORK_DIR"
        env CLAUDECODE= "$OTTO_BIN" run 2>&1
    ) | tee "$proj_results/output.txt"
    OTTO_EXIT=${PIPESTATUS[0]}
    set -e
    RUN_END=$(date +%s)
    RUN_TIME=$((RUN_END - RUN_START))

    # Copy artifacts
    cp "$WORK_DIR/tasks.yaml" "$proj_results/tasks.yaml" 2>/dev/null || true
    cp "$WORK_DIR/otto.yaml" "$proj_results/otto.yaml" 2>/dev/null || true
    cp -r "$WORK_DIR/otto_logs" "$proj_results/otto_logs" 2>/dev/null || true

    # Parse results
    passed=0
    failed=0
    total_cost="0.00"
    if [[ -f "$WORK_DIR/tasks.yaml" ]]; then
        passed=$(grep -c 'status: passed' "$WORK_DIR/tasks.yaml" 2>/dev/null || true)
        failed_count=$(grep -c 'status: failed' "$WORK_DIR/tasks.yaml" 2>/dev/null || true)
        blocked_count=$(grep -c 'status: blocked' "$WORK_DIR/tasks.yaml" 2>/dev/null || true)
        failed=$((failed_count + blocked_count))
        total_cost=$(grep 'cost_usd:' "$WORK_DIR/tasks.yaml" 2>/dev/null \
            | sed 's/.*cost_usd:[[:space:]]*//' \
            | awk '{s += $1} END {printf "%.2f", s}' 2>/dev/null || echo "0.00")
    fi

    if [[ $passed -eq $task_count && $task_count -gt 0 ]]; then
        status="PASS"
    else
        status="FAIL"
    fi

    echo "$status $task_count $passed $failed $RUN_TIME $total_cost" > "$META_DIR/$proj"
    echo ""
    echo "  Result: $status  ($passed/$task_count passed, ${RUN_TIME}s, \$$total_cost)"
    echo ""
done

# Summary table
echo ""
echo "============================================"
echo "  RESULTS SUMMARY"
echo "============================================"
printf "  %-30s %8s %6s %6s %8s %8s\n" "Project" "Status" "Pass" "Fail" "Time(s)" "Cost"
printf "  %-30s %8s %6s %6s %8s %8s\n" "------------------------------" "--------" "------" "------" "--------" "--------"

TOTAL_TASKS=0
TOTAL_PASSED=0
TOTAL_FAILED=0
TOTAL_TIME=0
TOTAL_COST="0.00"

for proj in "${PROJECT_NAMES[@]}"; do
    read -r status tasks passed failed time_s cost < "$META_DIR/$proj"
    printf "  %-30s %8s %6s %6s %8s %8s\n" "$proj" "$status" "$passed" "$failed" "${time_s}s" "\$$cost"
    TOTAL_TASKS=$((TOTAL_TASKS + tasks))
    TOTAL_PASSED=$((TOTAL_PASSED + passed))
    TOTAL_FAILED=$((TOTAL_FAILED + failed))
    TOTAL_TIME=$((TOTAL_TIME + time_s))
    TOTAL_COST=$(echo "$TOTAL_COST $cost" | awk '{printf "%.2f", $1 + $2}')
done

printf "  %-30s %8s %6s %6s %8s %8s\n" "------------------------------" "--------" "------" "------" "--------" "--------"
if [[ $TOTAL_TASKS -gt 0 ]]; then
    PASS_RATE=$(echo "$TOTAL_PASSED $TOTAL_TASKS" | awk '{printf "%.1f", ($1/$2)*100}')
else
    PASS_RATE="0.0"
fi
printf "  %-30s %7s%% %6s %6s %8s %8s\n" "TOTAL" "$PASS_RATE" "$TOTAL_PASSED" "$TOTAL_FAILED" "${TOTAL_TIME}s" "\$$TOTAL_COST"
echo ""
echo "Results: $RUN_DIR"
echo "Bugs: $BUGS_FILE"
