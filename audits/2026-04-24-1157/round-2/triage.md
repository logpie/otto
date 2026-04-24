# Round 2 Triage

All actionable round-2 findings were fixed.

Extra live-browser finding: after a successful merge, the landing queue showed `Merged` but the run detail still offered `merge selected`. Fixed by adding landing context to run detail, disabling duplicate merge actions, and rejecting already-merged merge requests with HTTP 409.

Extra live-browser finding: queued future branches showed `diff error` because the branch does not exist before the watcher starts. Fixed by suppressing branch diff checks for non-terminal in-flight queue states.

