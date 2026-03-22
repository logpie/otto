# Otto Audit: New Findings (Systematic File-by-File)

**Date**: 2026-03-22
**Principle**: "Give agents the problem and context. Don't substitute the system's judgment for the agent's. Don't micromanage, truncate, filter, prescribe, or prohibit unless absolutely necessary."
**Scope**: Every .py file in otto/. Only NEW findings not in the existing audit.

---

## config.py

### N1. `effort: "high"` hardcoded as default — system pre-decides effort for every agent

**Line**: `config.py:18`

```python
DEFAULT_CONFIG: dict[str, Any] = {
    ...
    "effort": "high",
}
```

Every agent (coding, QA, spec, planner) inherits this default. The config comment at line 196 says `# effort: high  # agent thinking effort (low/medium/high/max)` — implying the user can change it, but the default biases every run. A simple task doesn't need "high" effort. The Agent SDK supports effort levels for a reason: the model can allocate more or less thinking budget. By always defaulting to "high", the system pre-decides that every task deserves heavy thinking, wasting tokens on trivial tasks and potentially under-allocating for complex ones (where "max" would be more appropriate).

The existing audit mentions this tangentially in S2/S9 but doesn't flag the config default itself as a hardcoded decision that should be deferred.

**Why it matters**: Effort should either be left unset (let the SDK default handle it) or be per-task based on complexity. A global "high" is a one-size-fits-all decision.

---

### N2. `max_parallel: 3` hardcoded — system decides concurrency ceiling

**Line**: `config.py:17`

The system caps parallelism at 3 regardless of the machine's capabilities or the tasks' independence. On a machine with ample resources and 10 independent tasks, this forces 4 sequential batches instead of 1. The user can override in otto.yaml, but the default is an arbitrary constraint.

**Why it matters**: Low — but exemplifies the pattern of hardcoding decisions that depend on context the system doesn't have.

---

## context.py

### N3. `PipelineContext.learnings` is a flat list of strings — no provenance

**Lines**: `context.py:32`

```python
self.learnings: list[str] = []
```

Learnings are strings with no metadata: which task produced them, when, whether they're factual observations or inferred advice. When fed to downstream agents via `coding_loop()` (runner.py:703), the agent receives "LEARNINGS FROM PRIOR TASKS:" followed by anonymous bullet points. The agent can't evaluate the relevance or reliability of a learning without knowing its source.

**Why it matters**: Per the audit's design principle, cross-task learnings should be "factual observations, not advice." But without provenance, the agent can't distinguish "task #2 discovered the project uses ESM" (factual, useful) from "the planner thinks you should use a different approach" (inferred, potentially wrong). Adding `(from task #N)` would let the agent judge relevance.

---

## telemetry.py

### N4. `TaskStarted.prompt` is truncated before it reaches telemetry

**Line**: `runner.py:679` (caller of telemetry, not telemetry.py itself, but the truncation happens at the emit site)

```python
telemetry.log(TaskStarted(
    task_key=task_key, task_id=task_id,
    prompt=prompt[:80], strategy=task_plan.strategy,
))
```

The task prompt is truncated to 80 chars in the telemetry event. Telemetry is a historical record — there's no reason to truncate. Anyone reading the event log loses the full prompt context. This is information destruction in the observability layer.

**Why it matters**: Post-mortem analysis and cost-quality correlation (S9) need full prompts. Truncation here makes future analysis harder for no benefit.

---

## display.py

### N5. Display silently suppresses "internal" file operations from the user

**Lines**: `display.py:97, 226-228`

```python
_INTERNAL_PATTERNS = {"otto_arch/", "task-notes/"}
...
if any(p in raw_detail for p in _INTERNAL_PATTERNS):
    return
```

When the coding agent reads or writes to `otto_arch/` or `task-notes/`, the display suppresses it. The user never sees that the agent is writing task notes or reading architect context. This is benign for normal operation, but:

1. During debugging ("why did the agent make that decision?"), the user doesn't see what context the agent read.
2. If the agent is stuck in a loop reading architect files, the user sees nothing happening.

**Why it matters**: This is the system filtering information from the human, not from the agent. The principle applies to human observability too — don't hide what the agent is doing.

---

### N6. Display collapses diverse QA tool calls into generic labels

**Lines**: `display.py:309-338`

```python
def _qa_tool_label(self, name: str, detail: str) -> str:
    if name in ("Read", "Glob", "Grep"):
        return "Analyzing code..."
    ...
    if "curl " in cmd:
        return "Testing endpoints..."
```

When the QA agent runs `Read src/auth.py`, the user sees "Analyzing code..." When it runs `curl localhost:3000/api/login`, the user sees "Testing endpoints..." The actual file being read or endpoint being tested is lost.

This is the system substituting its interpretation ("Analyzing code") for the raw data ("Read src/auth.py"). During QA, the user watching the run wants to see WHAT is being tested, not a vague label. This is the display-layer version of a telephone game.

**Why it matters**: The user can't assess QA quality from generic labels. "Analyzing code..." doesn't tell you if the QA agent is testing the right files.

---

### N7. Display suppresses QA findings heuristically

**Lines**: `display.py:404-412`

```python
# Suppress noise
if "VERDICT" in text:
    return
if is_table_header:
    return
if text.startswith(("- Container", "- Header", "○ Edge")):
    return
if "Minor Observation" in text or "not spec violation" in text.lower():
    return
```

The display decides which QA findings are "noise" using hardcoded string patterns. "Minor Observation" is suppressed — but a minor observation might be the early signal of a real bug. "- Container" and "- Header" are suppressed — but these could be meaningful HTML structure findings for a web app.

**Why it matters**: The system is making a judgment call about what QA information is relevant to the user. The user should see everything and decide for themselves.

---

## architect.py

### N8. Architect prompt prescribes a specific exploration workflow

**Lines**: `architect.py:64-68`

```
EXPLORE the codebase:
1. Read the main source files (models, store, CLI, __main__.py)
2. Read existing test files to understand test patterns
3. Run --help if there's a CLI
4. Check the data storage format
```

This is a step-by-step workflow prescription. The architect agent should decide HOW to explore based on what it finds. Not every project has "models, store, CLI, __main__.py." Not every project has a CLI to `--help`. The agent will discover this on its own.

The existing audit (T4) flags this pattern in the spec agent but not in the architect agent.

**Why it matters**: The prescribed order might cause the agent to skip relevant exploration paths (e.g., reading config files, checking the build system) that aren't in the hardcoded list.

---

### N9. Architect prompt prescribes specific output files and their contents

**Lines**: `architect.py:72-106`

The prompt dictates seven specific files (codebase.md, conventions.md, data-model.md, interfaces.md, test-patterns.md, task-decisions.md, gotchas.md, file-plan.md) with specific content requirements and even ASCII diagram formatting requirements.

This is the system deciding what categories of architectural knowledge matter, before the agent has seen the project. A project might need a "deployment.md" or "auth-flow.md" but there's no slot for it. The agent is forced into a predetermined taxonomy.

**Why it matters**: The rigid file structure means architectural insights that don't fit the template are either shoehorned into the wrong file or lost. The agent should be told "document what downstream agents need to know" and organize it as it sees fit.

---

### N10. Architect validation requires specific filenames to declare success

**Lines**: `architect.py:149-151`

```python
expected = ["conventions.md", "data-model.md", "interfaces.md"]
produced = {f.name for f in arch_dir.iterdir()} if arch_dir.exists() else set()
if arch_dir.exists() and all(f in produced for f in expected):
```

If the architect agent decides the project doesn't need a separate "data-model.md" (e.g., a stateless CLI tool), the run is considered a failure. The system enforces its predetermined file taxonomy even when the agent's judgment says otherwise.

**Why it matters**: Forces the agent to produce files that may contain filler content just to pass validation, diluting the signal for downstream agents.

---

## testgen.py

### N11. Testgen prompt forces a specific validation workflow

**Lines**: `testgen.py:484-491`

```
Steps:
1. WRITE the test file immediately (don't explore first)
2. VALIDATE: python -c "import ast; ast.parse(open('<test_file>').read()); print('OK')"
3. If syntax error: fix and re-validate
4. VALIDATE: python -m pytest --collect-only <test_file>
5. If collection fails: read relevant files to debug, fix, re-validate
6. SELF-REVIEW: Could a lazy implementation pass these tests? Strengthen if needed.
7. If improved in step 6, re-validate (steps 2-4)
```

Seven prescribed steps in a specific order. Step 2 even dictates the exact validation command to use. The agent might want to write tests incrementally (write one, validate, write next) rather than batch-write-then-validate. It might want to explore first if the project context is ambiguous.

The existing audit (T4) flags this pattern in the spec agent; this is the same pattern in testgen.

**Why it matters**: The forced "write immediately, don't explore" instruction conflicts with "During self-review, if you need to verify specific details... you may read the relevant source file" (line 464). The agent is told not to explore, then told it can explore during self-review. Mixed signals.

---

### N12. Testgen forces "subprocess.run() for CLI testing, not CliRunner"

**Line**: `testgen.py:471`

```
- Use subprocess.run() for CLI testing, not CliRunner.
```

This is a specific implementation prescription. CliRunner (from Click) is often more reliable and gives better error messages for Click-based CLIs. The agent should choose the testing approach based on the project's framework.

**Why it matters**: If the project uses Click, CliRunner is actually the better choice. The system's blanket prohibition prevents the agent from using the right tool.

---

### N13. `build_blackbox_context()` only extracts stubs for Python files

**Lines**: `testgen.py:348-359`

```python
for rel_path in file_tree.splitlines():
    rel = rel_path.strip()
    if not rel.endswith(".py"):
        continue
```

The blackbox context builder only considers Python source files. For a TypeScript/JavaScript project, the testgen agent gets the file tree and existing test samples but NO API stubs. The agent receives an impoverished view of non-Python projects.

**Why it matters**: For JS/TS projects, the testgen agent must explore the codebase from scratch to understand the API, defeating the purpose of pre-built context. The function should either support multiple languages or pass raw file contents for the agent to process.

---

### N14. Test validation output truncated to 2000 chars

**Lines**: `testgen.py:630-631, 675, 682`

```python
error_output=(collect.stdout + collect.stderr)[:2000],
...
error_output=output[:2000],
```

When test validation fails, the error output is truncated to 2000 chars. This is the same information destruction pattern as T8 from the existing audit, but in a different location. The truncation happens before the data is stored in `TestValidationResult`, so no consumer ever sees the full output.

**Why it matters**: A pytest collection error with many failing imports might have the actual root cause beyond the 2000-char mark.

---

### N15. `get_relevant_file_contents()` skips all test files — agent can't learn from existing tests

**Lines**: `testgen.py:215-221`

```python
# Skip test files — agent writes its own
if p.name.startswith(("test_", "spec_")):
    continue
if p.name.endswith((".test.ts", ".test.tsx", ".test.js", ...)):
    continue
if "__tests__" in p.parts:
    continue
```

The function that pre-loads source files for the coding agent actively filters out all test files. The comment says "agent writes its own" — but existing tests are valuable context. They show testing patterns, fixture conventions, mock approaches, and what's already covered. The coding agent might want to see existing tests to:

1. Follow the same testing conventions
2. Avoid duplicating existing test coverage
3. Understand how the existing code is expected to behave

This is the system deciding what information is relevant and filtering out a category the agent might need.

**Why it matters**: The agent is told "write tests" but denied access to examples of how tests are written in this project. It must discover conventions through exploration turns instead of having them pre-loaded.

---

## planner.py

### N16. `TaskPlan.effort` always defaults to "high"

**Line**: `planner.py:28`

```python
effort: str = "high"          # agent effort level
```

Even when the planner LLM output omits effort, it defaults to "high" (line 133: `effort=tp_data.get("effort", "high")`). Combined with config.py's default (N1), this means effort is ALWAYS "high" unless the user explicitly overrides in otto.yaml. The system never allows the agent to self-regulate its thinking budget.

**Why it matters**: Redundant with N1 but shows the pattern is baked in at multiple layers, making it harder to change.

---

### N17. Planner system prompt says "Do NOT include hints" — but the data structure still carries them

**Lines**: `planner.py:236` vs `planner.py:25`

```python
# System prompt (line 236):
"- Do NOT include hints — the coding agent has full context and decides its own approach"

# But TaskPlan still has (line 25):
hint: str = ""                # hint passed to coding agent
```

The system prompt tells the planner not to produce hints, but the data structure still accepts and propagates them. If a future prompt change removes the prohibition, hints flow through silently. More importantly, this is a "Do NOT" prohibition — the system is prohibiting the planner from providing context, which contradicts the principle. If the planner has useful context, it should be allowed to pass it (as raw information, not advice).

**Why it matters**: The prohibition is correct per the audit's recommendations, but it's implemented via a prompt "Do NOT" rather than by removing the field from the schema. If the field exists, some LLM invocation will eventually populate it.

---

## spec.py

### N18. Spec system prompt includes 35+ lines of meta-rules about how to be a spec agent

**Lines**: `spec.py:109-175`

The spec system prompt contains:
- A `<role>` block defining identity
- A `<constraint_rules>` block with examples of good/bad behavior
- An `<output_rules>` block prescribing format
- A `<ux_consistency>` block with specific UX analysis instructions
- A `<compliance_check>` block with a 5-step self-check procedure

The `<constraint_rules>` section alone is 20 lines including two full examples. The `<compliance_check>` is a 5-step procedure the agent must follow "in your thinking."

Most of this is telling the agent HOW to think, not WHAT to achieve. The goal is simple: "produce testable acceptance criteria that faithfully capture the user's requirements." The constraint preservation examples could be replaced with: "preserve all user constraints exactly as stated — do not weaken thresholds or add conditions."

**Why it matters**: Prompt bloat. The spec agent sees 67 lines of system prompt instructions. With auto-compaction, the meta-rules about constraint preservation are more likely to be retained than the actual task requirements, because they're in the system prompt.

---

### N19. `_parse_spec_output()` silently upgrades unmarked items to "verifiable"

**Lines**: `spec.py:82-85`

```python
# Check for [verifiable] prefix
if _VERIFIABLE_RE.match(stripped):
    stripped = _VERIFIABLE_RE.sub("", stripped).strip()

if stripped:
    items.append({"text": stripped, "verifiable": True})
```

If a spec item has no `[verifiable]` or `[visual]` tag, it's automatically marked as verifiable. This means the system converts ambiguous items into verifiable ones, which then require automated tests. If the spec agent intentionally left an item untagged (because it's genuinely ambiguous), the system overrides that judgment.

**Why it matters**: False verifiability. An item like "the UI should feel responsive" gets marked verifiable and the coding agent must write a test for it. The agent wastes time trying to automate something that may not be automatable.

---

### N20. Markdown task parsing prompt prescribes "5-10" spec items per task

**Line**: `spec.py:289`

```python
- "spec": 5-10 concrete, testable acceptance criteria
```

Similar to T2 from the existing audit (spec capped at "5-8"), but in a different location — the markdown parsing prompt. This is a separate agent invocation with its own artificial count constraint.

**Why it matters**: A complex task extracted from a markdown document might legitimately need 15 criteria. Capping at 10 forces the markdown parsing agent to either merge or drop criteria.

---

## runner.py

### N21. Retry prompt is prescriptive — tells agent HOW to diagnose instead of giving it the error

**Lines**: `runner.py:1508-1517, 2267-2276`

```python
agent_prompt = (
    f"Verification failed. Fix the issue.\n\n"
    f"{last_error}\n\n"
    f"Original task: {prompt}\n\n"
    f"You are working in {effective_dir}. Do NOT create git commits.\n"
    f"Read the failing tests carefully. Is it a code bug or a test bug?\n"
    f"- Code bug: fix your implementation.\n"
    f"- Test bug (broken import, wrong stdlib usage): fix the test.\n"
    f"- Impossible constraint: explain why and implement the best feasible approach."
)
```

This appears in two places (line 1508 and line 2267 — both `run_task` and `run_task_with_qa`). The error output is passed correctly (good), but then the prompt prescribes a decision tree: "Is it a code bug or a test bug?" with three specific options. The agent might identify a fourth category (e.g., "the test framework is misconfigured," "a dependency is missing," "the spec is contradictory"). The prescribed options constrain the agent's diagnostic reasoning.

Better: "Verification failed. Here's the output. Fix the issue and re-implement." Let the agent decide how to diagnose.

**Why it matters**: The prescribed categories steer the agent toward specific diagnoses. If the real problem is a missing dependency or a race condition, the agent might force-fit it into "code bug" or "test bug."

---

### N22. "No changes" retry message tells agent WHAT to do instead of describing the problem

**Lines**: `runner.py:2406-2410`

```python
last_error = (
    "No code changes detected. The spec has not been implemented yet. "
    "Read the spec carefully and implement it. Do NOT add unnecessary "
    "improvements or padding — implement exactly what the spec asks for."
)
```

This is a prescriptive hint, not environmental feedback. The agent made no changes — that's the fact. But "Read the spec carefully" and "Do NOT add unnecessary improvements" are instructions that substitute the system's guess about WHY for what the agent should do. Maybe the agent explored and concluded the task was already implemented. Maybe it hit an error before starting. The raw fact — "no file changes detected after your session" — is sufficient.

**Why it matters**: The "Do NOT add unnecessary improvements or padding" is an accusation (the agent was padding?) that may not match reality. The agent should receive the fact and decide its own response.

---

### N23. Coding agent given subagents with prescriptive prompts on hardcoded models

**Lines**: `runner.py:1537-1548, 2296-2308`

```python
agent_opts.agents = {
    "researcher": AgentDefinition(
        description="Research APIs, read docs, investigate approaches",
        prompt="You are a research assistant. Investigate the topic thoroughly and report findings.",
        model="haiku",
    ),
    "explorer": AgentDefinition(
        description="Search codebase for patterns, find relevant files",
        prompt="You are a codebase explorer. Search for relevant code patterns, find files, and report what you find.",
        model="haiku",
    ),
}
```

Three issues:
1. **Model hardcoded to "haiku"**: The subagent model is always haiku regardless of task complexity. A research task about a complex API might need sonnet-level reasoning.
2. **Subagent prompts are generic**: "Investigate the topic thoroughly and report findings" tells the subagent nothing about the parent task's context.
3. **Duplicated in two places**: The same subagent definitions appear in both `run_task()` (line 1537) and `run_task_with_qa()` (line 2296).

**Why it matters**: The coding agent can dispatch subagents but the system pre-decides their capability (haiku) and gives them no task-specific context. The coding agent should be able to choose the subagent model or the subagent should inherit the parent's context.

---

### N24. `_should_show_tool()` filters tool calls by hardcoded command whitelist

**Lines**: `runner.py:286-303`

```python
def _should_show_tool(name: str, detail: str) -> bool:
    if name == "Bash":
        ...
        return first_word in (
            "pytest", "python", "python3", "npx", "npm", "jest",
            "make", "cargo", "go", "ruby", "dotnet", "node",
            "cat", "ls", "find", "grep", "head", "tail",
            "pnpm", "yarn", "uv", "bash", "sh", "tsc",
        )
```

If the agent runs a command not in the whitelist (e.g., `docker`, `curl`, `ffmpeg`, `cmake`, `swift`), the tool call is silently hidden from the user. The system decides which commands are "interesting" based on a static list.

**Why it matters**: The user misses agent activity that doesn't match the whitelist. A coding agent testing a Docker-based project would appear to be doing nothing during `docker build` and `docker run` calls.

---

### N25. TDD mode instructions contain "Do NOT" prohibitions for test modification

**Lines**: `runner.py:1499-1504`

```python
f"- You may NOT weaken assertions (change thresholds, remove checks, add skip/xfail).\n"
f"- If a test seems impossible to pass, explain why rather than hacking around it.\n\n"
f"Other test files in tests/ are from previous tasks.\n"
f"- If your changes intentionally break them, update their assertions.\n"
f"- Do NOT delete tests — only update assertions.\n\n"
```

Two "Do NOT"s and a "may NOT." The coding agent might have legitimate reasons to modify tests:
- A test might be genuinely wrong (testing incorrect behavior)
- A threshold might be unrealistic given implementation constraints
- A previous task's tests might conflict with the current task's requirements

Telling the agent to "explain why" instead of fixing is reasonable, but "may NOT weaken assertions" is an absolute prohibition that removes agent judgment.

**Why it matters**: The agent is forced to implement around potentially bad tests rather than fix them. If the testgen agent produced a test with an unrealistic assertion, the coding agent must contort the implementation to match the test rather than correcting the test.

---

### N26. Error in `_result()` truncated to 500 chars before storage

**Line**: `runner.py:2136`

```python
if error:
    updates["error"] = error[:500]
```

When `run_task_with_qa` stores the error in tasks.yaml, it truncates to 500 chars. This is the error that persists across runs and is shown to the user via `otto status` and `otto show`. The existing audit (T8) covers truncation to 200 chars in telemetry; this is a separate truncation at a different boundary with a different limit.

**Why it matters**: When a user reads `otto show #5` to understand why a task failed, the error shown is truncated. The full error is in the log files but the primary user-facing path loses detail.

---

### N27. QA agent prompt includes diff summary — a lossy compression of the actual changes

**Lines**: `runner.py:1918-1919`

```python
DIFF SUMMARY:
{diff_summary}
```

The QA agent receives `git diff --stat` output (file names and line counts), not the actual diff. It knows "auth.py | 50 +++++----" but not WHAT was changed in auth.py. The QA agent has Read access and can examine files, but it must guess which files matter from the summary rather than seeing the actual changes.

The argument for not including the full diff is context length — a large diff could be thousands of lines. But the QA agent could receive the diff in addition to the stat, and use its own judgment about which parts to focus on. Alternatively, include diffs for small changes and just the stat for large ones.

**Why it matters**: The QA agent tests more effectively when it knows exactly what changed. Without the actual diff, it might test areas that weren't modified while missing the changes that matter.

---

## cli.py

### N28. `_filter_generated_spec_items()` silently drops spec items using heuristic rules

**Lines**: `cli.py:32-113`

This 80-line heuristic system silently removes spec items that match patterns like:
- Lines matching `_SPEC_SEPARATOR_RE` (separator patterns)
- Lines starting with "acceptance spec", "acceptance criteria", "context:", "overview:"
- Lines matching `_SPEC_LABEL_RE` without a "requirement signal"
- Lines shorter than 40 chars without requirement signals matching `_SPEC_TITLE_RE`
- Lines containing context phrases like "existing ", "already ", "tests live in"

These rules remove items AFTER the spec agent produced them, without the agent knowing. The spec agent might have intentionally included a context line (e.g., "Existing API supports pagination") as important background for the coding agent. The heuristic silently deletes it.

This is a complex system of regex patterns and heuristics (7 different regexes, multiple string checks) making judgment calls about which spec items are "real" vs "preamble." If the spec agent's output format doesn't match these patterns, real criteria get dropped.

**Why it matters**: The system substitutes its regex-based judgment for the spec agent's judgment about what to include. If spec items are being mis-categorized as preamble, the fix should be in the spec agent's output format, not in a post-hoc heuristic filter.

---

## orchestrator.py

### N29. Orchestrator swallows summary printing errors silently

**Lines**: `orchestrator.py:243-246`

```python
try:
    _print_summary(results, run_duration, total_cost=context.total_cost)
except Exception:
    pass
```

The entire summary display is wrapped in a bare `except Exception: pass`. If `_print_summary` has a bug, the user sees no output at the end of a run — and no indication that something went wrong.

**Why it matters**: This is information suppression from the user. The summary is the primary feedback mechanism after a run. Silent failure here means the user doesn't know the run outcome.

---

### N30. Replan fallback is silent — no log of what the replanner produced

**Lines**: `orchestrator.py:217-221`

```python
replanned = await replan(context, remaining_plan, config, project_dir)
if _plan_covers_pending(replanned, remaining_pending):
    execution_plan = replanned
else:
    console.print("  [yellow]Replan returned invalid task coverage; keeping existing remaining plan[/yellow]")
    execution_plan = remaining_plan
```

When the replanner fails validation, the system prints a yellow warning but doesn't log WHAT the replanner produced. The raw output is lost. Combined with the planner's logger.warning (planner.py:404), which also truncates to 300 chars, there's no way to diagnose why replanning keeps failing.

**Why it matters**: Same pattern as S6 in the existing audit, but at the replan boundary. Without logging the raw output, you can't improve the replanner prompt.

---

## pilot_v3.py

### N31. Pilot system prompt tells it to produce "hints" — a known anti-pattern

**Lines**: `pilot_v3.py:801-811`

```
- If failed: decide retry strategy:
  - Read the error carefully. Give a targeted, specific hint — not generic advice.
  ...
  - run_task_with_qa(key, hint="specific guidance based on failure analysis")
```

The pilot is explicitly instructed to produce hints for the coding agent. The existing audit (P3, C4) identifies hints as a telephone game anti-pattern. The v4 orchestrator removes hints from the planner, but the v3 pilot still has this deeply embedded in its system prompt. Since v3 is still available via `--pilot` flag, this remains active code.

**Why it matters**: The existing audit recommends removing hints. The v3 pilot hasn't been updated to match. Users who use `--pilot` get the old telephone-game behavior.

---

### N32. Pilot prompt prescribes retry strategy in detail

**Lines**: `pilot_v3.py:801-812`

```
- Read the error carefully. Give a targeted, specific hint — not generic advice.
- Different error from last time? Good — making progress. Keep going.
- Same error repeating? Doom loop. Change strategy fundamentally:
  different algorithm, different library, different architecture.
- Before retrying a hard failure, dispatch Agent(researcher, "how to ...") first.
  Feed the research findings into the hint parameter.
- Think you're stuck? List 3 alternative approaches you haven't tried.
```

This is a full decision tree for how to respond to failures. The pilot is a capable LLM — it can reason about retry strategies without a prescribed algorithm. "Same error repeating? Change strategy fundamentally" assumes the pilot can't detect doom loops on its own. "List 3 alternative approaches" is a specific cognitive technique being imposed.

**Why it matters**: The pilot is micromanaged into a specific retry reasoning pattern. A smarter retry strategy might be: look at the actual error, identify the root cause, and fix it directly — which doesn't fit neatly into the "different algorithm, different library" template.

---

### N33. `abort_task` refuses with fewer than 3 attempts — hardcoded guardrail overrides agent judgment

**Lines**: `pilot_v3.py:594-605`

```python
MIN_ATTEMPTS = CONFIG.get("max_retries", 3)
...
if attempts < MIN_ATTEMPTS:
    return json.dumps({
        "refused": True,
        "error": f"Cannot abort — only {attempts} attempts made (minimum {MIN_ATTEMPTS}). "
                 f"Try a different approach: different algorithm, different library, ..."
    })
```

The pilot agent decides a task should be aborted. The system REFUSES and tells it to "try a different approach." This is the system overriding the agent's judgment with a hardcoded rule. If the pilot has determined that the task is genuinely impossible (e.g., spec requires a feature the language doesn't support), forcing 3 attempts wastes time and cost.

**Why it matters**: This is the most direct violation of "don't substitute the system's judgment for the agent's." The pilot is an LLM with full context. A hardcoded minimum-attempts guardrail prevents it from making an informed abort decision.

---

### N34. Multiple truncations in pilot display code lose context

**Lines**: `pilot_v3.py:159, 161, 166, 176, 183, 223, 307, 341, 397, 886`

Throughout pilot_v3.py, there are at least 10 separate truncation points:
- `task_key[:8]` — keys truncated to 8 chars
- `hint[:60]`, `hint[:120]` — hints truncated to different lengths
- `cmd[:80]` — commands truncated
- `error[:80]`, `error[:60]`, `error[:50]` — errors truncated to different lengths at different points
- `prompt[:55]` — prompts truncated

These are display truncations (not agent-facing), but they show a pervasive pattern of information loss. The inconsistent truncation lengths (50 vs 55 vs 60 vs 80) suggest ad-hoc decisions rather than a principled approach.

**Why it matters**: While display truncation is less harmful than agent-facing truncation, the inconsistency suggests the codebase lacks a principle about when and how to truncate. Each callsite makes its own judgment.

---

## Cross-Cutting Findings

### N35. `_subprocess_env()` injects CI=true — changes test behavior without the agent knowing

**Lines**: `verify.py:29`

```python
env["CI"] = "true"
```

Every subprocess (tests, builds, custom verify commands) runs with `CI=true`. This changes behavior in many frameworks:
- Jest runs in single-thread mode (slower)
- Create React App disables watch mode (correct)
- Some test suites skip integration tests in CI (information loss!)
- Some frameworks produce different output formats

The coding agent and QA agent don't know CI=true is set. If the agent debugs a test failure, it might try to reproduce locally and get different behavior because CI=true changes the test environment. The agent isn't told about this environment modification.

**Why it matters**: The system modifies the environment without informing the agents. A coding agent debugging flaky tests might waste turns because the test behavior differs from what it would see running tests manually.

---

### N36. Every agent session uses `permission_mode="bypassPermissions"` — no per-agent scoping

**Lines**: `runner.py:1523, 2283`, `spec.py:191`, `architect.py:117`, `testgen.py:513`, `planner.py:284`, `pilot_v3.py:853`

Every single agent invocation uses `bypassPermissions`. This means the spec agent (which only needs to read files and write one temp file) has the same permissions as the coding agent (which needs full read-write). The testgen agent (which is supposed to be mechanically isolated from implementation code) can read any file on disk.

This isn't a "trust the agent" issue — it's the opposite. The system SHOULD give agents scoped permissions that match their role. The testgen agent's adversarial isolation is enforced by a prompt instruction ("don't read implementation files") rather than by actual permission boundaries.

**Why it matters**: If the testgen agent accidentally reads implementation code (breaking adversarial isolation), nothing stops it. The prompt says "don't" but `bypassPermissions` says "you can." This is a case where the system SHOULD be more restrictive, not less.

---

### N37. `error[:200]` truncation at the exception handler boundary in `coding_loop()`

**Line**: `runner.py:759`

```python
telemetry.log(TaskFailed(
    task_key=task_key, task_id=task_id,
    error=str(exc)[:200], duration_s=duration,
))
```

When `coding_loop()` catches an unexpected exception, the error is truncated to 200 chars before logging to telemetry. The full error is available in the TaskResult (line 763: `error=f"unexpected error: {exc}"`), but the telemetry record — which is the primary diagnostic artifact — loses the detail.

This is a specific instance of T8 from the existing audit but at a different code path (coding_loop exception handler vs the general TaskFailed path). The existing audit covers runner.py:744 and :759 in T8, but this specific path in `coding_loop()` is worth calling out because it's the v4 orchestrator's primary error path.

*Borderline duplicate of T8 — included because the v4 path is distinct code.*

---

## Summary: Principle Violations by Category

| Category | Count | Findings |
|----------|-------|----------|
| **Prescribed workflows** | 4 | N8, N11, N21, N32 |
| **Information filtering/truncation** | 7 | N4, N5, N6, N7, N14, N15, N26 |
| **Hardcoded decisions** | 5 | N1, N2, N16, N23, N33 |
| **Output format/taxonomy rigidity** | 3 | N9, N10, N28 |
| **Silent information loss** | 4 | N3, N19, N24, N30 |
| **Environmental interference** | 2 | N35, N36 |
| **Prescriptive error handling** | 3 | N17, N22, N25 |

### Highest Impact (should fix first)

1. **N28** — Heuristic spec filtering silently drops agent output. Direct information destruction.
2. **N15** — Test files excluded from pre-loaded context. Forces unnecessary exploration.
3. **N33** — `abort_task` refuses based on hardcoded rule, overriding agent judgment.
4. **N13** — Blackbox context Python-only. Non-Python projects get degraded testgen.
5. **N27** — QA agent gets diff stat, not actual diff. Hampers adversarial testing.
6. **N35** — CI=true injected without agent knowledge. Changes test behavior silently.
