# Mission Control UI/UX Audit — Deferred (NOTE / MINOR)

**Audit date:** 2026-04-25
**Policy:** NOTE/MINOR findings recorded but **not fixed**. If the same NOTE persists across 3+ consecutive audits, the hunter may escalate to IMPORTANT.

Deferred items (76 total) — full text in `_hunter-findings/<hunter>.md`. Themed roll-up below for ease of review.

## First-time-user (NOTE — Codex)
- "Managed" / "managed workspace" / "managed root" / "queueing work" jargon — define once, then user-facing nouns
- "branches/worktrees" assumes Git knowledge — say "temporary git worktrees" or "temporary copies of this repo"
- Filters expose Improve/Certify/Merge/Queue/Interrupted/Removed/Other/Active before user has any tasks — hide until there's work
- Refresh failures rely on transient toast + tiny "refresh failed" text — make persistent near button
- Diagnostics uses operator-jargon headings (operator actions / command backlog / runtime issues / malformed event rows)
- Recent Activity says "queue, watcher, land, and run outcomes" — too many Otto terms in one sentence

## Info density (NOTE — Codex)
- Diagnostics command cards repeat commandBacklogLine in collapsed and expanded
- Run Detail loading and no-selection both render as `Select a run.` — track pending selected run; render `Loading review packet for <run>`

## Error / empty states (NOTE — Codex)
- All toasts auto-dismiss after 3.2s; errors should be sticky until dismissed
- Initial `projectsState=null && data=null` falls through to main shell briefly exposing Mission Control/New job
- Diff buttons/tabs disabled without visible reason when branch missing or in-progress

## Long-string / overflow (NOTE — Codex)
- Recent activity/timeline messages: one-line ellipsis with `title` only; events capped at 50 — row expansion or "load more"
- Inspector title ellipsizes long run titles/intents with no visible full-text affordance — title tooltip + copy/metadata link
- Project launcher list has no max-height/pagination; branch meta pills overflow on long branch names

## State management (NOTE — Codex)
- `?run=X&view=tasks` initially renders detail as "Select a run." while loading — pass loading flag, render "Loading run…"
- Filters intentionally not in URL — codify as ephemeral OR add URL params
- Global polling and 1.2s log polling continue while tab backgrounded — `visibilitychange` listener
- Two-tab consistency is poll-only; stale buttons remain clickable until next poll — `BroadcastChannel`

## Destructive-action safety (NOTE — Codex)
- Toast state is message-only; destructive outcomes can't offer Undo / Cancel queued / Open run recovery
- Disabled `Land all ready` is visually distinct but has no visible/accessible reason

## Evidence trustworthiness (NOTE — Codex)
- Usage/cost displays as bare $X without saying live/final/total/certifier-only/estimated
- UI is single-run only; retried/requeued runs have no parent/child cross-reference

## Packaging (NOTE — Codex)
- Package data includes `static/*` and `static/assets/*` only — recursive package + installed-wheel smoke test

## Heavy-user (NOTE — Claude — 12 items)
- No keyboard hotkeys for power use (folded into Important when keyboard-only theme)
- No "open in editor" affordance for project files
- Run history doesn't remember last opened tab
- No daily/weekly cost rollup card
- No outcome-based color in history table
- No "duplicate run with provider X" affordance
- Dirty-target consent forgets per-attempt (but same worktree)
- "View report" is the only proof affordance — no compare proof packets
- No "what changed since last successful run on this branch"
- No quick `?` help overlay
- No quick log-tail (only inspector)
- No project filter in launcher when many projects

## Accessibility (NOTE — Claude — 7 items)
- A11Y NOTE-1 — `aria-controls` to unmounted IDREFs; remove or render when target mounts
- A11Y NOTE-2 — dl-wrapper grouping should use `role="presentation"` to silence
- A11Y NOTE-3 — Missing `aria-describedby` on certification help text
- A11Y NOTE-4 — `prefers-reduced-motion` not honored (toast 160ms transition)
- A11Y NOTE-5 — Generic pre `aria-label`s ignored by screen readers
- A11Y NOTE-6 — Skip-link href target unmounted when no main content
- A11Y NOTE-7 — Section aria-label drift between similar surfaces

## Visual coherence (NOTE — Claude — 7 items)
- No icon set
- Modal/panel padding inconsistency
- Two ad-hoc shadow values
- Weight-700 doing both caps-label and emphasis duty + single weight-800 outlier
- Diagnostics denser than tasks at same outer rhythm
- No link/visited differentiation
- Jagged activity-item grid (fixed 76px column with variable content)

## Microinteractions (NOTE — Claude — 14 items)
- `tbody tr { cursor: pointer; }` applies to empty-state rows — false affordance
- `useDialogFocus` correctly restores focus, but trigger button can unmount after merge — focus drops to body
- `.proof-artifacts button.selected` looks like a hover state — selection ambiguity
- Inspector close-then-reopen loses scroll position
- No drag-and-drop affordances
- No resizable panels (inspector splitter)
- No "scroll into view" on click of nested element
- Animation duration not specified anywhere
- Optimistic UI not used — every action waits a refresh
- Hover state doesn't include focus state for some controls
- Cursor: changes to pointer over clickable, default over text — some controls miss this
- Tooltip delay inconsistent across hover-revealed UI
- After-action confirmation: silent success on some actions
- Form submit doesn't change to "Submitting..." on JobDialog inner state

## Keyboard-only (NOTE — Claude — 13 items)
- K-06 No Cmd-K command palette
- K-08 Destructive actions buried under `<details>`
- K-10 Mixed-focusable activity feed
- K-11/K-12 Disabled-button reasons live in `title` only (also flagged as IMPORTANT in microinteractions theme)
- K-13 Diff file list lacks roving tabindex
- K-14 No `prefers-reduced-motion` (also flagged a11y NOTE)
- K-15 Focus ring same color as primary background — invisible on CTAs
- K-16 Checkbox label focus weak
- K-17 Last-error banner not aria-live
- K-18 ConfirmDialog "Working" label-swap re-announces (screen-reader noise)
- K-19 Escape silently discards typed intent
- K-20 Tab order puts Switch project before New job (rare > common)
- (Plus folded items already in IMPORTANT under accessibility/keyboard themes.)

---

## Escalation notes

If any of these reappear in a future audit and the user's behavior shows it actually causes friction:
- "Filters not in URL" — likely escalates to IMPORTANT once a power user hits it
- "No keyboard hotkeys" cluster — already partly escalated; remainder may follow
- "No notification API" — would escalate when long-running unattended jobs become common
- "Optimistic UI" — escalates when polling latency becomes a complaint
