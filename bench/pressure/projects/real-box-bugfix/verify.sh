#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
from box import Box

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def build_box():
    return Box({"foo": {"bar": 1}, "plain": 9}, box_dots=True)


def check_existing_dotted_get():
    box = build_box()
    assert box.get("foo.bar", 99) == 1


def check_missing_intermediate_default():
    box = build_box()
    assert box.get("foo.grab.plum", 4) == 4


def check_missing_final_default():
    box = build_box()
    assert box.get("foo.missing", "fallback") == "fallback"


def check_no_default_behavior():
    box = build_box()
    assert box.get("foo.missing") is None


def check_box_dots_access_and_set():
    box = build_box()
    box["foo.new_leaf"] = 7
    assert box.foo.new_leaf == 7
    assert box.get("foo.new_leaf") == 7


def check_top_level_behavior_unchanged():
    box = build_box()
    assert box.get("plain", 0) == 9
    assert box.get("absent", "x") == "x"


report("existing dotted paths still resolve through get()", check_existing_dotted_get)
report("missing intermediate dotted keys return the provided default", check_missing_intermediate_default)
report("missing final dotted keys return the provided default", check_missing_final_default)
report("missing dotted paths without a default return None", check_no_default_behavior)
report("box_dots set/access behavior remains intact", check_box_dots_access_and_set)
report("non-dotted get behavior is unchanged", check_top_level_behavior_unchanged)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
