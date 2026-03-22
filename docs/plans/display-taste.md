# Display Taste — Focused Design Doc

## The Problem

Otto's display looks like debug output, not a product. Despite migrating to Rich and adding inline diffs, the result is inconsistent, noisy, and lacks visual rhythm. CC feels like watching someone work. Otto feels like reading a log.

## Side-by-Side: CC vs Otto Today

### CC running a task:
```
  ⏺ Read  src/components/App.tsx

  ⏺ Write  src/lib/api.ts
    + export async function fetchWeather(city: string) {
    +   const res = await fetch(`/api/weather?q=${city}`)
    +   return res.json()
    + }

  ⏺ Edit  src/components/App.tsx
    - import { useState } from 'react'
    + import { useState, useEffect } from 'react'
    + import { fetchWeather } from '../lib/api'

  ⏺ Bash  npm test
    Tests: 12 passed, 12 total

  ✓ All tests passing
```

What makes it work:
- **One tool = one visual block** with whitespace between
- **Tool name bold, detail dim** — clear hierarchy
- **Results inline** under the tool call
- **No decoration** — no separators, no emoji, no boxes
- **Breathing room** — blank lines between tool calls

### Otto today:
```
  coding
      ● Read  weatherCodes.ts
      ● Read  WeatherDetails.tsx
      ● Read  UVExposureTimer.tsx
      ● Read  weather.ts
      ● Read  jest.config.js
      ● Read  package.json
      ...
      ● Write  __tests__/windCompass.test.tsx
        + /**
        +  * @jest-environment jsdom
        +  */
          ...135 more lines
      ● Bash  windCompass.test.tsx --no-coverage 2>&1 | tail -30
      ● Write  src/components/WindCompass.tsx
```

Problems:
- **Dense wall** — no spacing between tool calls
- **Phase header `coding`** doesn't stand out enough
- **Read calls** are just a pile — no breathing room
- **Bash command truncated** weirdly (`windCompass.test.tsx` not `npx jest`)
- **No test results inline** after Bash
- **QA section uses different style** (`QA  reading` vs `● Read`)

---

## Design Principles

1. **Whitespace is information.** A blank line between tool calls is not wasted space — it's visual separation.
2. **One style for everything.** Read/Write/Edit/Bash look the same whether they're in coding, QA, or any phase.
3. **Show results, not just actions.** A Bash command without its result is half the story.
4. **Suppress noise, not signal.** Don't show 0s phases. Don't show "task task". Don't show emoji.
5. **Panels group related info.** A single stat doesn't need a box. A task detail view (status + spec + files + QA) does.
6. **The terminal is the UI.** No TUI framework needed — just console.print with good spacing and hierarchy.

---

## Specific Fixes

### Fix 1: Breathing room between tool calls

Current: tool calls packed together
```
      ● Read  weatherCodes.ts
      ● Read  WeatherDetails.tsx
      ● Write  windCompass.test.tsx
```

Target: blank line after Write/Edit/Bash (which have content below), not after Read
```
      ● Read  weatherCodes.ts
      ● Read  WeatherDetails.tsx

      ● Write  windCompass.test.tsx
        + export function WindCompass({ degrees }: Props) {
        +   return <div className="compass">...</div>
        + }
          ...45 more lines

      ● Bash  npx jest --no-coverage
        12 passed

      ● Edit  WeatherApp.tsx
        - import { WeatherDetails } from './WeatherDetails'
        + import { WeatherDetails } from './WeatherDetails'
        + import { WindCompass } from './WindCompass'
```

Rule: print a blank line BEFORE Write/Edit/Bash (the "heavy" tools), not after Read.

### Fix 2: Unified tool call style

Kill the `QA` prefix. QA tool calls should look identical to coding tool calls:
```
  qa
      ● Read  WindCompass.tsx
      ● Read  windCompass.test.tsx
      ● Bash  npx tsc --noEmit
      ● Bash  npx jest --no-coverage
```

In runner.py `_run_qa_agent`, emit `name="Read"` not `name="QA"` and `name="Bash"` not `name="QA"`.

### Fix 3: Kill remaining emoji

In pilot.py `_TOOL_DISPLAY`, all emoji are replaced with `●` and `✓`. But there are still references to emoji in the pilot text display and tool call formatting. Audit every `console.print` in pilot.py for emoji.

Also: `📋 Loading task state` still appears — that's from `_TOOL_DISPLAY["get_run_state"]` which was already changed to `●`. Check if the old binary is being used or if there's another code path.

### Fix 4: Remove "task task" redundancy

`● Running task  task 923ac6b6` → `● Running task #19  923ac6b6`

The word "task" appears twice. Show the task number (#19) which is meaningful, and the key for reference.

### Fix 5: Suppress trivial info

- `✓ prepare  0s` → don't show
- `✓ merge  0s` → don't show (already done in display.py, verify it works)
- `0s prepare · 4m38s coding · 19s test · 2m35s qa · 0s merge` → filter out 0s phases: `4m38s coding · 19s test · 2m35s qa`
- Panel around "Run complete" → remove panel, use simple bold line

### Fix 6: Separators — less is more

Current: `──────────` lines everywhere. Used for:
- Before "Pilot taking control"
- Before "Running task"
- Before "Done"
- Before run summary

Target: remove most separators. Use blank lines instead. Keep only ONE separator: before the run summary (the final verdict).

### Fix 7: `otto show` — use panels for grouping

Current: wall of text with no visual grouping
```
  Prompt: ...
  Spec (10 criteria):
    1. [v] ...
    2. [v] ...
  Files changed:
    ...
  QA VERDICT: PASS
  Verify: PASSED
  Agent log:
    ...
  Feedback: ...
```

Target: Rich Panel per section, collapse noise
```
╭─ Task #17  Pollen forecast panel ─────────────────────╮
│  passed  ·  1 attempt  ·  $2.02  ·  9m16s             │
│  5m32s coding · 25s test · 3m18s qa                    │
╰────────────────────────────────────────────────────────╯

  Spec (10 criteria — 9 verifiable, 1 visual)
  ✓ Pollen data fetched from Open-Meteo API
  ✓ Three categories: tree, grass, weed with level bars
  ✓ Color-coded levels (Low/Medium/High/Very High)
  ✓ Overall allergy risk score
  ◉ Card style matches existing panels
  ✓ Integrated into WeatherApp
  ✓ Tests exist and pass

  Files (6 changed, +1097 -1)
  __tests__/pollen.test.ts            +361
  __tests__/pollenPanel.test.tsx      +419
  src/components/PollenForecastPanel   +107
  src/components/WeatherApp.tsx        +19 -1
  src/lib/pollen.ts                    +179
  src/types/weather.ts                  +13

  QA: PASS — all specs verified
  Tests: PASSED (540 tests)
  Commit: 5aabbe16
```

Key changes:
- Panel only for the header (status, cost, time, phase breakdown)
- Spec items: ✓/◉ without `[v]`/`[~]` tags, without item numbers
- Files: compact table with +/- counts
- "Feedback" renamed to "Hint" or hidden entirely
- "Agent log" section removed (use `otto logs` for that)
- No `=====` separator lines in spec text

### Fix 8: Phase summary in `_print_summary` — filter 0s phases

Current: `0s prepare · 4m38s coding · 19s test · 2m35s qa · 0s merge`
Target: `4m38s coding · 19s test · 2m35s qa`

### Fix 9: Bash commands — show full command, not truncated

Current: `● Bash  windCompass.test.tsx --no-coverage 2>&1 | tail -30`
This is truncated and missing `npx jest`. The detail is being shortened wrong.

The issue: the JSONL event `detail` is `_tool_use_summary(block)[:80]` which truncates from the LEFT of the command. Need to preserve the command start and truncate the end.

### Fix 10: Spec display in `otto add` — cleaner

Current:
```
✓ Spec (16 criteria — 15 verifiable, 1 visual):
  ✓ Acceptance Spec: Wind Compass with Animated Arrow
  ✓ Context:
  ✓ Next.js 16 / React 19 / TypeScript app using Tailwind CSS 4
  ✓ Wind data (degrees 0-360, speed in mph) already fetched...
  ✓ getWindDirection() in lib/weatherCodes.ts converts degrees...
  ✓ WeatherDetails.tsx currently shows wind as text only...
  ✓ Existing circular SVG pattern in UVExposureTimer.tsx...
  ✓ Tests use Jest 29 + @testing-library/react 16...
  ✓ Criteria:
  ✓ A wind compass component renders and displays...
```

Problems: The spec includes context lines and headers as separate items ("Context:", "Criteria:"). These aren't spec items — they're preamble. The spec agent outputs them but they shouldn't be displayed as criteria.

Fix: Filter out spec items that are clearly preamble (start with "Context:", "Criteria:", contain "====", or are the title line).

---

## Implementation Order

1. **Fix 2 + 3**: Unified tool style + kill emoji (pilot.py, runner.py)
2. **Fix 1**: Breathing room (display.py)
3. **Fix 4 + 5 + 6 + 8**: Suppress noise — trivial phases, separators, panel removal (display.py, pilot.py, runner.py)
4. **Fix 9**: Bash command truncation (runner.py)
5. **Fix 10**: Spec preamble filter (cli.py)
6. **Fix 7**: `otto show` panel grouping (cli.py)

After each fix: test with a real `otto run` and `otto show`.

---

## What "Better Than CC" Looks Like

CC is minimal and clean. Otto should be that PLUS:
- **Phase structure** — CC doesn't have phases. Otto's prepare→coding→test→qa→merge is a feature, not noise. But phases should be subtle (bold header, not a separator line).
- **Inline test results** — CC shows Bash output. Otto can show parsed test counts in green/red.
- **QA spec results** — CC doesn't have QA. Otto's per-spec ✓/✗ is valuable. Show it cleanly.
- **Cost tracking** — CC doesn't track cost. Otto's per-phase cost is useful. Show it in the phase done line.
- **Multi-task awareness** — CC runs one task. Otto runs many. The task overview and summary are features.

The goal is not to copy CC — it's to match CC's quality bar while showing MORE useful information.
