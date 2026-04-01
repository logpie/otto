#!/usr/bin/env bash
set -uo pipefail

trap 'rc=$?; rm -f verify_check.py; exit $rc' EXIT

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    if command -v pytest >/dev/null 2>&1; then
        PYTEST_BIN="$(command -v pytest)"
        SHEBANG="$(head -n 1 "$PYTEST_BIN" 2>/dev/null || true)"
        if [[ "$SHEBANG" == '#!'* ]]; then
            CANDIDATE="${SHEBANG#\#!}"
            if [[ -x "$CANDIDATE" ]]; then
                PYTHON_BIN="$CANDIDATE"
            fi
        fi
    fi
fi

if [[ -z "$PYTHON_BIN" ]]; then
    PYTHON_BIN="python3"
fi

cat > verify_check.py <<'PY'
import importlib
import inspect
import json
import os
import pathlib
import subprocess
import sys

failures = 0
ROOT = pathlib.Path(".")

models = importlib.import_module("models")
store_mod = importlib.import_module("store")
blog_mod = importlib.import_module("blog")
PostStore = getattr(store_mod, "PostStore")
BlogService = getattr(blog_mod, "BlogService")


def report(name, fn):
    global failures
    try:
        fn()
        print(f"PASS {name}")
    except Exception as exc:
        failures += 1
        print(f"FAIL {name}: {exc}")


def make_service():
    sig = inspect.signature(BlogService)
    if len(sig.parameters) == 0:
        return BlogService()
    return BlogService(PostStore())


def get_store(service):
    if hasattr(service, "store"):
        return service.store
    if hasattr(service, "post_store"):
        return service.post_store
    if hasattr(service, "_post_store"):
        return service._post_store
    raise AttributeError("BlogService has neither .store nor .post_store nor ._post_store")


def value(obj, key):
    if hasattr(obj, key):
        return getattr(obj, key)
    return obj[key]


def check_unique_slug_generation():
    store = PostStore()
    first = store.create(title="Hello, World!", content="a", author="alice", tags=["x"])
    second = store.create(title="Hello, World!", content="b", author="alice", tags=["x"])
    assert value(first, "slug") == "hello-world"
    assert value(second, "slug") != value(first, "slug")
    assert "!" not in value(first, "slug")


def check_store_filters_and_pagination():
    store = PostStore()
    for idx in range(5):
        store.create(
            title=f"Post {idx}",
            content="content",
            author="alice" if idx % 2 == 0 else "bob",
            tags=["python"] if idx < 3 else ["js"],
            status="published" if idx % 2 == 0 else "draft",
        )
    published = store.list(status="published", page=1, limit=10)
    python_posts = store.list(tag="python", page=1, limit=10)
    page_two = store.list(page=2, limit=2)
    assert all(value(post, "status") == "published" for post in published)
    assert all("python" in value(post, "tags") for post in python_posts)
    assert len(page_two) == 2


def check_publish_and_comment_rules():
    service = make_service()
    store = get_store(service)
    post = store.create(title="Draft", content="...", author="ann", tags=["draft"])
    pid = value(post, "id")
    try:
        service.add_comment(pid, "reader", "hello")
    except Exception:
        pass
    else:
        raise AssertionError("commenting on a draft should fail")
    service.publish(pid)
    published = store.get_by_id(pid)
    assert value(published, "status") == "published"
    assert value(published, "updated_at")
    service.add_comment(pid, "reader", "hello")
    full = service.get_post_with_comments(value(published, "slug"))
    text = json.dumps(full, default=str).lower()
    assert "hello" in text


def check_search_and_feed():
    service = make_service()
    store = get_store(service)
    older = store.create(title="Python Intro", content="learn python", author="a", tags=["python"], status="published")
    newer = store.create(title="Databases", content="python and sqlite", author="a", tags=["db"], status="published")
    service.publish(value(older, "id"))
    service.publish(value(newer, "id"))
    results = service.search("sqlite")
    feed = service.get_feed(page=1, limit=10)
    assert any("sqlite" in json.dumps(item, default=str).lower() for item in results)
    assert value(feed[0], "created_at") >= value(feed[-1], "created_at")


def check_tag_cloud():
    service = make_service()
    store = get_store(service)
    one = store.create(title="One", content="...", author="a", tags=["python", "web"], status="published")
    two = store.create(title="Two", content="...", author="a", tags=["python"], status="published")
    draft = store.create(title="Draft", content="...", author="a", tags=["hidden"], status="draft")
    for post in (one, two, draft):
        if value(post, "status") == "published":
            service.publish(value(post, "id"))
    cloud = service.get_tag_cloud()
    assert cloud["python"] == 2
    assert "hidden" not in cloud


def check_cli_persistence():
    candidates = [
        [sys.executable, "cli.py"],
        [sys.executable, "blog.py"],
        [sys.executable, "main.py"],
        [sys.executable, "-m", "cli"],
        [sys.executable, "-m", "blog"],
        [sys.executable, "-m", "main"],
    ]
    for cmd in candidates:
        target = ROOT / cmd[-1]
        if cmd[1:2] != ["-m"] and not target.exists():
            continue
        help_result = subprocess.run(cmd + ["--help"], capture_output=True, text=True)
        if help_result.returncode != 0:
            continue
        before = {path: path.stat().st_mtime for path in ROOT.glob("*.json")}
        subprocess.run(cmd + ["new", "CLI Post", "--author", "cli", "--tags", "cli,python"], check=True, capture_output=True, text=True)
        subprocess.run(cmd + ["publish", "cli-post"], check=True, capture_output=True, text=True)
        subprocess.run(cmd + ["comment", "cli-post", "--author", "reader", "--content", "looks good"], check=True, capture_output=True, text=True)
        show = subprocess.run(cmd + ["show", "cli-post"], check=True, capture_output=True, text=True)
        tags = subprocess.run(cmd + ["tags"], check=True, capture_output=True, text=True)
        search = subprocess.run(cmd + ["search", "CLI"], check=True, capture_output=True, text=True)
        changed = [path for path in ROOT.glob("*.json") if path not in before or path.stat().st_mtime != before[path]]
        assert changed, "CLI did not update a JSON persistence file"
        assert "looks good" in show.stdout.lower()
        assert "cli" in tags.stdout.lower()
        search_lower = search.stdout.lower()
        # Search output format is allowed to vary as long as it clearly renders
        # the matching post. Accept either the title or the slug in the result.
        assert ("cli post" in search_lower) or ("cli-post" in search_lower)
        return
    raise AssertionError("no CLI entry point succeeded")


report("PostStore generates normalized unique slugs", check_unique_slug_generation)
report("PostStore filters and paginates correctly", check_store_filters_and_pagination)
report("BlogService enforces publish-before-comment and returns comments", check_publish_and_comment_rules)
report("search and feed operate on published content", check_search_and_feed)
report("tag cloud counts only published posts", check_tag_cloud)
report("CLI commands persist and read shared blog state", check_cli_persistence)

raise SystemExit(1 if failures else 0)
PY

"$PYTHON_BIN" verify_check.py
