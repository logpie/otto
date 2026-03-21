#!/usr/bin/env bash
set -euo pipefail
cat > calc.py << 'EOF'
def divide(a, b):
    return a / b  # Bug: no zero division check

def average(numbers):
    return sum(numbers) / len(numbers)  # Bug: crashes on empty list
EOF
cat > test_calc.py << 'EOF'
from calc import divide, average
def test_divide():
    assert divide(10, 2) == 5.0
def test_average():
    assert average([1, 2, 3]) == 2.0
EOF
git add -A && git commit -m "init with buggy calculator"
