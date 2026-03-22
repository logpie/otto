#!/usr/bin/env bash
set -euo pipefail
PASS=0; FAIL=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  OK  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
echo "Verifying: multi-blog-engine (3-layer integration)"

# Find CLI entry point
CLI=""
for f in cli.py main.py app.py blog_cli.py; do
  if [ -f "$f" ]; then CLI="python3 $f"; break; fi
done
if [ -z "$CLI" ]; then
  for m in cli main app blog_cli blog; do
    if python3 -m "$m" --help >/dev/null 2>&1; then CLI="python3 -m $m"; break; fi
  done
fi

# Clean state
rm -f blog.json blog_data.json data.json

check "data layer: Post model and PostStore exist" \
  "python3 -c '
# Try importing the data layer
for mod_name in [\"models\", \"store\", \"data\", \"blog\"]:
    try:
        mod = __import__(mod_name)
        if hasattr(mod, \"Post\") or hasattr(mod, \"PostStore\"):
            break
    except ImportError:
        continue
else:
    raise ImportError(\"no data layer found\")
'"

check "data layer: slug auto-generation from title" \
  "python3 -c '
import importlib
store_mod = None
for mod_name in [\"store\", \"models\", \"blog\", \"data\"]:
    try:
        store_mod = importlib.import_module(mod_name)
        break
    except ImportError:
        continue
PostStore = getattr(store_mod, \"PostStore\", None)
if PostStore is None:
    for mod_name in [\"store\", \"models\", \"blog\", \"data\"]:
        try:
            m = importlib.import_module(mod_name)
            PostStore = getattr(m, \"PostStore\", None)
            if PostStore: break
        except ImportError: continue
if PostStore is None:
    exit(1)
s = PostStore()
# Create a post and check slug
post = s.create(title=\"Hello World!\", content=\"test\", author=\"alice\", tags=[\"test\"])
slug = post.slug if hasattr(post, \"slug\") else post.get(\"slug\", \"\")
assert slug == \"hello-world\" or slug == \"hello-world!\".replace(\"!\",\"\").replace(\" \",\"-\").lower(), f\"slug={slug}\"
assert \"!\" not in slug, f\"special chars in slug: {slug}\"
'"

check "business layer: publish changes post status" \
  "python3 -c '
import importlib
blog_mod = None
for mod_name in [\"blog\", \"service\", \"blog_service\"]:
    try:
        blog_mod = importlib.import_module(mod_name)
        break
    except ImportError: continue
if blog_mod is None: exit(1)
BlogService = getattr(blog_mod, \"BlogService\", None)
if BlogService is None: exit(1)
svc = BlogService()
# Create then publish
import sys
for mod_name in [\"store\", \"models\", \"blog\", \"data\"]:
    try:
        m = importlib.import_module(mod_name)
        PostStore = getattr(m, \"PostStore\", None)
        if PostStore: break
    except ImportError: continue
if hasattr(svc, \"store\"):
    post = svc.store.create(title=\"Test\", content=\"c\", author=\"a\", tags=[])
else:
    # Try creating via service
    post = svc.create_post(title=\"Test\", content=\"c\", author=\"a\", tags=[])
pid = post.id if hasattr(post, \"id\") else post.get(\"id\")
slug = post.slug if hasattr(post, \"slug\") else post.get(\"slug\")
svc.publish(pid) if hasattr(svc, \"publish\") else None
# Check status
fetched = svc.store.get_by_id(pid) if hasattr(svc, \"store\") else svc.get_post(pid)
status = fetched.status if hasattr(fetched, \"status\") else fetched.get(\"status\")
assert status == \"published\", f\"status={status}\"
'"

check "business layer: search finds posts by content" \
  "python3 -c '
import importlib
blog_mod = None
for mod_name in [\"blog\", \"service\", \"blog_service\"]:
    try:
        blog_mod = importlib.import_module(mod_name)
        break
    except ImportError: continue
if blog_mod is None: exit(1)
BlogService = getattr(blog_mod, \"BlogService\", None)
if BlogService is None: exit(1)
svc = BlogService()
for mod_name in [\"store\", \"models\", \"blog\", \"data\"]:
    try:
        m = importlib.import_module(mod_name)
        PostStore = getattr(m, \"PostStore\", None)
        if PostStore: break
    except ImportError: continue
if hasattr(svc, \"store\"):
    svc.store.create(title=\"Python Guide\", content=\"learn python programming\", author=\"a\", tags=[])
else:
    svc.create_post(title=\"Python Guide\", content=\"learn python programming\", author=\"a\", tags=[])
results = svc.search(\"python\")
assert len(results) >= 1, f\"search returned {len(results)} results\"
'"

if [ -n "$CLI" ]; then
  check "CLI: new and list commands work end-to-end" \
    "$CLI new 'Integration Test Post' --author tester --tags test,verify 2>&1 && $CLI list 2>&1 | grep -i 'integration\|test\|post'"
else
  check "CLI: entry point exists" "false"
fi

echo ""
echo "$PASS passed, $FAIL failed"
rm -f blog.json blog_data.json data.json
[ $FAIL -eq 0 ]
