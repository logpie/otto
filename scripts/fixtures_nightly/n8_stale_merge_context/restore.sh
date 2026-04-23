#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

rm -f *.db .coverage
find . -name '__pycache__' -type d -prune -exec rm -rf {} +
find . -name '.pytest_cache' -type d -prune -exec rm -rf {} +

if [[ -d .git ]]; then
  git reset --hard HEAD >/dev/null
  git clean -fd >/dev/null
fi
