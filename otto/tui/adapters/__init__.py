"""Mission Control domain adapters."""

from __future__ import annotations

from otto.tui.adapters.atomic import AtomicMissionControlAdapter
from otto.tui.adapters.merge import MergeMissionControlAdapter
from otto.tui.adapters.queue import QueueMissionControlAdapter
from otto.tui.mission_control_model import MissionControlAdapter

_ADAPTERS: dict[str, MissionControlAdapter] = {
    "atomic.build": AtomicMissionControlAdapter(),
    "atomic.improve": AtomicMissionControlAdapter(),
    "atomic.certify": AtomicMissionControlAdapter(),
    "queue.attempt": QueueMissionControlAdapter(),
    "merge.run": MergeMissionControlAdapter(),
}


def adapter_for_key(adapter_key: str) -> MissionControlAdapter:
    return _ADAPTERS.get(adapter_key, _ADAPTERS["atomic.build"])


__all__ = ["adapter_for_key"]
