"""Compatibility alias for the shared Mission Control model."""

from __future__ import annotations

import sys as _sys

from otto.mission_control.model import *  # noqa: F401,F403
import otto.mission_control.model as _model

_sys.modules[__name__] = _model
