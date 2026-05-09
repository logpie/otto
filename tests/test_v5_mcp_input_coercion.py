"""Regression: MCP tools coerce LLM-supplied id lists to clean list[str].

The Claude SDK schema declares list[str], but the LLM frequently passes
malformed shapes:
  - "task_id" (single id as bare string, was iterated as chars)
  - "[]" (empty list as JSON literal string)
  - "a,b,c" (comma-joined string)
  - '["a","b"]' (JSON-encoded list as string)
All of these must coerce correctly.
"""

from otto.mcp_tools import _coerce_id_list


def test_coerce_handles_native_list() -> None:
    assert _coerce_id_list(["v5-foo", "v5-bar"]) == ["v5-foo", "v5-bar"]


def test_coerce_handles_empty_inputs() -> None:
    assert _coerce_id_list(None) == []
    assert _coerce_id_list([]) == []
    assert _coerce_id_list("") == []
    assert _coerce_id_list("   ") == []


def test_coerce_strips_json_literal_strings() -> None:
    """The URL-shortener bug: depends_on came as the literal string '[]'."""
    assert _coerce_id_list("[]") == []
    assert _coerce_id_list("{}") == []
    assert _coerce_id_list("null") == []
    assert _coerce_id_list("None") == []


def test_coerce_splits_comma_string() -> None:
    """The finance-dashboard bug: 'a,b,c' was iterated as chars."""
    assert _coerce_id_list("add_transaction,edit_delete_transaction") == [
        "add_transaction",
        "edit_delete_transaction",
    ]


def test_coerce_handles_single_string() -> None:
    assert _coerce_id_list("v5-foo") == ["v5-foo"]


def test_coerce_handles_json_encoded_list() -> None:
    assert _coerce_id_list('["v5-foo","v5-bar"]') == ["v5-foo", "v5-bar"]


def test_coerce_drops_embedded_json_literals_in_list() -> None:
    assert _coerce_id_list(["[]", "v5-foo", "{}", " ", "v5-bar"]) == [
        "v5-foo",
        "v5-bar",
    ]
