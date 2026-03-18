"""Validator: add-export-import — export to JSON/CSV, import with dedup."""

import csv
import json
import sys
sys.path.insert(0, ".")

from bookmarks import add_bookmark, load_bookmarks


def test_export_json(tmp_path):
    from bookmarks import export_bookmarks
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://a.com", "A", tags=["python"], filepath=fp)
    add_bookmark("https://b.com", "B", filepath=fp)

    out = str(tmp_path / "export.json")
    export_bookmarks(fp, format="json")
    # The export function should create a file or return data
    # Check that exported data is valid
    exported = json.loads((tmp_path / "export.json").read_text())
    assert len(exported) == 2


def test_export_csv(tmp_path):
    from bookmarks import export_bookmarks
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://a.com", "A", tags=["python", "web"], filepath=fp)

    out = str(tmp_path / "export.csv")
    export_bookmarks(fp, format="csv")
    content = (tmp_path / "export.csv").read_text()
    assert "https://a.com" in content
    assert "python" in content


def test_import_json(tmp_path):
    from bookmarks import import_bookmarks
    fp = str(tmp_path / "bm.json")

    # Create import file
    import_data = [
        {"url": "https://x.com", "title": "X", "tags": []},
        {"url": "https://y.com", "title": "Y", "tags": ["test"]},
    ]
    import_file = tmp_path / "import.json"
    import_file.write_text(json.dumps(import_data))

    import_bookmarks(str(import_file), filepath=fp)
    bookmarks = load_bookmarks(fp)
    assert len(bookmarks) == 2


def test_import_dedup(tmp_path):
    from bookmarks import import_bookmarks
    fp = str(tmp_path / "bm.json")
    add_bookmark("https://x.com", "X", filepath=fp)

    import_data = [
        {"url": "https://x.com", "title": "X duplicate", "tags": []},
        {"url": "https://y.com", "title": "Y", "tags": []},
    ]
    import_file = tmp_path / "import.json"
    import_file.write_text(json.dumps(import_data))

    import_bookmarks(str(import_file), filepath=fp)
    bookmarks = load_bookmarks(fp)
    assert len(bookmarks) == 2  # x.com not duplicated
