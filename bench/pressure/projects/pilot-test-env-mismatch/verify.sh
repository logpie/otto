#!/usr/bin/env bash
set -uo pipefail
failures=0

check() {
  if "$@" > /dev/null 2>&1; then
    echo "PASS $1"
  else
    echo "FAIL $1"
    failures=$((failures + 1))
  fi
}

# All tests should pass with vitest (not jest)
check npm test

# Verify functions exist
node -e "import('./math.js').then(m => { if(typeof m.multiply !== 'function') throw 'no multiply'; })" 2>/dev/null && echo "PASS multiply exists" || { echo "FAIL multiply exists"; failures=$((failures+1)); }
node -e "import('./math.js').then(m => { if(typeof m.divide !== 'function') throw 'no divide'; })" 2>/dev/null && echo "PASS divide exists" || { echo "FAIL divide exists"; failures=$((failures+1)); }
node -e "import('./math.js').then(m => { if(typeof m.power !== 'function') throw 'no power'; })" 2>/dev/null && echo "PASS power exists" || { echo "FAIL power exists"; failures=$((failures+1)); }
node -e "import('./math.js').then(m => { if(typeof m.sqrt !== 'function') throw 'no sqrt'; })" 2>/dev/null && echo "PASS sqrt exists" || { echo "FAIL sqrt exists"; failures=$((failures+1)); }

exit $failures
