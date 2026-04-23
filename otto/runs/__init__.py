"""Canonical run registry primitives."""

from otto.runs.registry import (
    RunPublisher,
    append_command_ack,
    begin_command_drain,
    allocate_run_id,
    finish_command_drain,
    finalize_record,
    garbage_collect_live_records,
    gc_terminal_records,
    load_command_ack_ids,
    read_live_records,
    update_record,
    write_record,
)
from otto.runs.schema import RunRecord

__all__ = [
    "RunRecord",
    "RunPublisher",
    "append_command_ack",
    "begin_command_drain",
    "allocate_run_id",
    "finish_command_drain",
    "write_record",
    "update_record",
    "finalize_record",
    "read_live_records",
    "garbage_collect_live_records",
    "gc_terminal_records",
    "load_command_ack_ids",
]
