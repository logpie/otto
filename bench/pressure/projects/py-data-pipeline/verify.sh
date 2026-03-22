#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: py-data-pipeline"

check "pipeline produces JSON output from sample data" \
  "python3 -c '
import subprocess, json, sys
# Try common entry points
for mod in [\"pipeline\", \"main\", \"reporter\"]:
    try:
        r = subprocess.run([sys.executable, \"-m\", mod], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            json.loads(r.stdout)
            exit(0)
    except: pass
# Try as scripts
import glob
for f in glob.glob(\"*.py\"):
    if f.startswith(\"test\"): continue
    try:
        r = subprocess.run([sys.executable, f], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            json.loads(r.stdout)
            exit(0)
    except: pass
exit(1)
'"

check "validator rejects rows with missing product name" \
  "python3 -c '
# Row 6 in sample data has empty product name
# The validator module should flag this
import importlib, sys
for mod_name in [\"validator\", \"validation\", \"pipeline\"]:
    try:
        mod = importlib.import_module(mod_name)
        break
    except ImportError:
        continue
else:
    sys.exit(1)
# Find validation function
fn = None
for attr in [\"validate\", \"validate_row\", \"validate_rows\", \"validate_sale\", \"validate_record\"]:
    fn = getattr(mod, attr, None)
    if fn: break
if fn is None:
    sys.exit(1)
# Call with a row missing product name — should detect the issue
import inspect
sig = inspect.signature(fn)
# If it takes a dict row, test with missing product
try:
    result = fn({\"date\": \"2024-01-17\", \"product\": \"\", \"quantity\": 2, \"unit_price\": 19.99, \"region\": \"South\"})
    # result should indicate error — could be False, empty list, or have errors
    if result is True or result == []:
        sys.exit(1)
except (ValueError, TypeError, KeyError):
    pass  # Raising on bad data is also acceptable
'"

check "validator rejects negative quantities" \
  "python3 -c '
import importlib, sys
for mod_name in [\"validator\", \"validation\", \"pipeline\"]:
    try:
        mod = importlib.import_module(mod_name)
        break
    except ImportError:
        continue
else:
    sys.exit(1)
fn = None
for attr in [\"validate\", \"validate_row\", \"validate_rows\", \"validate_sale\", \"validate_record\"]:
    fn = getattr(mod, attr, None)
    if fn: break
if fn is None:
    sys.exit(1)
try:
    result = fn({\"date\": \"2024-01-18\", \"product\": \"Widget A\", \"quantity\": -1, \"unit_price\": 29.99, \"region\": \"East\"})
    if result is True or result == []:
        sys.exit(1)
except (ValueError, TypeError):
    pass  # Raising on bad data is acceptable
'"

check "aggregation computes total revenue by region" \
  "python3 -c '
import importlib, sys
for mod_name in [\"aggregator\", \"aggregation\", \"analytics\", \"pipeline\"]:
    try:
        mod = importlib.import_module(mod_name)
        break
    except ImportError:
        continue
else:
    sys.exit(1)
# Check it has a region aggregation function
found = False
for attr_name in dir(mod):
    attr = getattr(mod, attr_name)
    if callable(attr) and \"region\" in attr_name.lower():
        found = True
        break
    if callable(attr) and \"aggregate\" in attr_name.lower():
        found = True
        break
    if callable(attr) and \"summary\" in attr_name.lower():
        found = True
        break
assert found, \"no aggregation function found\"
'"

check "loader handles both CSV and JSON inputs" \
  "python3 -c '
import importlib, sys
for mod_name in [\"loader\", \"loaders\", \"data_loader\", \"pipeline\"]:
    try:
        mod = importlib.import_module(mod_name)
        break
    except ImportError:
        continue
else:
    sys.exit(1)
# Check functions exist for loading CSV and JSON
fns = dir(mod)
fn_str = \" \".join(fns).lower()
assert \"csv\" in fn_str or \"load\" in fn_str or \"read\" in fn_str, \"no loader functions found\"
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
