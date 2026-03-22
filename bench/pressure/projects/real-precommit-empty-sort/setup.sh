#!/usr/bin/env bash
set -euo pipefail
git clone --quiet https://github.com/pre-commit/pre-commit-hooks.git .tmp-clone
cd .tmp-clone
git checkout --quiet f71fa2c
cd ..
rsync -a --exclude='.git' .tmp-clone/ ./
rm -rf .tmp-clone
# Override test command to scope to relevant tests only (full suite has unrelated failures)
mkdir -p .
cat > otto.yaml <<'YAML'
test_command: "python -m pytest tests/file_contents_sorter_test.py -v"
YAML
git add -A && git commit -m "init pre-commit-hooks at v4.4.0 (pre-empty-sort-fix)"
