"""Otto queue subsystem (Phase 2-6 of plan-parallel.md).

Single-writer state model:
- ``.otto-queue.yml`` — task definitions. CLI APPENDS; watcher READS.
- ``.otto-queue-state.json`` — runtime state. Watcher SOLE WRITER.
- ``.otto-queue-commands.jsonl`` — mutation requests. CLI APPENDS; watcher CONSUMES.

This invariant replicates Elixir/OTP's GenServer-mailbox guarantee in Python:
all state mutations sequenced through one execution context (the watcher's
main loop), eliminating the SIGCHLD-mid-write race class.

Signal handlers MUST only set flags or enqueue messages — never touch state
files directly. Subprocess reaping happens via ``os.waitpid(WNOHANG)`` IN
the main loop tick, not in a SIGCHLD handler.

See plan-parallel.md §3.4 (file format) and the "Hard invariant" subsection.
"""
