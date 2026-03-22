# Display Polish — UX-First Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make otto's display feel as clean and intentional as Claude Code's — responsive, glanceable, informative without noise.

**Design Principle:** Otto should feel like watching a skilled engineer work. You see what they're doing, how far along they are, what they found. No debug dumps, no raw markdown, no UI jank.

---

## Design Language

### Information Hierarchy (4 levels)

| Level | Style | Example |
|-------|-------|---------|
| **L1 — Structure** | Bold, full-brightness | Phase names, task headers, pass/fail verdict |
| **L2 — Key data** | Normal weight, color-coded | File names, test counts, cost, timing |
| **L3 — Supporting detail** | Dim | Tool commands, spec descriptions, paths |
| **L4 — Noise** | Hidden or suppressed | Internal files, pilot narration, repeated reads |

### Color Palette (semantic, not decorative)

| Color | Meaning | Usage |
|-------|---------|-------|
| Green | Success / creation | ✓, PASS, Write (+) |
| Yellow | Modification / warning | Edit (~), retries, baseline issues |
| Red | Failure / error | ✗, FAIL, errors |
| Cyan | Active / in-progress | Current phase, phase headers |
| Magenta | QA / verification | QA tool calls, spec checks |
| Dim | Secondary info | Paths, timing, cost, commands |
| Bold | Structural emphasis | Phase completion, task headers |

### Spacing Rules

- **Between phases**: 0 blank lines (phases flow continuously)
- **Before task header**: 1 blank line + separator
- **Tool calls**: 6-space indent, no blank lines between
- **QA findings**: 6-space indent for spec name, 8-space for detail
- **After task completion**: 0 blank lines (next section follows immediately)

### Path Display

- Strip project root: `/private/tmp/weatherapp/src/lib/api.ts` → `src/lib/api.ts`
- For Write/Edit: show relative path (informative for the user)
- For Read: show just filename (less important, reduce noise)
- For Bash: show the command, truncated at 72 chars

---

## Task 1: Foundation — Theme + Shared Console

**Files:**
- Create: `otto/theme.py`
- Modify: `otto/display.py`
- Modify: `pyproject.toml`

### Steps

- [ ] **Step 1:** Add `rich>=13.0` to `pyproject.toml` dependencies

- [ ] **Step 2:** Create `otto/theme.py` with the shared console and theme:
```python
"""Otto display theme — shared console and styling constants."""

from rich.console import Console
from rich.theme import Theme

OTTO_THEME = Theme({
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "info": "cyan",
    "active": "bold cyan",
    "qa": "magenta",
    "dim": "dim",
    "phase.done": "green",
    "phase.fail": "red",
    "phase.running": "bold cyan",
    "tool.write": "green",
    "tool.edit": "yellow",
    "tool.read": "dim",
    "tool.bash": "cyan",
    "tool.qa": "magenta",
    "cost": "dim",
    "timing": "dim",
})

console = Console(highlight=False, theme=OTTO_THEME)
```

- [ ] **Step 3:** Update `otto/display.py` to import console from theme.py instead of creating its own

- [ ] **Step 4:** Update `otto/pilot.py` and `otto/runner.py` imports to use theme console

- [ ] **Verify:** `python -m pytest tests/ -x -q`

---

## Task 2: Migrate cli.py from ANSI to Rich

**Files:**
- Modify: `otto/cli.py`

Replace all `_B`, `_D`, `_G`, `_Y`, `_C`, `_R`, `_0` ANSI codes + `click.echo()` with `console.print()` + Rich markup using the theme. This is a mechanical migration — same content, consistent styling.

### Steps

- [ ] **Step 1:** Import console from theme.py in cli.py. Remove ANSI constants.

- [ ] **Step 2:** Migrate `otto init` output to Rich

- [ ] **Step 3:** Migrate `otto add` output to Rich (including spec display)

- [ ] **Step 4:** Migrate `otto status` to Rich Table:
```python
from rich.table import Table

table = Table(show_header=True, box=None, pad_edge=False, show_edge=False)
table.add_column("#", style="bold", width=3)
table.add_column("Status", width=8)
table.add_column("Att", width=3, justify="right")
table.add_column("Spec", width=4, justify="right")
table.add_column("Cost", width=7, justify="right", style="dim")
table.add_column("Time", width=6, justify="right", style="dim")
table.add_column("Prompt", ratio=1)
```

- [ ] **Step 5:** Migrate `otto show` to Rich Panels:
```python
from rich.panel import Panel
from rich.columns import Columns

header = Panel(
    f"Status: {status}  |  Attempts: {att}  |  Cost: {cost}\n"
    f"Time: {time} ({phase_breakdown})",
    title=f"Task #{task_id}  {prompt[:50]}",
    border_style="dim",
)
console.print(header)
```

- [ ] **Step 6:** Migrate `otto logs`, `otto history`, `otto status -w` to Rich

- [ ] **Step 7:** Remove all ANSI constants (`_B`, `_D`, etc.) and verify no raw ANSI remains

- [ ] **Verify:** `python -m pytest tests/ -x -q` + manual test each command

---

## Task 3: Polish `otto run` Display

**Files:**
- Modify: `otto/display.py`
- Modify: `otto/pilot.py`
- Modify: `otto/runner.py`

This is where the UX matters most. The user watches `otto run` for minutes.

### Steps

- [ ] **Step 1:** Run header — show task overview at start:
```
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    otto run — 3 tasks, 18 specs
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ○ #1  Setup database (5 spec)
    ○ #2  Add API routes (4 spec) → #1
    ○ #3  Add search (3 spec)
```

- [ ] **Step 2:** Enhanced live footer — show multi-task awareness:
```
  ● coding  0:45  $0.12  │  2 pending
```

- [ ] **Step 3:** Structured run summary — replace pilot-generated markdown with otto-controlled Rich Table:
```python
# Don't let the pilot write the summary — otto controls it
summary_table = Table(box=None, show_header=False, pad_edge=False)
summary_table.add_column("", width=3)  # icon
summary_table.add_column("", width=4)  # task id
summary_table.add_column("", ratio=1)  # prompt
summary_table.add_column("", width=7, justify="right")  # time
summary_table.add_column("", width=7, justify="right")  # cost
```

- [ ] **Step 4:** Code diff display during coding — when the agent does an Edit, show a mini diff:
```
      ~ src/components/WeatherApp.tsx
        - import { WeatherDetails } from './WeatherDetails'
        + import { WeatherDetails } from './WeatherDetails'
        + import { PollenPanel } from './PollenPanel'
```
(Only for small edits — skip if diff > 6 lines)

- [ ] **Step 5:** Test result display — parse and show test counts inline:
```
  ✓ test      19s  502 passed
```
Not the raw pytest output string.

- [ ] **Step 6:** Suppress pilot summary markdown — the pilot's `finish_run` text output is noisy and inconsistent. Otto should generate its own summary from task data, not display LLM-generated tables.

- [ ] **Verify:** e2e test on weatherapp with new task

---

## Task 4: Polish `otto add` Display

**Files:**
- Modify: `otto/cli.py`
- Modify: `otto/spec.py` (if needed for progress callbacks)

### Steps

- [ ] **Step 1:** Add Rich Status spinner during spec generation:
```python
with console.status("Generating spec...", spinner="dots"):
    spec_items = generate_spec(prompt, project_dir)
```

- [ ] **Step 2:** Better spec display with alignment:
```
  ✓ Spec (7 criteria — 6 verifiable, 1 visual):
    ✓ AQI data fetched from Open-Meteo API
    ✓ Panel displays numeric AQI value
    ✓ Color-coded category labels
    ✓ Four pollutant cards (PM2.5, PM10, O3, NO2)
    ◉ Translucent card style matches existing panels
    ✓ Panel rendered alongside existing panels
    ✓ Tests exist for API, components, and error handling
```

- [ ] **Step 3:** Show spec agent tool calls during generation (the agent reads files, which takes time — show activity):

- [ ] **Verify:** `otto add 'test task'` then `otto reset`

---

## Task 5: Non-TTY and Edge Cases

**Files:**
- Modify: `otto/display.py`
- Modify: `otto/theme.py`

### Steps

- [ ] **Step 1:** Handle piped output — when `not console.is_terminal`, skip Rich Live footer, use plain console.print() only. All permanent output already works.

- [ ] **Step 2:** Handle `NO_COLOR` env var — Rich respects this automatically, but verify emoji/Unicode fallback.

- [ ] **Step 3:** Handle narrow terminals — test with 80-column terminal, ensure no line wrapping breaks layout.

- [ ] **Verify:** `otto run 2>&1 | tee output.txt` captures all permanent output correctly

---

## Design Decisions

1. **No full-screen TUI** — scrolling log + footer is the right model. Full-screen requires terminal state management (resize, alternate screen) that's fragile.

2. **No panels in streaming output** — panels create visual noise during `otto run`. Reserve panels for static views (`show`, `status`).

3. **Otto controls the summary, not the pilot** — the pilot's LLM-generated markdown is unpredictable. Otto should construct the summary from structured data (tasks.yaml, phase_timings).

4. **Code diffs only for small edits** — showing inline diffs for 2-3 line changes is valuable. Showing 100-line file creates is noise.

5. **Test counts, not raw output** — parse "502 passed" from pytest output, don't show the full pytest banner.

6. **Consistent dim for secondary info** — cost, timing, paths all use dim style. The eye focuses on the structure (phase names, pass/fail).

---

## Verification Criteria

1. `python -m pytest tests/ -x -q` passes after each task
2. `otto run` on weatherapp: clean display, no ANSI artifacts, all phases visible with detail
3. `otto status`: proper Rich Table, aligned columns
4. `otto show <id>`: Panel header, grouped sections
5. `otto run 2>&1 | tee out.txt`: captured output is readable (no transient-only content)
6. 80-column terminal: no broken layout
7. No raw markdown in output (pilot text is filtered/structured)
