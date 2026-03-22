# Otto: Next High-Priority Improvements

**Date:** 2026-03-21
**Context:** Based on analysis of Otto's current error recovery, UX gaps, competitive landscape, and proven deterministic pre-computation approaches (Aider's repo map, ast-grep).

**Mission reminder:** "Autonomous coding agent that makes Claude safe to run unattended." The improvements below are ordered by how directly they close the gap between this promise and current reality.

---

## Priority 1: Make "Unattended" Trustworthy

These changes address the core problem: the user doesn't trust otto enough to walk away.

### 1.1 Doom Loop Detection — Infrastructure Level

**Problem:** Doom loop detection currently exists only in the pilot's system prompt. If the pilot miscounts failures, loses context after compression, or rationalizes "this is a different error," it can retry infinitely.

**Current state:** `runner.py` tracks `last_error` (singular) and `attempts` count in tasks.yaml. No error fingerprinting. No cross-attempt comparison.

**What to build:**

- Add an `attempts/` directory per task: `otto_run/attempts/<task-key>/attempt-<N>.json`
- Each attempt file records:
  ```json
  {
    "attempt": 1,
    "timestamp": "2026-03-21T10:00:00Z",
    "strategy": "Used Open-Meteo API with caching layer",
    "error_fingerprint": "sha256 of normalized error output",
    "error_summary": "TypeError: cache.get is not a function",
    "error_full": "... full output ...",
    "diff_summary": "Modified 3 files: weather.py, cache.py, test_weather.py",
    "duration_s": 45,
    "cost_usd": 0.12
  }
  ```
- In `runner.py` or a new `recovery.py`:
  - After each failure, compute error fingerprint (normalize whitespace/paths/timestamps, then hash)
  - Compare against previous attempts for this task
  - **Same fingerprint twice consecutively** → inject `MUST_CHANGE_APPROACH=true` flag into retry prompt, with explicit instruction: "Your previous approach produced the identical error. You MUST use a fundamentally different strategy."
  - **3 unique failures OR 2 identical failures** → hard abort at infrastructure level (not pilot discretion). Set task status to `failed` with error_code `doom_loop` or `max_retries`.
- The pilot can still decide retry strategy, but the infrastructure enforces the ceiling.

**Files to modify:** `runner.py` (retry loop), `tasks.py` (add attempts history), new `otto/recovery.py` (fingerprinting + policy).

### 1.2 Failure Escalation Notifications

**Problem:** When otto fails, nobody knows until someone checks. The user must babysit.

**What to build:**

- Add a notification system with pluggable backends. Start with two:
  1. **macOS native:** `osascript -e 'display notification "Task X failed after 3 attempts" with title "Otto"'`
  2. **Telegram:** POST to user's bot (config: `otto_config.yaml → notifications.telegram.chat_id`)
- Notification triggers:
  - Task failed after final retry (doom loop / max retries)
  - Task passed (optional, configurable)
  - All tasks completed — summary with pass/fail counts, total cost, total time
  - QA found issues
- Config in `otto.yaml`:
  ```yaml
  notifications:
    on_failure: [macos, telegram]
    on_success: []
    on_complete: [macos, telegram]
    telegram:
      bot_token: "..."
      chat_id: "..."
  ```
- Keep it simple: a `notify(event, details)` function that dispatches to configured backends.

**Files to modify:** New `otto/notify.py`, `otto/config.py` (add notification config), `runner.py` and `pilot.py` (call notify at trigger points).

### 1.3 Decision Logging

**Problem:** No way to audit why the pilot made specific choices. Can't assess if pilot judgment is good or bad without re-reading its entire conversation.

**What to build:**

- Add MCP tool `log_decision(context, options, choice, reasoning)` to pilot's toolset
- Writes to `otto_run/decisions.md` (append-only):
  ```markdown
  ## 10:03:22 — Task weather-api retry strategy
  **Context:** Second failure. Error: timeout on Open-Meteo API call.
  **Options:** (a) Retry same approach with longer timeout (b) Switch to wttr.in API (c) Add retry/backoff logic (d) Abort
  **Choice:** (b) Switch to wttr.in
  **Reasoning:** Same timeout occurred in attempt 1. API may be unreliable. Switching provider is lower risk than adding complexity.
  ```
- Update pilot system prompt to require calling `log_decision` before every non-trivial action: retries, skips, strategy changes, aborts.
- Cost: ~zero (one more MCP tool, a few lines in system prompt).

**Files to modify:** `pilot.py` (add MCP tool + system prompt update).

### 1.4 Structured Event Stream

**Problem:** Pilot's display output is ephemeral. No machine-readable record of what happened.

**What to build:**

- `otto_run/events.jsonl` — one JSON line per event:
  ```json
  {"ts": "...", "event": "task_started", "task": "weather-api", "attempt": 1}
  {"ts": "...", "event": "task_failed", "task": "weather-api", "attempt": 1, "error": "timeout"}
  {"ts": "...", "event": "task_started", "task": "weather-api", "attempt": 2}
  {"ts": "...", "event": "task_passed", "task": "weather-api", "attempt": 2, "cost": 0.15}
  {"ts": "...", "event": "run_complete", "passed": 3, "failed": 1, "cost": 0.89, "duration": 312}
  ```
- Written by runner.py at each state transition. Not by the pilot (pilot writes decisions.md; runner writes events.jsonl).
- Enables: `tail -f events.jsonl` for live monitoring, future dashboards, post-run analytics.

**Files to modify:** `runner.py` (emit events), new `otto/events.py` (event types + writer).

---

## Priority 2: Improve Success Rate

### 2.1 Baseline Failures — Pass to Agent

**Problem:** `pilot.py` lines 845-877 record baseline test failures before otto runs, but this information is never passed to the coding agent. Agent wastes time fixing pre-existing failures or gets confused by tests that were already broken.

**What to build:**

- After baseline check, write `otto_run/baseline_failures.txt` with the failing test output.
- In `runner.py` `prepare_task()`, inject into the coding agent's prompt:
  ```
  IMPORTANT: The following tests were already failing BEFORE your changes.
  Do NOT attempt to fix these. Ignore them when evaluating your work.

  <baseline_failures>
  {contents of baseline_failures.txt}
  </baseline_failures>
  ```
- In `verify.py`, consider subtracting baseline failures from the failure count (a test that was already failing is not a regression).

**Files to modify:** `pilot.py` (write baseline file), `runner.py` (inject into prompt), `verify.py` (optional: subtract baseline).

### 2.2 Attempt History in Retry Prompts

**Problem:** Retries only get `last_error`. Agent has no memory of what was tried before and may repeat the same approach.

**What to build:**

- On retry, read all `otto_run/attempts/<task-key>/attempt-*.json` files.
- Inject a structured retry context into the coding agent's prompt:
  ```
  This is attempt 3 of task "weather-api". Previous attempts:

  Attempt 1: Used Open-Meteo API with direct calls. Failed: TypeError in cache layer.
  Attempt 2: Added caching with redis. Failed: redis not available in test environment.

  You MUST use a different approach from attempts 1 and 2.
  ```
- This is the "learning" mechanism — the agent sees the full trajectory, not just the last error.

**Depends on:** 1.1 (attempt history persistence).

**Files to modify:** `runner.py` (build retry prompt with history).

### 2.3 Spec Constraint Preservation

**Problem:** Spec generation silently weakens hard constraints (e.g., "<300ms for all cities" → "<300ms for cached requests only"). This was a recurring issue in v3 development.

**What to build:**

- In `spec.py`, after spec generation, add a constraint verification pass:
  1. Extract explicit constraints from user input (numbers, "must", "always", "never", "all", "every")
  2. Check that each constraint appears in the generated spec without weakening qualifiers ("when possible", "ideally", "for cached", "typically")
  3. If weakening detected, re-prompt with: "The following constraints were weakened in your spec. Restore them exactly as stated: ..."
- Add to spec generation prompt: "NEVER weaken, soften, or add conditions to user-stated constraints. If the user says '<300ms for all cities', the spec must say '<300ms for all cities', not '<300ms for cached cities'."

**Files to modify:** `otto/spec.py` (add constraint verification pass + prompt update).

---

## Priority 3: Deterministic Pre-Computation (Aider Repo Map Approach)

Core insight: don't let the agent guess what files to read or modify. Use deterministic analysis to compute this before the agent starts reasoning. This is a proven pattern — Aider (42k stars) uses it as the foundation of its context system.

### 3.1 Relevant File Pre-Computation (Tree-sitter + PageRank)

**Problem:** Otto's coding agent decides which files to read and modify. In large codebases, it frequently misses relevant files or wastes tokens reading irrelevant ones. The architect does codebase analysis but produces prose, not a ranked file list.

**Proven approach — Aider's repo map** (`aider/repomap.py`, Apache 2.0):

1. Tree-sitter parses all files → extracts definitions (functions, classes, variables) and references (calls, imports)
2. Builds a directed graph (NetworkX): file A references symbol in file B → edge A→B
3. Runs PageRank with personalization weights:
   - Files mentioned in task spec: **x50 weight**
   - Identifiers mentioned in spec: **x10 weight**
   - Private/internal symbols: **x0.1 weight**
4. Ranks files by PageRank score, binary-searches to fit within token budget
5. Outputs file skeletons (definitions only, no function bodies) — compact but informative

Key insight: **graph topology determines relevance better than text similarity.** No embeddings or LLM needed.

**What to build for Otto:**

- New module: `otto/context.py` — port Aider's repo map approach
- Dependencies:
  - `tree-sitter` + `tree-sitter-language-pack` (Python bindings, pip installable)
  - `networkx` (for graph + PageRank)
  - `ripgrep` (already available via Claude Code)
- Given a task spec:
  1. **Parse codebase:** Tree-sitter extracts all definitions and references across files
  2. **Build reference graph:** Directed edges from files that reference symbols to files that define them
  3. **Personalize PageRank:** Weight nodes matching task spec keywords higher
  4. **Rank and truncate:** Top-N files (e.g., 20) with relevance scores
  5. **Generate skeletons:** For top files, output only definition signatures (not full bodies)
- Inject into coding agent prompt:
  ```
  Based on codebase analysis, these files are most relevant to your task (ranked by dependency centrality):

  1. src/weather/api.py (score: 0.95)
     - def fetch_weather(city: str) -> WeatherData
     - def parse_response(raw: dict) -> WeatherData
     - class WeatherClient

  2. src/weather/cache.py (score: 0.82)
     - def get_cached(key: str) -> Optional[WeatherData]
     - class CacheLayer

  3. tests/test_weather.py (score: 0.78)
     - def test_fetch_weather_valid_city()
     - def test_cache_miss()
  ...

  Start by reading these files. You may discover additional relevant files during implementation.
  ```
- This doesn't constrain the agent (it can still read other files), but gives it a strong starting point backed by graph analysis.

**Companion tools to consider:**

- **grep-ast** (Aider's library, pip installable): grep that shows structural context (enclosing function/class). Useful for the coding agent itself.
- **ast-grep** (13k stars, Rust): Structural code search using AST patterns. Patterns look like real code: `cache.get($KEY)` matches only actual calls, not comments/strings. Useful for precise "who calls this function" analysis and blast radius estimation.

**Files to create:** `otto/context.py`. **Files to modify:** `runner.py` (call context analysis before dispatching coding agent, inject results into prompt). **Reference:** `github.com/Aider-AI/aider/blob/main/aider/repomap.py`

### 3.2 Execution Contracts (Scope Boundaries)

**Problem:** Coding agents sometimes modify files they shouldn't touch — unrelated modules, config files, CI pipelines. Otto has no mechanism to prevent this.

**What to build for Otto:**

- Extend the architect's output to include a `file-contract.json`:
  ```json
  {
    "task_key": "weather-api",
    "allowed_paths": [
      "src/weather/**",
      "tests/test_weather*"
    ],
    "protected_paths": [
      "src/auth/**",
      "*.config.js",
      ".github/**",
      "otto/**"
    ],
    "rationale": "Task is scoped to weather module. Auth, config, CI, and otto internals must not be modified."
  }
  ```
- The repo map from 3.1 can inform contract generation: files with high PageRank for this task → `allowed_paths`; files with zero relevance in critical directories → `protected_paths`.
- ast-grep can help with blast radius: "if you modify function X, these files also need updating."
- In `verify.py`, after the coding agent finishes, check the git diff:
  - If any modified file matches `protected_paths` → verification failure with clear message: "You modified protected file X. This file is outside the scope of your task."
  - If any modified file is NOT in `allowed_paths` → warning (not hard fail), logged for pilot review.
- Also inject contract into coding agent prompt as guidance (not just post-hoc enforcement):
  ```
  SCOPE CONTRACT: You may modify files matching: src/weather/**, tests/test_weather*
  DO NOT modify: src/auth/**, *.config.js, .github/**, otto/**
  ```

**Files to create:** New section in `otto/architect.py` (generate contracts). **Files to modify:** `verify.py` (enforce contracts), `runner.py` (inject contract into prompt).

---

## Implementation Order

```
Phase 1 — Trust (1-2 days each)
  1.3  Decision logging          ← near-zero cost, do first
  1.1  Doom loop detection       ← highest safety impact
  1.4  Event stream              ← foundation for everything else
  1.2  Failure notifications     ← enables actual unattended use

Phase 2 — Success Rate (1-2 days each)
  2.1  Baseline failures         ← trivial, high impact
  2.2  Attempt history in retry  ← depends on 1.1
  2.3  Spec constraint check     ← addresses known pain point

Phase 3 — Deterministic Pre-Computation (2-3 days each)
  3.1  Repo map (tree-sitter + PageRank) ← port Aider's proven approach
  3.2  Execution contracts               ← architect + verify changes
```

---

## What NOT to Build Now

- **Dashboard / TUI / web UI** — structured output (events.jsonl, decisions.md) comes first. UI later.
- **`otto chat` dual session** — CLI commands (`otto msg`, `otto pause`) are sufficient.
- **Agent-agnostic support** — Otto's value is in the harness, not in supporting multiple agents.
- **Cloud deployment** — premature.
- **Context+ / embedding-based context** — requires Ollama (local LLM dependency), has resource leak bugs, and Claude's own understanding > small embedding models. Use deterministic tools (tree-sitter, ripgrep, ast-grep) instead.
- **Tape Systems formalism** — events.jsonl + attempt files = pragmatic enough. No need for anchors/views/fork-merge abstractions yet.
