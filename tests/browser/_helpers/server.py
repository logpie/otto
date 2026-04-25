"""Launch a FastAPI Mission Control backend on an atomically-bound free port.

Why not ``uvicorn.run(host="127.0.0.1", port=0)``: under pytest-xdist two
workers can race between "find free port" and "bind it" if the port comes
from ``socket.bind(("", 0))`` then is closed before uvicorn starts. We avoid
that by handing uvicorn a pre-bound socket via ``Config(fd=...)`` so the OS
guarantees the port stays ours from bind through serve.

The launcher runs uvicorn in a daemon thread and exposes the URL + a stop
function. On stop we set ``Server.should_exit`` and join the thread; we also
explicitly close the bound socket if uvicorn never picked it up.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import uvicorn
from fastapi import FastAPI

from otto.web.app import create_app


@dataclass
class MCBackend:
    """Handle for a running Mission Control backend.

    Attributes:
        url: Base URL, e.g. ``http://127.0.0.1:54321``.
        port: Bound port number.
        project_dir: Path to the project the app was started against.
        projects_root: Managed-projects root the app was started with.
        app: The FastAPI instance (for direct introspection in tests).
        stop: Callable that signals shutdown, joins the server thread, and
            closes lingering sockets. Idempotent.
    """

    url: str
    port: int
    project_dir: Path
    projects_root: Path
    app: FastAPI
    stop: Callable[[], None]


def start_backend(
    project_dir: Path,
    *,
    projects_root: Path,
    project_launcher: bool = False,
    queue_compat: bool = True,
    startup_timeout: float = 10.0,
) -> MCBackend:
    """Start a Mission Control backend on a free port; return its handle.

    Args:
        project_dir: The project the app should serve.
        projects_root: Directory under which managed projects live. Browser
            tests should pass an isolated tmp dir so ``/api/projects`` cannot
            leak the real ``~/otto-projects`` (see plan-mc-audit.md 3.5A).
        project_launcher: Pass ``True`` to start the app in launcher (no
            project) mode for cold-start tests.
        queue_compat: Forwarded to ``create_app``.
        startup_timeout: Max seconds to wait for the server to accept
            connections before raising ``TimeoutError``.
    """

    sock = _bind_free_socket()
    sockname: tuple[str, int] = sock.getsockname()  # type: ignore[assignment]
    port: int = sockname[1]
    fd = sock.fileno()

    app = create_app(
        project_dir=project_dir,
        queue_compat=queue_compat,
        project_launcher=project_launcher,
        projects_root=projects_root,
    )
    config = uvicorn.Config(
        app=app,
        fd=fd,
        log_level="warning",
        # Keep the loop simple; tests are short-lived and don't need uvloop.
        loop="asyncio",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(
        target=server.run,
        name=f"mc-backend-{port}",
        daemon=True,
    )
    thread.start()

    _wait_until_serving(port, deadline=time.monotonic() + startup_timeout, server=server)

    stopped = False

    def stop() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        server.should_exit = True
        thread.join(timeout=10.0)
        # uvicorn dups the fd internally; close ours best-effort.
        try:
            sock.close()
        except OSError:
            pass

    return MCBackend(
        url=f"http://127.0.0.1:{port}",
        port=port,
        project_dir=project_dir,
        projects_root=projects_root,
        app=app,
        stop=stop,
    )


def _bind_free_socket() -> socket.socket:
    """Bind a socket on ``127.0.0.1`` to a kernel-assigned free port.

    The socket is left listening so ``uvicorn.Config(fd=...)`` can adopt it
    directly. ``SO_REUSEADDR`` lets us recycle ports cleanly between tests.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def _wait_until_serving(port: int, *, deadline: float, server: uvicorn.Server) -> None:
    """Poll ``127.0.0.1:port`` until it accepts a TCP connection or we time out."""

    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=0.25)):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    msg = (
        f"Mission Control backend on port {port} did not start within deadline "
        + f"(last connect error: {last_error!r})."
    )
    raise TimeoutError(msg)
