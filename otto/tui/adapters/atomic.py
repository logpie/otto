"""Compatibility alias for the shared atomic Mission Control adapter."""

from __future__ import annotations

import sys as _sys

from otto.mission_control.adapters.atomic import *  # noqa: F401,F403
import otto.mission_control.adapters.atomic as _atomic

_sys.modules[__name__] = _atomic
