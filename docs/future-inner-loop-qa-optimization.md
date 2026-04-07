# Future: Inner Loop QA Optimization

Status: **Not started — revisit when QA speed becomes a bottleneck**

Written: 2026-03-30

## Current State

Inner loop QA is a full Claude agent session per task (~1-2 min, ~$0.30-0.50).
The agent does VERIFY (behavioral testing of [must] items) + BREAK (adversarial testing).
In practice, ~70-80% of agent time is spent on behavioral testing — writing curls,
checking responses — which is exactly what compiled probes do, just slower.

## The Idea

Replace the agent's behavioral testing with the certifier's compile-then-execute approach.
Keep the agent for high-risk code review and BREAK only.

### Per-task QA would become:

```
1. Compiled probes      → test [must] spec items (seconds, $0)
2. Test suite runner    → npm test / pytest (subprocess, $0)
3. Compiled BREAK       → known adversarial patterns (seconds, $0)
4. Agent (optional)     → only for high-risk tasks (code review, visual, creative BREAK)
```

### What triggers agent escalation:

- Visual/UI claims ([must ] items)
- Auth, payments, migrations, concurrency
- Suspicious diffs: hardcoded fixtures, TODOs, disabled auth, no tests added
- Incomplete certifier coverage (spec items that aren't HTTP-testable)

## Key Design Questions (Unresolved)

1. **Spec item compiler**: Need to compile `[must] POST /api/products returns 201`
   into structured probes. Spec items are halfway structured but not executable yet.
   Could the spec agent output structured test steps directly?

2. **Static vs runtime discovery**: Adapter (static code analysis) misses dynamic
   routes, middleware-generated endpoints. Should combine with runtime discovery
   (start app, probe for routes) before compiling tests.

3. **Risk classifier**: How to decide which tasks get the agent escalation?
   Could be rule-based (keywords in spec: "auth", "payment") or diff-based
   (large diff, new files, migrations).

4. **Probe gaming**: Coding agents could learn to satisfy probes without proper
   implementation (hardcoded responses). Mitigation: randomized test data,
   state-dependent assertions, cross-request consistency checks.

5. **"Certifier passed" != "implementation is sound"**: Probes verify observable
   contract, not code quality. The agent's diff-reading catches stubs, TODOs,
   poor error handling. Losing this entirely is a risk.

## Why Not Now

- QA is not the bottleneck (coding agent is 5-15 min, QA is 1-2 min)
- Inner loop QA works and is battle-tested (35 projects)
- Savings: ~1 min and ~$0.30 per task, ~5 min and $1.50 per 5-task build
- Higher priority: wire certifier into outer loop, test full otto build pipeline

## Codex Opinion (2026-03-30)

Codex agreed: one unified verification core for both loops, agent becomes an
exception path. Key risks flagged: probe gaming, adapter overconfidence,
self-healing hiding real bugs, cache invalidation on spec/route/schema changes.

## References

- Certifier implementation: `otto/certifier/`
- Current QA agent: `otto/qa.py`
- Codex thread: 019d4293-f101-7410-abc4-e4bfff941993
