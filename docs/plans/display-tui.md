# Otto TUI — Full-Screen Display System

## Why TUI

The scrollback model breaks for parallel tasks. With N agents running, interleaved output is unreadable. A full-screen TUI with per-task panels solves this and also makes single-task display better — always-visible status, scrollable tool history, pinned stats.

CC uses a React-based terminal renderer. Codex CLI uses React+Ink. We use **Textual** — Python, built on Rich, battle-tested, handles resize/SSH/tmux.

## Which Commands Get TUI

Any command that takes >2s with streaming progress:

| Command | TUI Screen | What it shows |
|---------|-----------|---------------|
| `otto run` | RunScreen | Task panels, tool logs, phase progress, QA findings |
| `otto add` | AddScreen | Spec agent activity (file reads, thinking), criteria preview |
| `otto add -f` | AddScreen | Batch spec generation progress, per-task status |
| `otto arch` | AgentScreen | Architect agent exploring codebase |
| `otto status -w` | WatchScreen | Live dashboard, auto-refresh |
| `otto logs -f` | LogScreen | Live log tail |

Instant commands stay Rich console.print: `status`, `show`, `logs`, `history`, `init`, `diff`, `reset`.

## Architecture

```
any long-running otto command
  ├─ is_terminal? → OttoApp with appropriate Screen
  └─ piped/CI?    → PlainOutput (console.print or JSONL)
```

When the TUI exits, it dumps a final summary to normal scrollback so the result persists in terminal history.

### Shared Component Tree

```
OttoApp(App)
├─ Screens (one active at a time)
│   ├─ RunScreen          — the main event (otto run)
│   ├─ AddScreen          — spec generation (otto add)
│   ├─ AgentScreen        — any single-agent run (otto arch)
│   ├─ WatchScreen        — live dashboard (otto status -w)
│   └─ LogScreen          — log tail (otto logs -f)
├─ Reusable Widgets
│   ├─ ToolLog (RichLog)  — scrollable tool call history
│   ├─ PhaseBar (Static)  — phase progress indicators
│   ├─ TaskPanel (Widget) — per-task container with header + ToolLog
│   └─ StatsBar (Static)  — cost, time, model info
└─ Common
    ├─ Keyboard shortcuts (q=quit, 1-9=focus task)
    └─ Theme (from otto/theme.py)
```

## Layout: Single Task

```
╭─ Task #1  Create calc.py ─────────────────────────────────╮
│                                                            │
│  ● Read   README.md                                        │
│  ● Read   package.json                                     │
│                                                            │
│  ● Write  tests/test_calc.py                               │
│    + def test_add():                                       │
│    +     assert add(2, 3) == 5                             │
│    ...38 more lines                                        │
│                                                            │
│  ● Bash   python -m pytest tests/ -v                       │
│    12 passed ✓                                             │
│                                                            │
│  ● Write  calc.py                                          │
│    + def add(a, b):                                        │
│    +     return a + b                                      │
│    ...5 more lines                                         │
│                                                            │
│  ● Edit   tests/test_calc.py                               │
│    - import calc                                           │
│    + from calc import add, subtract                        │
│                                                            │
╰────────────────────────────────────────────────────────────╯
 ✓ prepare · coding 2:34 $0.29 · ○ test · ○ qa · ○ merge
```

- Top: task info (ID, name)
- Middle: scrollable tool call log (RichLog widget)
- Bottom: phase progress bar (fixed, always visible)

## Layout: Multiple Parallel Tasks

```
╭─ #1 API routes (coding) ──────╮╭─ #2 Auth system (coding) ─────╮
│ ● Write  routes.ts             ││ ● Read   middleware.ts         │
│ ● Bash   npm test              ││ ● Write  auth.ts               │
│   12 passed ✓                  ││ ● Bash   npm test              │
│ ● Edit   app.ts                ││   8 passed ✓                   │
│   -2 +15                       ││                                │
╰────────────────────────────────╯╰────────────────────────────────╯
╭─ #3 Search (pending) ─────────╮
│ Waiting for #1, #2...          │
╰────────────────────────────────╯
 #1 coding 2:34 · #2 coding 1:12 · #3 pending │ $0.87 4:15
```

- Each task gets its own panel with scrollable log
- Layout adapts: 1 task=full, 2=split horizontal, 3+=grid or focus+summary
- Pending tasks show dependencies
- Bottom bar: all task statuses + total cost/time

## Layout: QA Phase

When QA runs, the tool log continues in the same panel:

```
│  ✓ coding  4m38s  $1.45  4 files                          │
│                                                            │
│  ✓ test    19s  540 passed                                 │
│                                                            │
│  qa                                                        │
│  ● Read   WindCompass.tsx                                   │
│  ● Bash   npx tsc --noEmit                                 │
│  ● Bash   node -e "// Test rotation math..."               │
│                                                            │
│  ✓ Spec 1: Arrow rotation from wind degrees                │
│  ✓ Spec 2: Cardinal labels (N/E/S/W)                       │
│  ✓ Spec 3: Animated CSS transition                         │
│  ✓ Spec 4: Integrated into WeatherDetails                  │
│  ✓ Spec 5: Tests pass                                      │
│                                                            │
│  ✓ qa     2m35s  5/5 specs passed                          │
```

Phase completions are inline in the scrollable log — they're part of the story.

## Textual Component Tree

```
OttoRunApp(App)
├─ TaskOverview (Static)          # "3 tasks, 24 specs" header
├─ TaskContainer (Horizontal)     # adapts to task count
│   ├─ TaskPanel (Vertical)       # per-task
│   │   ├─ TaskHeader (Static)    # "#1 Create calc.py"
│   │   └─ ToolLog (RichLog)      # scrollable tool call history
│   ├─ TaskPanel ...
│   └─ TaskPanel ...
└─ StatusBar (Static)             # phase progress + cost + time
```

### Key Widgets

- **RichLog**: Scrollable text area that accepts Rich renderables. Perfect for tool call history — append lines, auto-scrolls to bottom.
- **Static**: Fixed content (headers, status bar). Updates via `update()`.
- **Horizontal/Vertical**: Layout containers. Textual CSS handles sizing.

### Textual CSS

```css
TaskPanel {
    border: round $primary;
    height: 1fr;
    min-width: 40;
}

ToolLog {
    height: 1fr;
    scrollbar-size: 1 1;
}

StatusBar {
    dock: bottom;
    height: 1;
    background: $surface;
}
```

## Data Flow

```
JSONL side-channel (pilot_results.jsonl)
    ↓ background reader (200ms poll)
    ↓ _process_progress_event()
    ↓ post_message() to Textual app (thread-safe)
    ↓ on_event handler updates the right TaskPanel
    ↓ Textual re-renders
```

Textual is thread-safe via `post_message()` — the background JSONL reader can safely send events from its thread to the UI thread.

### Event Types → Widget Updates

| Event | Widget | Action |
|-------|--------|--------|
| phase running | StatusBar | Update phase indicator |
| phase done | ToolLog | Append "✓ coding 2:34 $0.29" |
| agent_tool | ToolLog | Append "● Write src/api.ts" + content |
| agent_tool_result | ToolLog | Append test result below last tool |
| qa_finding | ToolLog | Append "✓ Spec 1: description" |

## Exit Behavior

When the run finishes:
1. TUI shows final state for 1 second
2. TUI exits (returns to normal terminal)
3. Print summary to scrollback (same as current `_print_summary`)
4. Summary persists in terminal history

This means after `otto run`, you see the summary in your terminal. During the run, you see the full dashboard. Best of both worlds.

## Non-TTY Fallback

When piped (`otto run 2>&1 | tee output.txt`) or in CI:
- Skip TUI entirely
- Use the current console.print scrollback model
- Or emit JSONL with `--json`

Detection: `sys.stdout.isatty()`

## Implementation Plan

### Phase 1: Single-task TUI (replaces current display)

1. Create `otto/tui.py` with `OttoRunApp` class
2. `TaskPanel` widget with `RichLog` for tool history
3. `StatusBar` widget for phase progress
4. Wire into `run_piloted()` — if TTY, launch TUI; else fallback
5. Background JSONL reader posts events via `post_message()`
6. Exit dumps summary to scrollback

### Phase 2: Multi-task layout

7. `TaskContainer` adapts layout based on task count
8. Multiple `TaskPanel` instances, events routed by task_key
9. Focused view: click/keyboard to expand one panel full-width

### Phase 3: Polish

10. Keyboard shortcuts: q=quit, 1/2/3=focus task, s=toggle summary
11. Mouse scroll within panels
12. Theme consistency with rest of otto
13. `otto status -w` also uses TUI (live dashboard)

## Dependencies

- `textual>=1.0` added to pyproject.toml
- No other new deps (Textual depends on Rich which we already have)

## Risk Assessment

- **Textual maturity**: v8.1, production-ready, 30K+ GitHub stars
- **Terminal compatibility**: Handles xterm, iTerm2, Ghostty, tmux, SSH, Windows Terminal
- **Performance**: Efficient differential rendering, no full redraws
- **Testing**: Textual has its own test framework (`pilot`) for programmatic UI testing
