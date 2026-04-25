# Hunter: Information Density / Signal-to-Noise (Codex)

Thread: `019dc6bd-efe6-7a91-81ea-34d941a82582`
Total findings: **0 CRITICAL, 12 IMPORTANT, 2 NOTE**

1. IMPORTANT, M, hierarchy — `App.tsx:1253`: Task cards lead with id/title; summary, branch, reason hidden behind `More`. Fix: visible card body shows `summary/intent`, `branch`, `reason/last event`, elapsed/cost; drawer for long metadata.
2. IMPORTANT, S, status-clarity — `App.tsx:1237`: card meta mixes files, stories, queue status, live cost into identical unlabeled pills. Fix: typed chips `3 files`, `2/2 stories`, `$0.42`, `12m`; suppress `-`.
3. IMPORTANT, S, status-clarity — `styles.css:652`: every task status badge is the same gray pill — `ready`, `blocked`, `running`, `failed` don't scan differently. Fix: add state/tone classes; success/warning/danger/running treatments.
4. IMPORTANT, S, status-clarity — `styles.css:1210`: `queued`, `paused`, `interrupted`, `stale` all render as amber table text despite different urgency. Fix: split stale/interrupted into danger/attention; queued stays neutral.
5. IMPORTANT, S, status-clarity — `App.tsx:1169`: `resultBanner.severity === "information"` renders with warning style (every non-error maps to `warning`). Fix: add `information`/neutral banner class; map severities explicitly.
6. IMPORTANT, M, missing-signal — `App.tsx:2388`: Mission Focus working state says only `N tasks in flight`; doesn't identify active task, elapsed, cost, last event. Fix: headline hottest active run: `<task> · <branch> · <elapsed> · <cost> · <last event>`.
7. IMPORTANT, M, hierarchy — `App.tsx:2557`: task cards sort only by title, so failures, stale runs, ready-to-land work can be buried alphabetically. Fix: sort by operational priority: attention severity, active/elapsed age, ready, then title.
8. IMPORTANT, S, missing-signal — `App.tsx:1388`: history rows omit `completed_at_display` and `intent` even though both exist in `types.ts:250`. Fix: add age/completed column; render intent under or instead of summary.
9. IMPORTANT, M, hierarchy — `App.tsx:1298`: Recent Activity renders all events before history; history shows duration not age, so latest outcomes get pushed below less-important events. Fix: merge events and history into one time-sorted feed with outcome + age + intent.
10. IMPORTANT, M, missing-signal — `App.tsx:1042`: runtime warnings compressed into one pipe-separated string with details only in tooltip. Fix: compact issue rows/chips sorted by severity, with visible label, next action, backlog counts.
11. NOTE, S, redundancy — `App.tsx:1079`: diagnostics command cards repeat `commandBacklogLine` in collapsed and expanded; runtime issues repeat next action at `:1093`. Fix: keep summary unique; expanded shows only extra detail/action.
12. IMPORTANT, M, hierarchy — `App.tsx:197`: run selection always resets inspector to `proof`; primary shortcut is always `Open proof` at `:1498`. Fix: choose default tab by state — live → Logs, failed → failure/proof or logs, ready/merged → Proof, diff-focused → Diff.
13. IMPORTANT, S, hierarchy — `App.tsx:1618`: Proof tab shows `Next action` before `What failed`; failure cause not first scannable field. Fix: for failed packets, render failure reason/excerpt first, then next action.
14. NOTE, S, missing-signal — `App.tsx:1472`: run detail loading and no-selection both render `Select a run.` Fix: track pending selected run, render `Loading review packet for <run>` with compact skeleton.
