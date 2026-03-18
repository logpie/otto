"""Validator: add-search — search against title and URL."""

import sys
sys.path.insert(0, ".")

from bookmarks import add_bookmark


def test_search_function_exists(tmp_path):
    from bookmarks import search_bookmarks
    assert callable(search_bookmarks)


def test_search_by_title(tmp_path):
    from bookmarks import search_bookmarks
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://example.com", "Python Tutorial", filepath=fp)
    add_bookmark("https://rust.com", "Rust Guide", filepath=fp)

    results = search_bookmarks("python", filepath=fp)
    assert len(results) == 1
    assert results[0]["title"] == "Python Tutorial"


def test_search_by_url(tmp_path):
    from bookmarks import search_bookmarks
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://python.org", "Home", filepath=fp)

    results = search_bookmarks("python.org", filepath=fp)
    assert len(results) == 1


def test_search_case_insensitive(tmp_path):
    from bookmarks import search_bookmarks
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://example.com", "Python Tutorial", filepath=fp)

    assert len(search_bookmarks("PYTHON", filepath=fp)) == 1
    assert len(search_bookmarks("python", filepath=fp)) == 1


def test_search_no_results(tmp_path):
    from bookmarks import search_bookmarks
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://example.com", "Example", filepath=fp)

    assert search_bookmarks("nonexistent", filepath=fp) == []


def test_search_empty_query(tmp_path):
    """Empty query should raise or return error indicator."""
    from bookmarks import search_bookmarks
    fp = str(tmp_path / "bm.json")
    import pytest
    with pytest.raises((ValueError, TypeError)):
        search_bookmarks("", filepath=fp)
