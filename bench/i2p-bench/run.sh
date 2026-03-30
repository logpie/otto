#!/usr/bin/env bash
# i2p benchmark — compares otto build vs bare CC on the same product intents.
#
# Usage:
#   ./bench/i2p-bench/run.sh [intent_id] [--otto-only | --cc-only]
#
# Runs each intent twice:
#   1. bare CC:    claude -p "intent" (single agent session)
#   2. otto build: otto build "intent" --no-review
#
# Then runs the SAME product QA evaluation against both outputs.
# Results written to /tmp/i2p-bench/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
INTENTS_FILE="$SCRIPT_DIR/intents.yaml"
RESULTS_DIR="/tmp/i2p-bench"
TIMESTAMP="$(date +%Y-%m-%d-%H%M%S)"
RUN_DIR="$RESULTS_DIR/$TIMESTAMP"

OTTO_BIN="${OTTO_BIN:-$REPO_DIR/.venv/bin/otto}"
PYTHON="$REPO_DIR/.venv/bin/python"

# Parse args
FILTER="${1:-}"
MODE="${2:-}"  # --otto-only or --cc-only

mkdir -p "$RUN_DIR"

echo "============================================"
echo "  i2p Benchmark — $TIMESTAMP"
echo "  Intents: $INTENTS_FILE"
echo "  Results: $RUN_DIR"
echo "============================================"
echo ""

# Parse intents from YAML
INTENT_IDS=$($PYTHON -c "
import yaml
with open('$INTENTS_FILE') as f:
    data = yaml.safe_load(f)
for intent in data['intents']:
    if '$FILTER' and intent['id'] != '$FILTER':
        continue
    print(intent['id'])
")

if [[ -z "$INTENT_IDS" ]]; then
    echo "No matching intents found."
    exit 1
fi

# Run each intent
for INTENT_ID in $INTENT_IDS; do
    INTENT_TEXT=$($PYTHON -c "
import yaml
with open('$INTENTS_FILE') as f:
    data = yaml.safe_load(f)
for intent in data['intents']:
    if intent['id'] == '$INTENT_ID':
        print(intent['intent'])
        break
")
    COMPLEXITY=$($PYTHON -c "
import yaml
with open('$INTENTS_FILE') as f:
    data = yaml.safe_load(f)
for intent in data['intents']:
    if intent['id'] == '$INTENT_ID':
        print(intent.get('complexity', 'unknown'))
        break
")

    echo "────────────────────────────────────────────"
    echo "  Intent: $INTENT_ID ($COMPLEXITY)"
    echo "  \"$INTENT_TEXT\""
    echo "────────────────────────────────────────────"

    # --- BARE CC ---
    if [[ "$MODE" != "--otto-only" ]]; then
        echo ""
        echo "  [bare-cc] Setting up..."
        CC_WORK="/tmp/i2p-bench-$INTENT_ID-cc"
        CC_RESULTS="$RUN_DIR/$INTENT_ID/bare-cc"
        mkdir -p "$CC_RESULTS"
        rm -rf "$CC_WORK"
        mkdir -p "$CC_WORK"
        (
            cd "$CC_WORK"
            git init -q
            git config user.email "bench@otto.dev"
            git config user.name "Benchmark"
            echo "# $INTENT_ID" > README.md
            git add -A && git commit -q -m "init"
        )

        echo "  [bare-cc] Running claude -p ..."
        CC_START=$(date +%s)
        (
            cd "$CC_WORK"
            claude -p "$INTENT_TEXT" --allowedTools "Bash,Read,Write,Edit,Glob,Grep" 2>&1
        ) > "$CC_RESULTS/output.txt" 2>&1 || true
        CC_END=$(date +%s)
        CC_DURATION=$((CC_END - CC_START))

        # Copy artifacts
        cp -r "$CC_WORK" "$CC_RESULTS/project" 2>/dev/null || true
        echo "  [bare-cc] Done: ${CC_DURATION}s"

        # Count files
        CC_FILES=$(find "$CC_WORK" -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.py" | grep -v node_modules | grep -v .next | wc -l | tr -d ' ')
        CC_LINES=$(find "$CC_WORK" -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.py" | grep -v node_modules | grep -v .next | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}' || echo 0)
        echo "  [bare-cc] Output: $CC_FILES files, $CC_LINES lines"

        # Save metadata
        cat > "$CC_RESULTS/meta.json" << EOF
{"intent_id": "$INTENT_ID", "mode": "bare-cc", "duration_s": $CC_DURATION, "files": $CC_FILES, "lines": $CC_LINES}
EOF
    fi

    # --- OTTO BUILD ---
    if [[ "$MODE" != "--cc-only" ]]; then
        echo ""
        echo "  [otto] Setting up..."
        OTTO_WORK="/tmp/i2p-bench-$INTENT_ID-otto"
        OTTO_RESULTS="$RUN_DIR/$INTENT_ID/otto"
        mkdir -p "$OTTO_RESULTS"
        rm -rf "$OTTO_WORK"
        mkdir -p "$OTTO_WORK"
        (
            cd "$OTTO_WORK"
            git init -q
            git config user.email "bench@otto.dev"
            git config user.name "Benchmark"
            echo "# $INTENT_ID" > README.md
            git add -A && git commit -q -m "init"
        )

        echo "  [otto] Running otto build ..."
        OTTO_START=$(date +%s)
        (
            cd "$OTTO_WORK"
            PYTHONPATH="$REPO_DIR" env CLAUDECODE= $PYTHON -c "
import sys, os
sys.argv = ['otto', 'build', '''$INTENT_TEXT''', '--no-review', '--no-qa']
from otto.cli import main
main(standalone_mode=False)
" 2>&1
        ) > "$OTTO_RESULTS/output.txt" 2>&1 || true
        OTTO_END=$(date +%s)
        OTTO_DURATION=$((OTTO_END - OTTO_START))

        # Copy artifacts
        rsync -a --exclude='node_modules' --exclude='.next' "$OTTO_WORK/" "$OTTO_RESULTS/project/" 2>/dev/null || true
        echo "  [otto] Done: ${OTTO_DURATION}s"

        # Count files
        OTTO_FILES=$(find "$OTTO_WORK" -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.py" | grep -v node_modules | grep -v .next | wc -l | tr -d ' ')
        OTTO_LINES=$(find "$OTTO_WORK" -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.py" | grep -v node_modules | grep -v .next | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}' || echo 0)
        echo "  [otto] Output: $OTTO_FILES files, $OTTO_LINES lines"

        # Get cost from build events
        OTTO_COST="0.00"
        if [[ -f "$OTTO_WORK/otto_logs/events.jsonl" ]]; then
            OTTO_COST=$($PYTHON -c "
import json
for line in open('$OTTO_WORK/otto_logs/events.jsonl'):
    e = json.loads(line)
    if e.get('event') == 'build_completed':
        print(f\"{e.get('cost_total', 0):.2f}\")
        break
else:
    # Fallback: sum from tasks.yaml
    import yaml
    try:
        tasks = yaml.safe_load(open('$OTTO_WORK/tasks.yaml'))['tasks']
        print(f\"{sum(t.get('cost_usd', 0) for t in tasks):.2f}\")
    except: print('0.00')
" 2>/dev/null || echo "0.00")
        fi
        echo "  [otto] Cost: \$$OTTO_COST"

        cat > "$OTTO_RESULTS/meta.json" << EOF
{"intent_id": "$INTENT_ID", "mode": "otto", "duration_s": $OTTO_DURATION, "files": $OTTO_FILES, "lines": $OTTO_LINES, "cost_usd": $OTTO_COST}
EOF
    fi

    echo ""
done

# --- EVALUATION ---
echo "============================================"
echo "  EVALUATION — running product QA on outputs"
echo "============================================"
echo ""

for INTENT_ID in $INTENT_IDS; do
    # Extract journeys for this intent
    JOURNEYS=$($PYTHON -c "
import yaml, json
with open('$INTENTS_FILE') as f:
    data = yaml.safe_load(f)
for intent in data['intents']:
    if intent['id'] == '$INTENT_ID':
        print(json.dumps(intent.get('journeys', [])))
        break
")
    VERIFY=$($PYTHON -c "
import yaml, json
with open('$INTENTS_FILE') as f:
    data = yaml.safe_load(f)
for intent in data['intents']:
    if intent['id'] == '$INTENT_ID':
        print(json.dumps(intent.get('verify', [])))
        break
")

    echo "  Evaluating: $INTENT_ID"

    for MODE_DIR in "$RUN_DIR/$INTENT_ID/bare-cc" "$RUN_DIR/$INTENT_ID/otto"; do
        [[ -d "$MODE_DIR/project" ]] || continue
        MODE_NAME=$(basename "$MODE_DIR")
        echo "    [$MODE_NAME] Running evaluation..."

        # Write evaluation spec to the project
        cat > "$MODE_DIR/project/eval-spec.md" << EOF
# Evaluation Spec (do not modify — generated by benchmark)

## Verification Checks
$(echo "$VERIFY" | $PYTHON -c "import json,sys; [print(f'- {v}') for v in json.load(sys.stdin)]")

## User Journeys
$(echo "$JOURNEYS" | $PYTHON -c "
import json, sys
journeys = json.load(sys.stdin)
for j in journeys:
    print(f\"### {j['name']}\")
    for s in j.get('steps', []):
        print(f'- {s}')
    print()
")
EOF

        # Run product QA against this output
        (
            cd "$MODE_DIR/project"
            PYTHONPATH="$REPO_DIR" $PYTHON -c "
import asyncio, json
from pathlib import Path
from otto.product_qa import run_product_qa

result = asyncio.run(run_product_qa(
    product_spec_path=Path('eval-spec.md'),
    project_dir=Path('.'),
    config={},
))
print(json.dumps(result, indent=2))
" 2>&1
        ) > "$MODE_DIR/eval-result.json" 2>&1 || true

        # Parse result
        EVAL_PASSED=$($PYTHON -c "
import json
try:
    r = json.load(open('$MODE_DIR/eval-result.json'))
    passed = sum(1 for j in r.get('journeys', []) if j.get('passed'))
    total = len(r.get('journeys', []))
    print(f'{passed}/{total}')
except: print('error')
" 2>/dev/null || echo "error")
        echo "    [$MODE_NAME] Journeys: $EVAL_PASSED"
    done
    echo ""
done

# --- SUMMARY ---
echo "============================================"
echo "  COMPARISON SUMMARY"
echo "============================================"
echo ""
printf "%-20s %-10s %-8s %-8s %-10s %-10s\n" "Intent" "Mode" "Files" "Lines" "Time" "Journeys"
printf "%-20s %-10s %-8s %-8s %-10s %-10s\n" "------" "----" "-----" "-----" "----" "--------"

for INTENT_ID in $INTENT_IDS; do
    for MODE_DIR in "$RUN_DIR/$INTENT_ID/bare-cc" "$RUN_DIR/$INTENT_ID/otto"; do
        [[ -f "$MODE_DIR/meta.json" ]] || continue
        MODE_NAME=$(basename "$MODE_DIR")
        FILES=$($PYTHON -c "import json; print(json.load(open('$MODE_DIR/meta.json'))['files'])" 2>/dev/null || echo "?")
        LINES=$($PYTHON -c "import json; print(json.load(open('$MODE_DIR/meta.json'))['lines'])" 2>/dev/null || echo "?")
        DURATION=$($PYTHON -c "import json; d=json.load(open('$MODE_DIR/meta.json'))['duration_s']; print(f'{d//60}m{d%60}s')" 2>/dev/null || echo "?")
        EVAL=$($PYTHON -c "
import json
try:
    r = json.load(open('$MODE_DIR/eval-result.json'))
    passed = sum(1 for j in r.get('journeys', []) if j.get('passed'))
    total = len(r.get('journeys', []))
    print(f'{passed}/{total}')
except: print('n/a')
" 2>/dev/null || echo "n/a")
        printf "%-20s %-10s %-8s %-8s %-10s %-10s\n" "$INTENT_ID" "$MODE_NAME" "$FILES" "$LINES" "$DURATION" "$EVAL"
    done
done

echo ""
echo "Full results: $RUN_DIR"
