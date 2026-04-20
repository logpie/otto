"""Tests for otto/queue/ids.py — Phase 2.2 task ID dedup + cycle detection."""

from __future__ import annotations

import pytest

from otto.queue.ids import (
    detect_cycles,
    generate_task_id,
    validate_after_refs,
)


# ---------- generate_task_id ----------


def test_generate_uses_intent_slug():
    out = generate_task_id(intent="add csv export", command="build", existing_ids=[])
    assert out == "add-csv-export"


def test_generate_dedups_against_existing():
    out = generate_task_id(
        intent="add csv export",
        command="build",
        existing_ids=["add-csv-export"],
    )
    assert out == "add-csv-export-2"


def test_generate_dedups_with_higher_counter_when_taken():
    out = generate_task_id(
        intent="add csv export",
        command="build",
        existing_ids=["add-csv-export", "add-csv-export-2", "add-csv-export-3"],
    )
    assert out == "add-csv-export-4"


def test_generate_falls_back_to_command_seq_when_no_intent():
    out = generate_task_id(intent=None, command="improve", existing_ids=[])
    assert out == "improve-1"


def test_generate_command_seq_increments_against_existing():
    out = generate_task_id(
        intent=None,
        command="improve",
        existing_ids=["improve-1", "improve-2"],
    )
    assert out == "improve-3"


def test_generate_command_seq_with_blank_intent():
    out = generate_task_id(intent="   ", command="certify", existing_ids=[])
    assert out == "certify-1"


def test_generate_explicit_as_used_directly():
    out = generate_task_id(
        intent="some intent",
        command="build",
        existing_ids=[],
        explicit_as="my-custom-id",
    )
    assert out == "my-custom-id"


def test_generate_explicit_as_lowercased():
    out = generate_task_id(
        intent=None, command="build", existing_ids=[], explicit_as="UPPER",
    )
    assert out == "upper"


def test_generate_explicit_as_rejects_collision():
    with pytest.raises(ValueError, match="already exists"):
        generate_task_id(
            intent=None, command="build",
            existing_ids=["taken"],
            explicit_as="taken",
        )


def test_generate_explicit_as_rejects_reserved_word():
    with pytest.raises(ValueError, match="reserved"):
        generate_task_id(
            intent=None, command="build", existing_ids=[], explicit_as="ls",
        )


@pytest.mark.parametrize("bad", ["", "   ", "has space", "foo/bar", "Foo!", "-leading", "trailing-"])
def test_generate_explicit_as_rejects_bad_chars(bad: str):
    with pytest.raises(ValueError):
        generate_task_id(
            intent=None, command="build", existing_ids=[], explicit_as=bad,
        )


# ---------- validate_after_refs ----------


def test_validate_after_passes_for_existing_refs():
    validate_after_refs(after=["t1", "t2"], self_id="t3", all_ids=["t1", "t2", "t3"])


def test_validate_after_rejects_self_reference():
    with pytest.raises(ValueError, match="cannot depend on itself"):
        validate_after_refs(after=["t1"], self_id="t1", all_ids=["t1"])


def test_validate_after_rejects_unknown_refs():
    with pytest.raises(ValueError, match="unknown task"):
        validate_after_refs(after=["unknown"], self_id="t1", all_ids=["t1"])


def test_validate_after_passes_for_empty_after():
    validate_after_refs(after=[], self_id="t1", all_ids=["t1"])


# ---------- detect_cycles ----------


def test_detect_cycles_empty_graph():
    assert detect_cycles(edges={}) == []


def test_detect_cycles_acyclic():
    edges = {"a": ["b"], "b": ["c"], "c": []}
    assert detect_cycles(edges=edges) == []


def test_detect_cycles_simple_two_node():
    edges = {"a": ["b"], "b": ["a"]}
    cycles = detect_cycles(edges=edges)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b"}


def test_detect_cycles_three_node_cycle():
    edges = {"a": ["b"], "b": ["c"], "c": ["a"]}
    cycles = detect_cycles(edges=edges)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b", "c"}


def test_detect_cycles_disconnected_subgraphs():
    edges = {"a": ["b"], "b": [], "c": ["d"], "d": ["c"]}
    cycles = detect_cycles(edges=edges)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"c", "d"}


def test_detect_cycles_ignores_external_refs():
    """Refs to ids not in `edges` are not cycles by themselves."""
    edges = {"a": ["external-id"], "b": ["a"]}
    assert detect_cycles(edges=edges) == []
