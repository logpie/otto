"""Compatibility wrapper for shared Mission Control actions."""

from __future__ import annotations

import sys as _sys

from otto.mission_control.actions import *  # noqa: F401,F403
import otto.mission_control.actions as _actions

_sys.modules[__name__] = _actions
