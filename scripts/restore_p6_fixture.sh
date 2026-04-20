#!/usr/bin/env bash
# Restore the P6 inventory CLI fixture for merge-agent experiments.
#
# Saves you from re-running phase 1+2 ($11+ and 16min). The bundle contains:
# - main branch (with base + alerts merged)
# - improve/imp-categories-2026-04-20 (failed cert, has commits)
# - improve/imp-suppliers-2026-04-20 (failed cert, has commits)
# - improve/imp-alerts-2026-04-20 (passed cert, already merged into main)
#
# Usage:
#   ./scripts/restore_p6_fixture.sh /tmp/my-merge-test
#   cd /tmp/my-merge-test
#   # Set merge_mode in otto.yaml if testing F13:
#   echo -e "default_branch: main\n\nqueue:\n  merge_mode: consolidated" > otto.yaml
#   git add otto.yaml && git commit -q -m "test config"
#   # Run the merge under test:
#   otto merge improve/imp-categories-2026-04-20 improve/imp-suppliers-2026-04-20 --no-certify
#   # Compare cost/wall/result between test variants on the same starting state.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <target-dir>" >&2
  exit 2
fi

TARGET="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE="$SCRIPT_DIR/../bench-fixtures/p6-branches.bundle"

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
# Note: bundle refs land under refs/fixture-tmp/refs/<original>/...
git update-ref refs/heads/main refs/fixture-tmp/refs/fixtures/main
for b in alerts categories suppliers; do
  git update-ref "refs/heads/improve/imp-$b-2026-04-20" \
    "refs/fixture-tmp/refs/heads/improve/imp-$b-2026-04-20"
done
git update-ref refs/heads/build/base-2026-04-20 \
  refs/fixture-tmp/refs/heads/build/base-2026-04-20
git for-each-ref refs/fixture-tmp/ --format='%(refname)' | xargs -n1 -I{} git update-ref -d {} 2>/dev/null || true
git checkout -q main
git reset -q --hard main

echo "Restored P6 fixture at $TARGET"
echo "Branches:"
git branch
echo
echo "main HEAD:"
git log --oneline -3
