#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py << 'PY'
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

def check_ttl():
    from store import Store
    s = Store()
    s.set("k", "v", ttl=1)
    assert s.get("k") == "v"
    time.sleep(1.1)
    assert s.get("k") is None

def check_list_active():
    from store import Store
    s = Store()
    s.set("a", 1)
    s.set("b", 2, ttl=60)
    active = s.list_active()
    assert isinstance(active, list)
    assert len(active) == 2

def check_cache_basic():
    from store import Store
    from cache import CacheLayer
    s = Store()
    c = CacheLayer(s, max_size=2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1

def check_cache_eviction():
    from store import Store
    from cache import CacheLayer
    s = Store()
    c = CacheLayer(s, max_size=2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)  # should evict LRU
    stats = c.stats()
    assert stats["evictions"] >= 1
    assert stats["size"] <= 2

def check_api():
    from api import app
    client = app.test_client()
    r = client.put("/cache/test", json={"value": "hello"})
    assert r.status_code in (200, 201)
    r = client.get("/cache/test")
    assert r.status_code == 200
    data = r.get_json()
    assert data["value"] == "hello"

report("TTL expires keys", check_ttl)
report("list_active returns all non-expired", check_list_active)
report("cache basic get/put", check_cache_basic)
report("cache eviction works", check_cache_eviction)
report("API put and get", check_api)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
