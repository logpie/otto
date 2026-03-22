# Research: How Modern Coding Agent CLIs Display Output

Research date: 2026-03-21

## 1. Claude Code CLI (Anthropic)

**Architecture**: React-based terminal rendering using Yoga WASM layout engine. Not a full-screen TUI — uses scrollback buffer with selective re-rendering.

### Tool Call Display

Tool calls render as **collapsible blocks** with a header line:

```
Bash(npm test)
  PASS src/utils.test.ts
  ... +27 lines (ctrl+o to expand)
```

```
Read(src/index.ts)
  [file contents, first few lines]
  ... +142 lines (ctrl+o to expand)
```

- **Collapsed by default** when output exceeds a line threshold
- **Ctrl+O** toggles expansion
- No persistent config for auto-expand (requested in issue #25776)
- MCP read/search tool calls collapse into `Queried {server}` (expand with Ctrl+O)

### Tense-Based Progress Indication

- **In-progress**: present tense ("Reading", "Searching for", "Editing")
- **Complete**: past tense ("Read", "Searched for", "Edited")

### File Edit Display

```
- const TOKEN_EXPIRY = 3600;
+ const TOKEN_EXPIRY = 7200;
```

- Standard unified diff format: red `-` lines (removed), green `+` lines (added)
- Preview appears before approval
- Keyboard shortcuts at approval prompt: **y** (accept), **n** (reject), **d** (show full diff), **e** (edit before accepting)
- **Esc** aborts, **Shift+Tab** enables auto-accept mode

### Thinking/Spinner Display

- Animated **braille spinner** characters: `⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏` (50ms animation loop)
- Configurable "spinner verbs" (customizable rotating status messages)
- Default verbs: whimsical phrases like "Befuddling...", "Pondering...", etc.
- Can be overridden with neutral messages like "Working..." via `spinnerVerbs` config
- **Effort level indicators**: `○` (low), `◐` (medium), `●` (high)
- **"Thinking pill"** (VS Code): shows "Thought for Ns" after response completes
- Terminal title gets animated spinner glyph while thinking

### Status Line (Bottom Bar)

Fully customizable shell script. Receives JSON via stdin with:
- Model name, session ID, working directory
- Context window: `used_percentage`, token counts, `remaining_percentage`
- Cost: `total_cost_usd`, `total_duration_ms`
- Rate limits: 5-hour and 7-day windows with `used_percentage`
- Git info (via user script)
- Vim mode indicator

Example rendered status line:
```
[Opus] 📁 my-project | 🌿 main
████░░░░░░ 42% | $0.15 | ⏱️ 3m 12s
```

### Text Streaming

- Line-by-line streaming (macOS/Linux; disabled on Windows due to rendering issues)
- Markdown rendering with syntax highlighting in code blocks
- Blockquotes: italic with left bar (dark themes)
- Ordered list numbers render correctly
- CJK character handling prevents visual overflow
- RTL text support (Hebrew, Arabic)

### What Claude Code Suppresses

- Async hook completion messages (hidden by default, visible with `--verbose`)
- CLAUDE.md HTML comments hidden from display
- System prompt content not shown
- MCP tool internals collapsed to single-line summary
- `--verbose` mode floods terminal with raw JSON (known usability issue)

### Key Visual Elements

- ANSI colors (orange brand color, syntax highlighting)
- OSC 8 hyperlinks (Cmd+click in supported terminals)
- Session color customizable via `/color` command
- Synchronized output escape sequences (CSI ?2026h/l) to prevent flicker

---

## 2. Aider (aider.chat)

**Architecture**: Python CLI using the **Rich** library for terminal rendering. Scrollback-based, not full-screen.

### Startup Banner

```
Aider v0.37.1-dev
Models: gpt-4o with diff edit format, weak model gpt-3.5-turbo
Git repo: .git with 258 files
Repo-map: using 1024 tokens
Use /help to see in-chat commands, run with --help to see cmd line args
───────────────────────────────────────────────────────────────────────
```

### Prompt

```
> Make a program that asks for a number and prints its factorial
```

Simple `> ` prefix. In multiline mode: `multi> `.

### Edit Display (Search/Replace Format)

Aider's primary edit format shows the model's output directly:

```python
mathweb/flask/app.py
<<<<<<< SEARCH
from flask import Flask

app = Flask(__name__)
=======
from flask import Flask
import math

app = Flask(__name__)
>>>>>>> REPLACE
```

**Key usability complaint**: Shows entire old method body then entire new method body, with **no visual indication of which lines changed**. Users report changes "scroll by with typically more than I can digest."

### Unified Diff Format (Alternative)

```diff
--- mathweb/flask/app.py
+++ mathweb/flask/app.py
@@ ... @@
-from flask import Flask
+from flask import Flask
+import math
```

### Color Scheme

| Element | Default Color |
|---------|--------------|
| User input | `#00cc00` (green) |
| Assistant output | `#0088ff` (blue) |
| Tool output | configurable |
| Tool errors | `#FF2222` (red) |
| Tool warnings | `#FFA500` (orange) |

### Markdown Rendering

- Uses Rich `Markdown` class with configurable code themes
- Options: monokai, solarized-dark, solarized-light, or other Pygments themes
- `--pretty` flag (default: on) enables colorized, formatted output
- `--no-pretty` shows raw text

### Git Integration Display

- Auto-commits with Conventional Commits format
- `/diff` command shows changes since last message
- `--show-diffs` option shows diffs when committing
- `--dark-mode` / `--light-mode` for theme adaptation

### What Aider Suppresses

- In `--no-pretty` mode: all formatting stripped
- With `NO_COLOR` env var: all color disabled
- Repo map details (summary only in banner)

### Display Methods

- `tool_output()`: informational messages with configurable color
- `tool_error()`: red-colored error messages
- `tool_warning()`: orange-colored warnings
- `assistant_output()`: Rich Markdown rendering of LLM responses

---

## 3. OpenAI Codex CLI

**Architecture**: React + **Ink** library (React for terminal). Full-screen TUI with interactive controls. Built in TypeScript/Rust hybrid.

### Full-Screen Layout

Codex launches a full-screen terminal UI. Key components:
- **TerminalChat**: Main container managing history, input, loading states, overlays
- **TerminalMessageHistory**: Uses Ink's `<Static>` component for efficient rendering
- **TerminalChatInput**: Multiline editor with command history and slash commands
- **TerminalChatCommandReview**: Command approval interface

### Session Start Screen

Displays current model (e.g., "gpt-5.2-codex") and reasoning level ("medium") with options to adjust via `/model` and reasoning settings.

### Command Display

```
$ command text here
```

- Commands prefixed with `$` in **magentaBright** color
- Output appears inline and stays in conversation history

### Diff Display

- Syntax-highlighted diffs in the TUI
- Red/green coloring for removed/added lines
- Theme customizable via `/theme` (live preview)
- `parse-apply-patch.ts` handles diff rendering

### Approval Flow

```
[Command requiring approval]
> Yes (y) / No (n) / Edit (e) / Esc
```

- Interactive selection menu
- Keyboard shortcuts: `y` / `n` / `e` / `Esc`
- Approval modes: readonly, suggest, auto-edit, full-auto
- Can change mid-session via `/approvals` or `/permissions`

### Background Task Progress

Shows each background terminal's command plus up to **three recent, non-empty output lines** for progress monitoring.

### Thinking Display

- "Thinking..." status during API calls
- Plans shown before execution
- Users can approve or reject steps inline

### What Codex Suppresses

- In `codex exec` mode: streams response to terminal, minimal UI
- `--json` flag outputs newline-delimited JSON events (for scripting)
- Session ID shown on exit for later resumption

---

## 4. Amazon Q Developer CLI

**Architecture**: **Rust** with **Ratatui** TUI framework. Immediate-mode rendering.

### Tool Call Display

Inline indicators during execution:

```
[Tool uses: execute_bash] > npm test
[Tool uses: fs_read]
[Tool uses: fs_write]
```

### Built-in Tools

| Tool | Display Name | Purpose |
|------|-------------|---------|
| `fs_read` | `[Tool uses: fs_read]` | Read files, directories, images |
| `fs_write` | `[Tool uses: fs_write]` | Create and edit files |
| `execute_bash` | `[Tool uses: execute_bash]` | Execute shell commands |
| `use_aws` | `[Tool uses: use_aws]` | AWS API calls |

### Workflow

- Shows tool usage indicators during execution
- Provides a **summary of completed work** when done
- Suggests next steps after completion
- Interactive approval for file modifications and commands

### Configuration

Tool access controlled via JSON:
```json
{
  "toolsSettings": {
    "fs_read": {
      "allowedPaths": ["~/projects", "./src/**"],
      "deniedPaths": ["/etc/**", "~/.ssh/**"]
    }
  }
}
```

---

## 5. Cline (VS Code Extension)

**Architecture**: VS Code sidebar panel. React webview UI with `ChatRow.tsx` rendering. Not a terminal — a rich GUI panel.

### Tool Call Display

Tool use formatted in XML-style tags internally. Displayed in the activity panel as sequential steps:

- **Read File**: Shows file name being read
- **Write File**: Opens VS Code's native **diff view** (side-by-side)
- **Execute Command**: Shows command with approval prompt
- **Browser**: Shows URL being visited

### Diff View (Key Differentiator)

Uses VS Code's built-in `vscode.diff` command:
- Custom `cline-diff` URI scheme
- **Side-by-side diff**: original vs. modified
- **Faded overlay** on lines not yet written during streaming
- **Active line highlighting** marks the current line being processed
- Real-time streaming into the diff view

### Approval UI

- **Human-in-the-loop GUI** for every file change and terminal command
- "Approve" button to allow writes/execution
- Auto-approve menu (overhauled in v3.35 — inline expansion, no popup interruption)
- Plan Mode vs. Act Mode toggle

### Post-Save Feedback

After approval, surfaces:
1. **newProblemsMessage**: Diagnostic errors appearing after the edit
2. **userEdits**: Diff of manual changes made pre-approval
3. **autoFormattingEdits**: Changes from editor's format-on-save

### Activity Timeline

Colored bars in the execution timeline:
- **Grey bars**: "Model is thinking"
- Other colors for: shell output parsing, file operations, etc.
- Token usage, cache, and context window metrics at the top

### Cost Display

Tracks total tokens and API usage cost for the entire task loop and individual requests.

---

## 6. OpenHands CLI

**Architecture**: Python/binary CLI. Web UI + terminal modes. Event stream architecture.

### Event Stream Model

All interactions flow as typed events:
```
User Message → Agent → LLM → Action → Runtime (sandbox) → Observation → Agent
```

Action types: `CmdRunAction`, `FileReadAction`, `FileWriteAction`, `FileEditAction`

### Command Output Truncation (Proposed/Implemented)

```
Command Output (showing 15 of 200 lines)
┌─────────────────────────────────────┐
│ line 1 of output                    │
│ line 2 of output                    │
│ ...                                 │
│ line 15 of output                   │
│ ... and 185 more lines              │
│ (use --full to see complete output) │
└─────────────────────────────────────┘
```

- Default: first 10-15 lines shown
- Remaining line count displayed
- Box-drawing characters for borders
- "first 10 and last 20" compromise approach suggested

### File Edit Display

`FileEditAction` produces edits in diff format. `FileEditObservation` returns a git patch.

### Progress

- "Real-time interaction" with "instant feedback"
- "Live status monitoring" — watch agent progress
- Confirmation mode prompts before sensitive operations

---

## 7. SWE-agent

**Architecture**: Python framework. Not a TUI — primarily a headless agent runner with terminal logging.

### Output Format

- **Thought + Action** pairs at each step
- Observations from earlier steps collapsed into single lines (only last 5 shown in full)
- If no output: "Your command ran successfully and did not produce any output"

### Batch Mode

- **Progress bar** (tqdm-based) at the bottom for multiple instances
- Configurable via `TQDM_DISABLE` environment variable

### Context Management

Only the last 5 observations shown in full. Earlier observations each collapsed to a single-line summary to keep context window manageable.

---

## 8. Continue.dev CLI (`cn`)

**Architecture**: TypeScript/Node CLI. Interactive and headless modes.

### Interactive Mode

- Shows git branch when available
- Tips system provides hints during usage
- Shell mode for direct command execution

### Headless Mode

- Only outputs final response (for piping/scripting)
- Perfect for Unix philosophy workflows

### Approval System

- Prompts before file changes and command execution
- Details not publicly documented in terminal format

### Verbose Mode

- `--verbose` flag for detailed logs
- Logs written to `~/.continue/logs/cn.log`

---

## Cross-Cutting Patterns & Comparison

### How Tools Show File Reads

| Tool | Format |
|------|--------|
| Claude Code | `Read(path)` header + collapsed content + `... +N lines (ctrl+o to expand)` |
| Aider | Not explicitly shown — file contents loaded silently |
| Codex CLI | Inline in conversation history |
| Amazon Q | `[Tool uses: fs_read]` indicator |
| Cline | Shows in activity panel; file opened in editor |
| OpenHands | Box-bordered output with truncation |

### How Tools Show File Edits/Diffs

| Tool | Format |
|------|--------|
| Claude Code | Unified diff (`-`/`+` lines), red/green, approval prompt (y/n/d/e) |
| Aider | Search/Replace blocks (`<<<<<<< SEARCH` / `>>>>>>> REPLACE`), no inline highlighting |
| Codex CLI | Syntax-highlighted diffs in full-screen TUI, red/green, inline approval |
| Amazon Q | `[Tool uses: fs_write]` indicator, approval required |
| Cline | VS Code native side-by-side diff viewer with streaming preview |
| OpenHands | Git patch format in observations |

### How Tools Show Command Execution

| Tool | Format |
|------|--------|
| Claude Code | `Bash(command)` header + collapsed output + `... +N lines` |
| Aider | Not explicitly shown in conversation (happens in background) |
| Codex CLI | `$` prefix in magentaBright + inline output |
| Amazon Q | `[Tool uses: execute_bash] > command` |
| Cline | Shows command in activity panel, approval before execution |
| OpenHands | Box-bordered truncated output (first 10-15 lines) |

### How Tools Show Thinking/Reasoning

| Tool | Format |
|------|--------|
| Claude Code | Braille spinner (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏) + rotating verb + effort indicator (○◐●) |
| Aider | No explicit thinking indicator |
| Codex CLI | "Thinking..." status text |
| Amazon Q | Not documented |
| Cline | Grey bars in timeline + "Model is thinking" |
| OpenHands | Not documented for CLI |
| SWE-agent | "Thought:" prefix in agent output |

### How Tools Show Progress/Phases

| Tool | Format |
|------|--------|
| Claude Code | Tense-based ("Reading" → "Read"), spinner verbs, session duration |
| Aider | Git auto-commits mark phase boundaries |
| Codex CLI | Plan explanation before execution, step-by-step approval |
| Cline | Colored timeline bars, token/cost counters |
| SWE-agent | tqdm progress bar for batch mode |

### Collapse/Expand Patterns

| Tool | Pattern |
|------|---------|
| Claude Code | Auto-collapse at threshold, `ctrl+o` to expand, `+N lines` indicator |
| Codex CLI | Full-screen TUI — scrollable within window |
| OpenHands | Proposed: show first 10-15 lines, `... and N more lines` |
| SWE-agent | Older observations → single-line summaries |

### Approval Patterns

| Tool | Pattern |
|------|---------|
| Claude Code | `y` accept, `n` reject, `d` diff, `e` edit, `Esc` abort, `Shift+Tab` auto-accept |
| Codex CLI | `y`/`n`/`e`/`Esc`, configurable auto-approval modes |
| Cline | GUI "Approve" button, auto-approve settings per tool type |
| Amazon Q | Approval required for writes/commands |

---

## Key Design Decisions & Trade-offs

### 1. Scrollback vs. Full-Screen TUI

- **Scrollback** (Claude Code, Aider, OpenHands): Uses terminal's native scrollback buffer. Natural scrolling, search, copy. But limited re-draw capability.
- **Full-screen TUI** (Codex CLI, Amazon Q): Richer interactive controls. But loses scrollback, harder to review history.
- **Hybrid approach** (Pi agent): Retained mode UI with differential rendering in scrollback. Best of both worlds but complex to implement.

### 2. Verbosity: Show Everything vs. Collapse

The biggest tension across all tools:
- **Claude Code** went from showing everything → collapsing with summaries → users complaining they can't see details → adding Ctrl+O toggle
- **Aider** shows everything, users complain "too much scrolls by"
- **OpenHands** proposing truncation after similar complaints

**Emerging pattern**: Default collapse with easy expand. Show count of hidden content.

### 3. Diff Display: In-Terminal vs. Editor

- **Terminal diffs** (Claude Code, Aider, Codex): Unified diff format, red/green colors. Fast to review but limited.
- **Editor diffs** (Cline): Side-by-side in VS Code. Much richer but requires IDE integration.

### 4. Status/Progress: Spinner vs. Progress Bar vs. Tense

- **Spinner** (Claude Code): Shows activity, customizable. No progress indication.
- **Progress bar** (SWE-agent): Clear progress for batch operations. Not applicable to single tasks.
- **Tense change** (Claude Code): "Reading" → "Read" shows completion without extra UI. Elegant.
- **X of Y** pattern: Not widely used in coding agents (tasks are unpredictable length).

### 5. What NOT to Show

Common suppressions:
- System prompts (all tools)
- Raw API payloads (unless `--verbose`)
- Internal tool schemas
- Cache/token details (pushed to status line or hidden)
- Intermediate reasoning (unless "thinking" mode enabled)

---

## Technology Stacks

| Tool | Language | TUI Framework | Rendering |
|------|----------|---------------|-----------|
| Claude Code | TypeScript | React + Yoga WASM | Custom terminal renderer |
| Aider | Python | Rich library | Markdown + syntax highlighting |
| Codex CLI | TypeScript/Rust | React + Ink | Full-screen TUI |
| Amazon Q | Rust | Ratatui | Immediate-mode |
| Cline | TypeScript | React (VS Code webview) | Native VS Code diff |
| OpenHands | Python | Custom TUI (`tui.py`) | Box-drawing chars |
| SWE-agent | Python | tqdm + logging | Terminal output |
| Continue CLI | TypeScript | Custom | Minimal |

---

## Sources

- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code Changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
- [Claude Code Status Line Docs](https://code.claude.com/docs/en/statusline)
- [Claude Code Tool Output Issue #25776](https://github.com/anthropics/claude-code/issues/25776)
- [Claude Code Collapse Feature Request #17043](https://github.com/anthropics/claude-code/issues/17043)
- [Claude Code Output Formatting Guide](https://claudefa.st/blog/guide/mechanics/output-formatting)
- [Claude Code Spinner Verbs](https://medium.com/@joe.njenga/claude-code-2-1-23-is-out-with-spinner-verbs-i-tested-it-ae94a6325f79)
- [Claude Code Spinner Reverse Engineering](https://medium.com/@kyletmartinez/reverse-engineering-claudes-ascii-spinner-animation-eec2804626e0)
- [HN: Claude Code Log Viewer](https://news.ycombinator.com/item?id=47004712)
- [Aider Edit Formats](https://aider.chat/docs/more/edit-formats.html)
- [Aider Usage Docs](https://aider.chat/docs/usage.html)
- [Aider Options Reference](https://aider.chat/docs/config/options.html)
- [Aider Usability Review](https://slinkp.com/programming-with-aider-20250725.html)
- [Codex CLI GitHub](https://github.com/openai/codex)
- [Codex CLI Features](https://developers.openai.com/codex/cli/features)
- [Codex CLI Ink Components](https://the-pocket.github.io/PocketFlow-Tutorial-Codebase-Knowledge/Codex/01_terminal_ui__ink_components_.html)
- [Codex CLI First Days](https://amanhimself.dev/blog/first-few-days-with-codex-cli/)
- [Amazon Q Developer CLI GitHub](https://github.com/aws/amazon-q-developer-cli)
- [Amazon Q Built-in Tools](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-built-in-tools.html)
- [Cline GitHub](https://github.com/cline/cline)
- [Cline File Operations (DeepWiki)](https://deepwiki.com/cline/cline/8.1-file-operations)
- [Cline Auto-Approve](https://docs.cline.bot/features/auto-approve)
- [OpenHands CLI](https://docs.openhands.dev/openhands/usage/cli/terminal)
- [OpenHands CLI Truncation Issue #102](https://github.com/OpenHands/OpenHands-CLI/issues/102)
- [SWE-agent Docs](https://swe-agent.com/latest/usage/cl_tutorial/)
- [Continue CLI Docs](https://docs.continue.dev/guides/cli)
- [Pi Agent: Minimal Coding Agent](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/)
- [OpenCode TUI](https://opencode.ai/docs/tui/)
- [CLI UX Progress Display Patterns](https://evilmartians.com/chronicles/cli-ux-best-practices-3-patterns-for-improving-progress-displays)
