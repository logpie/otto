# TUI Revisit — Analysis & Future Plan

## What We Tried (2026-03-21)

### Textual Full-Screen TUI
- Built `OttoRunApp` with `TaskPanel`, `PhaseBar`, `RichLog` widgets
- Wired into `otto run` with pilot running as `@work(thread=True)` background worker
- **Worked functionally** — headless test passed, events routed, screenshots captured
- **Failed on UX** — even with `inline=True` and `mouse=False`, Textual captures the terminal render region. Text selection and terminal scrollback don't work natively.

### Textual Inline Mode
- `app.run(inline=True, inline_no_clear=True, mouse=False)`
- Renders in normal buffer (no alternate screen)
- Still manages a render region with cursor positioning — terminal treats it as "owned"
- Mouse=False helps but scrolling within the widget doesn't work without mouse capture
- **Verdict**: Inline mode is a half-measure. It's not truly transparent like CC.

## Why CC Works

CC uses a **custom cell-based differential renderer** (shipped Jan 2026):
- Maintains two `Int32Array` screen buffers (old frame, new frame)
- Diffs cell-by-cell, emits only cursor-move + char-write for changed cells
- Uses DEC 2026 synchronized output (`\x1B[?2026h`...`\x1B[?2026l`) for atomic painting
- **No alternate screen** — content in normal scrollback
- **No mouse capture** — terminal handles selection natively
- **No TUI framework** — custom React-to-cells rasterizer

This is essentially a userspace terminal compositor. It's ~2000 lines of specialized rendering code.

## Options for Otto

### Option A: Custom Renderer (CC approach)
- Build a Python equivalent of CC's cell-diffing renderer
- Estimated effort: 1-2 weeks for a basic version
- Pros: Full control, CC-quality UX, native selection
- Cons: Significant engineering, maintenance burden, reinventing the wheel

### Option B: Rich Live with Enhanced Layout
- Current approach — `console.print()` scrollback + `Rich Live` footer
- For parallel tasks: prefixed lines `[#1] ● Write api.ts`
- Pros: Simple, works today, native selection, pipe-friendly
- Cons: Interleaved output for 3+ parallel tasks is noisy

### Option C: Blessed/Curses Hybrid
- Use Python `curses` or `blessed` for a bottom status panel only
- Keep the scrollback for tool calls (native selection)
- Status panel: phase progress, task overview, cost (updates in-place)
- Like tmux status bar but within the otto process
- Pros: Native selection for content, in-place status updates
- Cons: curses/blessed is lower-level than Rich

### Option D: Textual with Future Fixes
- Textual may add better inline selection support in future versions
- Monitor https://github.com/Textualize/textual/issues for selection-related issues
- Revisit when Textual inline mode matures
- Pros: Widget system already built, just needs Textual to improve
- Cons: Dependent on upstream, timeline unknown

### Option E: Separate Dashboard
- `otto run` stays scrollback (current approach)
- `otto dashboard` launches a separate Textual full-screen app that connects to the JSONL side-channel
- Run in a second terminal: `otto dashboard` while `otto run` is active
- Pros: Best of both worlds — scrollback for the run, rich dashboard for monitoring
- Cons: Requires two terminals

## Recommendation

**Short-term (now)**: Option B — Rich scrollback with taste fixes. Ship it.

**Medium-term (when parallel tasks land)**: Option C or E
- Option C (curses status bar) if we want single-terminal
- Option E (separate dashboard) if we're OK with two terminals

**Long-term**: Option A (custom renderer) only if otto becomes a major product with dedicated engineering resources. Not worth it as a side project.

## Research Files (preserved)

- `research-tui.md` — Survey of CC, Aider, Codex CLI, Amazon Q, Cline, OpenHands, SWE-agent display patterns
- `research-inline-tui.md` — Textual inline mode capabilities and limitations
- `research-cc-rendering.md` — Deep dive into CC's custom cell-based differential renderer
- `research.md` — Initial display research (Rich components, modern CLI patterns)

## Code to Preserve

The Textual code in `otto/tui.py` is functional and tested. If we revisit:
- `OttoRunApp` — app shell with task panels and phase bar
- `TaskPanel` — per-task widget with RichLog, inline diffs, QA findings
- `PhaseBar` — always-visible phase progress
- `ProgressEvent` / `RunComplete` — thread-safe message passing
- The headless test pattern (`app.run_test(size=(100,40))`) works for CI

## Key Learnings

1. **Textual ≠ CC's approach**. Textual is a widget framework that manages rendering. CC is a custom renderer that lets the terminal handle everything except changed cells.

2. **Selection is non-negotiable**. Users expect to select and copy text from their terminal. Any approach that breaks this is a dealbreaker.

3. **Scrollback is a feature**. Being able to scroll up and see what happened 5 minutes ago is valuable. TUI frameworks that use alternate screen lose this.

4. **The taste is in the content, not the chrome**. CC looks good not because of fancy rendering but because of good information hierarchy, spacing, and knowing what to show/hide.

5. **Parallel display is a separate problem**. Don't over-engineer the display for parallel tasks before the parallel execution engine exists.
