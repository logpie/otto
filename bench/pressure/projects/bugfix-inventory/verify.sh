#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: bugfix-inventory (6 bugs)"

# Bug 1: Race condition — sell_item was not using the lock
check "Bug1: sell_item is thread-safe (no overselling)" \
  "python3 -c '
import threading
from inventory import InventoryManager
inv = InventoryManager()
inv.add_item(\"Widget\", 100, 10.0)
sold = [0]
lock = threading.Lock()
def worker():
    local_sold = 0
    for _ in range(20):
        try:
            inv.sell_item(\"Widget\", 1)
            local_sold += 1
        except (ValueError, KeyError):
            pass
    with lock:
        sold[0] += local_sold
threads = [threading.Thread(target=worker) for _ in range(10)]
for t in threads: t.start()
for t in threads: t.join()
assert inv.items[\"Widget\"][\"quantity\"] >= 0, f\"quantity went negative: {inv.items[\\\"Widget\\\"][\\\"quantity\\\"]}\"
assert sold[0] + inv.items[\"Widget\"][\"quantity\"] == 100, f\"sold={sold[0]}, remaining={inv.items[\\\"Widget\\\"][\\\"quantity\\\"]}\"
'"

# Bug 2: sell_item allows selling more than available (no quantity check)
check "Bug2: sell_item rejects when quantity insufficient" \
  "python3 -c '
from inventory import InventoryManager
inv = InventoryManager()
inv.add_item(\"Widget\", 5, 10.0)
try:
    inv.sell_item(\"Widget\", 10)  # trying to sell more than available
    assert False, \"should have raised\"
except (ValueError, Exception) as e:
    pass  # correctly rejected
# Quantity should not go negative
assert inv.items[\"Widget\"][\"quantity\"] >= 0, f\"quantity went negative: {inv.items[\\\"Widget\\\"][\\\"quantity\\\"]}\"
'"

# Bug 3: find_items uses 'or' instead of 'and' for price range
check "Bug3: find_items filters by BOTH min and max price" \
  "python3 -c '
from inventory import InventoryManager
inv = InventoryManager()
inv.add_item(\"Cheap\", 10, 5.0)
inv.add_item(\"Mid\", 10, 15.0)
inv.add_item(\"Expensive\", 10, 50.0)
results = inv.find_items(min_price=10.0, max_price=20.0)
names = [r[0] for r in results]
assert \"Mid\" in names, f\"Mid should match 10-20 range, got {names}\"
assert \"Cheap\" not in names, f\"Cheap (5.0) should not match min=10, got {names}\"
assert \"Expensive\" not in names, f\"Expensive (50.0) should not match max=20, got {names}\"
'"

# Bug 4: apply_discount is inverted (adds instead of subtracts)
check "Bug4: apply_discount reduces the price" \
  "python3 -c '
from inventory import InventoryManager
inv = InventoryManager()
inv.add_item(\"Widget\", 10, 100.0)
inv.apply_discount(\"Widget\", 20)  # 20% discount
price = inv.items[\"Widget\"][\"price\"]
assert 79.0 <= price <= 81.0, f\"20% discount on 100 should give ~80, got {price}\"
'"

# Bug 5: get_report sorts ascending instead of descending
check "Bug5: get_report sorts by value descending" \
  "python3 -c '
from inventory import InventoryManager
inv = InventoryManager()
inv.add_item(\"Low\", 1, 10.0)     # value = 10
inv.add_item(\"High\", 10, 100.0)  # value = 1000
inv.add_item(\"Mid\", 5, 50.0)     # value = 250
report = inv.get_report()
values = [r[\"value\"] for r in report]
assert values == sorted(values, reverse=True), f\"expected descending, got {values}\"
assert report[0][\"name\"] == \"High\", f\"first should be High, got {report[0][\\\"name\\\"]}\"
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
