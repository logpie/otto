# Hunter: Production Bugs

Findings received from read-only subagent Peirce.

- High: merge status could go stale across targets. Fixed target-aware merge state indexing and merge_commit reachability check.
- High: watcher status probing could race watcher startup by briefly taking the queue lock. Fixed non-invasive watcher status polling during launch.
- Medium: stop watcher signaled unvalidated PID. Fixed supervisor metadata and stop identity validation against lock holder or supervised pid.
- Medium: async action failures were not recorded. Fixed post_result callbacks from service.
- Medium: event reads unbounded. Fixed bounded tail reader.

