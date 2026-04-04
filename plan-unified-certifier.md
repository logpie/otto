# Plan: Unified Certifier — Single QA System (v4 — simplified)

## Goal

Replace per-task LLM QA with a unified certifier. Single QA system, no duplicate coverage.

```
Build:   code → agent's own tests → merge        (cheap, fast)
Certify: preflight → journey agents → findings    (thorough, post-merge)
Fix:     targeted fix tasks → re-certify
```

## Design

### Two layers, not four

```
Preflight: is the product reachable?    (seconds, free, deterministic)
Journeys:  does it work for real users? (minutes, LLM agents, the actual test)
```

**Preflight** = confirm we can reach the product. App starts, routes respond. If dead, report findings and skip journeys (don't waste $1.50 on a dead server). Not a test layer — just a reachability check.

**Journeys** = the real certification. Journey agents simulate users via HTTP/CLI, verify behavior, try to break things. Every story runs every time. Break findings (high/critical) fail certification and trigger fix tasks.

No tiers. No graduation. No probes-as-tests. No regression cache.

### Speed optimization: parallel stories

Sequential: 7 stories × ~60s = ~7 min
Parallel: 7 stories concurrent = ~1-2 min

Parallel story execution is the speed lever, not caching. Much simpler — just `asyncio.gather()` on the journey agents.

### Per-interaction executors (Phase 3)

How the journey agent talks to the product is an implementation detail:
- **HttpExecutor**: curl/HTTP requests (web apps, APIs) — exists today
- **CliExecutor**: bash commands (CLI tools) — Phase 3
- **LibraryExecutor**: import + function calls (libraries) — Phase 3

### What's implemented (Phase 1, committed)

- `run_unified_certifier()` orchestrating preflight + journeys
- `skip_qa=True` in build phase (no spec gen, no LLM QA)
- Break findings (high/critical) fail certification, trigger fix tasks
- All break findings displayed loudly in CLI
- Legacy fallback via `unified_certifier=False`
- 685 tests pass, E2E validated

### What to simplify

The current code has 4 tiers (structural, probes, regression, journeys). Simplify to:
- Remove TierResult/TierStatus machinery — just preflight + journeys
- Remove tier 3 regression (not implementing graduation)
- Tier 1 structural + tier 2 probes collapse into preflight
- Tier 4 journeys = the certification

This is cleanup, not new functionality. The current code works — it's just over-structured.

### What remains

1. **Parallel story execution** — `asyncio.gather()` in journey_agent.verify_all_stories (it may already support this)
2. **CLI executor** — journey agent with Bash tool instead of HTTP (Phase 3)
3. **Deprecate qa.py/spec.py** — already skipped in build mode, remove dead code (Phase 4)

## Plan Review

### Rounds 1-5 — Codex (plan gate)
See commit 1abf427 for full review trail (26 issues across 5 rounds).

### Simplification rationale
- Tiers added complexity for marginal value — preflight + journeys is the real structure
- Test graduation added significant complexity (parameterization, fingerprinting, staleness, quarantining, health gates) for ~$1/run savings
- Parallel stories achieve the same speed improvement (10 min → 2 min) with trivial complexity
- Per-task QA was removed intentionally — graduated tests in test_command would bring it back in disguise
