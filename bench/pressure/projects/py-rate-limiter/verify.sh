#!/usr/bin/env bash
set -euo pipefail

trap 'rm -f verify_check.py' EXIT

cat > verify_check.py <<'PY'
import importlib
import inspect
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
    for name in ("rate_limiter", "ratelimiter", "limiter"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise AssertionError("could not import a rate limiter module")


def find_class(module, required_methods):
    for _, value in inspect.getmembers(module, inspect.isclass):
        if all(hasattr(value, method) for method in required_methods):
            return value
    raise AssertionError(f"no class with methods {required_methods}")


def build_instance(cls, values):
    kwargs = {}
    sig = inspect.signature(cls)
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if name in values:
            kwargs[name] = values[name]
        elif param.default is inspect._empty:
            lowered = name.lower()
            if "rate" in lowered:
                kwargs[name] = values.get("rate", 1)
            elif "capacity" in lowered or "burst" in lowered:
                kwargs[name] = values.get("capacity", 1)
            elif "limit" in lowered:
                kwargs[name] = values.get("limit", 1)
            elif "window" in lowered:
                kwargs[name] = values.get("window_seconds", 1)
            else:
                raise AssertionError(f"unsupported constructor parameter: {name}")
    return cls(**kwargs)


module = load_module()
TokenBucket = getattr(module, "TokenBucket", None) or find_class(module, ("consume",))
SlidingWindowLimiter = getattr(module, "SlidingWindowLimiter", None)
if SlidingWindowLimiter is None:
    for _, value in inspect.getmembers(module, inspect.isclass):
        if value is TokenBucket:
            continue
        if hasattr(value, "consume"):
            SlidingWindowLimiter = value
            break
if SlidingWindowLimiter is None:
    raise AssertionError("missing sliding window limiter class")


def check_bool_return():
    bucket = build_instance(TokenBucket, {"rate": 0, "capacity": 1})
    assert isinstance(bucket.consume(), bool)


def check_capacity_limit():
    bucket = build_instance(TokenBucket, {"rate": 0, "capacity": 3})
    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is False


def check_refill():
    bucket = build_instance(TokenBucket, {"rate": 5, "capacity": 2})
    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is False
    time.sleep(0.5)
    assert bucket.consume() is True


def check_thread_safety():
    bucket = build_instance(TokenBucket, {"rate": 0, "capacity": 100})
    barrier = threading.Barrier(50)
    successes = []
    lock = threading.Lock()

    def worker():
        local = 0
        barrier.wait()
        for _ in range(20):
            if bucket.consume():
                local += 1
        with lock:
            successes.append(local)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(successes) == 100, sum(successes)


def check_sliding_key_isolation():
    limiter = build_instance(SlidingWindowLimiter, {"rate": 0, "capacity": 2, "limit": 2, "window_seconds": 1})
    assert limiter.consume("alpha") is True
    assert limiter.consume("alpha") is True
    assert limiter.consume("alpha") is False
    assert limiter.consume("beta") is True


def check_sliding_window_reset():
    limiter = build_instance(SlidingWindowLimiter, {"rate": 1, "capacity": 1, "limit": 1, "window_seconds": 1})
    assert limiter.consume("gamma") is True
    assert limiter.consume("gamma") is False
    time.sleep(1.1)
    assert limiter.consume("gamma") is True


report("consume returns bool", check_bool_return)
report("token bucket enforces burst capacity", check_capacity_limit)
report("token bucket refills over time", check_refill)
report("token bucket is thread-safe under contention", check_thread_safety)
report("sliding window limits per key", check_sliding_key_isolation)
report("sliding window resets after the window elapses", check_sliding_window_reset)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
