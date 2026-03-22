#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import importlib
import inspect
import json
import os
import pathlib
import tempfile
import threading
import time

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def load_module():
    for name in ("kvstore", "kv_store", "store"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise AssertionError("no KV store module found")


def find_store_class(module):
    for _, value in inspect.getmembers(module, inspect.isclass):
        methods = ("get", "set", "delete", "keys", "clear")
        if all(hasattr(value, method) for method in methods):
            return value
    raise AssertionError("no KV store class found")


def build_store(cls, path):
    sig = inspect.signature(cls)
    kwargs = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if name in ("path", "filename", "file_path", "storage_path", "db_path"):
            kwargs[name] = str(path)
        elif param.default is inspect._empty:
            raise AssertionError(f"unsupported constructor parameter: {name}")
    return cls(**kwargs)


module = load_module()
Store = getattr(module, "KVStore", None) or find_store_class(module)
tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="kvstore-verify-"))
store_path = tmpdir / "store.db"


def fresh_store():
    return build_store(Store, store_path)


def check_crud_and_json():
    store = fresh_store()
    payload = {"name": "alice", "tags": [1, 2], "meta": {"a": True}}
    store.set("user:1", payload)
    assert store.get("user:1") == payload
    assert store.exists("user:1") is True
    store.delete("user:1")
    assert store.get("user:1") is None
    store.set("x", 1)
    store.clear()
    assert store.keys() == [] or len(store.keys()) == 0


def check_default_and_ttl():
    store = fresh_store()
    assert store.get("missing", "fallback") == "fallback"
    store.set("ephemeral", {"ok": True}, ttl_seconds=1)
    assert store.get("ephemeral") == {"ok": True}
    time.sleep(1.2)
    assert store.get("ephemeral", "gone") == "gone"


def check_glob_keys():
    store = fresh_store()
    store.set("user:1", 1)
    store.set("user:2", 2)
    store.set("config:theme", "dark")
    keys = sorted(store.keys("user:*"))
    assert keys == ["user:1", "user:2"]


def check_compaction():
    store = fresh_store()
    large = "x" * 200
    for index in range(1105):
        store.set("rolling", f"{large}{index}")
    size = os.path.getsize(store_path)
    reopened = fresh_store()
    assert reopened.get("rolling").startswith(large)
    assert size < 120000, size


def check_concurrent_readers_and_writer():
    store = fresh_store()
    errors = []
    stop = threading.Event()

    def writer():
        try:
            for index in range(200):
                store.set("counter", index)
                time.sleep(0.002)
        except Exception as exc:
            errors.append(exc)
        finally:
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                value = store.get("counter", 0)
                assert isinstance(value, int)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(10)]
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    for thread in threads:
        thread.start()
    writer_thread.join()
    for thread in threads:
        thread.join()
    reopened = fresh_store()
    assert not errors, errors
    assert isinstance(reopened.get("counter"), int)


def check_persistence_after_reopen():
    store = fresh_store()
    store.set("persisted", {"value": 42})
    reopened = fresh_store()
    assert reopened.get("persisted") == {"value": 42}


report("KV store supports CRUD with JSON values", check_crud_and_json)
report("get default and TTL expiry both work", check_default_and_ttl)
report("keys(pattern) performs glob filtering", check_glob_keys)
report("append-only log compacts after large write volume", check_compaction)
report("concurrent readers observe consistent values during writes", check_concurrent_readers_and_writer)
report("data persists after reopening the store", check_persistence_after_reopen)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
