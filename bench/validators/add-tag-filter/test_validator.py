"""Validator: add-tag-filter — case-insensitive tag filtering."""

import sys
sys.path.insert(0, ".")

from bookmarks import add_bookmark, load_bookmarks


def test_filter_by_tag_exists(tmp_path):
    """filter_by_tag function must exist and be importable."""
    from bookmarks import filter_by_tag
    assert callable(filter_by_tag)


def test_filter_by_tag_basic(tmp_path):
    from bookmarks import filter_by_tag
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://a.com", "A", tags=["python", "web"], filepath=fp)
    add_bookmark("https://b.com", "B", tags=["rust"], filepath=fp)
    add_bookmark("https://c.com", "C", tags=["python"], filepath=fp)

    result = filter_by_tag("python", filepath=fp)
    urls = [b["url"] for b in result]
    assert "https://a.com" in urls
    assert "https://c.com" in urls
    assert "https://b.com" not in urls


def test_filter_by_tag_case_insensitive(tmp_path):
    from bookmarks import filter_by_tag
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://a.com", "A", tags=["Python"], filepath=fp)

    result = filter_by_tag("python", filepath=fp)
    assert len(result) == 1

    result = filter_by_tag("PYTHON", filepath=fp)
    assert len(result) == 1


def test_filter_by_tag_no_match(tmp_path):
    from bookmarks import filter_by_tag
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://a.com", "A", tags=["python"], filepath=fp)

    result = filter_by_tag("rust", filepath=fp)
    assert result == []
