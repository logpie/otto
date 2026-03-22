#!/usr/bin/env bash
# Validate all pressure test project setups.
# Checks: setup.sh runs, existing tests pass (for bugfix/real-repo projects),
# tasks.txt is parseable with correct task count.
# Run this BEFORE any pressure test to catch broken setups early.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_DIR="$SCRIPT_DIR/projects"

FILTER="${1:-}"
PROJECT_NAMES=()
if [[ -n "$FILTER" ]]; then
    PROJECT_NAMES+=("$FILTER")
else
    for dir in "$PROJECTS_DIR"/*/; do
        PROJECT_NAMES+=("$(basename "$dir")")
    done
fi
IFS=$'\n' PROJECT_NAMES=($(sort <<<"${PROJECT_NAMES[*]}")); unset IFS

TOTAL=${#PROJECT_NAMES[@]}
PASS=0
FAIL=0
ERRORS=()

warn() {
    echo "  WARN  $1"
}

echo "Validating $TOTAL project setups..."
echo ""

for proj in "${PROJECT_NAMES[@]}"; do
    proj_dir="$PROJECTS_DIR/$proj"
    WORK_DIR="$(mktemp -d)"

    # Count tasks
    task_count=0
    while IFS= read -r line; do
        line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [[ -z "$line" || "$line" == \#* ]] && continue
        task_count=$((task_count + 1))
    done < "$proj_dir/tasks.txt"

    if [[ "$proj" != real-* && ! -f "$proj_dir/verify.sh" ]]; then
        echo "  FAIL  $proj — missing verify.sh"
        ERRORS+=("$proj: missing verify.sh")
        FAIL=$((FAIL + 1))
        rm -rf "$WORK_DIR"
        continue
    fi

    if [[ -f "$proj_dir/verify.sh" ]] && ! bash -n "$proj_dir/verify.sh"; then
        echo "  FAIL  $proj — verify.sh has invalid bash syntax"
        ERRORS+=("$proj: verify.sh syntax check failed")
        FAIL=$((FAIL + 1))
        rm -rf "$WORK_DIR"
        continue
    fi

    # Setup
    setup_ok=1
    setup_log="$WORK_DIR/setup.log"
    (
        cd "$WORK_DIR"
        git init -q
        git config user.email "validate@otto.dev"
        git config user.name "Validate"
        bash "$proj_dir/setup.sh"
    ) > "$setup_log" 2>&1 || setup_ok=0

    if [[ $setup_ok -eq 0 ]]; then
        echo "  FAIL  $proj — setup.sh failed"
        ERRORS+=("$proj: setup.sh failed (see $setup_log)")
        FAIL=$((FAIL + 1))
        continue
    fi

    # For real-repo projects, verify existing tests mostly pass (code is pre-fix).
    # For bugfix projects, verify tests can RUN (they may fail — that's by design).
    # Allow up to 2 pre-existing test failures (version compat, env-specific).
    test_ok=1
    is_bugfix=0
    [[ "$proj" == bugfix-* ]] && is_bugfix=1
    if [[ "$proj" == bugfix-* || "$proj" == real-* ]]; then
        # Detect test command
        test_cmd=""
        if [[ -f "$WORK_DIR/package.json" ]]; then
            # Install deps first
            if ! (cd "$WORK_DIR" && npm install --no-audit --no-fund) >> "$setup_log" 2>&1; then
                warn "$proj — npm install failed, continuing with available dependencies (see $setup_log)"
            fi
            test_cmd="npm test"
        elif [[ -f "$WORK_DIR/pyproject.toml" ]] || [[ -f "$WORK_DIR/setup.py" ]] || [[ -f "$WORK_DIR/setup.cfg" ]]; then
            # Install Python package in a temp venv (avoids PEP 668 system Python issues)
            # Try [all] extras first, fall back to plain install
            install_ok=1
            if ! (cd "$WORK_DIR" && python3 -m venv .venv) >> "$setup_log" 2>&1; then
                install_ok=0
            elif ! (cd "$WORK_DIR" && (.venv/bin/pip install -q -e ".[all,test,dev]" || .venv/bin/pip install -q -e .)) >> "$setup_log" 2>&1; then
                install_ok=0
            elif ! (cd "$WORK_DIR" && .venv/bin/pip install -q pytest) >> "$setup_log" 2>&1; then
                install_ok=0
            fi
            if [[ $install_ok -eq 0 ]]; then
                warn "$proj — Python dependency install failed, continuing with available environment (see $setup_log)"
            fi
            test_cmd=".venv/bin/python -m pytest -q --override-ini='addopts='"
        elif ls "$WORK_DIR"/test_*.py > /dev/null 2>&1; then
            test_cmd="python3 -m pytest -q"
        fi

        if [[ -n "$test_cmd" ]]; then
            test_log="$WORK_DIR/test.log"
            (cd "$WORK_DIR" && eval "$test_cmd") > "$test_log" 2>&1 || test_ok=0
            if [[ $test_ok -eq 0 ]]; then
                last_lines=$(tail -5 "$test_log" 2>/dev/null | tr '\n' ' ')
                if [[ $is_bugfix -eq 1 ]]; then
                    # Bugfix projects: tests SHOULD fail (code has bugs).
                    # Only fail validation if tests can't RUN at all (infra issue).
                    if echo "$last_lines" | grep -qiE "command not found|cannot find module|no module named|SyntaxError"; then
                        echo "  FAIL  $proj — test infrastructure broken: $last_lines"
                        ERRORS+=("$proj: test infra broken — $last_lines")
                        FAIL=$((FAIL + 1))
                        rm -rf "$WORK_DIR"
                        continue
                    else
                        echo "  OK    $proj ($task_count tasks) [tests fail as expected — bugfix project]"
                        PASS=$((PASS + 1))
                        rm -rf "$WORK_DIR"
                        continue
                    fi
                fi
                # Allow small number of pre-existing failures (version compat, env-specific)
                fail_count=$(echo "$last_lines" | grep -oE '[0-9]+ failed' | awk '{print $1}' || echo 999)
                pass_count=$(echo "$last_lines" | grep -oE '[0-9]+ passed' | awk '{print $1}' || echo 0)
                if [[ "$fail_count" -le 2 && "$pass_count" -gt 0 ]]; then
                    echo "  OK    $proj ($task_count tasks) [$fail_count pre-existing test failures — acceptable]"
                    PASS=$((PASS + 1))
                    rm -rf "$WORK_DIR"
                    continue
                fi
                echo "  FAIL  $proj — existing tests fail: $last_lines"
                ERRORS+=("$proj: existing tests fail — $last_lines")
                FAIL=$((FAIL + 1))
                rm -rf "$WORK_DIR"
                continue
            fi
        fi
    fi

    echo "  OK    $proj ($task_count tasks)"
    PASS=$((PASS + 1))
    rm -rf "$WORK_DIR"
done

echo ""
echo "============================================"
echo "  $PASS/$TOTAL passed, $FAIL failed"
echo "============================================"

if [[ ${#ERRORS[@]} -gt 0 ]]; then
    echo ""
    echo "Failures:"
    for err in "${ERRORS[@]}"; do
        echo "  - $err"
    done
    exit 1
fi
