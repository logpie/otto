"""Write ``otto/web/static/build-stamp.json`` after Vite finishes a build.

The file proves a bundle is fresh and lets ``otto web`` fail fast when
sources have drifted. It is invoked from ``otto/web/client/vite.config.ts``
in a ``closeBundle`` hook so that the stamp is always part of every build
output (no separate developer step required).

Fields written:

* ``source_hash`` — SHA-256 of every file under ``otto/web/client/src/`` plus
  the toolchain manifests. See :func:`otto.web.bundle.compute_source_hash`.
* ``built_at`` — ISO-8601 UTC timestamp.
* ``vite_version`` / ``node_version`` — best-effort, populated when the
  caller passes them via env vars (Vite knows its own version, Node knows
  its). Empty string if unknown.
* ``git_commit`` — output of ``git rev-parse HEAD`` if available, else "".

The script is intentionally dependency-free (stdlib only) so it runs in any
Python 3.11+ env that has the otto repo on disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# We are scripts/build_stamp.py — the package lives one dir up.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from otto.web.bundle import BUILD_STAMP_PATH, compute_source_hash  # noqa: E402


def _git_head_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def write_stamp() -> Path:
    payload = {
        "source_hash": compute_source_hash(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vite_version": os.environ.get("OTTO_BUILD_VITE_VERSION", ""),
        "node_version": os.environ.get("OTTO_BUILD_NODE_VERSION", ""),
        "git_commit": _git_head_sha(),
    }
    BUILD_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUILD_STAMP_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return BUILD_STAMP_PATH


def main() -> int:
    path = write_stamp()
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
