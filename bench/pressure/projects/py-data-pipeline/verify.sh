#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

cat > verify_check.py <<'PY'
import csv
import importlib
import inspect
import json
import pathlib
import subprocess
import sys

failures = 0
ROOT = pathlib.Path(".")


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def import_optional(*names):
    for name in names:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


loader_mod = import_optional("loader", "loaders", "data_loader", "pipeline")
validator_mod = import_optional("validator", "validation", "pipeline")
transformer_mod = import_optional("transformer", "transforms", "pipeline")
aggregator_mod = import_optional("aggregator", "aggregation", "analytics", "pipeline")


def find_callable(module, needles):
    if module is None:
        raise AssertionError("module not found")
    for name, value in inspect.getmembers(module, callable):
        lowered = name.lower()
        if all(needle in lowered for needle in needles):
            return value
    raise AssertionError(f"missing callable for {needles}")


def load_data_with_project_code():
    sales_fn = find_callable(loader_mod, ("csv",))
    products_fn = find_callable(loader_mod, ("json",))
    sales = sales_fn(str(ROOT / "data" / "sales.csv"))
    products = products_fn(str(ROOT / "data" / "products.json"))
    assert sales and products
    return sales, products


def parse_sales():
    with open(ROOT / "data" / "sales.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_products():
    with open(ROOT / "data" / "products.json", encoding="utf-8") as handle:
        return json.load(handle)


def validate_rows(rows):
    fn = None
    for module in (validator_mod,):
        if module is None:
            continue
        for name, value in inspect.getmembers(module, callable):
            lowered = name.lower()
            if "validate" in lowered:
                fn = value
                break
        if fn:
            break
    if fn is None:
        raise AssertionError("missing validation function")
    result = fn(rows)
    return result


def transform_rows(rows, products):
    fn = None
    for name, value in inspect.getmembers(transformer_mod, callable):
        lowered = name.lower()
        if "transform" in lowered or "join" in lowered:
            fn = value
            break
    if fn is None:
        raise AssertionError("missing transform function")
    try:
        return fn(rows, products)
    except TypeError:
        return fn(rows, products, "USD")


def aggregate_rows(rows):
    fn = None
    for name, value in inspect.getmembers(aggregator_mod, callable):
        lowered = name.lower()
        if "aggregate" in lowered or "summary" in lowered:
            fn = value
            break
    if fn is None:
        raise AssertionError("missing aggregation function")
    return fn(rows)


def check_loader():
    sales, products = load_data_with_project_code()
    assert len(sales) >= 5
    assert len(products) == 3


def check_validator_reports_bad_rows():
    rows = parse_sales()
    validation = validate_rows(rows)
    text = json.dumps(validation, default=str).lower()
    assert "row" in text
    assert "missing" in text or "invalid" in text
    assert any(token in text for token in ("6", "7", "8", "9"))


def check_transformer_joins_and_revenue():
    rows = [{"date": "2024-01-15", "product": "Widget A", "quantity": "2", "unit_price": "29.99", "region": "North"}]
    products = parse_products()
    transformed = transform_rows(rows, products)
    assert transformed
    row = transformed[0]
    text = json.dumps(row, default=str)
    assert "Hardware" in text or row.get("category") == "Hardware"
    value = row.get("total_revenue") or row.get("revenue")
    assert round(float(value), 2) == 59.98


def check_aggregator_region_total():
    sample = [
        {"region": "North", "quantity": 2, "total_revenue": 30.0, "product": "A", "date": "2024-01-01"},
        {"region": "North", "quantity": 1, "total_revenue": 15.0, "product": "B", "date": "2024-01-01"},
    ]
    summary = aggregate_rows(sample)
    text = json.dumps(summary, default=str)
    assert "north" in text.lower()
    assert "45" in text or "45.0" in text


def check_aggregator_top_product_and_average():
    sample = [
        {"region": "North", "quantity": 2, "total_revenue": 30.0, "product": "A", "date": "2024-01-01"},
        {"region": "South", "quantity": 5, "total_revenue": 50.0, "product": "B", "date": "2024-01-02"},
    ]
    summary = aggregate_rows(sample)
    text = json.dumps(summary, default=str)
    assert "b" in text.lower()
    assert any(token in text for token in ("40", "40.0", "40.00"))


def check_entrypoint_outputs_json():
    commands = [
        [sys.executable, "-m", "pipeline"],
        [sys.executable, "-m", "main"],
        [sys.executable, "pipeline.py"],
        [sys.executable, "main.py"],
    ]
    for cmd in commands:
        target = pathlib.Path(cmd[-1].split(".")[0] + (".py" if cmd[-1].endswith(".py") else ""))
        if cmd[1:2] != ["-m"] and not target.exists():
            continue
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout.strip():
            payload = json.loads(result.stdout)
            serialized = json.dumps(payload).lower()
            assert "revenue" in serialized
            return
    raise AssertionError("no JSON-reporting entry point succeeded")


report("loader reads both sales CSV and products JSON", check_loader)
report("validator reports malformed rows with row references", check_validator_reports_bad_rows)
report("transformer joins products and computes revenue", check_transformer_joins_and_revenue)
report("aggregator computes regional totals", check_aggregator_region_total)
report("aggregator computes top-product and average-order stats", check_aggregator_top_product_and_average)
report("project entry point emits formatted JSON", check_entrypoint_outputs_json)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
