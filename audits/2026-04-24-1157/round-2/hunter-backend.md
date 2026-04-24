# Hunter: Backend Round 2

Read-only subagent findings and disposition:

- Medium: `/api/watcher/start` attempted a launch while runtime was stale. Fixed by blocking with HTTP 409 and recording `watcher.start.blocked`.
- Low: event tail reading dropped the first row even when the tail started on a line boundary. Fixed by inspecting the byte before the tail window.

