"""Mission Control domain adapters."""

from __future__ import annotations

from otto.mission_control.adapters.atomic import AtomicMissionControlAdapter
from otto.mission_control.adapters.merge import MergeMissionControlAdapter
from otto.mission_control.adapters.queue import QueueMissionControlAdapter
from otto.mission_control.model import MissionControlAdapter

_ATOMIC_ADAPTER = AtomicMissionControlAdapter()
_QUEUE_ADAPTER = QueueMissionControlAdapter()
_MERGE_ADAPTER = MergeMissionControlAdapter()

_ADAPTERS: dict[str, MissionControlAdapter] = {
    "atomic.build": _ATOMIC_ADAPTER,
    "atomic.improve": _ATOMIC_ADAPTER,
    "atomic.certify": _ATOMIC_ADAPTER,
    "queue.attempt": _QUEUE_ADAPTER,
    "merge.run": _MERGE_ADAPTER,
}


def adapter_for_key(adapter_key: str) -> MissionControlAdapter:
    return _ADAPTERS.get(adapter_key, _ADAPTERS["atomic.build"])


def all_adapters() -> tuple[MissionControlAdapter, ...]:
    return (_ATOMIC_ADAPTER, _QUEUE_ADAPTER, _MERGE_ADAPTER)


__all__ = ["adapter_for_key", "all_adapters"]
