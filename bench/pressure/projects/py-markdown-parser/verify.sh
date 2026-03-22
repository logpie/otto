#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: py-markdown-parser"

# Find the main module
MOD=""
for m in markdown_parser parser markdown md_parser converter; do
  if python3 -c "import $m" 2>/dev/null; then MOD=$m; break; fi
done
if [ -z "$MOD" ]; then echo "  FAIL  Could not find markdown parser module"; exit 1; fi

check "h1 heading converts to <h1>" \
  "python3 -c '
from $MOD import *
# Find the render/convert function
import inspect
mod = __import__(\"$MOD\")
fn = None
for name in [\"render\", \"convert\", \"to_html\", \"parse\", \"markdown_to_html\", \"render_markdown\"]:
    fn = getattr(mod, name, None)
    if fn and callable(fn): break
assert fn is not None, \"no render function found\"
result = fn(\"# Hello\")
assert \"<h1>\" in result and \"Hello\" in result, f\"got: {result}\"
'"

check "bold text converts to <strong>" \
  "python3 -c '
from $MOD import *
import inspect
mod = __import__(\"$MOD\")
fn = None
for name in [\"render\", \"convert\", \"to_html\", \"parse\", \"markdown_to_html\", \"render_markdown\"]:
    fn = getattr(mod, name, None)
    if fn and callable(fn): break
result = fn(\"This is **bold** text\")
assert \"<strong>\" in result and \"bold\" in result, f\"got: {result}\"
'"

check "code block with language converts to <pre><code>" \
  "python3 -c '
from $MOD import *
mod = __import__(\"$MOD\")
fn = None
for name in [\"render\", \"convert\", \"to_html\", \"parse\", \"markdown_to_html\", \"render_markdown\"]:
    fn = getattr(mod, name, None)
    if fn and callable(fn): break
md = \"\"\"\`\`\`python
print(\"hello\")
\`\`\`\"\"\"
result = fn(md)
assert \"<pre>\" in result or \"<code>\" in result, f\"got: {result}\"
assert \"print\" in result, f\"code content missing: {result}\"
'"

check "links convert to <a href>" \
  "python3 -c '
from $MOD import *
mod = __import__(\"$MOD\")
fn = None
for name in [\"render\", \"convert\", \"to_html\", \"parse\", \"markdown_to_html\", \"render_markdown\"]:
    fn = getattr(mod, name, None)
    if fn and callable(fn): break
result = fn(\"Visit [Google](https://google.com)\")
assert \"<a\" in result and \"href\" in result and \"google.com\" in result, f\"got: {result}\"
'"

check "h3 heading converts to <h3>" \
  "python3 -c '
from $MOD import *
mod = __import__(\"$MOD\")
fn = None
for name in [\"render\", \"convert\", \"to_html\", \"parse\", \"markdown_to_html\", \"render_markdown\"]:
    fn = getattr(mod, name, None)
    if fn and callable(fn): break
result = fn(\"### Third level\")
assert \"<h3>\" in result and \"Third level\" in result, f\"got: {result}\"
'"

echo ""
echo "$PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
