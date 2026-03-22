#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import inventory
import threading

failures = 0
Manager = getattr(inventory, "InventoryManager")


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def build_inventory():
    return Manager()


def item_quantity(inv, name):
    return inv.items[name]["quantity"]


def item_price(inv, name):
    return inv.items[name]["price"]


def check_thread_safe_sales():
    inv = build_inventory()
    inv.add_item("Widget", 100, 10.0)
    sold = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker():
        local = 0
        barrier.wait()
        for _ in range(10):
            try:
                inv.sell_item("Widget", 1)
                local += 1
            except Exception:
                pass
        with lock:
            sold.append(local)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    remaining = item_quantity(inv, "Widget")
    assert remaining >= 0
    assert sum(sold) + remaining == 100


def check_oversell_rejected():
    inv = build_inventory()
    inv.add_item("Widget", 5, 10.0)
    try:
        inv.sell_item("Widget", 6)
    except Exception:
        pass
    else:
        raise AssertionError("oversell did not raise")
    assert item_quantity(inv, "Widget") == 5


def check_price_filter_is_and():
    inv = build_inventory()
    inv.add_item("Cheap", 1, 5.0)
    inv.add_item("Mid", 1, 15.0)
    inv.add_item("Expensive", 1, 50.0)
    names = [name for name, _ in inv.find_items(min_price=10, max_price=20)]
    assert names == ["Mid"]


def check_discount_reduces_price():
    inv = build_inventory()
    inv.add_item("Widget", 1, 100.0)
    inv.apply_discount("Widget", 20)
    assert round(item_price(inv, "Widget"), 2) == 80.0


def check_total_value():
    inv = build_inventory()
    inv.add_item("A", 2, 10.0)
    inv.add_item("B", 3, 5.0)
    assert round(inv.get_total_value(), 2) == 35.0


def check_report_ordering():
    inv = build_inventory()
    inv.add_item("Low", 1, 5.0)
    inv.add_item("High", 5, 20.0)
    inv.add_item("Mid", 2, 15.0)
    report_rows = inv.get_report()
    values = [row["value"] for row in report_rows]
    assert values == sorted(values, reverse=True)
    assert report_rows[0]["name"] == "High"


def check_invalid_operations_raise():
    inv = build_inventory()
    try:
        inv.sell_item("Missing", 1)
    except Exception:
        return
    raise AssertionError("selling a missing item should raise")


report("concurrent sells are thread-safe and conserve stock", check_thread_safe_sales)
report("overselling is rejected without going negative", check_oversell_rejected)
report("find_items applies min and max price conjunctively", check_price_filter_is_and)
report("apply_discount lowers the stored price", check_discount_reduces_price)
report("inventory valuation sums quantity times price", check_total_value)
report("reports are sorted by value descending", check_report_ordering)
report("invalid sell operations surface explicit errors", check_invalid_operations_raise)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
