# Repository Guidelines — Otto

**Otto's project-wide development instructions live in `CLAUDE.md`. Read it
first.** That file contains the Hard Rules, architectural invariants,
anti-patterns, debugging protocols, and workflows that bind on every agent
working on Otto, regardless of provider. Maintained as the single source
of truth so Claude and Codex stay aligned.

This file (`AGENTS.md`) is Codex-specific addenda only. If a rule appears
in both files, `CLAUDE.md` wins.

## Codex-specific addenda

Codex has different sandbox semantics, model behavior, and tooling than
Claude. The notes below cover gotchas that are particular to Codex
sessions on Otto.

### Repeated mistakes to avoid

See `/Users/yuxuan/work/cc-autonomous/codex-learnings.md` for the
persistent Codex memory file. Highlights:

- **Stay in the user-requested worktree.** Inspect `pwd`,
  `git branch --show-current`, `git status --short --branch`, and
  `git worktree list` before acting. Do not assume a specific I2P worktree.
- **Don't assume the browser shows the latest code.** After web client or
  backend changes, rebuild + restart + verify the served bundle/API
  reflects the new commit. Stale servers caused multiple false "fixed"
  claims.
- **Don't race `npm run web:build` with web backend tests.** Build first,
  then test — the bundle stamp can race the tests under freshness checks.
- **Use the repo Python env.** `.venv/bin/pytest` or `uv run pytest`.
  System `python3 -m pytest` may not have pytest installed.
- **Verify host binding** for cross-device testing. For
  MacBook/iPhone over Tailscale, server must listen on `0.0.0.0` (not
  just `127.0.0.1`).
- **For UI bugs, exercise the live browser flow.** Don't rely only on
  screenshots or happy-path API checks. Modal submission, queue
  start/stop, run detail, landing, logs, proof/evidence views.
- **Don't open new long-lived exec sessions casually.** Poll existing
  sessions instead of stacking duplicates.

Full version with token-accounting rules and per-area gotchas is in
`codex-learnings.md`.

### When Claude is the caller

When this Codex session was dispatched by Claude (the typical case
during Otto development), expect:

- **`approval-policy: "never"`** is always set by the caller. Don't ask
  for confirmation; do the work or escalate via the structured
  escalation record.
- **`sandbox: "workspace-write"`** when editing; `"read-only"` when
  consulting. Don't widen the sandbox.
- **Don't create branches or PRs** — `git checkout -b` can hang in the
  Codex sandbox. The caller (Claude) does branch/PR work itself.
- **Use `mcp__codex__codex-reply`** with a `threadId` to continue a
  conversation, not a fresh `mcp__codex__codex` call.

### Build / test / dev commands

| Command | Purpose |
|---|---|
| `uv run python scripts/test_tiers.py smoke` | Smallest fast confidence gate |
| `uv run python scripts/test_tiers.py fast` | Day-to-day non-browser gate (excludes slow/process/integration/heavy) |
| `uv run python scripts/test_tiers.py web` | TypeScript + Mission Control backend/model tests |
| `uv run pytest -q --maxfail=10` | Full default non-browser Python suite |
| `uv run ruff check otto scripts tests` | Lint Python |
| `npm run web:typecheck` | Type-check the web client |
| `npm run web:build` | Build the static web bundle |
| `.venv/bin/python3 -m otto.cli web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher --projects-root /Users/yuxuan/otto-projects --no-open` | Launch Mission Control locally |

### Testing posture

Add focused regression tests for every behavioral fix. Tier discipline:
- Smoke for low-risk Python edits.
- `test_tiers.py fast` for ordinary changes.
- `test_tiers.py web` for Mission Control backend/client changes.
- Full pytest for broad infra changes.
- Browser-level user flows for interactive UI behavior.

Do not rely only on screenshots or API checks for interactive UI bugs.

### Debugging policy

Use direct fixes for obvious compiler/lint/typo/UI-polish. Use
lightweight reproduce-inspect-fix-test for ordinary bugs. Escalate to
the full `debug-hypothesis` workflow only for:
- ambiguous / stateful / flaky bugs
- process/runtime / queue/resume/merge / browser / persistence /
  performance / repeated-failure bugs

### Mission Control as the primary surface

Web Mission Control is the primary product surface. Do not revive
deprecated TUI work except where needed to keep existing CLI/queue
behavior correct.

When dogfooding Otto, use the core autonomous path: queue a real task,
let the queue runner execute build/certify/fix or proof-repair, then
review/land through Mission Control. Standalone `otto certify` is
diagnostics only unless the user explicitly asks for it.

### Commit & PR guidelines

Keep commits scoped and describe the user-visible behavior fixed or
added. Include tests run in PR notes. Do not merge or push `main`
unless explicitly requested. Work in the active worktree and preserve
unrelated user changes.

For `$code-health`: use parallel subagents and multiple rounds unless
explicitly told otherwise.

---

**Single-source-of-truth note**: when an Otto-dev practice needs to
change, update `CLAUDE.md`. The two files MUST stay aligned on rules
that apply to both agents. `AGENTS.md` (this file) holds Codex-only
addenda; if you're tempted to edit a section here that has a Claude
equivalent in `CLAUDE.md`, move the shared rule to `CLAUDE.md` instead.
