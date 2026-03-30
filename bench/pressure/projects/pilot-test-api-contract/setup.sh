#!/usr/bin/env bash
set -euo pipefail

cat > store.py << 'PYEOF'
"""In-memory key-value store — base module."""

class Store:
    def __init__(self):
        self._data = {}

    def set(self, key, value):
        self._data[key] = value

    def get(self, key):
        return self._data.get(key)

    def delete(self, key):
        return self._data.pop(key, None)

    def keys(self):
        return list(self._data.keys())
PYEOF

cat > test_store.py << 'PYEOF'
from store import Store

def test_set_and_get():
    s = Store()
    s.set("a", 1)
    assert s.get("a") == 1

def test_delete():
    s = Store()
    s.set("a", 1)
    s.delete("a")
    assert s.get("a") is None
PYEOF

git add -A && git commit -m "init key-value store"
