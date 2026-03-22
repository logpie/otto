# Display Redesign Research

## 1. Otto's Current Display Architecture

### 1.1 Current Stack

- **Rich library** imported directly (Console, Live, Text, Spinner, Group) but **not declared in pyproject.toml dependencies**
- **ANSI escape codes** still used in cli.py (`_B`, `_D`, `_G`, `_Y`, `_C`, `_R`, `_0`) for status tables, show, logs, history commands
- **display.py** (401 lines): `TaskDisplay` class with Rich Live footer + scrolling log via `console.print()`
- **pilot.py**: Three-tier display system (primary/secondary/noise) for pilot tool calls, background JSONL reader for progress events
- **runner.py**: `_print_tool_use()`, `_print_tool_result()`, `_log_*()` helpers, `_print_summary()`
- **cli.py**: ANSI-styled output for status, show, logs, history, init, add, reset commands

### 1.2 Display Surfaces (7 commands)

| Command | Current Approach | Key Display Elements |
|---------|-----------------|---------------------|
| `otto run` | Rich Live footer + scrolling log | Phase progress, tool calls, QA findings, verify results, task summary |
| `otto add` | `click.echo()` + ANSI | Spec generation progress, criteria list |
| `otto add -f` | `click.echo()` + ANSI | Batch import progress, task list |
| `otto status` | Raw ANSI table | Task table, live phase from `live-state.json` |
| `otto status -w` | ANSI table + cursor escape overwrite | Auto-refresh status table every 2s |
| `otto show <id>` | `click.echo()` + ANSI | Task details, spec, diff, QA, verify, agent log, timing |
| `otto logs <id>` | `click.echo()` + ANSI | Structured log viewer with sections |
| `otto logs -f` | Polling loop + `click.echo()` | Real-time log tail |
| `otto history` | `click.echo()` + ANSI | Run history table |
| `otto init` | `click.echo()` + ANSI | Config creation confirmation |
| `otto diff <id>` | `subprocess.run(git show)` | Raw git diff output |
| `otto arch` | `click.echo()` + ANSI | Analysis progress, file list |
| `otto reset` | `click.echo()` + ANSI | Cleanup confirmation and summary |
| `otto bench run` | `click.echo()` + ANSI | Benchmark progress and results |

### 1.3 Current Problems

1. **Mixed styling systems**: Rich in display.py/runner.py, raw ANSI in cli.py -- inconsistent look
2. **No Rich dependency declared**: `rich` is imported but not in `pyproject.toml`
3. **Status table is raw ANSI**: Manual column padding, no proper table formatting
4. **Watch mode uses cursor hacks**: `\033[{N}A\r` for overwrite instead of Rich Live
5. **`otto show` is wall-of-text**: No visual grouping, no panels, no syntax highlighting
6. **`otto logs` raw formatting**: No syntax highlighting for code in logs, no diff coloring
7. **No consistent theme**: Colors chosen ad-hoc across files
8. **No non-TTY fallback declared**: Rich auto-detects but no explicit handling for piped output
9. **`otto run` live footer is minimal**: Single line with phase + timer, could show more
10. **No progress indication for spec generation**: Just "Generating spec..." text with no spinner


---

## 2. Modern CLI Display Patterns

### 2.1 Docker BuildKit

**Pattern**: Parallel build steps with real-time status updates.

```
 => [internal] load .dockerignore                          0.0s
 => [internal] load build definition from Dockerfile       0.0s
 => [1/4] FROM python:3.11-slim@sha256:abc...              0.0s
 => [2/4] COPY requirements.txt .                          0.1s
 => [3/4] RUN pip install -r requirements.txt             12.3s
 => [4/4] COPY . .                                         0.2s
 => exporting to image                                     0.5s
```

**Key patterns**:
- **Prefix icons**: `=>` for in-progress, checkmark for done
- **Step numbering**: `[1/4]`, `[2/4]` shows position in sequence
- **Right-aligned timing**: Duration pinned to right edge
- **Parallel visibility**: Multiple steps shown simultaneously
- **Collapse on completion**: Finished steps show final time, active steps animate
- **Vertex grouping**: Related sub-steps collapse under a parent
- **TTY vs Plain modes**: Degrades to line-by-line for non-TTY

**Applicable to otto**: Phase progress during `otto run` -- each phase (prepare, coding, test, qa, merge) is analogous to a Docker build step.

### 2.2 Turborepo / pnpm

**Pattern**: Parallel workspace task execution with configurable output modes.

Turborepo output modes:
- **full**: All logs from all tasks
- **hash-only**: Just cache hashes (minimal)
- **new-only**: Only cache misses (avoid noise from cached)
- **errors-only**: Only failures (CI-friendly)
- **none**: Silent

Log ordering:
- **stream**: Interleaved real-time output (prefixed with `package:task`)
- **grouped**: Aggregated per-task (wait for completion, then dump)
- **auto**: Stream for TTY, grouped for CI

**Key patterns**:
- **Prefixed output**: `app:build: Compiling...` clearly attributes output to source
- **Aggregated vs streaming modes**: User chooses based on use case
- **Machine-readable NDJSON**: `--json` flag for structured output
- **Dry run mode**: Shows what would happen without executing

**Applicable to otto**: Multi-task runs benefit from grouped/streamed modes. `otto run --output=errors-only` for CI. NDJSON already exists in `pilot_results.jsonl` -- could be a first-class output mode.

### 2.3 Vercel CLI

**Pattern**: Deployment progress with semantic output levels.

```
Vercel CLI 34.2.0
> Deploying ~/my-app
> Building...
> Ready! Deployed to https://my-app.vercel.app [42s]
```

**Key patterns**:
- **Semantic methods**: `log()`, `warn()`, `ready()`, `success()`, `error()`, `debug()`
- **Spinner with delay threshold**: Only show spinner after 300ms (avoid flash for fast ops)
- **Color stripping when NO_COLOR**: Also strips emoji for clean fallback
- **Hyperlink support detection**: Clickable URLs when terminal supports them
- **Timing on completion**: Duration shown only on success

**Applicable to otto**: The delayed spinner pattern is excellent -- don't show a spinner for fast operations. Semantic output methods map directly to otto's needs.

### 2.4 Claude Code

**Pattern**: Tool calls with thinking indicators and streaming results.

```
  Read file.py

  I'll analyze the code...

  Edit file.py
    - old line
    + new line

  Bash npm test
    PASS  src/tests/app.test.js
    Tests: 3 passed, 3 total
```

**Key patterns**:
- **Tool name as header**: Bold tool name, detail inline (file path, command)
- **Diff display**: Red/green for edit changes
- **Thinking text**: Dimmed, collapsible
- **Streaming results**: Output appears as it arrives
- **Error highlighting**: Red for failures, with context
- **Minimal chrome**: No boxes or panels -- just indented text

**Applicable to otto**: Already partially adopted in display.py. The minimal-chrome approach works well for scrolling logs. Panels/tables should be reserved for summary/dashboard views, not streaming output.

### 2.5 uv (Python package manager)

**Pattern**: Multi-phase operations with per-phase timing.

```
Resolved 2 packages in 170ms
Built example @ file:///home/user/example
Prepared 2 packages in 627ms
Installed 2 packages in 1ms
  + example==0.1.0
  + ruff==0.5.0
```

**Key patterns**:
- **Phase completion as permanent log lines**: Each phase prints when done
- **Timing per phase**: Shows bottleneck identification
- **Change list with icons**: `+` prefix for additions
- **No progress bars**: Just timing after completion (fast enough to not need bars)

**Applicable to otto**: This is already close to otto's current approach. The phase-done lines with timing are well-suited to otto's pipeline.

### 2.6 Fly.io / Railway CLI

**Key patterns**:
- **Default streaming with `--detach` for async**: Deploy blocks by default, shows progress
- **Build log streaming with health check status**: Visual progression through deploy phases
- **Interactive vs non-interactive modes**: `-y` flag for CI
- **Colored output with NO_COLOR support**: Graceful degradation

### 2.7 GitHub Actions (nested steps)

**Pattern**: Hierarchical task > step > output display.

```
  Build
    Setup Node.js
      Run actions/setup-node@v4
      node --version → v20.11.0
    Install dependencies
      Run npm ci
      added 847 packages in 12s
    Run tests
      Run npm test
      PASS src/app.test.js (3.2s)
    Build
      Run npm run build
      dist/main.js (142 KB)
  Deploy
    ...
```

**Key patterns**:
- **Collapsible groups**: Failed steps auto-expand, passed steps collapse
- **Timing per step**: Duration shown on completion
- **Status annotations**: `::warning::` and `::error::` for structured feedback
- **Group nesting**: Job > Step > Command output

**Applicable to otto**: The hierarchy maps perfectly: Run > Task > Phase > Tool calls. Failed phases should auto-expand to show details; passed phases can collapse to one line.


---

## 3. Rich Library Capabilities Mapped to Otto's Needs

### 3.1 Component Inventory

| Rich Component | Otto Use Case | Priority |
|---------------|--------------|----------|
| **Console** | Central output, theme, non-TTY detection | Must-have (already used) |
| **Table** | `otto status`, `otto history`, run summary | High -- replaces ANSI tables |
| **Panel** | `otto show` sections, run header/footer | High -- adds visual grouping |
| **Tree** | Dependency graph, phase hierarchy | Medium -- for `otto show` deps |
| **Live** | `otto status -w`, `otto run` footer | Must-have (already used) |
| **Progress** | Spec generation, architect, long operations | Medium -- nice for multi-step |
| **Spinner/Status** | Phase in-progress indicator | Medium -- already have Live footer |
| **Syntax** | `otto diff`, `otto logs` code display | Low-medium -- nice for diffs |
| **Group** | Combining renderables in Live | Already used |
| **Text** | Styled text construction | Already used |
| **Columns** | Side-by-side layout in show/status | Low |
| **Layout** | Full-screen dashboard | Low -- overkill for most commands |
| **Theme** | Consistent color scheme | High -- unifies styling |
| **Logging handler** | Structured debug logging | Low -- current approach is fine |

### 3.2 Rich Table for Status

Current `otto status` output uses manual ANSI column padding. Rich Table provides:

```python
from rich.table import Table
from rich import box

table = Table(
    title="Tasks",
    box=box.SIMPLE_HEAD,  # Clean header line, no side borders
    show_edge=False,
    padding=(0, 1),
    expand=True,
)
table.add_column("#", justify="right", style="bold", width=4)
table.add_column("Status", width=10)
table.add_column("Att", justify="right", width=3)
table.add_column("Spec", justify="right", width=4)
table.add_column("Cost", justify="right", width=7)
table.add_column("Time", justify="right", width=6)
table.add_column("Prompt", ratio=1, no_wrap=True)

# Color status cells
status_style = {"passed": "green", "failed": "red", "running": "cyan", "pending": "dim"}
```

**Benefits**: Automatic column width calculation, proper alignment, word wrapping, consistent borders.

### 3.3 Rich Panel for `otto show`

The current wall-of-text `show` command could use Panels for visual grouping:

```python
from rich.panel import Panel
from rich import box

# Header panel
console.print(Panel(
    f"[bold]Task #{task_id}[/bold]  {prompt}",
    subtitle=f"{status} | {attempts} attempts | {cost} | {duration}",
    box=box.ROUNDED,
    expand=True,
))

# Spec panel
spec_text = "\n".join(f"  {icon} {text}" for icon, text in spec_items)
console.print(Panel(spec_text, title="Spec", box=box.SIMPLE))

# QA panel (if exists)
console.print(Panel(qa_summary, title="QA Results", box=box.SIMPLE))
```

### 3.4 Rich Tree for Dependencies

```python
from rich.tree import Tree

tree = Tree("[bold]Task #1[/bold] Add search")
dep = tree.add("[dim]depends on[/dim]")
dep.add("[green]#2[/green] Setup database [dim](passed)[/dim]")
dep.add("[yellow]#3[/yellow] Add API routes [dim](pending)[/dim]")
```

### 3.5 Rich Progress for Multi-Step Operations

For `otto add -f` (importing many tasks) or `otto arch`:

```python
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

with Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TextColumn("{task.completed}/{task.total}"),
    transient=True,
) as progress:
    task = progress.add_task("Generating specs", total=len(prompts))
    for prompt in prompts:
        spec = generate_spec(prompt, project_dir)
        progress.advance(task)
```

### 3.6 Rich Live for Watch Mode

Replace cursor-hack watch mode with proper Rich Live:

```python
from rich.live import Live

def render_status():
    table = build_status_table()
    return table

with Live(render_status(), refresh_per_second=2) as live:
    while True:
        live.update(render_status())
        time.sleep(2)
```

### 3.7 Rich Theme for Consistency

Define otto's color scheme once:

```python
from rich.theme import Theme

OTTO_THEME = Theme({
    "otto.phase": "bold cyan",
    "otto.pass": "green",
    "otto.fail": "red",
    "otto.warn": "yellow",
    "otto.dim": "dim",
    "otto.cost": "dim magenta",
    "otto.time": "dim blue",
    "otto.tool.read": "dim",
    "otto.tool.write": "green",
    "otto.tool.edit": "yellow",
    "otto.tool.bash": "cyan",
    "otto.tool.qa": "magenta",
    "otto.header": "bold",
    "otto.separator": "dim",
    "otto.task.id": "bold",
    "otto.task.prompt": "",
    "otto.pilot": "dim cyan",
})

console = Console(theme=OTTO_THEME, highlight=False)
```

### 3.8 Non-TTY / Piped Output Handling

Rich automatically strips control codes when not writing to a terminal. Key behaviors:

- `console.is_terminal` -- read-only property, True when output goes to TTY
- When piped: no colors, no spinners, no Live updates
- `NO_COLOR` env var: disables color even in TTY
- `FORCE_COLOR` env var: enables color even when piped
- `force_terminal=True`: override for testing

**Recommendation**: No special code needed -- Rich handles this. But:
1. Avoid emoji in piped output (strip when `not console.is_terminal`)
2. Don't use Live/Progress when not in TTY (they degrade but may still confuse)
3. Consider a `--json` flag for machine-readable output


---

## 4. Multi-Agent Display Patterns

### 4.1 Current State

Otto currently runs tasks sequentially via the pilot. Each task goes through: prepare -> coding -> test -> qa -> merge. The display shows one task at a time with a scrolling log and Live footer.

### 4.2 Parallel Task Display Options

If otto moves to parallel task execution, several patterns are possible:

#### Option A: Grouped Output (Turborepo-style)

```
  Task #1 — Add search functionality
    coding  [running 45s]
  Task #2 — Setup database schema
    coding  [running 32s]
  Task #3 — Add API routes
    pending (depends on #2)

  ─────────────────────────────
  Progress: 0/3 passed  $0.00  1m17s
```

- Pros: Simple, shows all tasks at a glance, works well with Rich Live
- Cons: Loses per-task detail (tool calls, QA findings)
- Implementation: Rich Table inside Rich Live, refresh every 2s

#### Option B: Streaming with Prefixed Output (pnpm-style)

```
  [#1] coding
  [#1]   + src/search.py
  [#1]   ~ src/app.py
  [#2] coding
  [#2]   $ npm install fuse.js
  [#1] test
  [#1]   PASS (12 tests)
  [#2]   + src/db/schema.sql
```

- Pros: Shows all detail, real-time, good for debugging
- Cons: Interleaved output can be confusing with many tasks
- Implementation: Prefix each line with task ID, color-code per task

#### Option C: Active Task Focus + Summary Bar (Recommended)

```
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Task #1  Add search functionality
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    coding
      + src/search.py
      ~ src/app.py
      $ npm test
    test
      PASS (12 tests)

  ──────────────────────────────────────────────────
  #1 coding 45s  |  #2 coding 32s  |  #3 pending
```

- Pros: Full detail for active task, summary bar for parallel awareness
- Cons: Can only show one task's detail at a time
- Implementation: Rich Live footer with multi-task summary, scrolling log for focused task

#### Recommendation

**Option C** is the best fit for otto because:
1. Most information density comes from the currently-active task
2. Parallel tasks are "fire and forget" -- their detail matters only on failure
3. A compact status bar gives situational awareness without overwhelming
4. It's closest to the current architecture (minimal migration)
5. Failed tasks can auto-expand their detail section

### 4.3 Task Dependency Graph Visualization

For `otto show` or `otto status`:

```
  #1 Setup database     PASSED   ──┐
  #2 Add API routes     PASSED   ──┤──> #4 Integration tests  PENDING
  #3 Add search         RUNNING  ──┘
```

Rich Tree can represent this, but a simple ASCII dep indicator in the table is more practical:

```
  #  Status    Deps    Prompt
  1  passed            Setup database
  2  passed    1       Add API routes
  3  running   1       Add search
  4  pending   1,2,3   Integration tests
```


---

## 5. AI Observability Patterns

### 5.1 What an AI Monitoring Otto Needs

If another AI agent (or a human dashboard) is watching otto, it needs:

1. **Structured progress events**: Already exists in `pilot_results.jsonl`
2. **Phase transitions with timestamps**: Already emitted
3. **Cost tracking per phase**: Partially exists (total cost per task, not per phase)
4. **Error classification**: Distinguish "spec not met" from "test infrastructure broken" from "agent confused"
5. **Doom loop detection signals**: Repeated same-error, escalating costs without progress
6. **Token/turn usage**: How many turns did the agent take? Approaching context limit?

### 5.2 Machine-Readable Output Mode

```
otto run --output=json
```

Would emit NDJSON to stdout:

```json
{"event":"run_start","tasks":3,"timestamp":"2024-03-21T10:00:00Z"}
{"event":"task_start","task_id":1,"key":"abc123","prompt":"Add search"}
{"event":"phase","task_key":"abc123","name":"coding","status":"running"}
{"event":"agent_tool","task_key":"abc123","name":"Write","detail":"src/search.py"}
{"event":"phase","task_key":"abc123","name":"coding","status":"done","time_s":45.2,"cost":0.12}
{"event":"task_done","task_id":1,"success":true,"cost_usd":0.45,"duration_s":120}
{"event":"run_done","passed":3,"failed":0,"cost_usd":1.35,"duration_s":360}
```

This is essentially `pilot_results.jsonl` promoted to a first-class output format.

### 5.3 Cost Visualization

```
  Task #1  $0.45   ██████████░░░░░  (30% of run)
  Task #2  $0.82   ████████████████████████░░  (55% of run)
  Task #3  $0.23   █████░░░░░░░░░░  (15% of run)
  ─────────────────────────────────
  Total:   $1.50
```

Rich Progress bars can render this in the summary.


---

## 6. Concrete Design Proposals per Command

### 6.1 `otto run` (the main event)

**Header**:
```
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    otto run — 3 tasks, 12 specs
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ○ #1  Setup database (5 spec)
    ○ #2  Add API routes (4 spec) → #1
    ○ #3  Add search (3 spec)
```

**Per-task execution** (mostly unchanged, already good):
```
  ──────────────────────────────────────────────────────
  🚀 Running task  #1 Setup database

  coding
      + src/db/schema.sql
      ~ src/db/connection.py
      $ python -m pytest tests/
  ✓ coding      23s  $0.08
  test
      ✓ tier-1 (8 tests)
  ✓ test        5s   $0.02
  qa
      ✓ Spec 1: Tables exist with correct schema
      ✓ Spec 2: Connection pooling configured
      ✓ Spec 3: Migrations run cleanly
  ✓ qa          12s  $0.05  3/3 specs passed
  ✓ merge       2s
```

**Live footer** (enhanced):
```
  ● coding  0:45  $0.12  │  #2 pending  #3 pending
```

**Summary** (enhanced with Rich Table):
```
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Run complete  4m23s  $1.50
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✓ #1  Setup database          42s  $0.15
        23s coding · 5s test · 12s qa · 2s merge
  ✓ #2  Add API routes          1m12s  $0.52
        45s coding · 8s test · 15s qa · 4s merge
  ✗ #3  Add search              2m29s  $0.83
        1m45s coding · 12s test (FAIL) · 30s qa · 2s merge
        ↳ test tier-1 failed: 2/8 tests failing

  3/3 tasks passed  $1.50
```

### 6.2 `otto status`

**Replace ANSI table with Rich Table**:
```
  ╭─────────────────────────────────────────────────────╮
  │  #  Status    Att  Spec   Cost    Time   Prompt     │
  ├─────────────────────────────────────────────────────┤
  │  1  passed      1     5  $0.15     42s   Setup da…  │
  │  2  passed      1     4  $0.52   1m12s   Add API…   │
  │  3  failed      3     3  $0.83   2m29s   Add sear…  │
  │     ↳ test tier-1 failed: 2/8 tests failing         │
  ╰─────────────────────────────────────────────────────╯
    2 passed, 1 failed — $1.50, 4m23s
```

Actually, a simpler box style works better for status tables:

```
   #  Status    Att  Spec   Cost    Time  Prompt
  ── ──────── ──── ───── ─────── ────── ─────────────────
   1  passed      1     5  $0.15    42s  Setup database
   2  passed      1     4  $0.52  1m12s  Add API routes
   3  failed      3     3  $0.83  2m29s  Add search
      ↳ test tier-1 failed: 2/8 tests failing
  ─────────────────────────────────────────────────────
  2 passed, 1 failed — $1.50, 4m23s
```

Use `box=box.SIMPLE_HEAD` or `box=None` with `show_header=True`.

### 6.3 `otto status -w`

**Replace cursor hacks with Rich Live**:

```python
def render_dashboard():
    table = build_status_table()  # Rich Table
    live_info = build_live_phase_info()  # Current phase details
    return Group(table, live_info)

with Live(render_dashboard(), refresh_per_second=1, console=console) as live:
    while True:
        live.update(render_dashboard())
        time.sleep(2)
```

### 6.4 `otto show <id>`

**Use Panels for visual sections**:
```
  ╭──────────────────────────────────────────────────────╮
  │  Task #1  Setup database schema                      │
  │  Status: passed  │  Attempts: 1  │  Cost: $0.15      │
  │  Time: 42s (23s coding · 5s test · 12s qa · 2s merge)│
  ╰──────────────────────────────────────────────────────╯

  Spec (5 criteria — 4 verifiable, 1 visual)
  ── ─────────────────────────────────────────
   1  [v] Tables exist with correct column types
   2  [v] Connection pooling with max 10 connections
   3  [v] Migration up/down works cleanly
   4  [v] Foreign key constraints enforced
   5  [~] Schema naming follows project conventions

  Files changed
  ── ─────────────────────────────────────────
   src/db/schema.sql      | +45
   src/db/connection.py   | +12 -3
   tests/test_database.py | +28

  QA: 5/5 specs passed
  Verify: PASSED (8 tests)
  Commit: abc1234 otto: setup database schema (#1)
```

### 6.5 `otto logs <id>`

**Add Rich Syntax highlighting for code/diffs, section panels**:

```
  Logs for Task #1  (abc123def456)

  ┌ Verification ─────────────────────────────
  │  Attempt 1: PASS (8 tests)
  └────────────────────────────────────────────

  ┌ Agent Activity ───────────────────────────
  │  Attempt 1 — 15 tool calls:
  │    Read src/db/schema.sql
  │    Write src/db/schema.sql
  │    Edit src/db/connection.py
  │    Bash python -m pytest tests/test_db.py
  │    ... (11 more)
  └────────────────────────────────────────────

  ┌ QA Report ────────────────────────────────
  │  ✓ Spec 1: Tables exist
  │  ✓ Spec 2: Connection pooling
  │  ...
  └────────────────────────────────────────────
```

### 6.6 `otto add`

**Add spinner for spec generation**:

```
  ⠋ Generating spec...

  ✓ Spec (5 criteria — 4 verifiable, 1 visual):
    [v] ✓ Tables exist with correct column types
    [v] ✓ Connection pooling with max 10 connections
    [v] ✓ Migration up/down works cleanly
    [v] ✓ Foreign key constraints enforced
    [~] ◉ Schema naming follows project conventions

  ✓ Added task #1 (abc123def456): Setup database schema
```

### 6.7 `otto history`

**Rich Table with row coloring**:

```
  Date                Tasks  Pass  Fail     Cost     Time
  ─────────────────── ───── ───── ───── ──────── ────────
  2024-03-21 10:00      3     3     0    $1.50    4m23s
  2024-03-20 15:30      5     4     1    $3.20   12m05s
                                         ↳ task #3 failed: test timeout
  2024-03-19 09:15      2     2     0    $0.80    2m10s
```


---

## 7. Recommended Approach

### 7.1 Phase 1: Foundation (prerequisite for all)

1. **Add `rich>=13.0` to pyproject.toml dependencies**
2. **Create `otto/theme.py`**: Define `OTTO_THEME` and a shared `console` instance
3. **Migrate display.py console**: Use themed console from theme.py
4. **Migrate cli.py from ANSI to Rich**: Replace `_B`, `_D`, `_G`, etc. with Rich markup

### 7.2 Phase 2: Static Commands

5. **`otto status`**: Replace ANSI table with Rich Table
6. **`otto status -w`**: Replace cursor hacks with Rich Live
7. **`otto show`**: Add Panel grouping for sections
8. **`otto history`**: Replace ANSI table with Rich Table
9. **`otto logs`**: Add section headers with Rich markup

### 7.3 Phase 3: Dynamic Commands

10. **`otto add`**: Add Rich Status spinner for spec generation
11. **`otto run` summary**: Use Rich Table for the run summary
12. **`otto run` header**: Better run header with task overview
13. **`otto run` footer**: Enhanced live footer with multi-task awareness

### 7.4 Phase 4: Polish

14. **Non-TTY handling**: Test and document behavior when piped
15. **`--json` output mode**: First-class machine-readable output for `otto run` and `otto status`
16. **Syntax highlighting**: For `otto diff` and `otto logs` code sections


---

## 8. Open Questions and Trade-offs

### 8.1 Rich as mandatory dependency?

**Pro**: Already imported, provides massive UX improvement, widely used (30M+ monthly downloads).
**Con**: Adds a dependency to a minimal CLI tool.
**Decision**: Add it. It's already imported -- this just makes it explicit. The dependency is well-maintained and has no transitive dependencies that would cause conflicts.

### 8.2 Panels vs. minimal chrome?

The scrolling log during `otto run` should remain minimal (indented text, no panels). Panels work for summary/detail views (`show`, `status`, summary). Mixing panels into streaming output creates visual noise.

**Rule of thumb**: Panels for static views, indented text for streaming views.

### 8.3 How to handle Live + console.print() during `otto run`?

Current approach works well: Rich Live footer stays pinned at bottom, `console.print()` output scrolls above it. This is Rich's built-in behavior and is the right pattern.

One enhancement: use `get_renderable` callback for the footer (already done) so it auto-refreshes without needing explicit `live.update()` calls.

### 8.4 Full-screen Layout for dashboard?

Rich Layout can create full-screen dashboards:
```
  ┌─────────────────┬──────────────────┐
  │  Task Status     │  Current Phase   │
  │  #1 passed       │  coding 0:45     │
  │  #2 running      │  + file.py       │
  │  #3 pending      │  ~ other.py      │
  ├─────────────────┴──────────────────┤
  │  Recent Log Output                  │
  │  ...                                │
  └─────────────────────────────────────┘
```

**Decision**: Not for v1. This is appropriate for `otto status -w` eventually, but overkill for the initial redesign. The scrolling-log-with-footer pattern is simpler and more robust.

### 8.5 Should `otto run` hide passed phase details?

BuildKit collapses completed steps. Should otto collapse passed phases?

**No**. Otto runs are short enough (minutes, not hours) that seeing all phases is valuable. The scrollback serves as a record. If output gets too long (many tasks), consider collapsing per-task detail in the summary but keeping full detail in the scrolling log.

### 8.6 Emoji handling in non-TTY?

Current code uses Unicode characters (checkmarks, bullets) and some emoji. When piped:
- Unicode characters (checkmark, cross, bullet) are fine -- they're in the basic multilingual plane
- Emoji (rocket, clipboard) may not render in all contexts

**Decision**: Keep Unicode symbols (they're widely supported). Avoid emoji in log lines that will be captured. If `not console.is_terminal`, use ASCII alternatives for any remaining emoji.

### 8.7 Thread safety concerns?

Rich Console is thread-safe by design. The current `TaskDisplay._lock` protects shared state (counters, phase tracking) correctly. No changes needed for thread safety when migrating to more Rich components.

### 8.8 Performance of Rich Live refresh?

Rich Live at 2 refreshes/second (current setting) is negligible overhead. Even at 10/second it's fine. The footer renderable is a single Text line -- rendering cost is microseconds. No concern here.

### 8.9 Backward compatibility?

Any display changes are purely visual -- no API changes. The JSONL side-channel format should remain stable. The `--json` output mode (if added) should be additive.


---

## 9. Summary of Key Recommendations

1. **Add `rich` to pyproject.toml** -- it's already imported, make it official
2. **Create a shared theme** -- unify colors across all commands
3. **Replace ANSI tables with Rich Table** -- `status`, `history`, run summary
4. **Use Rich Live for watch mode** -- replace cursor escape hacks
5. **Add Panels to `otto show`** -- visual grouping for the detail view
6. **Add spinners to `otto add`** -- feedback during spec generation
7. **Keep the scrolling-log-with-footer pattern for `otto run`** -- it works, just enhance the footer
8. **Plan for multi-agent display** -- status bar with per-task summary, detail for active task
9. **Consider `--json` output mode** -- promote existing JSONL to first-class
10. **Don't over-decorate** -- otto's value is in results, not in CLI chrome
