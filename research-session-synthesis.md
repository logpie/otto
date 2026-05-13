# Synthesis: this session's findings + open structural work

Date: 2026-05-13

Goal: pull together every finding from today's audits, fixes, and live
runs so we can plan the next round structurally rather than as a series
of patches.

## What got fixed (8 commits this session)

| SHA | What | Class addressed |
|---|---|---|
| `d32cc29c6` | shape-aware verification + coverage discipline | False-pass at integration |
| `e7badd20b` | live-stack discipline + realistic starting states | Tests-against-themselves |
| `6ae43e369` | unified `verify_from_clean` primitive | 3 fragmented clean-state checks |
| `bc43d7ae1` | architect retries on scaffold preflight failure | Architect verdict survived broken scaffold |
| `5fce61ceb` | re-enter dispatch loop after retry triggered | Retry path was no-op (caught by test, not live) |
| `5c182c7d1` | recover misplaced verdict.json + kill stale ports | Lost-work-from-wrong-path + cross-run zombies |
| `a4d0171e9` | actually share node_modules/.venv across worktrees | Dead optimization — agents redownloading deps |
| `e01ee45e0` | preinstall Playwright browsers + add `*.tsbuildinfo` to gitignore | Agent reinstall reflex + cache-file merge conflicts |

Total: ~90 new unit tests, all targeting generic classes.

## Validation status

| Fix | Validation |
|---|---|
| `d32cc29c6` integration prompt | ✅ live (Run 1 of itracker) |
| `e7badd20b` live-stack | ✅ live (Run 2 itracker showed integration writing live httpx tests) |
| `6ae43e369` clean-state primitive | ✅ live (real bug exposed on previously-failing project) |
| `bc43d7ae1` architect retry | ✅ unit tests (live didn't exercise — happy path) |
| `5fce61ceb` continue-fix | ✅ unit tests caught the bug pre-ship |
| `5c182c7d1` verdict recovery + port cleanup | ✅ unit tests; live unexercised |
| `a4d0171e9` install-dir sharing | ⏳ unit tests only; live pending |
| `e01ee45e0` Playwright preinstall + tsbuildinfo | ⏳ unit tests; tsbuildinfo class confirmed in live |

Pending live validation: install-dir sharing + Playwright preinstall.
The most recent run (Run 4) was launched before these landed, so it
can't validate them. Next run will.

## What this session's runs showed

### Frequency of recurring agent-time wasters (across 400 narrative logs)

| Pattern | Frequency | Fix in |
|---|---|---|
| `npm install` reflex | 38% | `a4d0171e9` |
| `uv venv` reflex | 22% | `a4d0171e9` (same dir-sharing) |
| `playwright install` reflex | 20% | `e01ee45e0` |
| `pip install -e/-r` reflex | 16% | `a4d0171e9` |
| `*.tsbuildinfo` cache committed | 3 logs | `e01ee45e0` |

Top 4 (96% of setup-rerun events) share the shape: *agents
defensively re-run setup that's already been done*. The install-dir
sharing primitive (`a4d0171e9`) is the meta-fix for all of them — each
command becomes a fast no-op when deps already exist.

### Cost of the most recent run (Run 4 — postretry validation)

- 56 min wall, $21.95
- 5 children (1 architect + 1 BE + 3 FE feature children) all reached
  `verdict=pass`
- 2 of the 3 FE children got `merge_blocked` (couldn't merge to
  integration branch)
- Final aggregate: `merge_blocked`

Why this is the **first** merge_blocked we've seen on issuetracker:
prior runs had 1 FE child total. This had 3 FE feature children, so
multiple siblings touched the same shared FE files for the first time.

### Hard evidence — which files conflicted

Exact set of shared FE shell files each FE child wrote:

| Child | App.tsx | playwright.config.ts | tsbuildinfo |
|---|---|---|---|
| `v5-e9cdd347f08a` (Auth/Settings) | ✓ | ✓ | ✓ |
| `v5-30bfad0a52ef` (Issues/Kanban) |  | ✓ | ✓ |
| `v5-f52d0dd6b028` (Cycles/Search) | ✓ | ✓ | ✓ |

3-of-3 on `playwright.config.ts`. 2-of-3 on `App.tsx`. All 3 on
tsbuildinfo (post-fix this would not be committed).

## Open structural issues

### A. Sibling shared-file conflicts (HIGHEST PRIORITY)

**Symptom**: ≥2 feature children touch the same file in the merged
worktree. The architect's pre-wiring rule (`lead.md:177-183`)
illustratively mentions a few files (`App.tsx`, `Nav.tsx`,
`store/index.ts`, `package.json`) but is incomplete in two ways:

1. Files not on the list (`playwright.config.ts`, `vite.config.ts`,
   `useWebSocket.ts`) consistently conflict.
2. Even files on the list (`App.tsx`) aren't actually pre-wired with
   all routes — the architect mentions them in CHARTER but each
   sibling still has to modify them to register its own routes.

**Cost evidence**: causes `merge_blocked` verdicts, which are
unrecoverable today without manual intervention. Live runs hit this
3 times on issuetracker (`vite.config.ts`, `useWebSocket.ts`,
`playwright.config.ts`/`App.tsx`).

**Why prompt-patching won't work**: we've already tried tightening
the architect's pre-wiring list. Each new decomp shape exposes a new
shared file. The prompt-patch trajectory will keep growing forever.

**Protocol fix** (proposed earlier):
1. Add `owned_paths: list[str]` to subtask schema (alongside `intent`
   and `depends_on`).
2. When the architect calls `mcp__otto__submit_subtask`, require it
   to declare each child's owned globs.
3. Preflight checks: for any pair of pending siblings, error if their
   `owned_paths` overlap.
4. Architect retries with the overlap error attached (existing retry
   mechanism handles this).

**Effort estimate**: ~200 LOC across:
- `otto/queue/subtask.py` — schema change, `enqueue_subtask` signature
- `otto/v5_preflight.py` — new overlap check
- `otto/mcp_tools.py` — `submit_subtask` MCP tool signature
- `otto/prompts/lead.md` — architect must declare owned_paths

### B. Per-agent port allocation (SECOND PRIORITY)

**Symptom**: CHARTER pins ports (5173, 8000) globally. When 2
feature children run Playwright tests concurrently, both want 5173.
First binds; second errors. Today's `cleanup_stale_declared_ports`
handles *cross-run* zombies, not *within-run* concurrent siblings.

**Cost evidence**: agents spend 30-60s diagnosing and retrying port
collisions. Less impactful than (A) — typically 1-2 collisions per
run, ~1-2 min of waste — but recurring.

**Protocol fix**:
1. Runner allocates per-child port ranges at dispatch time (e.g.,
   child 1 → `5173+0/8000+0`, child 2 → `5174/8001`).
2. Pass via env vars to the child's session (`OTTO_PORT_VITE=5174`,
   `OTTO_PORT_API=8001`).
3. Architect's CHARTER must reference ports from env, not hardcoded.
4. `vite.config.ts`, `start.sh`, etc. read from those envs.

**Effort estimate**: ~150 LOC + prompt update. Touches:
- `otto/v5_runner.py` — port allocator
- `otto/v5_branching.py` — env-var injection at worktree setup
- `otto/prompts/lead.md` — architect convention

### C. Architect retry validated only via unit tests (LOW PRIORITY)

**Symptom**: today's commit `bc43d7ae1` + `5fce61ceb` adds architect
retry-on-preflight-failure. The unit tests pass (8/8); the live run
didn't exercise it (architect happened to produce a clean scaffold
first try). The deterministic test caught a real bug pre-ship
(`continue` was missing), so the implementation is validated for
correctness — but real-world frequency / value is unknown.

**Action**: monitor next few runs for any `architect_retry` events.
If they fire and result in pass, the feature delivered. If they
never fire over (say) 5 runs, the feature is "implemented but
unexercised" and we should look at whether scaffold preflight is
ever blocking architects in practice.

## Out-of-scope but worth noting

- **Live-stack discipline only kicks in when FE is healthy.** Run 2
  showed integration agent writing real httpx tests when FE
  preflight blocked. Run 3 had no FE issues but didn't reach
  Playwright either (integration crashed on API outage). No run has
  yet shown the integration agent writing a real Playwright spec
  against a live merged FE.
- **The validation pyramid is incomplete at the integration→browser
  step.** We have unit (leaf) and live-stack-HTTP (integration). We
  don't yet have a confirmed live-stack-browser test, even though
  the prompt now demands it.

## Suggested next-session plan (rank-ordered)

1. **Owned-paths protocol** (A). Single biggest impact. Eliminates
   the `merge_blocked` failure class. Write
   `research-owned-paths.md` first to spec the schema + check
   semantics, then plan, then implement.

2. **Per-agent port allocation** (B). Second-biggest impact. Wait on
   (A) — both are protocol changes; doing them together avoids
   double-touching `subtask.py` schema.

3. **Live run that exercises the full validation pyramid through
   browser.** Pick a project where architect ships clean, integration
   reaches Playwright, and we can confirm the live-stack discipline
   fires.

## What to ship vs research vs defer

| Item | Status |
|---|---|
| Owned-paths fix | research + plan, not yet code |
| Port allocation | research + plan, can wait for owned-paths |
| Architect-retry live validation | passive observation across next runs |
| Integration browser exercise | next run setup, no code |
| Sibling-conflict architect prompt tightening | DO NOT pursue — patch trajectory, won't work |
| Per-agent env-functional check | DEFER — likely redundant post install-dir sharing |
| Per-agent journey-readiness check | DEFER — too speculative, high-touch |

## How we'll know we're done

The current decomp shape (5 children with ≥3 FE siblings) should run
to `verdict=pass` (not `merge_blocked`) without architect retries.
That's the acceptance criterion for the owned-paths work.
