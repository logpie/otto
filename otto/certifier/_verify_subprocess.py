"""Subprocess entry point for product verification loop.

Runs certify → fix → re-certify in its own process with its own main thread.
The Claude SDK requires signal handlers which only work in the main thread —
this subprocess provides that.

Usage: echo '{"intent":..., "project_dir":..., ...}' | python -m otto.certifier._verify_subprocess
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def main() -> None:
    payload = json.loads(sys.stdin.read())

    from otto.verification import run_product_verification

    result = asyncio.run(run_product_verification(
        product_spec_path=Path(payload["product_spec_path"]) if payload.get("product_spec_path") else None,
        project_dir=Path(payload["project_dir"]),
        tasks_path=Path(payload["tasks_path"]) if payload.get("tasks_path") else None,
        config=payload.get("config", {}),
        intent=payload["intent"],
    ))

    # Write result as JSON to stdout (last line)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
