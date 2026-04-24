# Hunter: Tests

Findings received from read-only subagent Avicenna.

- High: stale/reconcile coverage only modeled running tasks; starting-without-child crash window missing. Fixed with starting queue stale handling and test.
- High: merge action tests missed late background process failures. Fixed by passing post_result from service and adding late failure event test.
- Medium: integration test accepts loose outcomes and mutates stale tracker internals. Deferred to broader integration cleanup; exact web stale coverage added.
- Medium: watcher start tests covered only launch-requested fallback. Fixed with watcher started and immediate failure tests.
- Low: stale cost assertion was weak. Fixed exact assertion.

