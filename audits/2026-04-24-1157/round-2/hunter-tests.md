# Hunter: Tests Round 2

Read-only subagent findings and disposition:

- Important: diff-error test over-mocked a path the real git wrapper swallowed. Fixed production diff handling to preserve stderr and changed the test to use a real missing branch.
- Important: merge commit reachability was not covered. Added a test where a recorded merge commit is not reachable from the target and the task stays ready.
- Important: stop-watcher PID ownership was not covered for live-but-unverified PIDs. Added refusal and supervised-allow tests.
- Minor: late background failure test used polling sleeps. Replaced the timing loop with a `threading.Event` synchronization point.

