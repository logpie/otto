"""Compatibility wrapper for the shared Mission Control merge adapter."""

from __future__ import annotations

import sys as _sys

from otto.mission_control.adapters.merge import *  # noqa: F401,F403
import otto.mission_control.adapters.merge as _merge

_sys.modules[__name__] = _merge
