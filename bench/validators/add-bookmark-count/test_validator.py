"""Validator: add-bookmark-count — count command and function."""

import sys
sys.path.insert(0, ".")

from bookmarks import add_bookmark


def test_count_bookmarks_exists(tmp_path):
    from bookmarks import count_bookmarks
    assert callable(count_bookmarks)


def test_count_bookmarks_empty(tmp_path):
    from bookmarks import count_bookmarks
    fp = str(tmp_path / "bm.json")
    assert count_bookmarks(filepath=fp) == 0


def test_count_bookmarks_with_data(tmp_path):
    from bookmarks import count_bookmarks
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://a.com", "A", filepath=fp)
    add_bookmark("https://b.com", "B", filepath=fp)
    assert count_bookmarks(filepath=fp) == 2
