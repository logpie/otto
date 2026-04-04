#!/usr/bin/env bash
# Otto pressure benchmark — compare runners on the same golden test set.
#
# Usage:
#   bench.sh --runner otto --label "v3-baseline"        # Run with otto
#   bench.sh --runner bare-cc --label "bare-baseline"   # Run with bare Claude Code
#   bench.sh --runner bare-codex --label "codex-baseline" # Run with bare Codex CLI
#   bench.sh --runner otto --projects all                # All 35 projects (default: golden)
#   bench.sh --compare run-a run-b                      # Compare two runs
#
# Results stored in bench/pressure/results/<label>/
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_DIR="$SCRIPT_DIR/projects"
RESULTS_BASE="$SCRIPT_DIR/results"

# Defaults
RUNNER="otto"
PROJECT_SET="golden"  # golden = real-repo only, all = everything
LABEL=""
COMPARE_MODE=false
COMPARE_A=""
COMPARE_B=""
FILTER=""
OTTO_BENCH_CONFIG="${OTTO_BENCH_CONFIG:-}"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --runner) RUNNER="$2"; shift 2 ;;
        --projects) PROJECT_SET="$2"; shift 2 ;;
        --label) LABEL="$2"; shift 2 ;;
        --compare) COMPARE_MODE=true; COMPARE_A="$2"; COMPARE_B="$3"; shift 3 ;;
        --filter) FILTER="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ─── Compare mode ───────────────────────────────────────────
if $COMPARE_MODE; then
    file_a="$RESULTS_BASE/$COMPARE_A/summary.json"
    file_b="$RESULTS_BASE/$COMPARE_B/summary.json"
    if [[ ! -f "$file_a" ]]; then echo "Run not found: $COMPARE_A"; exit 1; fi
    if [[ ! -f "$file_b" ]]; then echo "Run not found: $COMPARE_B"; exit 1; fi

    echo "============================================"
    echo "  Benchmark Comparison"
    echo "  A: $COMPARE_A"
    echo "  B: $COMPARE_B"
    echo "============================================"
    echo ""

    python3 -c "
import json, sys

a = json.load(open('$file_a'))
b = json.load(open('$file_b'))

# Normalize key names (old format used otto_pass, new uses runner_pass)
def get_summary(d, key):
    s = d.get('summary', {})
    # Try runner_ first, fall back to otto_
    return s.get(key, s.get(key.replace('runner_', 'otto_'), 0))

def get_proj_field(p, key):
    return p.get(key, p.get(key.replace('runner_', 'otto_'), '—'))

ra = a.get('summary', {}).get('runner', a.get('projects', {}).get(list(a.get('projects', {}).keys() or [''])[0], {}).get('runner', 'A'))
rb = b.get('summary', {}).get('runner', b.get('projects', {}).get(list(b.get('projects', {}).keys() or [''])[0], {}).get('runner', 'B'))

print(f'  {\"Metric\":<30} {ra:>10} {rb:>10} {\"Delta\":>10}')
print(f'  {\"─\"*30} {\"─\"*10} {\"─\"*10} {\"─\"*10}')

for key in ['runner_pass_rate', 'verify_pass_rate', 'false_pass_rate', 'avg_cost', 'avg_time_s', 'total_cost']:
    va = get_summary(a, key)
    vb = get_summary(b, key)
    if va is None: va = 0
    if vb is None: vb = 0
    delta = vb - va
    sign = '+' if delta > 0 else ''
    label = key.replace('runner_', '').replace('_', ' ')
    if isinstance(va, float) or isinstance(vb, float):
        print(f'  {label:<30} {va:>10.1f} {vb:>10.1f} {sign}{delta:>9.1f}')
    else:
        print(f'  {label:<30} {va:>10} {vb:>10} {sign}{delta:>9}')

print()
print('  Per-project comparison:')
print(f'  {\"Project\":<35} {ra:>8} {rb:>8} {ra+\" vfy\":>8} {rb+\" vfy\":>8}')
print(f'  {\"─\"*35} {\"─\"*8} {\"─\"*8} {\"─\"*8} {\"─\"*8}')

all_projects = sorted(set(list(a.get('projects', {}).keys()) + list(b.get('projects', {}).keys())))
for p in all_projects:
    pa = a.get('projects', {}).get(p, {})
    pb = b.get('projects', {}).get(p, {})
    rpa = get_proj_field(pa, 'runner_pass')
    rpb = get_proj_field(pb, 'runner_pass')
    vpa = pa.get('verify_pass', '—')
    vpb = pb.get('verify_pass', '—')
    # Highlight verify differences
    marker = ''
    if vpa != '—' and vpb != '—' and vpa != vpb:
        marker = ' ◀'
    print(f'  {p:<35} {rpa:>8} {rpb:>8} {vpa:>8} {vpb:>8}{marker}')
"
    exit 0
fi

# ─── Run mode ───────────────────────────────────────────────

# Label defaults to runner-timestamp
if [[ -z "$LABEL" ]]; then
    LABEL="${RUNNER}-$(date +%Y%m%d-%H%M%S)"
fi

RESULTS_DIR="$RESULTS_BASE/$LABEL"
mkdir -p "$RESULTS_DIR"
WORK_LABEL="$(printf '%s' "$LABEL" | tr -cs '[:alnum:]' '-')"

# Otto binary
OTTO_BIN="${OTTO_BIN:-}"
if [[ -z "$OTTO_BIN" ]]; then
    repo_otto="$(cd "$SCRIPT_DIR/../.." && pwd)/.venv/bin/otto"
    if [[ -x "$repo_otto" ]]; then
        OTTO_BIN="$repo_otto"
    else
        OTTO_BIN="otto"
    fi
fi
if [[ "$OTTO_BIN" == */* ]]; then
    OTTO_BIN="$(cd "$(dirname "$OTTO_BIN")" && pwd)/$(basename "$OTTO_BIN")" 2>/dev/null || true
elif command -v "$OTTO_BIN" &>/dev/null; then
    OTTO_BIN="$(command -v "$OTTO_BIN")"
fi

# Claude binary for bare-cc
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
if ! command -v "$CLAUDE_BIN" &>/dev/null; then
    CLAUDE_BIN="$(which claude 2>/dev/null || echo claude)"
fi

# Codex binary for bare-codex
CODEX_BIN="${CODEX_BIN:-codex}"
if ! command -v "$CODEX_BIN" &>/dev/null; then
    CODEX_BIN="$(which codex 2>/dev/null || echo codex)"
fi
CODEX_MODEL="${CODEX_MODEL:-}"
CODEX_EXTRA_ARGS="${CODEX_EXTRA_ARGS:-}"

# Collect projects
PROJECT_NAMES=()
if [[ -n "$FILTER" ]]; then
    IFS=',' read -r -a FILTER_NAMES <<< "$FILTER"
    for name in "${FILTER_NAMES[@]}"; do
        name="$(echo "$name" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [[ -z "$name" ]] && continue
        PROJECT_NAMES+=("$name")
    done
elif [[ "$PROJECT_SET" == "golden" ]]; then
    for dir in "$PROJECTS_DIR"/real-*/; do
        PROJECT_NAMES+=("$(basename "$dir")")
    done
else
    for dir in "$PROJECTS_DIR"/*/; do
        PROJECT_NAMES+=("$(basename "$dir")")
    done
fi
IFS=$'\n' PROJECT_NAMES=($(sort <<<"${PROJECT_NAMES[*]}")); unset IFS

TOTAL=${#PROJECT_NAMES[@]}
OTTO_BENCH_CONFIG_JSON="$(python3 -c 'import json, os; print(json.dumps(os.environ.get("OTTO_BENCH_CONFIG", "")))')"

cat > "$RESULTS_DIR/run-meta.json" << EOF
{
  "label": "$LABEL",
  "runner": "$RUNNER",
  "project_set": "$PROJECT_SET",
  "filter": "$FILTER",
  "total_projects": $TOTAL,
  "otto_bin": "${OTTO_BIN:-}",
  "claude_bin": "${CLAUDE_BIN:-}",
  "codex_bin": "${CODEX_BIN:-}",
  "codex_model": "${CODEX_MODEL:-}",
  "codex_extra_args": "${CODEX_EXTRA_ARGS:-}",
  "otto_bench_config": $OTTO_BENCH_CONFIG_JSON
}
EOF

echo "============================================"
echo "  Pressure Benchmark"
echo "  Runner: $RUNNER"
echo "  Projects: $TOTAL ($PROJECT_SET)"
echo "  Label: $LABEL"
echo "  Results: $RESULTS_DIR"
echo "============================================"
echo ""

# ─── Per-project execution ──────────────────────────────────
run_otto() {
    local proj="$1" work_dir="$2" proj_dir="$3" proj_results="$4"

    if [[ -n "$OTTO_BENCH_CONFIG" ]]; then
        printf '%s\n' "$OTTO_BENCH_CONFIG" > "$work_dir/otto.yaml"
    fi

    # Add tasks
    (
        cd "$work_dir"
        while IFS= read -r line; do
            line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
            [[ -z "$line" || "$line" == \#* ]] && continue
            env CLAUDECODE= "$OTTO_BIN" add "$line"
        done < "$proj_dir/tasks.txt"
    ) > "$proj_results/add.log" 2>&1 || return 1

    # Run otto
    (
        cd "$work_dir"
        env CLAUDECODE= "$OTTO_BIN" run 2>&1
    ) | tee "$proj_results/output.txt"
    return ${PIPESTATUS[0]}
}

run_bare_cc() {
    local proj="$1" work_dir="$2" proj_dir="$3" proj_results="$4"

    # Read task text
    local task_text=""
    while IFS= read -r line; do
        line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [[ -z "$line" || "$line" == \#* ]] && continue
        task_text+="$line "
    done < "$proj_dir/tasks.txt"

    # Run bare claude
    (
        cd "$work_dir"
        "$CLAUDE_BIN" -p "$task_text" --dangerously-skip-permissions 2>&1
    ) | tee "$proj_results/output.txt"
    return ${PIPESTATUS[0]}
}

run_bare_codex() {
    local proj="$1" work_dir="$2" proj_dir="$3" proj_results="$4"

    # Read task text
    local task_text=""
    while IFS= read -r line; do
        line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [[ -z "$line" || "$line" == \#* ]] && continue
        task_text+="$line "
    done < "$proj_dir/tasks.txt"

    local codex_args=()
    if [[ -n "$CODEX_MODEL" ]]; then
        codex_args+=(--model "$CODEX_MODEL")
    fi
    if [[ -n "$CODEX_EXTRA_ARGS" ]]; then
        # Intentionally word-split extra args from env for caller convenience.
        # shellcheck disable=SC2206
        codex_args+=($CODEX_EXTRA_ARGS)
    fi

    local cmd=(
        "$CODEX_BIN" exec
        --skip-git-repo-check
        --dangerously-bypass-approvals-and-sandbox
        --ephemeral
        -C "$work_dir"
    )
    if (( ${#codex_args[@]} > 0 )); then
        cmd+=("${codex_args[@]}")
    fi
    cmd+=("$task_text")

    (
        cd "$work_dir"
        "${cmd[@]}" 2>&1
    ) | tee "$proj_results/output.txt"
    return ${PIPESTATUS[0]}
}

# Cleanup workdirs on exit/kill
_ACTIVE_WORKDIR=""
trap 'rm -rf "$_ACTIVE_WORKDIR" 2>/dev/null' EXIT INT TERM

for proj in "${PROJECT_NAMES[@]}"; do
    proj_dir="$PROJECTS_DIR/$proj"
    proj_results="$RESULTS_DIR/$proj"
    WORK_DIR="/tmp/bench-${WORK_LABEL}-${proj}"
    _ACTIVE_WORKDIR="$WORK_DIR"
    # Clean stale results from previous runs to prevent contamination
    rm -rf "$proj_results" && mkdir -p "$proj_results"

    echo "────────────────────────────────────────────"
    echo "  $proj"
    echo "────────────────────────────────────────────"

    # Setup
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"
    setup_ok=1
    setup_attempts=2
    : > "$proj_results/setup.log"
    for setup_try in $(seq 1 "$setup_attempts"); do
        rm -rf "$WORK_DIR"
        mkdir -p "$WORK_DIR"
        {
            echo "[setup attempt $setup_try/$setup_attempts]"
            (
                cd "$WORK_DIR"
                git init -q
                git config user.email "bench@otto.dev"
                git config user.name "Bench"
                bash "$proj_dir/setup.sh"
            )
        } >> "$proj_results/setup.log" 2>&1 && break
        setup_ok=0
        if [[ "$setup_try" -lt "$setup_attempts" ]]; then
            echo "  setup attempt $setup_try failed, retrying..."
            sleep 1
            setup_ok=1
        fi
    done

    if [[ $setup_ok -eq 0 ]]; then
        echo "  SETUP FAIL"
        echo '{"runner_pass":"SETUP_FAIL","verify_pass":"SKIP","cost":0,"time_s":0}' > "$proj_results/result.json"
        continue
    fi

    # Run
    RUN_START=$(date +%s)
    set +e
    "run_${RUNNER//-/_}" "$proj" "$WORK_DIR" "$proj_dir" "$proj_results"
    RUN_EXIT=$?
    set -e
    RUN_END=$(date +%s)
    RUN_TIME=$((RUN_END - RUN_START))

    # Preserve run artifacts for debugging/forensics before cleanup.
    if [[ -f "$WORK_DIR/otto.yaml" ]]; then
        cp "$WORK_DIR/otto.yaml" "$proj_results/otto.yaml" 2>/dev/null || true
    fi
    if [[ -d "$WORK_DIR/otto_logs" ]]; then
        rm -rf "$proj_results/otto_logs"
        cp -R "$WORK_DIR/otto_logs" "$proj_results/otto_logs" 2>/dev/null || true
    fi
    if [[ -d "$WORK_DIR/.git" ]]; then
        (
            cd "$WORK_DIR"
            git status --short > "$proj_results/git-status.txt" 2>/dev/null || true
            git diff --binary > "$proj_results/final.diff" 2>/dev/null || true
            git diff --binary --cached > "$proj_results/final-staged.diff" 2>/dev/null || true
        )
    fi
    (
        cd "$WORK_DIR"
        tar \
            --exclude=.git \
            --exclude=node_modules \
            --exclude=.venv \
            --exclude=otto_logs \
            --exclude=.tasks.lock \
            -czf "$proj_results/worktree-snapshot.tgz" . 2>/dev/null || true
    )

    # Parse otto results (if available)
    runner_pass="UNKNOWN"
    cost="0.00"
    attempts=0
    if [[ -f "$WORK_DIR/tasks.yaml" ]]; then
        cp "$WORK_DIR/tasks.yaml" "$proj_results/tasks.yaml" 2>/dev/null || true
        passed=$(awk '/^[[:space:]]*status:[[:space:]]*passed([[:space:]]*|$)/ { c += 1 } END { print c + 0 }' "$WORK_DIR/tasks.yaml" 2>/dev/null)
        task_count=$(awk '/^[[:space:]]*status:[[:space:]]*/ { c += 1 } END { print c + 0 }' "$WORK_DIR/tasks.yaml" 2>/dev/null)
        cost=$(awk '
            /^[[:space:]]*cost_usd:[[:space:]]*/ {
                if ($2 ~ /^[0-9]+(\.[0-9]+)?$/) s += $2
            }
            END { printf "%.2f", s + 0 }
        ' "$WORK_DIR/tasks.yaml" 2>/dev/null)
        attempts=$(awk '
            /^[[:space:]]*attempts:[[:space:]]*/ { s += $2 }
            END { print s + 0 }
        ' "$WORK_DIR/tasks.yaml" 2>/dev/null)
        [[ "$passed" -eq "$task_count" && "$task_count" -gt 0 ]] && runner_pass="PASS" || runner_pass="FAIL"
    elif [[ "$RUNNER" == "bare-cc" || "$RUNNER" == "bare-codex" ]]; then
        # Bare CC doesn't produce tasks.yaml — rely on verify.sh for pass/fail.
        # Mark as DONE (ran to completion) — verify determines actual correctness.
        runner_pass="DONE"
    fi

    # Independent verification
    verify_pass="SKIP"
    if [[ -f "$proj_dir/verify.sh" ]]; then
        verify_log="$proj_results/verify.log"
        if (cd "$WORK_DIR" && bash "$proj_dir/verify.sh") > "$verify_log" 2>&1; then
            verify_pass="PASS"
        else
            verify_pass="FAIL"
        fi
    fi

    # Write result
    cat > "$proj_results/result.json" << ENDJSON
{
    "runner_pass": "$runner_pass",
    "verify_pass": "$verify_pass",
    "cost": $cost,
    "time_s": $RUN_TIME,
    "attempts": $attempts,
    "run_exit": $RUN_EXIT,
    "runner": "$RUNNER"
}
ENDJSON

    # Label based on runner
    runner_label="otto"
    [[ "$RUNNER" == "bare-cc" ]] && runner_label="bare-cc"
    [[ "$RUNNER" == "bare-codex" ]] && runner_label="bare-codex"
    cost_display="\$$cost"
    [[ "$RUNNER" == "bare-cc" || "$RUNNER" == "bare-codex" ]] && cost_display="n/a"
    echo "  $runner_label: $runner_pass | Verify: $verify_pass | ${RUN_TIME}s | $cost_display"
    echo ""

    # Cleanup workdir
    rm -rf "$WORK_DIR"
done

# ─── Generate summary ──────────────────────────────────────
RESULTS_DIR="$RESULTS_DIR" RUNNER="$RUNNER" python3 << 'PYEOF'
import json, os, sys

results_dir = os.environ.get("RESULTS_DIR", ".")
projects = {}
total = runner_passes = verify_passes = false_passes = 0
total_cost = 0.0
total_time = 0

for proj in sorted(os.listdir(results_dir)):
    result_file = os.path.join(results_dir, proj, "result.json")
    if not os.path.isfile(result_file):
        continue
    with open(result_file) as f:
        r = json.load(f)
    projects[proj] = r
    total += 1
    if r["runner_pass"] in ("PASS", "DONE"):
        runner_passes += 1
    if r["verify_pass"] == "PASS":
        verify_passes += 1
    if r["runner_pass"] in ("PASS", "DONE") and r["verify_pass"] == "FAIL":
        false_passes += 1
    total_cost += r.get("cost", 0)
    total_time += r.get("time_s", 0)

summary = {
    "total_projects": total,
    "runner_pass_rate": round(runner_passes / total * 100, 1) if total else 0,
    "verify_pass_rate": round(verify_passes / total * 100, 1) if total else 0,
    "false_pass_rate": round(false_passes / total * 100, 1) if total else 0,
    "total_cost": round(total_cost, 2),
    "avg_cost": round(total_cost / total, 2) if total else 0,
    "avg_time_s": round(total_time / total, 1) if total else 0,
}

output = {"summary": summary, "projects": projects}
out_path = os.path.join(results_dir, "summary.json")
with open(out_path, "w") as f:
    json.dump(output, f, indent=2)

print()
print("============================================")
print("  BENCHMARK RESULTS")
print("============================================")
print(f"  Projects:        {total}")
runner_name = os.environ.get("RUNNER", "otto")
print(f"  Runner:          {runner_name}")
print(f"  Runner pass rate: {summary['runner_pass_rate']}%")
print(f"  Verify pass rate: {summary['verify_pass_rate']}%")
print(f"  False pass rate: {summary['false_pass_rate']}%")
print(f"  Total cost:      ${summary['total_cost']}")
print(f"  Avg cost:        ${summary['avg_cost']}")
print(f"  Avg time:        {summary['avg_time_s']}s")
print(f"  Results:         {out_path}")
print()
PYEOF

echo "Done. Compare runs with:"
echo "  bash bench/pressure/bench.sh --compare $LABEL <other-label>"
