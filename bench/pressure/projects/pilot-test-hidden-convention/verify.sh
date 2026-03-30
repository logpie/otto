#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py << 'PY'
import subprocess
failures = 0

def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")

def check_number_utils_registered():
    import registry
    import number_utils
    for name in ['clamp', 'lerp', 'map_range']:
        assert registry.get(name) is not None, f"{name} not registered"
    assert registry.call('clamp', 5, 0, 10) == 5
    assert registry.call('clamp', -1, 0, 10) == 0
    assert registry.call('lerp', 0, 10, 0.5) == 5.0

def check_string_transforms_registered():
    import registry
    import string_transforms
    for name in ['caesar_cipher', 'reverse_words', 'title_case']:
        assert registry.get(name) is not None, f"{name} not registered"
    assert registry.call('reverse_words', 'hello world') == 'world hello'
    assert registry.call('title_case', 'hello world') == 'Hello World'

def check_all_registered():
    import registry
    import text_utils, number_utils, string_transforms
    all_funcs = registry.list_all()
    assert len(all_funcs) >= 9, f"Expected 9+ registered functions, got {len(all_funcs)}: {all_funcs}"

def check_cli_list():
    result = subprocess.run(['python3', 'cli.py', 'list'], capture_output=True, text=True)
    assert result.returncode == 0, f"CLI list failed: {result.stderr}"
    output = result.stdout
    assert 'slugify' in output
    assert 'clamp' in output
    assert 'caesar_cipher' in output

def check_cli_call():
    result = subprocess.run(['python3', 'cli.py', 'call', 'word_count', 'one two three'], capture_output=True, text=True)
    assert result.returncode == 0, f"CLI call failed: {result.stderr}"
    assert '3' in result.stdout

report("number_utils functions are registered", check_number_utils_registered)
report("string_transforms functions are registered", check_string_transforms_registered)
report("all 9+ functions registered", check_all_registered)
report("CLI list shows all functions", check_cli_list)
report("CLI call executes functions", check_cli_call)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
