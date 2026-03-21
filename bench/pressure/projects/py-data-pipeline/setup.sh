#!/usr/bin/env bash
set -euo pipefail
# Create sample data files for the pipeline
mkdir -p data
cat > data/sales.csv << 'EOF'
date,product,quantity,unit_price,region
2024-01-15,Widget A,10,29.99,North
2024-01-15,Widget B,5,49.99,South
2024-01-16,Widget A,0,29.99,East
2024-01-16,Widget C,3,,West
2024-01-17,Widget B,7,49.99,North
2024-01-17,,2,19.99,South
2024-01-18,Widget A,-1,29.99,East
2024-01-18,Widget C,15,35.50,
invalid-date,Widget A,10,29.99,North
2024-01-20,Widget B,8,49.99,South
EOF
cat > data/products.json << 'EOF'
[
  {"id": "Widget A", "category": "Hardware", "weight_kg": 0.5},
  {"id": "Widget B", "category": "Software", "weight_kg": null},
  {"id": "Widget C", "category": "Hardware", "weight_kg": 1.2}
]
EOF
echo "# Data Pipeline" > README.md
git add -A && git commit -m "init with sample data"
