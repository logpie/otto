#!/usr/bin/env bash
set -euo pipefail

cat > inventory.py << 'PYEOF'
"""Inventory management system."""
import threading
from datetime import datetime

class InventoryManager:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()
        self.transaction_log = []

    def add_item(self, name, quantity, price):
        """Add or restock an item."""
        with self.lock:
            if name in self.items:
                self.items[name]['quantity'] += quantity
            else:
                self.items[name] = {
                    'quantity': quantity,
                    'price': price,
                    'created_at': datetime.now()
                }
            self._log('add', name, quantity)

    def sell_item(self, name, quantity):
        """Sell items. Returns total price."""
        if name not in self.items:
            raise KeyError(f"Item '{name}' not found")
        item = self.items[name]
        if item['quantity'] > 0:
            item['quantity'] -= quantity
            total = quantity * item['price']
            self._log('sell', name, quantity)
            return total
        raise ValueError("Out of stock")

    def get_total_value(self):
        """Calculate total inventory value."""
        total = 0
        for item in self.items.values():
            total += item['quantity'] * item['price']
        return total

    def find_items(self, min_price=None, max_price=None):
        """Find items within price range."""
        results = []
        for name, item in self.items.items():
            if (min_price is None or item['price'] >= min_price) or \
               (max_price is None or item['price'] <= max_price):
                results.append((name, item))
        return results

    def apply_discount(self, name, percent):
        """Apply percentage discount to item price."""
        if name not in self.items:
            raise KeyError(f"Item '{name}' not found")
        self.items[name]['price'] *= (1 + percent / 100)

    def _log(self, action, name, quantity):
        self.transaction_log.append({
            'action': action,
            'item': name,
            'quantity': quantity,
            'timestamp': datetime.now()
        })

    def get_report(self):
        """Generate inventory report sorted by value (qty * price) descending."""
        report = []
        for name, item in self.items.items():
            report.append({
                'name': name,
                'quantity': item['quantity'],
                'price': item['price'],
                'value': item['quantity'] * item['price']
            })
        report.sort(key=lambda x: x['value'])
        return report
PYEOF

cat > test_inventory.py << 'PYEOF'
import pytest
from inventory import InventoryManager

def test_add_item():
    inv = InventoryManager()
    inv.add_item("Widget", 10, 9.99)
    assert inv.items["Widget"]["quantity"] == 10

def test_sell_item():
    inv = InventoryManager()
    inv.add_item("Widget", 10, 9.99)
    total = inv.sell_item("Widget", 3)
    assert total == pytest.approx(29.97)

def test_get_total_value():
    inv = InventoryManager()
    inv.add_item("A", 10, 5.0)
    inv.add_item("B", 5, 10.0)
    assert inv.get_total_value() == pytest.approx(100.0)
PYEOF

git add -A && git commit -m "init inventory system"
