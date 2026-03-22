# Research: Inline TUI Rendering — Terminal UI Without Fullscreen Takeover

## Context

We want a terminal display that:
- Renders below the prompt (inline), not fullscreen
- Has scrollable internal regions (e.g., a log viewer)
- Preserves native terminal text selection and copy/paste
- Updates in-place without flickering

---

## 1. Can Textual Run in Inline Mode?

**Yes.** Textual supports inline mode since ~2024. Pass `inline=True` to `app.run()`:

```python
app = MyApp()
app.run(inline=True)
```

### How it works
- App renders **below the prompt** in the normal terminal buffer (NOT the alternate screen)
- Uses cursor positioning escape codes to overwrite previous frames in-place
- The last line uses a "move cursor back" escape instead of newline, so subsequent frames overwrite
- When output shrinks, a "clear lines below cursor" escape prevents stale remnants
- Mouse input works: Textual queries cursor position to translate mouse coords relative to the app origin

### Scrollable widgets in inline mode: YES
- Textual's full widget system works in inline mode — `ScrollableContainer`, `TextArea`, `ListView`, etc.
- You can constrain height with CSS: `height: auto; max-height: 50vh;`
- The `:inline` CSS pseudo-selector lets you apply styles only when running inline
- Scrollbars appear automatically when content exceeds the constrained height

### Limitations
- **Not supported on Windows** (as of early 2025)
- Command palette has issues in inline mode (GitHub issue #4385)
- Mouse origin is at top-left of terminal, not top-left of app (Textual handles the translation)
- `INLINE_PADDING` class var controls blank line padding above the app (default is some padding; set to 0 to disable)

### Key CSS for inline mode
```css
Screen:inline {
    height: 50vh;
    border: none;
}
```

### Sources
- [Textual: Behind the Curtain of Inline Terminal Applications](https://textual.textualize.io/blog/2024/04/20/behind-the-curtain-of-inline-terminal-applications/)
- [Textual: Style Inline Apps](https://textual.textualize.io/how-to/style-inline-apps/)
- [Textual: App Basics](https://textual.textualize.io/guide/app/)

---

## 2. How Does Claude Code Handle Copy/Paste?

**Claude Code does NOT use the alternate screen.** This is a deliberate architectural choice.

### Architecture
- Built with **React + Ink** (React for CLI apps)
- Ink by default renders **inline** — it does NOT use the alternate screen buffer
- Anthropic has since **rewritten the renderer from scratch** while keeping React as the component model
- The custom renderer diffs each cell and emits minimal escape sequences to update only what changed

### Why no alternate screen?
Anthropic explicitly avoids alt-screen because it breaks:
- **Native text selection** — users expect standard terminal selection
- **Native scrolling** — preserves terminal scrollback history
- **Terminal search** — Cmd+F / Ctrl+Shift+F works normally

A team member stated: *"We value this native experience a lot. We may explore alternate screen mode in the future, but our bar is quite high."*

### How copy/paste works
By staying in the **primary screen buffer**, all native terminal operations work:
- Click-drag to select text
- Cmd+C / Ctrl+C to copy
- Terminal's built-in search (Cmd+F) works
- Scrollback history is preserved

### Flicker mitigation
The rewritten renderer (v2.0.72+) does incremental differential rendering — updating only changed cells. This reduced flickering ~85%.

### Sources
- [The Signature Flicker (Peter Steinberger)](https://steipete.me/posts/2025/signature-flicker)
- [HN discussion on Ink rendering](https://news.ycombinator.com/item?id=46850188)
- [Ink GitHub](https://github.com/vadimdemedes/ink)

---

## 3. Rich Live vs Textual — The Spectrum

| Feature | Rich Live | Textual (fullscreen) | Textual (inline) |
|---|---|---|---|
| Alternate screen | Optional (`screen=True`) | Yes (default) | **No** |
| Renders inline | Yes (default) | No | **Yes** |
| Scrollable widgets | **No** (just overflow modes) | Yes (full widget system) | **Yes** |
| Mouse input | No | Yes | Yes |
| Keyboard input | No | Yes | Yes |
| Updates in-place | Yes | Yes | Yes |
| Native text selection | Yes (when inline) | No (alt screen) | **Yes** |
| Internal scroll buffer | **No** | Yes | **Yes** |

### Rich Live limitations
- Three overflow modes: `ellipsis` (default), `crop`, `visible`
- **None of them provide scrolling** — content is either truncated or shown in full (which breaks clearing)
- No internal scroll buffer — Rich Live is a "renderable replacer", not a widget system
- You can print above the live display via `live.console`, but the live area itself is not scrollable
- An unanswered [GitHub discussion #3084](https://github.com/Textualize/rich/discussions/3084) confirms this gap

### Textual inline = the middle ground
Textual inline mode gives you **everything Rich Live has** (inline rendering, in-place updates, native text selection) **plus** everything Textual has (scrollable widgets, mouse/keyboard input, layout system). It is literally the middle ground between Rich Live and fullscreen Textual.

---

## 4. Can You Have Scrollable Regions Without Fullscreen?

**Yes, with several approaches:**

### A. Textual inline mode (best option for Python)
- Full scrollable widget system in inline mode
- Set `max-height` on Screen or containers to constrain vertical space
- Scrollbars appear automatically for overflow content
- Mouse wheel scrolling works
- Keyboard scrolling works

### B. ANSI scroll regions (DECSTBM — low-level)
- The VT100 escape sequence `CSI <top> ; <bottom> r` (DECSTBM) sets scroll margins
- Text scrolls only within the defined region
- Used by programs like tmux internally
- Python library [TerminalScrollRegionsDisplay](https://github.com/pdanford/TerminalScrollRegionsDisplay) wraps this
- Limitations: no scrollback within regions, lightweight but no widget system

### C. Rich Live with manual buffer management (workaround)
- Maintain a fixed-size deque of recent lines
- Render only the last N lines in the Live display
- Simulate "scrolling" by slicing the buffer
- No actual scroll interaction — just a sliding window
- Clunky but works for pure output scenarios

### D. Ink (Node.js) — what Claude Code does
- Renders inline by default (no alt screen)
- No built-in scroll widgets, but custom implementations possible
- Claude Code's approach: custom React renderer with differential updates

---

## 5. Textual Inline Mode — Detailed Behavior

### Enabling
```python
from textual.app import App

class MyApp(App):
    INLINE_PADDING = 0  # No blank lines above app

    CSS = """
    Screen:inline {
        height: auto;
        max-height: 50vh;
    }
    """

app = MyApp()
app.run(inline=True)
```

### What happens under the hood
1. App renders into the normal terminal buffer (no `\x1b[?1049h` alternate screen switch)
2. Each frame writes lines terminated by newlines, except the last line which uses cursor-up escapes
3. Next frame overwrites from the saved cursor position
4. If fewer lines than before, clear-below escape removes stale content
5. Mouse coordinates are translated from terminal-absolute to app-relative using cursor position query (`\x1b[6n`)

### What works in inline mode
- All Textual widgets (containers, scrollables, inputs, text areas, data tables, etc.)
- CSS styling with `:inline` pseudo-selector for mode-specific rules
- Mouse and keyboard input
- Reactive data binding
- Multiple screens (though this is unusual for inline)

### What doesn't work well
- Command palette (known issue)
- Windows (not supported)
- Very tall inline apps may behave oddly with terminal scrollback

---

## Recommendations for Otto Display

**Textual inline mode is the clear winner** for our use case:

1. **Inline rendering** — no alternate screen, preserves terminal context
2. **Scrollable log viewer** — use `ScrollableContainer` or `RichLog` widget with `max-height`
3. **Native text selection** — works because we're in the primary buffer
4. **Structured layout** — CSS grid/flexbox for status bar + log area + input
5. **In-place updates** — Textual handles frame diffing
6. **Rich integration** — Textual is built on Rich, so all Rich renderables work as content

### Architecture sketch
```
Screen:inline (max-height: 60vh)
├── Header (status: phase, iteration, file counts)
├── ScrollableContainer (log viewer, grows to fill)
│   └── RichLog widget (streaming agent output)
└── Footer (controls hint, elapsed time)
```

### Alternative: Pure Rich Live
If we want absolute minimal complexity and don't need scrolling or interaction:
- Rich Live with a manually managed buffer (deque of last N lines)
- Status table + recent log lines rendered as a Rich Group
- Simpler but no scroll-back, no mouse interaction, no keyboard input

### Alternative: Raw ANSI (DECSTBM scroll regions)
- Maximum control, zero dependencies beyond print()
- But would need to reimplement everything (layout, scrolling, input handling)
- Not worth it when Textual inline exists
