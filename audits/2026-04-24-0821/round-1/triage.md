# Triage

| Candidate | Status | Notes |
| --- | --- | --- |
| Failed queue requeue id collision | fixed | Implemented retry-id dedupe and test coverage. |
| Legacy in-flight queue rows not stale/removable | fixed | Added writer identity propagation, stale overlays, queue status classification, removal path, and tests. |
| Failed terminal queue rows not inspectable | fixed | Added retention rule and regression coverage. |
| Detail lookup hidden by filters | fixed | Detail now resolves unfiltered; covered by test. |
| Stale detail/log 404s in browser | fixed | TS client clears/ignores stale selected rows. |
| Overview attention undercount | fixed | Landing/live/history attention set. |
| Confirmation copy used internal phrasing | fixed | Added action-specific confirmation bodies. |
| Broad exception guards | invalid | Intentional local-state resilience boundaries. |
| Typed persisted JSON shapes | deferred | Larger schema-hardening project, not required for this release slice. |
