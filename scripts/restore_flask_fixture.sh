#!/usr/bin/env bash
# Restore the Flask API fixture for merge-agent experiments.
#
# Saves you from re-running phase 1+2 ($5+ and 5min). The bundle contains:
# - main branch (with base merged)
# - improve/imp-tags-* (failed cert, has commits)
# - improve/imp-priority-* (failed cert, has commits)
# - build/base-*
#
# Usage:
#   ./scripts/restore_flask_fixture.sh /tmp/my-merge-test
#   cd /tmp/my-merge-test
#   printf "default_branch: main\n" > otto.yaml
#   git add otto.yaml && git commit -q -m "test config"
#   git branch
#   otto merge <restored-tags-branch> <restored-priority-branch> --no-certify

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <target-dir>" >&2
  exit 2
fi

TARGET="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE="$SCRIPT_DIR/../bench-fixtures/flask-api-branches.bundle"

if [ ! -f "$BUNDLE" ]; then
  echo "Bundle not found at $BUNDLE" >&2
  exit 1
fi

if [ -e "$TARGET" ]; then
  echo "Target $TARGET already exists; refusing to overwrite" >&2
  exit 1
fi

mkdir -p "$TARGET"
cd "$TARGET"
git init -q -b main
git config user.email "fixture@bench"
git config user.name "Fixture"
git fetch -q "$BUNDLE" '*:refs/fixture-tmp/*'
git update-ref refs/heads/main refs/fixture-tmp/refs/fixtures/main

restore_branch() {
  local pattern="$1"
  mapfile -t refs < <(git for-each-ref --format='%(refname)' "refs/fixture-tmp/refs/heads/$pattern")
  if [ "${#refs[@]}" -ne 1 ]; then
    echo "Expected exactly one bundle ref for $pattern, found ${#refs[@]}" >&2
    printf '  %s\n' "${refs[@]}" >&2
    exit 1
  fi
  local branch="${refs[0]#refs/fixture-tmp/refs/heads/}"
  git update-ref "refs/heads/$branch" "${refs[0]}"
}

restore_branch 'improve/imp-tags-*'
restore_branch 'improve/imp-priority-*'
restore_branch 'build/base-*'
git for-each-ref refs/fixture-tmp/ --format='%(refname)' | xargs -n1 -I{} git update-ref -d {} 2>/dev/null || true
git checkout -q main
git reset -q --hard main

echo "Restored Flask API fixture at $TARGET"
echo "Branches:"
git branch
echo
echo "main HEAD:"
git log --oneline -3
