# How Claude Code Renders Its Terminal UI

## Summary

Claude Code uses a **cell-based differential renderer** operating in the terminal's **normal screen buffer** (NOT the alternate screen). It maintains two screen buffers (double buffering), diffs them cell-by-cell, and emits only the minimal ANSI escape sequences needed to update changed cells. Text selection and scrollback work natively because the rendering operates within the standard terminal scrollback model -- cursor-up + erase-line only touch the viewport area that Claude Code "owns," leaving scrollback history intact.

---

## Architecture Overview

```
React component tree
    |
    v
Yoga flexbox layout (via yoga.wasm)
    |
    v
Rasterize to 2D screen buffer (Int32Array / BigInt64Array cells)
    |
    v
Diff current buffer vs previous buffer (cell-by-cell)
    |
    v
Generate minimal ANSI escape sequence ops
    |
    v
Write to stdout (wrapped in DEC 2026 synchronized output)
```

The pipeline runs at ~16ms frame budget (~60fps cap), with ~5ms allocated from scene graph to ANSI output.

---

## Key Design Decisions

### 1. Normal screen buffer, NOT alternate screen

Claude Code deliberately does NOT use the terminal alternate screen buffer (`\x1B[?1049h`). The alternate screen is what vim, less, htop use -- it gives you a clean canvas but:
- Destroys scrollback history
- Breaks native text selection (you can't select text that scrolled off)
- Content disappears when the app exits

Claude Code wants users to scroll back and see previous conversation turns, copy text naturally, and have output persist after exit. So it renders in the normal buffer.

The code defines `ALT_SCREEN_CLEAR` (mode 1049) but only uses it for specific sub-screens (like when launching an external editor), not for the main UI.

### 2. No mouse capture in normal mode

Mouse capture (`\x1B[?1000h` etc.) is only enabled when the alternate screen is active (`altScreenMouseTracking` is gated on `altScreenActive`). In the normal UI, mouse events are NOT captured, so:
- Native text selection works (terminal handles it)
- Right-click context menus work
- Scroll wheel works normally
- Click-to-position cursor works in the input area

### 3. Ink's `<Static>` component is NOT used

The current Claude Code renderer has `0` references to `internal_static` (Ink's static component marker). The old Ink approach used `<Static>` to write completed content permanently to stdout and only re-render the "live" area below. The new custom renderer abandoned this in favor of full cell-based rendering of the entire viewport.

---

## The Rendering Technique in Detail

### Screen buffer structure

Each cell is packed into two 32-bit integers (8 bytes per cell, stored as `Int32Array` / `BigInt64Array`):

```
Cell[0]: character index (interned string pool -- PM8 class)
Cell[1]: packed bitfield of:
  - styleId (shifted left by S06 bits)
  - hyperlink pool index (shifted left by C06 bits, masked by bp6)
  - width (1 = normal, 2 = wide char first half, 3 = continuation)
```

A screen is created via `l36(width, height, ...)` which allocates `width * height * 8` bytes as an `ArrayBuffer`, viewed as both `Int32Array` (for individual cell access) and `BigInt64Array` (for fast 64-bit comparisons and bulk operations like `fill` and `copyWithin`).

### Double buffering with "blit"

The renderer maintains two frames:
- **Front buffer** (previously rendered state, what's currently on terminal)
- **Back buffer** (new state being rendered)

When a component hasn't changed (`!dirty` and same position), its region is copied ("blitted") from the previous screen to the new screen via `blit()` -- a fast `Int32Array.set()` / `subarray()` copy. This avoids re-rasterizing unchanged components.

### Damage tracking

Each screen has an optional `damage` region (`{x, y, width, height}`). When a cell is written via `fM8()`, the damage region is expanded to include it. The diff function (`AZ1`) only examines cells within the damage region, skipping unchanged areas entirely.

### Cell-by-cell diffing (AZ1 function)

The diff function compares the front and back buffers:

```javascript
// Simplified from the minified source
function AZ1(prevScreen, nextScreen, callback) {
  // Calculate damage region from both screens
  let damageRegion = combineDamage(prevScreen.damage, nextScreen.damage);

  // If widths match, use fast path (Sj9)
  // If widths differ (terminal resize), use slow path (Cj9)

  // For each cell in damage region:
  //   Compare prev.cells[i] vs next.cells[i] (64-bit comparison)
  //   If different, call callback(col, row, prevCell, nextCell)
}
```

The fast path (`Sj9`) scans rows within the damage region. For each row, `yj9()` uses a tight loop comparing `Int32Array` pairs, skipping runs of identical cells. When it finds a difference, it calls the callback with the old and new cell data.

### Op generation

The diff callback builds a list of rendering "ops" via the `$Z1` class (a cursor-tracking diff builder):

```
Op types:
  - "stdout"          -> raw text content
  - "clear"           -> erase N lines (CSI 2K + CSI 1A repeated)
  - "clearTerminal"   -> full terminal clear
  - "cursorHide"      -> CSI ?25l
  - "cursorShow"      -> CSI ?25h
  - "cursorMove"      -> CSI {dy};{dx}H (relative move)
  - "cursorTo"        -> CSI {col}G (absolute column)
  - "carriageReturn"  -> \r
  - "hyperlink"       -> OSC 8 hyperlink escape
  - "styleStr"        -> pre-computed ANSI SGR transition string
```

The `$Z1` class tracks the virtual cursor position. When a cell needs updating, it:
1. Moves the cursor to the cell's position (only if it's not already there)
2. Emits the style transition (SGR codes to go from current style to target style)
3. Writes the character

Style transitions are cached: `stylePool.transition(fromStyleId, toStyleId)` returns a pre-computed ANSI string. Consecutive cells with the same style need no transition.

### Flush to stdout

All ops are converted to a single ANSI string in one pass:

```javascript
// Simplified from minified source
function flushOps(target, ops, syncEnabled) {
  let output = syncEnabled ? BSU : "";  // Begin Synchronized Update: \x1B[?2026h

  for (let op of ops) {
    switch (op.type) {
      case "stdout":         output += op.content; break;
      case "clear":          output += eraseLines(op.count); break;
      case "clearTerminal":  output += clearTerminal(); break;
      case "cursorHide":     output += "\x1B[?25l"; break;
      case "cursorShow":     output += "\x1B[?25h"; break;
      case "cursorMove":     output += cursorMove(op.x, op.y); break;
      case "cursorTo":       output += cursorTo(op.col); break;
      case "carriageReturn": output += "\r"; break;
      case "hyperlink":      output += oscHyperlink(op.uri); break;
      case "styleStr":       output += op.str; break;
    }
  }

  if (syncEnabled) output += ESU;  // End Synchronized Update: \x1B[?2026l
  target.stdout.write(output);     // Single write call
}
```

The entire frame update is:
1. One `stdout.write()` call
2. Wrapped in DEC mode 2026 (synchronized output) begin/end markers
3. Contains only the minimal escape sequences to update changed cells

---

## How In-Place Updates Work (Spinners, Status Changes)

### The "Reading" -> "Read" example

When a tool call status changes from "Reading file.ts" to "Read file.ts (12 lines)":

1. React state update triggers re-render
2. Yoga recomputes layout (the status component's text changed)
3. The new screen buffer gets the updated text rasterized into it
4. `AZ1` diffs old vs new buffer:
   - Most cells are identical (conversation above, input below)
   - Only the ~30 cells on the status line differ
5. Ops generated: `cursorMove` to the status line + style transitions + new text characters
6. Single `stdout.write()` with ~50 bytes of ANSI escape sequences

The terminal sees something like:
```
\x1B[?2026h          (begin synchronized update)
\x1B[15;4H           (move cursor to row 15, column 4)
\x1B[32m             (green color)
Read file.ts (12 lines)
\x1B[K               (clear to end of line, if old text was longer)
\x1B[20;1H           (move cursor back to input position)
\x1B[?2026l          (end synchronized update)
```

### Why text selection survives

The cursor movement (`\x1B[15;4H`) and character writing only affect the specific cells that changed. The terminal's text selection is maintained by the terminal emulator independently of cursor position. The key factors:

1. **No screen clear**: Unlike old-Ink which erased ALL lines and rewrote everything, the new renderer only touches changed cells
2. **No alternate screen**: Text is in the normal scrollback buffer, which the terminal emulator manages
3. **No mouse capture**: The terminal handles mouse events for selection, not the app
4. **Synchronized output**: DEC 2026 prevents the terminal from painting intermediate states, so the user never sees cursor movement artifacts

If you're actively selecting text while an update happens, the terminal emulator may or may not disrupt the selection depending on implementation -- but because updates are small and atomic (synchronized output), this is rarely noticeable.

---

## Evolution: Old Ink vs New Custom Renderer

### Old approach (stock Ink / log-update)

```
1. React renders full component tree -> string
2. ansiEscapes.eraseLines(previousLineCount)  -- erase ALL previous output
3. stream.write(newOutput)                     -- write ALL new output
```

Problems:
- Erasing 60 lines generates 60x `\x1B[2K\x1B[1A` sequences
- Complete redraw even if only 1 character changed
- Visible flickering (erase -> blank -> new content)
- `<Static>` partially mitigated this by only re-rendering the "live" area below static content

### Intermediate: Ink with `<Static>` (used until late 2025)

```
Static content (completed messages) -> written to stdout once, never re-rendered
Live area (current message, input)  -> erased and rewritten on each update
```

This reduced the re-render area but still had full-erase behavior within the live area.

### Current: Custom cell-based differential renderer (shipped Jan 2026)

```
1. React renders component tree
2. Yoga computes layout
3. Rasterize to 2D Int32Array screen buffer
4. Diff against previous buffer (cell-by-cell, damage-region-bounded)
5. Generate minimal cursor-move + write ops
6. Single stdout.write() wrapped in synchronized output
```

No `<Static>` component. No `eraseLines`. No full redraws (except on terminal resize or when content scrolls past viewport). The renderer is essentially a tiny terminal compositor.

---

## Scrollback Handling

The fundamental tension: Claude Code operates in normal screen mode (for scrollback/selection), but the terminal has no API for "incrementally update scrollback." You can only write to the viewport.

When content is entirely within the viewport (hasn't scrolled off the top), the differential renderer can update any cell by positioning the cursor and writing. But once content scrolls into the scrollback buffer, it becomes immutable from the application's perspective.

Claude Code handles this by:

1. **Tracking viewport position**: The renderer knows `screen.height` vs `viewport.height` and the scroll offset
2. **Detecting scrollback changes**: If the diff finds changes in rows that have scrolled into the scrollback (`row < scrollbackRows`), it falls back to a full clear+redraw (`up6(q, "offscreen", Y)`)
3. **Minimizing scrollback mutations**: UI design ensures that completed conversation turns don't change, so scrollback content is naturally stable

The "full reset" fallback uses `clearTerminal()` which on macOS/Linux is `\x1B[2J\x1B[3J\x1B[H` -- clear screen, clear scrollback, home cursor. This is the nuclear option that causes visible flicker, but happens rarely (mostly on terminal resize).

---

## DEC Mode 2026: Synchronized Output

The single most important anti-flicker mechanism. When the terminal supports it:

```
\x1B[?2026h   -- "I'm about to update the screen, don't paint yet"
...cursor moves, style changes, text writes...
\x1B[?2026l   -- "Done, you can paint now"
```

The terminal buffers all the escape sequences and applies them atomically in a single paint. This eliminates visible cursor movement and partial updates.

Supported in: Ghostty, iTerm2, WezTerm, kitty, modern tmux (with patches), VS Code terminal (with patches). NOT supported in: Apple Terminal, older terminal emulators.

Without DEC 2026, users see more flickering because each cursor movement and write is painted individually.

---

## What This Means for Building Similar UIs

The key insight: **you can have in-place updates without breaking terminal text selection by using cursor positioning + minimal writes instead of erase-and-rewrite.**

The recipe:
1. Maintain a virtual screen buffer (2D array of cells)
2. On each frame, diff the new buffer against the old one
3. For each changed cell, emit `cursorTo(col, row)` + style codes + character
4. Wrap the entire update in synchronized output (DEC 2026)
5. Do NOT use alternate screen (preserves scrollback/selection)
6. Do NOT capture mouse events (preserves native selection)
7. Use React/Yoga for the component model and layout, but replace the Ink rendering backend entirely

This is essentially what terminal emulators themselves do internally (maintain a cell grid, diff on updates), but implemented in userspace as the application's output layer.

---

## Sources

- [Claude Code GitHub repo](https://github.com/anthropics/claude-code)
- [How Claude Code is built - Pragmatic Engineer](https://newsletter.pragmaticengineer.com/p/how-claude-code-is-built)
- [Boris Cherny's thread on renderer rewrite](https://www.threads.com/@boris_cherny/post/DSZbZatiIvJ/)
- [HN comment from Claude Code TUI engineer (chrislloyd)](https://news.ycombinator.com/item?id=46701013)
- [Claude Code flickering profiling analysis](https://dev.to/vmitro/i-profiled-claude-code-some-more-part-2-do-androids-dream-of-on-diffs-2kp6)
- [The Signature Flicker - Peter Steinberger](https://steipete.me/posts/2025/signature-flicker)
- [Ink React terminal renderer](https://github.com/vadimdemedes/ink)
- [Ink rendering analysis (flickering)](https://github.com/atxtechbro/test-ink-flickering/blob/main/INK-ANALYSIS.md)
- [Claude Code source reverse engineering](https://leehanchung.github.io/blogs/2025/03/07/claude-code/)
- [Claude Code scrolling fix article](https://angular.schule/blog/2026-02-claude-code-scrolling/)
- [Claude Code npm package](https://www.npmjs.com/package/@anthropic-ai/claude-code) (v2.1.81 analyzed)
