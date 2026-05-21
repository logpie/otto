# Repository Guidelines — Otto (Codex addenda)

**The shared playbook lives in `CLAUDE.md`. Read it first.** Hard Rules,
Decision triggers, NEVERs, architecture invariants, debugging recipes —
all the rules that bind on EVERY agent working on Otto live there,
not here.

This file is Codex-only addenda. If a rule appears in both files,
`CLAUDE.md` wins. If you're tempted to add a rule here that would
apply to Claude too, put it in `CLAUDE.md` instead.

## Codex-specific NEVER (in addition to CLAUDE.md NEVER list)

- Never set MCP `approval-policy` to anything but `"never"`. Any other value hangs forever.
- Never widen `sandbox` beyond what the caller asked for. `workspace-write` to edit; `read-only` to consult.
- Never `git checkout -b` or attempt to create branches / PRs from inside a Codex sandbox — they can hang. The Claude caller does branch/PR work itself.
- Never open new long-lived exec sessions casually. Poll the existing session.

## Codex-specific decision triggers

| When you... | Do this |
|---|---|
| Are continuing prior Codex work | Use `mcp__codex__codex-reply` with the `threadId`. Don't start a fresh thread. |
| Encounter a stale browser bundle after a web change | Rebuild + restart + verify served bundle reflects the new commit. Stale servers caused multiple false "fixed" claims. |
| Need to run `npm run web:build` and web backend tests | Build first, then test. The bundle stamp can race the tests under freshness checks. |
| Run Python tests | Use `.venv/bin/pytest` or `uv run pytest`. System `python3 -m pytest` may not have pytest installed. |
| Need cross-device UI testing | Verify host binding. MacBook/iPhone over Tailscale needs `0.0.0.0`, not just `127.0.0.1`. |
| Asked to validate a UI bug | Exercise the live browser flow. Don't rely on screenshots or happy-path API checks for interactive UI. |
| About to act on an ambiguous worktree | Inspect `pwd`, `git branch --show-current`, `git status --short --branch`, `git worktree list` first. |

## Build / test / dev commands

| Command | Purpose |
|---|---|
| `uv run python scripts/test_tiers.py smoke` | Smallest fast confidence gate |
| `uv run python scripts/test_tiers.py fast` | Day-to-day non-browser gate |
| `uv run python scripts/test_tiers.py web` | TypeScript + Mission Control backend/model tests |
| `uv run pytest -q --maxfail=10` | Full default non-browser Python suite |
| `uv run ruff check otto scripts tests` | Lint Python |
| `npm run web:typecheck` | Type-check the web client |
| `npm run web:build` | Build the static web bundle |
| `.venv/bin/python3 -m otto.cli web --host 0.0.0.0 --port 9000 --allow-remote --project-launcher --projects-root /Users/yuxuan/otto-projects --no-open` | Launch Mission Control locally |

## Testing posture

Tier discipline (escalate before merge):

| Risk level | Tier |
|---|---|
| Low (typo / lint / UI polish) | Direct fix, smoke |
| Ordinary Python edit | `test_tiers.py fast` |
| Mission Control backend/client | `test_tiers.py web` |
| Broad infra change | Full pytest |
| Interactive UI behavior | Browser-level user flow |

Add focused regression tests for every behavioral fix. Do not rely on
screenshots or happy-path API checks for interactive UI bugs.

## Debugging policy

| Bug type | Workflow |
|---|---|
| Obvious compiler / lint / typo / UI polish | Direct fix |
| Ordinary | Reproduce → inspect → fix → test |
| Ambiguous / stateful / flaky / process / queue / merge / browser / persistence / performance / repeat-failure | Full `debug-hypothesis` workflow |

## Product surface

Web Mission Control is the primary product surface. Do not revive
deprecated TUI work except where needed to keep existing CLI/queue
behavior correct.

When dogfooding Otto, use the core autonomous path: queue a real task,
let the queue runner execute build/certify/fix or proof-repair, then
review/land through Mission Control. Standalone `otto certify` is
diagnostics only unless the user explicitly asks for it.

## For `$code-health`

Use parallel subagents and multiple rounds unless explicitly told otherwise.

## Pointers

- **`CLAUDE.md`** (this directory) — shared playbook. Source of truth.
- **`/Users/yuxuan/work/cc-autonomous/codex-learnings.md`** — persistent
  Codex memory for Otto. Read before non-trivial work; covers
  token-accounting rules and per-area gotchas.

---

**Sync contract**: rules that apply to both Claude and Codex live in
`CLAUDE.md`. This file is Codex-only addenda. If you find yourself
copying a rule from `CLAUDE.md` here, stop — the two files have
drifted out of sync. Update `CLAUDE.md` (the source) and remove the
copy here.
