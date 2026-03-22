#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import json
import pathlib
import tempfile

from tinydb import TinyDB
from tinydb.storages import JSONStorage, MemoryStorage

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def check_memory_storage_persist_empty():
    db = TinyDB(storage=MemoryStorage)
    table = db.table("persistent", persist_empty=True)
    assert "persistent" in db.storage.read()
    assert db.storage.read()["persistent"] == {}
    assert len(table) == 0


def check_default_table_is_lazy():
    db = TinyDB(storage=MemoryStorage)
    table = db.table("lazy")
    assert "lazy" not in (db.storage.read() or {})
    table.insert({"name": "x"})
    assert "lazy" in db.storage.read()


def check_clear_keeps_empty_entry():
    db = TinyDB(storage=MemoryStorage)
    table = db.table("persistent", persist_empty=True)
    table.insert({"name": "x"})
    table.truncate()
    assert db.storage.read()["persistent"] == {}


def check_json_storage_round_trip():
    path = pathlib.Path(tempfile.mkdtemp(prefix="tinydb-verify-")) / "db.json"
    db = TinyDB(path)
    db.table("persistent", persist_empty=True)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["persistent"] == {}
    db.close()
    reopened = TinyDB(path)
    assert "persistent" in reopened.storage.read()
    reopened.close()


def check_default_behavior_unchanged():
    db = TinyDB(storage=MemoryStorage)
    table = db.table("normal")
    doc_id = table.insert({"name": "alice"})
    assert table.get(doc_id=doc_id)["name"] == "alice"
    table.remove(doc_ids=[doc_id])
    assert table.all() == []


def check_non_persist_empty_explicit_false():
    db = TinyDB(storage=MemoryStorage)
    db.table("explicit_false", persist_empty=False)
    assert "explicit_false" not in (db.storage.read() or {})


report("persist_empty=True materializes an empty table immediately", check_memory_storage_persist_empty)
report("default tables remain lazy until first insert", check_default_table_is_lazy)
report("truncate keeps an empty entry for persist_empty tables", check_clear_keeps_empty_entry)
report("persist_empty survives JSON storage round-trips", check_json_storage_round_trip)
report("existing TinyDB table behavior remains unchanged", check_default_behavior_unchanged)
report("persist_empty=False remains explicitly lazy", check_non_persist_empty_explicit_false)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
