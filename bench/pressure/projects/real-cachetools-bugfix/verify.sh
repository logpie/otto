#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import sys
sys.path.insert(0, 'src')
import threading
import time

from cachetools.func import lru_cache, ttl_cache

failures = 0


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def run_wave(decorator_factory):
    calls = {"count": 0}

    @decorator_factory
    def slow(value):
        calls["count"] += 1
        time.sleep(0.05)
        return value * 2

    barrier = threading.Barrier(100)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        value = slow(5)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    info = slow.cache_info()
    return slow, calls["count"], results, info


def check_ttl_cache_stampede():
    slow, count, results, info = run_wave(ttl_cache(maxsize=32, ttl=60))
    assert len(results) == 100
    assert all(result == 10 for result in results)
    assert count == 1
    assert info.misses == 1 and info.hits == 99
    assert slow(5) == 10
    assert slow.cache_info().hits >= 100


def check_lru_cache_stampede():
    _, count, results, info = run_wave(lru_cache(maxsize=32))
    assert len(results) == 100
    assert count == 1
    assert info.misses == 1 and info.hits == 99


def check_distinct_arguments_still_cache_independently():
    calls = {"count": 0}

    @lru_cache(maxsize=32)
    def slow(value):
        calls["count"] += 1
        time.sleep(0.01)
        return value

    assert slow(1) == 1
    assert slow(2) == 2
    assert slow(1) == 1
    assert calls["count"] == 2


def check_post_wave_cache_hit():
    @ttl_cache(maxsize=8, ttl=60)
    def slow(value):
        time.sleep(0.02)
        return value

    assert slow(7) == 7
    before = time.perf_counter()
    assert slow(7) == 7
    elapsed = time.perf_counter() - before
    assert elapsed < 0.01


def check_cache_info_is_consistent():
    @lru_cache(maxsize=4)
    def value(x):
        return x + 1

    value(1)
    value(1)
    value(2)
    info = value.cache_info()
    assert info.misses == 2
    assert info.hits == 1


def check_different_keys_not_serialized():
    """Spawn 10 threads each calling a DIFFERENT key with 0.1s sleep.
    If calls are properly parallel: ~0.1-0.2s total.
    If calls are serialized (naive global lock during compute): ~1.0s total.
    Assert < 0.5s to catch serialization with generous margin."""

    @lru_cache(maxsize=32)
    def slow(value):
        time.sleep(0.1)
        return value * 2

    barrier = threading.Barrier(10)
    results = {}
    lock = threading.Lock()

    def worker(key):
        barrier.wait()
        result = slow(key)
        with lock:
            results[key] = result

    start = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    # Verify correctness
    assert len(results) == 10
    for i in range(10):
        assert results[i] == i * 2, f"Wrong result for key {i}: {results[i]}"

    # Verify concurrency — different keys must not be serialized
    assert elapsed < 0.5, (
        f"Different-key calls took {elapsed:.2f}s (expected <0.5s). "
        f"Calls appear serialized — the fix must not hold a global lock during compute."
    )


report("ttl_cache allows one miss and 99 hits under 100-thread contention", check_ttl_cache_stampede)
report("lru_cache also suppresses cache stampedes", check_lru_cache_stampede)
report("different arguments still populate distinct cache entries", check_distinct_arguments_still_cache_independently)
report("subsequent calls after the first wave are immediate cache hits", check_post_wave_cache_hit)
report("cache_info remains internally consistent", check_cache_info_is_consistent)
report("different-key calls run concurrently, not serialized", check_different_keys_not_serialized)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
