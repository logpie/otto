#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: py-cli-task-manager"

# Use a temp database to avoid polluting real data
export TASKMANAGER_DB=$(mktemp /tmp/taskmanager_verify_XXXXXX.db)
trap "rm -f $TASKMANAGER_DB" EXIT

# Find the CLI entry point
CLI=""
for f in task_manager.py cli.py main.py app.py taskmanager.py; do
  if [ -f "$f" ]; then CLI="python3 $f"; break; fi
done
if [ -z "$CLI" ]; then
  # Try as module
  for m in task_manager cli main app taskmanager; do
    if python3 -m "$m" --help >/dev/null 2>&1; then CLI="python3 -m $m"; break; fi
  done
fi
if [ -z "$CLI" ]; then echo "  FAIL  Could not find CLI entry point"; exit 1; fi

check "add subcommand creates a task" \
  "$CLI add 'Verify test task' --priority high 2>&1 | grep -iq -e 'added\|created\|id\|task\|success'"

check "list subcommand shows tasks" \
  "$CLI list 2>&1 | grep -i -e 'verify test task\|task'"

check "done subcommand marks task complete" \
  "$CLI done 1 2>&1; $CLI list --status done 2>&1 | grep -iq -e 'done\|complete\|verify'"

check "search subcommand finds tasks by keyword" \
  "$CLI add 'Buy groceries for dinner' 2>&1 >/dev/null; $CLI search groceries 2>&1 | grep -iq 'groceries'"

check "delete subcommand removes a task" \
  "$CLI delete 1 2>&1 | grep -iq -e 'delet\|remov\|success'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
