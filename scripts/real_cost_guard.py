"""Shared opt-in guard for scripts that can spend real LLM budget."""

from __future__ import annotations

import os
import sys


REAL_COST_ENV = "OTTO_ALLOW_REAL_COST"


def real_cost_allowed() -> bool:
    return os.environ.get(REAL_COST_ENV) == "1"


def require_real_cost_opt_in(activity: str = "real Otto work") -> None:
    if real_cost_allowed():
        return
    print(
        f"Refusing to run {activity} without {REAL_COST_ENV}=1.",
        file=sys.stderr,
    )
    print(f"Set {REAL_COST_ENV}=1 to continue.", file=sys.stderr)
    raise SystemExit(2)
