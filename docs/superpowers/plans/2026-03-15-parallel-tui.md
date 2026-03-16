# Parallel Task TUI — Rich Live Panels

**Date:** 2026-03-15
**Status:** TODO
**Depends on:** Task dependencies & parallel execution (implemented)

## Problem

When otto runs tasks in parallel, verbose output (agent streaming, tool use, verification) from all tasks goes to the same terminal. Current fix: suppress verbose output in parallel mode, show only compact status lines. This loses visibility into what agents are doing.

## Goal

A `rich.Live` TUI where each parallel task gets its own panel ("workzone box"). Output streams independently per task, no interleaving. Toggle key to expand/collapse detail level.

## Design

### Visual Layout

```
⚡ Running 3 tasks in parallel                     [v] verbose  [1-9] focus
┌─ #1 Add user model ──────────────────── running (att 1/4) ───────────────────┐
│  ● Edit  user.py                                                             │
│    - class User:                                                             │
│    + class User(BaseModel):                                                  │
│  ● Bash  pytest tests/test_user.py                                           │
└──────────────────────────────────────────────────────────────────────────────┘
┌─ #2 Add search ──────────────────────── testgen ─────────────────────────────┐
│  Testgen agent writing adversarial tests (6 criteria)...                     │
│  ● Read  store.py                                                            │
└──────────────────────────────────────────────────────────────────────────────┘
┌─ #3 Add favorites ─────────────────── ✓ PASSED (45s, $0.12) ────────────────┐
│  Merged to main                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Modes

1. **Compact** (default): Each task = 1 line status. Like current parallel output.
2. **Panel** (toggle `v`): Each task = bordered panel, last N lines of output, scrolling.
3. **Focus** (press `1`-`9`): One task expanded full-screen, others collapsed to 1 line. Like `tmux zoom`.

### Architecture

```
TaskDisplay (per task)
├── ring_buffer: deque[str]  (last 50 lines)
├── status: str  (pending/testgen/running/verifying/passed/failed)
├── attempt: int
├── duration: float
└── cost: float

ParallelRenderer
├── displays: dict[int, TaskDisplay]
├── mode: compact | panel | focus
├── focus_task_id: int | None
├── live: rich.Live
└── render() → rich.Layout
```

### Implementation

#### 1. `otto/display.py` — TaskDisplay + ParallelRenderer

- `TaskDisplay` wraps a `deque(maxlen=50)` ring buffer
- `write(msg)` appends to buffer, updates timestamp
- `render_panel() → rich.Panel` renders the box
- `render_compact() → str` renders one-line status

- `ParallelRenderer` manages the `rich.Live` context
- `start()` enters Live context, starts stdin reader for keypresses
- `stop()` exits Live context, restores terminal
- `render()` builds a `rich.Layout` from all TaskDisplays based on mode

#### 2. Keypress detection

Use `asyncio.get_event_loop().add_reader(sys.stdin, callback)` with terminal in raw mode:
```python
import tty, termios
old_settings = termios.tcgetattr(sys.stdin)
tty.setcbreak(sys.stdin.fileno())  # single char, no echo
loop.add_reader(sys.stdin.fileno(), on_keypress)
# restore in finally: termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
```

Keys: `v` toggle panel/compact, `1`-`9` focus task, `q`/`Esc` back to compact.

#### 3. Integration with `run_task`

In parallel mode, `run_task` receives a `TaskDisplay` instance instead of printing to stdout:

```python
async def run_task(task, config, project_dir, tasks_file,
                   work_dir=None, display=None):
    # ...
    if display:
        display.write(f"● {block.name}  {detail}")
    else:
        print(...)
```

All existing `print()`, `_log_pass()`, `_log_fail()`, `_log_verify()` calls route through the display when available.

#### 4. Integration with `run_all`

```python
if len(runnable) > 1 and max_parallel > 1:
    renderer = ParallelRenderer()
    for t in runnable:
        renderer.add_task(t["id"], t["prompt"])

    async with renderer:
        coros = [_run_in_worktree(t, base_sha, renderer.displays[t["id"]])
                 for t in runnable]
        results = await asyncio.gather(*coros, return_exceptions=True)
```

#### 5. Dependency: `rich`

Add `rich` to project dependencies. It's already widely used in Python CLIs and has zero native deps.

### Serial mode — unchanged

`otto run --no-parallel` and single-task execution continue to use direct `print()` with full streaming output. No TUI overhead.

### Files

| File | Change |
|------|--------|
| `otto/display.py` | `TaskDisplay`, `ParallelRenderer` (new) |
| `otto/runner.py` | `run_task` accepts `display` param, routes output |
| `otto/runner.py` | `run_all` creates renderer for parallel batches |
| `pyproject.toml` | Add `rich` dependency |

### Verification

1. Serial mode unchanged — `otto run --no-parallel` streams normally
2. Parallel mode shows panels — 3 independent tasks render in boxes
3. Toggle `v` switches compact ↔ panel
4. Focus `1` expands task #1 full-screen
5. Completed tasks collapse to one-line result
6. `Ctrl+C` gracefully tears down TUI and cleans up

### Risks

- `rich.Live` refresh rate vs asyncio event loop — may need `Live(refresh_per_second=4)` tuning
- Terminal size detection — panels need to handle narrow terminals gracefully
- Agent SDK output that bypasses our print path — may need subprocess stdout capture
- SSH/non-TTY environments — detect `sys.stdout.isatty()`, fall back to compact mode
