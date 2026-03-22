#!/usr/bin/env bash
set -euo pipefail

trap 'rm -f verify_check.py' EXIT

cat > verify_check.py <<'PY'
import importlib
import inspect

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
    for name in ("markdown_parser", "parser", "markdown", "md_parser", "converter"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise AssertionError("no markdown parser module found")


module = load_module()


def render(markdown_text):
    for name in ("markdown_to_html", "render_markdown", "to_html", "render", "convert"):
        fn = getattr(module, name, None)
        if callable(fn):
            return fn(markdown_text)
    for _, value in inspect.getmembers(module, inspect.isclass):
        if "render" in value.__name__.lower():
            instance = value()
            if hasattr(instance, "render"):
                return instance.render(markdown_text)
    raise AssertionError("no renderer entry point found")


def check_architecture_and_empty_input():
    names = {name.lower() for name, _ in inspect.getmembers(module)}
    assert any("lexer" in name for name in names)
    assert any("parser" in name for name in names)
    html = render("")
    assert html == "" or html.strip() in ("", "<p></p>")


def check_headings():
    html = render("# One\n\n### Three")
    assert "<h1>" in html and "One" in html
    assert "<h3>" in html and "Three" in html


def check_nested_inline_formatting():
    html = render("**bold *and italic***")
    lowered = html.lower()
    assert "<strong>" in lowered
    assert "<em>" in lowered or "<i>" in lowered


def check_code_block():
    html = render("```python\nprint('hi')\n```")
    lowered = html.lower()
    assert "<pre" in lowered and "<code" in lowered
    assert "print" in html


def check_lists():
    html = render("- top\n  - nested\n1. first\n2. second")
    lowered = html.lower()
    assert "<ul" in lowered
    assert "<ol" in lowered
    assert html.count("<li") >= 3


def check_links_images_and_blocks():
    html = render("[site](https://example.com)\n\n![alt](img.png)\n\n> quoted\n\n---")
    lowered = html.lower()
    assert "<a " in lowered and "href=" in lowered
    assert "<img" in lowered and "alt=" in lowered
    assert "<blockquote" in lowered
    assert "<hr" in lowered


report("module exposes lexer/parser architecture and handles empty input", check_architecture_and_empty_input)
report("heading syntax renders to matching heading tags", check_headings)
report("nested inline formatting renders both strong and emphasis", check_nested_inline_formatting)
report("fenced code blocks preserve code content", check_code_block)
report("ordered and unordered lists render as list structures", check_lists)
report("links, images, blockquotes, and rules render correctly", check_links_images_and_blocks)

raise SystemExit(1 if failures else 0)
PY

python3 verify_check.py
