#!/usr/bin/env bash
set -euo pipefail

trap 'rm -f verify_check.py' EXIT

cat > verify_check.py <<'PY'
import importlib
import inspect
import json
import pathlib
import tempfile
import threading

failures = 0
tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="jsondb-verify-"))


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def load_module():
    for name in ("jsondb", "json_db", "db", "database"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise AssertionError("no JsonDB module found")


module = load_module()


def find_db_class():
    for _, value in inspect.getmembers(module, inspect.isclass):
        if hasattr(value, "collection"):
            return value
    raise AssertionError("no DB class exposing collection()")


DB = getattr(module, "JsonDB", None) or find_db_class()


def build_db(path):
    sig = inspect.signature(DB)
    kwargs = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if name in ("path", "directory", "db_path", "root", "base_path"):
            kwargs[name] = str(path)
        elif param.default is inspect._empty:
            raise AssertionError(f"unsupported DB constructor parameter: {name}")
    return DB(**kwargs)


def new_collection(name="users"):
    db = build_db(tmpdir)
    return db.collection(name)


def inserted_id(doc):
    return doc.get("id") or doc.get("_id")


def check_insert_and_file_persistence():
    col = new_collection("users")
    doc = {"name": "Alice", "age": 30}
    inserted = col.insert(doc)
    stored = col.find_one({"name": "Alice"})
    identifier = inserted if isinstance(inserted, str) else inserted_id(stored)
    assert isinstance(identifier, str) and identifier
    files = list(tmpdir.glob("users*.json"))
    assert files, "collection JSON file missing"


def check_query_operators():
    col = new_collection("operators")
    for value in (10, 20, 30):
        col.insert({"value": value, "group": "a" if value < 30 else "b"})
    gt = col.find({"value": {"$gt": 15}})
    both = col.find({"$and": [{"value": {"$gte": 20}}, {"group": "b"}]})
    either = col.find({"$or": [{"value": 10}, {"value": 30}]})
    assert sorted(item["value"] for item in gt) == [20, 30]
    assert [item["value"] for item in both] == [30]
    assert sorted(item["value"] for item in either) == [10, 30]


def check_update_operators():
    col = new_collection("updates")
    col.insert({"name": "Bob", "count": 1, "flag": True})
    col.update({"name": "Bob"}, {"$set": {"name": "Bobby"}})
    col.update({"name": "Bobby"}, {"$inc": {"count": 4}})
    col.update({"name": "Bobby"}, {"$unset": {"flag": True}})
    row = col.find_one({"name": "Bobby"})
    assert row["count"] == 5
    assert "flag" not in row


def check_delete_and_find_one():
    col = new_collection("delete_me")
    col.insert({"name": "A"})
    col.insert({"name": "B"})
    col.delete({"name": "A"})
    assert col.find_one({"name": "A"}) in (None, {})
    assert col.find_one({"name": "B"})["name"] == "B"


def check_reopen_persistence():
    db1 = build_db(tmpdir)
    db1.collection("persist").insert({"name": "Persisted", "count": 3})
    db2 = build_db(tmpdir)
    stored = db2.collection("persist").find_one({"name": "Persisted"})
    assert stored["count"] == 3


def check_index_behavior():
    col = new_collection("indexed")
    for idx in range(10):
        col.insert({"email": f"user{idx}@example.com", "name": f"user{idx}"})
    col.create_index("email")
    row = col.find_one({"email": "user5@example.com"})
    assert row["name"] == "user5"


def check_concurrent_access():
    col = new_collection("threads")
    errors = []

    def worker(offset):
        try:
            for index in range(25):
                col.insert({"worker": offset, "value": index})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    data = col.find({})
    assert not errors, errors
    assert len(data) == 200
    json.loads(next(tmpdir.glob("threads*.json")).read_text(encoding="utf-8"))


report("documents get string ids and persist to collection JSON files", check_insert_and_file_persistence)
report("exact, comparison, and logical query operators work", check_query_operators)
report("update operators set, increment, and unset fields", check_update_operators)
report("delete removes only matching docs and find_one handles misses", check_delete_and_find_one)
report("data persists across DB re-instantiation", check_reopen_persistence)
report("create_index preserves correct exact-match lookups", check_index_behavior)
report("concurrent access does not corrupt collection state", check_concurrent_access)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
