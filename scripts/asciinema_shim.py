#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import struct
import sys
import time
from pathlib import Path


def set_winsize(fd: int, rows: int, cols: int) -> None:
    import fcntl
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def run_rec(command: str, output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = int(os.environ.get("LINES", "30"))
    cols = int(os.environ.get("COLUMNS", "120"))
    master_fd, slave_fd = pty.openpty()
    set_winsize(slave_fd, rows, cols)

    pid = os.fork()
    if pid == 0:
        try:
            os.login_tty(slave_fd)
            os.execvpe("/bin/sh", ["/bin/sh", "-lc", command], os.environ)
        finally:
            os._exit(127)

    os.close(slave_fd)
    os.set_blocking(master_fd, False)
    started = time.time()
    header = {
        "version": 2,
        "width": cols,
        "height": rows,
        "timestamp": int(started),
        "env": {
            "TERM": os.environ.get("TERM", "xterm-256color"),
            "SHELL": os.environ.get("SHELL", "/bin/sh"),
        },
    }
    with output.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header) + "\n")
        while True:
            try:
                chunk = os.read(master_fd, 65536)
            except BlockingIOError:
                chunk = b""
            except OSError as exc:
                if exc.errno == errno.EIO:
                    chunk = b""
                else:
                    raise
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                sys.stdout.write(text)
                sys.stdout.flush()
                handle.write(json.dumps([round(time.time() - started, 6), "o", text]) + "\n")
                handle.flush()
                continue
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.01)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Minimal asciinema rec shim.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    rec = subparsers.add_parser("rec")
    rec.add_argument("--command", required=True)
    rec.add_argument("--output", required=True)
    rec.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.subcommand != "rec":
        raise SystemExit("only `rec` is supported by this shim")
    return run_rec(args.command, Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
