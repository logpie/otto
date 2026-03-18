"""Validator: add-concurrent-bookmarks — thread safety and atomic writes."""

import json
import os
import sys
import threading
sys.path.insert(0, ".")

from bookmarks import add_bookmark, load_bookmarks


def test_bulk_add_exists(tmp_path):
    from bookmarks import bulk_add
    assert callable(bulk_add)


def test_bulk_add(tmp_path):
    from bookmarks import bulk_add
    fp = str(tmp_path / "bm.json")
    items = [
        {"url": "https://a.com", "title": "A", "tags": []},
        {"url": "https://b.com", "title": "B", "tags": ["test"]},
        {"url": "https://c.com", "title": "C", "tags": []},
    ]
    bulk_add(items, filepath=fp)
    bookmarks = load_bookmarks(fp)
    assert len(bookmarks) == 3


def test_concurrent_adds(tmp_path):
    """Multiple threads adding bookmarks shouldn't lose data."""
    fp = str(tmp_path / "bm.json")
    errors = []

    def add_many(start):
        try:
            for i in range(5):
                add_bookmark(f"https://{start + i}.com", f"Site {start + i}", filepath=fp)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=add_many, args=(i * 5,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors during concurrent adds: {errors}"
    bookmarks = load_bookmarks(fp)
    assert len(bookmarks) == 20  # 4 threads × 5 bookmarks each


def test_atomic_write(tmp_path):
    """File should not be corrupted even after many rapid writes."""
    fp = str(tmp_path / "bm.json")
    for i in range(50):
        add_bookmark(f"https://{i}.com", f"Site {i}", filepath=fp)

    # File should be valid JSON
    bookmarks = load_bookmarks(fp)
    assert len(bookmarks) == 50
