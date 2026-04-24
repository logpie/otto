"""Compatibility alias for the shared queue Mission Control adapter."""

from __future__ import annotations

import sys as _sys

from otto.mission_control.adapters.queue import *  # noqa: F401,F403
import otto.mission_control.adapters.queue as _queue

_sys.modules[__name__] = _queue
