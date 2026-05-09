# Otto v5 Tier Guide

Otto v5's `--tier` flag tells the Lead how to decompose the work. Tiers
are knob presets; the Lead's prompt is adapted accordingly.

## Tiers at a glance

| Tier | When | Lead behavior |
|---|---|---|
| `auto` (default) | You don't know or don't care | Lead decides based on the intent. Trivial intents stay inline; multi-feature intents emit children. |
| `solo` | You know the work is one focused unit | Lead is forced to call `mcp__otto__begin_inline`. No subtask emission allowed. Cheapest. |
| `lead` | You expect ~3–7 user-visible features | Lead is encouraged to emit ~3–7 child tasks via `mcp__otto__submit_subtask`. Each child gets its own session, worktree, integration branch. |
| `modular` | Multi-subsystem (web + CLI + API) | Lead is required to emit ≥3 strategic child tasks. Strong preference for decomposition. |

## How to choose

If you have to pick, ask yourself: **"How many distinct user-visible
behaviors does this intent describe?"**

- **One behavior** ("convert CSV to JSON CLI", "fix typo in README", "add a
  delete button") → `solo`. Forces single inline build with the build/test
  agent split. Cheapest.

- **A handful of related behaviors that share state** ("add CRUD for
  transactions with filter/search") → `solo` or `auto`. Treat the cluster as
  one unit because the features share data and UI surface.

- **Several distinct user-visible features** ("a dashboard with X, Y, Z")
  → `lead` or `auto`. Lead emits one child per feature; each child has its
  own audit and proof packet. Children run in parallel up to `--max-parallel`.

- **Multiple subsystems with their own runtimes** ("a web app + a CLI +
  a public API") → `modular`. Lead does discovery first, then emits one
  child per subsystem.

## What each tier costs (rough)

| Tier | Per-task cost | Wall time | Risk |
|---|---|---|---|
| `solo` | $0.10–$1.00 | 1–5 min | Low. One Lead, one verify call. |
| `lead` | $1.00–$8.00 | 5–30 min | Medium. N subagent calls + N audit calls + integration. Linear in feature count. |
| `modular` | $5.00–$30.00 | 15–90 min | High. Same as `lead` plus a discovery phase. |

## Examples

```bash
# Single-feature CLI:
otto v5 run "Convert CSV stdin to JSON stdout" --tier solo

# Medium app:
otto v5 run "TODO list with localStorage persistence" --tier auto

# Many features with shared state:
otto v5 run "Personal finance dashboard with transactions, budgets, charts, CSV import/export" --tier lead

# Multi-subsystem:
otto v5 run "Slack-style chat: web client + REST API + admin CLI" --tier modular
```

## Manual override of decomposition

If autopilot's decomposition is wrong:

```bash
# Force inline even though the intent looks complex:
otto v5 run "<intent>" --tier solo

# Force fan-out:
otto v5 run "<intent>" --tier modular
```

## When the Lead overrides the tier

The Lead can override its tier preset if the actual project state forces
its hand. For example, `--tier modular` on a brownfield repo will quietly
fall back to inline if the change is small. The override is logged in the
proof packet's "Decisions" section.

## Limits and bounds

- `--max-parallel <N>`: maximum concurrent child tasks. Default 3. Higher
  → more wall-clock parallelism, higher peak provider cost.
- `--tree-budget-usd <N>`: cumulative cost cap across the full task tree.
  Default $25. When hit, the watcher refuses new dispatches; in-flight
  tasks finish, integration runs, verdict is `partial`.
- `--budget <seconds>`: per-task wall-time cap. Default 600.

## Cross-reference

- `--review-first-decomp`: pause after root Lead emits children, allow user
  to accept/edit/replace before dispatch. Independent of tier.
- `--phase1-only`: run root Lead only, skip child processing. For testing
  the Lead primitive in isolation.
