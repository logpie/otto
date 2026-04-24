# Triage

| Candidate | Status | Notes |
| --- | --- | --- |
| Stale watcher PID without held lock can be killed | fixed | Tightened `blocking_pid` ownership to fresh heartbeat or verified held flock; added regression test. |
| Unheld `.otto-queue.lock` file appears stale | fixed | `runtime._queue_lock_holder_pid()` now probes the flock instead of trusting PID text. |
| Ready landing rows do not open detail if only queue-compatible state exists | fixed | `MissionControlModel.selected_record()` now falls back to full queue-compatible live records. |
| Users cannot see what ready tasks changed | fixed | Landing rows and review packet include changed file counts and file names. |
| Evidence/failure review not first-class | fixed | Added review packet with next action, proof, changes, evidence, and failure reason. |
| Queue/state/command recovery hidden behind logs | fixed | Added runtime diagnostics API and web runtime warning banner. |
| Review packet should be its own module | deferred | Useful if packet grows; current scope left it in service to avoid over-refactoring. |
| Replace JSON state with SQLite | deferred | Out of scope for this single-user pass; added diagnostics around JSON state instead. |
| Auth/multi-user controls | invalid | User explicitly excluded multi-user/auth for this pass. |
