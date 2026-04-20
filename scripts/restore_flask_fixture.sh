#!/usr/bin/env bash
# Restore the Flask API fixture for merge-agent experiments.
#
# Saves you from re-running phase 1+2 ($5+ and 5min). The bundle contains:
# - main branch (with base merged)
# - improve/imp-tags-2026-04-20 (failed cert, has commits)
# - improve/imp-priority-2026-04-20 (failed cert, has commits)
# - build/base-2026-04-20
#
# Usage:
#   ./scripts/restore_flask_fixture.sh /tmp/my-merge-test
#   cd /tmp/my-merge-test
#   echo -e "default_branch: main\n\nqueue:\n  merge_mode: consolidated" > otto.yaml
#   git add otto.yaml && git commit -q -m "test config"
#   otto merge improve/imp-tags-2026-04-20 improve/imp-priority-2026-04-20 --no-certify

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
for tid in tags priority; do
  git update-ref "refs/heads/improve/imp-$tid-2026-04-20" \
    "refs/fixture-tmp/refs/heads/improve/imp-$tid-2026-04-20"
done
git update-ref refs/heads/build/base-2026-04-20 \
  refs/fixture-tmp/refs/heads/build/base-2026-04-20
git for-each-ref refs/fixture-tmp/ --format='%(refname)' | xargs -n1 -I{} git update-ref -d {} 2>/dev/null || true
git checkout -q main
git reset -q --hard main

echo "Restored Flask API fixture at $TARGET"
echo "Branches:"
git branch
echo
echo "main HEAD:"
git log --oneline -3
