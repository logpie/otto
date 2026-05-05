"""Start an otto MC server pointed at the RUA fixture project.

Usage:
    python scripts/rua/serve_fixture.py [project_dir] [port]
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import uvicorn

from otto.web.app import create_app


def find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> None:
    project = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rua-fixture-project").resolve()
    port = int(sys.argv[2]) if len(sys.argv) > 2 else find_free_port()
    app = create_app(project)
    print(f"RUA server: http://127.0.0.1:{port}/  project={project}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
