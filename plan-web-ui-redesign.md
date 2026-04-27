# Otto Web Mission Control — UI/UX Redesign Plan

Owner: TBD · Date opened: 2026-04-26 · Status: draft for review

This plan synthesizes a hands-on audit of the running app (1440px desktop, 1024px tablet, 375px mobile) plus a code-level review of `otto/web/client/src/` (App.tsx 8.4k lines, styles.css 4.8k lines, types.ts 650 lines). Findings are organized into themes; each theme has prioritized fixes and a recommended order of work.

---

## 0. Summary of the central problem

The app is functionally rich but **does not have a strong primary object**. Every screen tries to be a complete dashboard: header status, IDLE banner, kanban, project overview, recent activity, review packet, execution panel — all visible at once. As a result:

- **New users** can't tell what they should look at first or what action is expected of them.
- **Existing users** scan the same data in three different places (Task Board landed column, Review Packet "landed in main", Recent Activity success row, Project Overview "5 landed").
- **Visual hierarchy is flat**: every region has the same heading size, every status uses a slightly different green, every stat block has the same density.
- **Code is a god-component**: 22 sub-components and 38 `useState` calls live in one 8.4k-line file with no shared design tokens.

The redesign target: **one primary object per screen**, supported by a clear status rail, with everything else demoted to drawers or tabs. Plus a small, real component library so visual consistency stops being optional.

---

## 1. Information architecture (where things live)

### 1.1 Pick a primary object per screen

| Screen | Primary object | Secondary | Demoted |
|---|---|---|---|
| **Project launcher** | Project list | Create project, projects root path | Background explainer |
| **Project home (Tasks)** | Task Board kanban | Active run rail | Stats, recent activity, system health |
| **Run detail** | Selected run (logs / diff / proof / artifacts) | Status rail | Job queue (collapsed) |
| **Health** | System status (watcher/repo/queue files) | Diagnostics issues | Per-task list (remove — duplicate of Tasks) |

### 1.2 Tasks vs Health duplication
Health currently re-renders the same 5 landed tasks under "Review and landing." Strip task lists out of Health entirely. Health = "is the *system* healthy?" only.

### 1.3 Review Packet should not be a permanent right rail
Today the Review Packet rail occupies ~300px on the right at all times. When nothing needs review (post-merge), it shows "No merge action is needed" — a 600px tall card saying nothing. Replace with:
- **Active run** (in flight): rail shows live status, phase, elapsed, current tool call.
- **Needs review** (queued ready-to-land): rail is the merge action surface.
- **Idle**: rail collapses to a 1-line strip ("Nothing pending — last landed: X 12m ago"), workspace expands to full-width.

### 1.4 Idle banner ("No task needs action")
When queue is empty, the IDLE banner + duplicate `Queued/running 0 / Needs action 0 / Ready 0` tiles take ~150px vertical for redundant zero counts. Collapse into:
- One state strip: `Queue is empty. Last activity: X · [New job]`
- The Task Board's column counters keep the same numbers; no need to repeat.

### 1.5 Recent Activity — collapse repeat lifecycle events
Current feed shows `watcher started / watcher stopped / watcher started / watcher stopped` four times in a row. Group consecutive same-event lines into "watcher restarted 4× over 38m." Surface one icon per group, expand on click.

### 1.6 Deep-link bug
URL `?view=tasks&run=add-dark-mode-then-polish-ui-to-look-ff4fbc` while project is unselected lands on launcher. Clicking the project loses the `run=` param and lands on a *different* run (the latest one). Either:
- (a) Persist the deep-link `run=` through project-open, or
- (b) Show "open project to view this run" with a one-click resolve.

---

## 2. New-user onboarding

### 2.1 Launcher needs context
Today the launcher tells you *where* projects live but not *what an Otto job is*, *what it costs*, or *how long*. Add (collapsible, dismissible):
> "**How Otto works.** Queue a job → Otto runs it in an isolated git worktree → review the result and merge. A typical small feature takes 5–15 min and uses ~$0.50–$2 in tokens."
Link to a longer doc.

### 2.2 First-run empty workspace
When a project has zero tasks, the workspace currently shows the same idle banner + Task Board with empty kanban. Replace with a 3-step inline tour:
1. **Describe what you want** → opens New Job modal
2. **Otto runs it** → preview of what live status looks like
3. **Review and merge** → preview of Review Packet

Dismissed permanently after first successful run.

### 2.3 Glossary tooltips
The kanban column labels (`NEEDS ACTION / QUEUED / RUNNING / READY TO LAND / LANDED`), watcher, heartbeat, in-flight — none are explained. Add a `?` icon by each first-time concept that hover-explains.

### 2.4 Don't show internal IDs to new users
`pid 30608`, `0a3494c`, `2026-04-25-051721-18f4a2`, `merge-1777223250-38335-dd5e0e66`, run-id slugs — all useful for power users, all noise for new users. Hide behind a "Show technical details" toggle in the user prefs (default: hidden for first 7 days).

---

## 3. Visual design

### 3.1 State color palette (top fix)
Today there are at minimum:
- 4 different greens (LANDED chip, watcher running, success activity, merged tag)
- 3 different reds (failure activity, error toast, blocked merge)
- 2 different yellows (warning, "describe intent" disclosure)

Define **5 semantic states**: `success / warning / error / info / neutral`. Each gets one fill, one tint, one text shade. Apply everywhere — chips, banners, tags, metrics, buttons. Removes the "different shades = different meanings?" cognitive load.

### 3.2 Typographic scale
Today: H2 (Task Board / Review Packet / Project Overview / Recent Activity) all same. H3 mostly same as H2.
Define 4 levels:
- **Display**: page title (only one per screen)
- **Section**: large region heading
- **Subsection**: card heading
- **Meta**: small caps labels (PROJECT, BRANCH, etc.)

Reserve display for the screen's primary object only.

### 3a. Copy / text audit

`App.tsx` contains ~150 user-facing strings. Audit found significant duplication, verbosity, and inconsistent terminology.

#### 3a.1 Direct duplications (same fact, different sentence)

| # | Strings | Issue |
|---|---|---|
| 1 | "Project ready · No jobs yet" + "No task needs action" + "Queue the next product task when the current work is complete." + "Start your first build / Describe what you want Otto to build…" | Four different empty-state strings for the same condition |
| 2 | "Inherit from otto.yaml" used by both provider and model defaults via separate helpers | Centralize to one constant |
| 3 | "No prior runs to improve. Run a build first." vs "…Run a build first, then come back." | Two near-identical strings |
| 4 | "Log will appear when the agent starts writing." in both empty-state and loading paths | Loading should say "Loading log…" only |
| 5 | "Otto owns build, certify, fix, and recovery as separate phases." + "Otto runs AI coding jobs in isolated git worktrees…" + intent placeholder | Lifecycle explained 3× |
| 6 | "Confirm the dirty target project before queueing." × 3 variants | Three call sites, one idea |
| 7 | "Start the watcher to dispatch this queued task." + "…to run queued jobs." + "…to run the queued job." + "…to apply pending commands." | Four watcher-CTA variants |
| 8 | "Stories" surfaces: stat, chip, log line `STORIES_TESTED/PASSED`, proof `5 passed, 0 fail…` | Five surfaces for one number |
| 9 | Counts surfaces: sidebar `queued/ready/landed`, overview `ready · attention · landed`, board column counters, idle banner tiles | Four locations, same numbers |
| 10 | Merged-state strings on a single packet: `merged` tag + `LANDED IN MAIN` eyebrow + "Already merged into main" headline + "No merge action is needed." | Five strings for "this is done" |

#### 3a.2 Verbose / wordy

| # | Current | Replace with |
|---|---|---|
| V1 | "All projects live under this directory. Otto manages projects in isolated git worktrees so it never touches your other repos on this machine. The repo that launched Mission Control is intentionally excluded — pick or create a project below to start." (60 words) | "Otto creates each project in its own folder. Pick one below or create a new one." |
| V2 | "Otto runs AI coding jobs in isolated git worktrees, then lets you review logs, diffs, and merge results." | "Queue a job, review the result, merge." |
| V3 | "Watcher is running; stop it only when you need to pause queue processing." | "Running. Stop to pause the queue." |
| V4 | "Queue a job before starting the watcher." | "Queue a job to start." |
| V5 | "This job can create temporary git worktrees and modify files under this folder." | Drop — implied. |
| V6 | "Describe the requested outcome (intent) to enable queueing." | "Describe what to build to queue." |
| V7 | "Abort the interrupted git merge, then run Otto's conflict-resolving merge for the remaining ready work. This may invoke the configured merge provider." | "Abort the merge and re-run conflict resolution. May invoke the merge provider." |
| V8 | "Clean up the in-progress git merge without landing the remaining ready tasks. Use this when you want the repository back in a safe state before deciding what to do next." | "Abort and don't land. Returns the repo to a clean state." |
| V9 | "A previous landing left git mid-merge. Recover will clean up that state and relaunch conflict-resolving landing for the remaining ready work." | "A previous landing left a partial merge. Recover will clean it up and retry." |
| V10 | "Open blocked work or the health view to inspect the failure, stale run, missing branch, or recovery action." | "Open the failure or Health to investigate." |
| V11 | "Otto generates a spec, pauses, and waits for approval in Mission Control." | "Generates a spec, then waits for approval." |
| V12 | "Use an existing approved spec file; the CLI validates it before building." | "Use an existing spec file." |
| V13 | "Build runs without post-build product certification." | "Skips certification." |
| V14 | "Inherits certifier_mode and skip_product_qa from otto.yaml, then built-in defaults." | "Uses otto.yaml defaults." |
| V15 | "Improve bugs defaults to thorough certification unless you choose fast or standard here." | "Defaults to thorough for bug improvements." |
| V16 | "Open the README, proof, logs, and changed files to infer how to run this artifact." | "See README and logs to run." |
| V17 | "Mission Control is loading the queue, runs, and repository status." | "Loading project state." |
| V18 | "Detailed progress is shown on each task card." | Drop — implied. |
| V19 | "A single agent session owns the whole loop; useful for exploration or fallback." | "Single session. Faster, less reliable." |
| V20 | "Otto owns build, certify, fix, and recovery as separate phases." | "Phases run separately. Default." |

#### 3a.3 Jargon / inconsistent terminology

| # | Issue | Fix |
|---|---|---|
| T1 | "Heartbeat / PID / In flight / Watcher" — internal lifecycle exposed as primary | Hide behind "Show technical details" toggle |
| T2 | "Landed" vs "Merged" vs "Landed in main" vs "Already merged into main" | Pick one verb (`landed`) and use consistently |
| T3 | "Queue / Queueing / Queued" mix | Standardize verb/noun/adjective forms |
| T4 | "Run" vs "Job" vs "Task" used interchangeably | Define: **job** = what you queue, **run** = one execution, **task** = kanban card |
| T5 | "Certify / Verify / Verification / Certification / Certifier / QA / evaluator" — 6 terms for one phase | Pick one user-facing word, reserve internals for prompts |
| T6 | "improve / improve bugs / improve feature / improve target / hillclimb evaluation / target evaluation" exposed as CLI taxonomy | Rename in UI or hide behind explainer |
| T7 | "fast / standard / thorough" with `--fast`, `--standard`, `--thorough`, `--no-qa` flag suffixes in dropdown | Drop flag suffixes; tooltip only |
| T8 | All-caps eyebrows ("PROJECT FOLDER", "LANDED IN MAIN", "NEEDS ACTION") at varying sizes/colors | Standardize one size + one tint |
| T9 | "Inherit: Claude (otto.yaml)" / "Inherit: thorough bug certification (improve default)" | Move parenthetical metadata to subtext |

#### 3a.4 Small writing nits

- "Will run with: claude · default model · effort=default effort · verification=fast" — `default effort` repeated reads as a typo.
- "23.1M tokens" with label "Total tokens" — unit doubled.
- "Final · 155 lines · 14.1 KB" log header — useful but missing verdict, time, cost.
- "5 visible tasks for main." — awkward; → "5 tasks on main."
- Confirm dialogs use polite full sentences ("Yes, stop the watcher now.") — confirm titles should be the action.
- Toast punctuation inconsistent: short ones with periods, long ones without.
- Empty kanban columns: "No blocked work." / "No queued or running tasks." / "Nothing ready yet." — three differently-toned negations. Standardize to "—" or "No tasks."

#### 3a.5 Style guide to adopt

1. **One vocabulary**: job (verb+noun) / run (execution) / task (board card) / phase (build/certify/fix). No synonyms.
2. **One state name per state**: `Idle / Queued / Running / Ready / Landed / Failed`.
3. **Empty states ≤5 words**: "Nothing here." / "No tasks." / "No prior runs."
4. **Helper text ≤10 words**. If you need more, link to docs.
5. **Confirm dialogs**: title is the action; body explains what changes in 1 sentence.
6. **No flag syntax in labels** (`--fast`, `--no-qa`). Tooltip only.
7. **Don't repeat units** (`23.1M tokens` + label `Total tokens` → `23.1M` with label `Tokens`).
8. **No filler** ("when the current work is complete", "if you still want to queue").
9. **Status pills**: 1 word ("Idle", "Running"), never sentences.
10. **Drop period from toasts under 6 words**.

Estimated impact: dedup + tightening cuts **30–40% of total copy weight**. Most of this lands in Phase 1 — text-only changes with no architecture risk.

---

### 3b. Information duplication across components

Independent of the copy audit (§3a, which is about *string* duplication), there's a structural problem: **the same data point is rendered in multiple components on the same screen at the same time.** The user has to triangulate between 3–5 surfaces to confirm one fact. Examples below — these are not the only ones; the pattern is pervasive.

#### 3b.1 Inventory of duplicate facts visible simultaneously

| Fact | Surfaces showing it (same screen, same time) |
|---|---|
| **Queue counts** | (1) Sidebar "TASKS queued 0 / ready 0 / landed 5"; (2) Idle banner tiles "Queued/running 0, Needs action 0, Ready 0"; (3) Task Board column counters; (4) Project Overview "Current work 0 open · 0 ready · 0 attention · 5 landed" |
| **Selected run identity** | (1) Inspector header "queue: build-an-expense…"; (2) Review Packet "Build an expense approval portal…"; (3) Task card pressed in board; (4) Browser tab title; (5) URL `?run=…` |
| **Run verdict / state** | (1) Task card pill "✓ LANDED"; (2) Review Packet `merged` tag + `LANDED IN MAIN` eyebrow + "Already merged into main"; (3) Recent Activity "SUCCESS"; (4) Logs `VERDICT: PASS` + `— SUCCESS in 11:24 —`; (5) Result tab proof markdown `Verdict: PASS` |
| **Story count** | (1) Review Packet stat "Stories 5/5"; (2) Task card chip "5/5 stories"; (3) Project Overview "Stories 29/29"; (4) Logs `STORIES_TESTED: 5 / STORIES_PASSED: 5`; (5) Proof markdown "5 passed, 0 fail, 0 warn" |
| **Changed-file count** | (1) Review Packet stat "Changes 10 files"; (2) Review Packet `Changed files 10 files` disclosure; (3) Code changes pane file list; (4) Task card chip "4 files" |
| **Evidence count** | (1) Review Packet stat "Evidence 10/10"; (2) Review Packet `Evidence 10/10` disclosure; (3) "View all evidence" button — three Evidence touchpoints stacked vertically |
| **Watcher state** | (1) Sidebar `WATCHER running pid 30608`; (2) Sidebar Start/Stop buttons + helper text; (3) Health card "Watcher running pid 30608 / 3s heartbeat / Stop watcher to pause queue dispatch"; (4) Recent Activity "watcher started / watcher stop requested" rows; (5) Top Health stat tile "Watcher running" |
| **Project meta** | (1) Sidebar PROJECT/BRANCH/STATE; (2) Launcher project row `acme-expense-portal /Users/yuxuan/otto-projects/… main clean 0a3494c`; (3) New Job modal "Target project" block (Path/Branch/State); (4) Code Changes header `target main @ e3f2600 branch build/… @ 8931c98 base e3f2600` |
| **Run elapsed time** | (1) Task card "Elapsed 12m" header + chip "12m"; (2) Recent Activity "0:01" / "6:23"; (3) Project Overview "Runtime 2:00:00"; (4) Logs `[+11:24]` and `RUN SUMMARY: build=8:15, certify=3:08, total=…` |
| **Phase config** | "codex / model default / reasoning default / No usage recorded" repeated 3× in Execution panel for SKIPPED phases; same info also in Run metadata disclosure and on TaskCard "Config" line |
| **Inspector entry points** | Top tab strip (Try product / Result / Code changes / Logs / Artifacts) + Review Packet bottom button row (same 5 buttons) — duplicate navigation to the same destinations |
| **"New job" CTA** | (1) Sidebar primary; (2) Idle banner button; (3) Toolbar / palette; (4) Modal title bar (after open) — 3 entry points + 1 confirmation |
| **Run history overlap** | (1) Recent Activity panel; (2) Project Overview "Run history 12 runs · 5 tracked tasks · 8 success · 4 failed"; (3) History tab (separate view); (4) Health screen "Diagnostics Summary" REVIEW AND LANDING list of same landed tasks |

#### 3b.2 Why it happens (root cause)

Each of these surfaces was built *independently* (consistent with App.tsx growing organically). Each component re-derives its slice of state from the top-level `data: StateResponse` and renders it in its own format. There is no notion of a "canonical view of fact X" that other components reference; instead, every component owns its own rendering, and the user gets to compare them.

It's reinforced by the IA pattern in §1.1 — when every region is its own dashboard, every region wants to show the headline numbers.

#### 3b.3 Fix pattern: "one fact, one canonical surface"

For each duplicated fact, pick **one** surface as canonical. Other surfaces either (a) drop it, (b) become a one-line reference, or (c) become a tab on the canonical view.

| Fact | Canonical surface | Other surfaces become |
|---|---|---|
| Queue counts | Task Board column counters | Sidebar drops `TASKS …` line; Idle banner drops tiles; Project Overview drops `Current work` |
| Run verdict | Task card pill (board view) **or** Review Packet eyebrow (run view) — depends on which view is active | Recent Activity row keeps verdict for context; Inspector header drops it (already implied by which run is open); Result/Logs keep machine output but UI doesn't render redundant chips |
| Story count | Review Packet stat | Task card chip drops; Project Overview "Stories 29/29" stays only as project-level rollup |
| Evidence | Review Packet "View evidence" button | The duplicate stat tile + disclosure both drop; one click leads to evidence view |
| Watcher state | Sidebar (always visible) | Health drops Watcher card; Recent Activity collapses watcher events; Health top-stat-strip drops Watcher tile |
| Project meta | Sidebar | Code Changes header drops branch/SHA repetition; New Job modal drops "Target project" block (replaced by 1-line "Build into `<project>` on `<branch>`") |
| Inspector nav | Inspector tab strip | Review Packet bottom button row drops — replaced by one "Open inspector" CTA that opens to state-derived default tab |
| New job CTA | Sidebar (persistent) | Idle banner drops the button (and the whole banner shrinks per §1.4); modal title drops the tag |
| Phase config | One row in Run metadata disclosure | Per-phase config drops from Execution panel (only verdict + duration shown there); TaskCard drops Config line |

#### 3b.4 Practical principles

1. **A primary screen object owns the headline data.** Other components on the same screen *reference* it but don't restate it. Sidebar can show "selected: build-…" — it shouldn't show count + state + verdict that the main panel already shows.
2. **Don't put the same chip in three nesting levels.** "Stories 5/5" as both a stat tile *and* a disclosure header *and* a body line within the disclosure is three layers of the same fact.
3. **Eyebrow + headline + tag often say the same thing.** Pick one of (eyebrow tag, headline, status chip), not all three. Today the Review Packet uses all three for "merged" alone.
4. **Disclosures should hide *more* detail, not duplicate the closed-state headline.** The "Evidence 10/10" disclosure currently expands to show the same `10/10` plus a list — drop the redundant header count once it's open.
5. **Stat tiles are for facts that don't appear elsewhere on the screen.** If the kanban already shows it, the stat tile is wasted pixels.
6. **Activity feeds should compress same-actor same-event sequences.** Four "watcher started/stopped" rows in 6 minutes = one row "watcher restarted 4×".
7. **Action duplication is a fork in the road, not redundancy.** Multiple "New job" buttons aren't always wrong — a primary persistent CTA + a contextual one in the empty-state hero is fine. But three identical-styling primary teal CTAs reading the same words is too much.

#### 3b.5 Where this lands in rollout

- **Phase 1**: easy wins (drop redundant tiles in idle banner; drop watcher card from Health top strip; collapse Activity repeats).
- **Phase 2**: structural (Review Packet rail re-architected per §1.3; sidebar re-purposed; inspector entry points consolidated).
- **Phase 3**: the component-tree refactor is when "one fact, one canonical surface" becomes a *type-level* rule — each fact has one component that owns it, others import a small `<FactRef />` reference instead of re-rendering.

Verification: pick 5 facts at random from §3b.1, count surfaces showing them. Today: ~4 each. Target after Phase 2: ≤2. Target after Phase 3: 1 canonical + optional reference.

---

### 3.3 Sidebar / workspace seam
The deep-navy sidebar against near-white workspace creates a hard visual seam. Soften with one of:
- Match sidebar to a muted variant of brand teal (less black, more depth)
- Keep dark sidebar but warm the workspace `--bg-canvas` to a slight off-white that complements
- Soften the seam line itself (1px line → 4px shadow gradient)

### 3.4 Logo
Flat green "O" + "Otto / Mission Control" reads as Tailwind scaffolding. Wordmark + small mark, or icon-only mark in sidebar with full lockup only on launcher.

### 3.5 Stat density
`Total tokens 23.1M tokens / 16.6M input · 6M cache read · 357.8K cache write · 177.6K output` — one line that's actually four numbers. Pick **2 headline metrics** for the project overview row (current work + total cost is plausible) and put the rest in a "Show details" expander. Same treatment for Project Overview's 5-tile strip.

### 3.6 Disabled vs decorative
Today `Start watcher` (disabled) and `No tasks ready` (disabled-looking but is a stat) and the `LANDED` pill (decorative chip) all use similar gray / pill styling. Differentiate:
- Buttons (interactive) get a button shape and hover state
- Status chips get a fill+tint look distinct from buttons
- Read-only stats use no chip — just typography weight

### 3.7 Inactive states
Disabled buttons currently use solid gray fills that read as primary buttons until you see they're disabled. Switch to outlined-ghost style for disabled.

---

## 4. Workflow / interaction

### 4.1 New Job modal restructure
Today: Will-run-with summary | Edit | Command picker | Target project block | Intent textarea | Yellow "fill intent" warning | Advanced.

Issues:
- Intent textarea is below 4 other fields; it's *the* field.
- "Will run with: claude · default model · effort=default effort · verification=fast" reads as a typo (`effort=default effort`).
- Yellow warning appears below the empty textarea — looks like a delayed validation rather than a hint.
- Advanced has 3 nested levels of provider/reasoning/model overrides (top-level + per-phase BUILD/CERTIFIER/FIX). 9+ dropdowns hidden behind 2 disclosures.

Recommended order:
1. **Intent textarea** — full width, 6 rows, autofocus, placeholder shows good intent examples. Hint above field, not below.
2. **Command** — Build (default) / Improve / Certify pills, not dropdown.
3. **Target project** — collapsed line ("Build into `acme-expense-portal` on `main`"). Click to expand.
4. **Will run with** — collapsed summary line; "Customize" link.
5. **Customize** (modal-replacement page or inline panel): top-level provider/model/effort, then optional "Customize per phase" toggle that shows phase routing. No nested expander.
6. **Queue job** — sticky bottom CTA.

### 4.2 Three "New job" buttons
Sidebar, IDLE banner, modal CTA — all primary teal. Keep sidebar always-visible (since it's the persistent CTA), make the IDLE banner one a smaller secondary, and remove the duplicate modal label clash. After a job is running, the sidebar button could read "Queue another job" to communicate that you don't have to wait.

### 4.3 Run inspector tab order & default
Tabs: `Try product / Result / Code changes / Logs / Artifacts`. For an in-flight run, the user wants Logs. For a landed-needing-review run, they want Code changes. For a merged run, they want Try product. **Default tab should be state-derived** rather than always opening on the first.

### 4.4 Logs pane
Today it's a dense monochrome stream with `[+0:00]` timestamps and Unicode markers (`▸ ● ← ⋯ ✓ ✦ ∎`). 155 lines × 14 KB.
- **Filter by level**: thinking (`▸`), tool call (`●`), tool result (`←`), heartbeat (`⋯`), evidence (`✓`), result (`✦`), summary (`∎`). Default: hide heartbeats.
- **Jump to verdict**: button "Jump to VERDICT/end" — for landed runs, that's what reviewers want.
- **STORY_RESULT formatting**: today these wrap as a single 350-char string. Render as a structured card with `claim / steps / result / surface` rows.
- **Header summary**: today `Final · 155 lines · 14.1 KB`. Replace with `PASS · 11:24 · 5/5 stories · $0.00`.
- **Search hits**: input shows zero highlight visually.

### 4.5 Code changes pane
Today: file list left, diff right, no syntax highlighting, no per-hunk nav.
- Add language-aware syntax highlighting (Shiki / Prism).
- Add per-hunk previous/next nav.
- "Open in editor" link (file:// or `code` URL) — useful for power users.
- Show diff stats (`+412 / -83`) per file, not just count.

### 4.6 Artifacts pane
Today: 11 tiles all looking similar — `intent / queue manifest / manifest / summary / checkpoint / primary log / messages / proof report / proof markdown / proof json / worktree`. No grouping.
- Group by purpose: **Inputs** (intent, queue manifest), **Output** (worktree, code), **Proof** (proof report/markdown/json + screenshots), **Debug** (primary log, messages, checkpoint, summary).
- Hide debug group by default behind a "Show debug artifacts" toggle.
- Filename suffixes are timestamped opaque IDs — render display name + timestamp separately.

### 4.7 Try Product
Today shows command tiles (`Start server / Start Flask app`). Useful, but no copy-to-clipboard or auto-launch. Add:
- Copy button per command
- (Stretch) one-click launch via local-only POST to a launcher endpoint (already exists in some shape based on `web_as_user.py` script).
- Show port + URL clickably so reviewer can hit `http://127.0.0.1:5000` directly after starting.

### 4.8 Keyboard navigation
Skip-link is implemented. Beyond that, no global keyboard shortcuts for a tool that's essentially a queue manager.
- `n` → New job
- `j/k` → next/prev card in board
- `o` → open selected run
- `1..5` → switch inspector tabs
- `?` → show shortcuts overlay
- `cmd+k` → command palette (state already has `paletteOpen` — finish wiring it)

### 4.9 Toast positioning
"Opened acme-expense-portal" toast appears bottom-right and overlaps the rail's action buttons. Move to top-right or auto-dismiss after 1.5s.

### 4.10 Watcher state honesty
Sidebar shows `WATCHER running pid 30608 / HEARTBEAT 2s ago`. If heartbeat goes stale (>15s), the UI keeps saying "running" until the user does math. State should auto-flip to `stale (last seen 47s ago)` with a warn color. Same for crashed-but-pid-still-listed case.

---

## 5. Layout / responsive

### 5.1 1024px breakpoint is too aggressive
Current CSS at `max-width: 1024px` collapses sidebar into a top stack — taking ~700px vertical before content. 1024 catches most laptops and tablets. **Lower to 900px or 880px**, and design a dedicated tablet view (sidebar becomes a top bar with drawer toggle).

### 5.2 1180–1320px is the dead zone
Above 1180 the right rail (300px) competes with a 4-column kanban. On a 1280 monitor the LANDED column is one card wide. Either:
- Drop the kanban to 3 columns at this width (merge `NEEDS ACTION + QUEUED/RUNNING + READY TO LAND` into one "active" column with sub-sections),
- Or auto-collapse the right rail into a tab/drawer below 1320.

### 5.3 Mobile (375px) is decent but improvable
Sidebar collapses into a metadata grid (good), but watcher description text + 4 buttons take the entire viewport before any content. Move secondary actions (Start/Stop watcher, Switch project) into a kebab menu. Keep New job + status snapshot only.

### 5.4 Run inspector modal sizing
On 1440px the modal is fixed-position with 14px insets — basically full screen. On 1024+ it's the same. There's no tablet-friendly half-screen mode; consider a "dock right" option so users can pin the inspector beside the kanban while reviewing.

---

## 6. Specific defects (small, fix-while-touching)

| ID | Issue | Where |
|---|---|---|
| D1 | "Total tokens 23.1M tokens" — unit doubled | Project Overview metric |
| D2 | "effort=default effort" looks like typo | New Job summary line |
| D3 | Stories `5/5` for landed, `-` (em-dash) for merged-bundle in same view; mixed null styling | Review Packet stat tiles |
| D4 | Merge-only runs show 3 SKIPPED execution phases (Build/Certify/Fix) — misleading | Run inspector |
| D5 | Toast covers Review Packet bottom buttons | Toast positioning |
| D6 | Landed task card has elapsed shown twice (header + chip) | TaskCard |
| D7 | Cost `$0.00` displayed without caveat — likely missing-data placeholder presented as truth | TaskCard chip |
| D8 | Recent Activity duration `0:01` lacks unit/label | RecentActivity row |
| D9 | Run id `2026-04-25-051721-18f4a2` shown without label or relative time | RunInspector header |
| D10 | Disabled "Start watcher" + "No tasks ready" use same fill — different roles | Sidebar / Task Board |
| D11 | Skip-link present but tab-order through kanban not verified | a11y |
| D12 | Inspector tab focus vs selection mismatch (focused: Try product, selected: Logs) on open | a11y |
| D13 | "Will run with" `Edit` button next to summary — unclear what it edits | New Job modal |

---

## 7. Code-level refactor

### 7.1 App.tsx is 8,393 lines with 22 sub-components and 38 `useState` calls
This is the *root* cause of inconsistencies above — there's no shared component vocabulary, so every region drifts independently.

**Refactor into a real component tree:**
```
otto/web/client/src/
├── components/
│   ├── primitives/   (Button, Pill, Card, MetricTile, EmptyState, Toast, Modal)
│   ├── layout/       (AppShell, Sidebar, MainPanel, RightRail)
│   ├── launcher/     (ProjectLauncher, ProjectRow, CreateProject)
│   ├── tasks/        (TaskBoard, TaskCard, TaskColumn, IdleBanner)
│   ├── inspector/    (RunInspector, LogsPane, DiffPane, ArtifactsPane, TryPane, ResultPane)
│   ├── overview/     (ProjectOverview, RecentActivity, MetricStrip)
│   ├── health/       (SystemHealth, DiagnosticsSummary, HealthCard)
│   ├── new-job/      (NewJobModal, IntentField, AdvancedOptions, PhaseRouting)
│   └── review/       (ReviewPacket, ExecutionPanel)
├── hooks/            (existing + useRunDetail, usePolling, useLogStream)
├── state/            (consolidate top-level state into reducers/contexts)
└── App.tsx           (~300 lines — composition only)
```

### 7.2 Design tokens
Single source of truth for the 5-state palette, typographic scale, spacing scale, radii, shadows. Today the values are inlined in styles.css with no `--token` consistency.

### 7.3 State reducer
38 useState in App = bug surface. Group into 5–6 reducers:
- `runState` (selected, detail, log, diff, artifact)
- `inspectorState` (open, mode, tab)
- `uiState` (toast, confirm, palette, jobOpen, viewMode)
- `dataState` (data, projects, loaded, bootError)
- `filtersState` (filters + pagination + sort)
- `pendingState` (refreshStatus, optimistic, lastError, banners)

### 7.4 Polling abstraction
Hardcoded `LOG_POLL_BASE_MS = 1200`, `STATE_POLL_HIDDEN_MS = 30_000`, `LOG_POLL_BACKOFF_MS = [2000, 5000, 15000, 30000]` etc. Wrap in `usePolling(intervalSpec, fetcher, options)` so visibility/backoff/cleanup is centralized.

### 7.5 Tests
There are browser tests (`tests/browser/test_*.py`). After the layout/IA changes, expect 60–80% of those to need updating. Pin a `data-testid` strategy in the new component primitives so future refactors don't break tests over CSS-class churn.

---

## 8. Phased rollout

### Phase 1 — quick wins (1–2 weeks, no architectural change)
Goal: fix the most visible/painful issues without restructuring.
- [ ] **D1–D13 defect list** (small text/visual fixes)
- [ ] State color palette + typographic scale (CSS variables) — apply to most-visible places (chips, headings)
- [ ] 1024 → 900 breakpoint move
- [ ] Recent Activity collapse-repeat-events
- [ ] IDLE banner collapse to 1-line state strip when empty
- [ ] Mobile: kebab menu for sidebar secondary actions
- [ ] Watcher "stale heartbeat" auto-flip
- [ ] Toast top-right repositioning
- [ ] Launcher onboarding card (dismissible)
- [ ] Glossary tooltips on watcher/heartbeat/in-flight
- [ ] Logs: hide heartbeats by default, jump-to-verdict button, format STORY_RESULT cards
- **Verify**: load each screen at 1440/1280/1024/900/640/375 widths; capture screenshots; smoke through all flows.

### Phase 2 — IA realignment (2–4 weeks)
Goal: pick a primary object per screen.
- [ ] Tasks screen: kanban-primary, Review Packet becomes contextual rail (active-run / needs-review / collapsed-when-idle)
- [ ] Health screen: remove per-task list duplication
- [ ] Run inspector: state-derived default tab
- [ ] New Job: restructure to intent-first, customize-after
- [ ] Artifacts: group by purpose, hide debug behind toggle
- [ ] Code changes: syntax highlighting, per-hunk nav
- [ ] Try Product: copy-to-clipboard, clickable URL, optional one-click launch
- [ ] Deep-link: persist `run=` through project-open
- [ ] Keyboard shortcuts (n / j / k / o / 1..5 / ? / cmd+k)
- **Verify**: take a fresh new user (or a teammate) through "create project → queue first job → review → merge" flow without prior context. Record paper cuts.

### Phase 3 — code refactor (3–6 weeks)
Goal: split App.tsx, define design tokens, group state.
- [ ] Extract primitives library (`Button / Pill / Card / MetricTile / EmptyState`)
- [ ] Extract feature-folder components from App.tsx
- [ ] Define design tokens; migrate styles.css to use them
- [ ] Refactor 38 useState → 5–6 reducers
- [ ] Extract `usePolling`, `useRunDetail`, `useLogStream` hooks
- [ ] Add `data-testid` strategy
- [ ] Update browser tests
- **Verify**: `tsc --noEmit` clean; all browser tests green; visual diff vs Phase 2 screens shows no unintended drift.

### Phase 4 — polish (1–2 weeks)
- [ ] Sidebar/workspace seam softening
- [ ] Logo refresh
- [ ] First-run inline tour
- [ ] "Show technical details" preference (default: hide for new users)
- [ ] Performance pass (log buffering, virtual list for long history)

---

## 9. Open questions for review

1. **Is there a single user persona to optimize for first** — power users running 100+ jobs, or new users running 1–5? The redesign biases toward the new-user side; if Otto's primary audience is power users, we'd rebalance some of the "hide IDs / hide pid" decisions.
2. **Is Mission Control a *queue manager* or a *result-review tool*?** Today it's both; the IA decisions in §1.1 hinge on this. My read: queue management is the active use case (multiple times a day), result review is the high-value-but-rare case. Default to queue-primary, make review-primary on click.
3. **Should we keep the dark sidebar?** It's a strong brand cue but it's also half of the visual seam problem. A unified light-mode-with-accent design might be a bigger but cleaner win.
4. **Mobile / tablet usage — is anyone actually on those?** If no, we can drop §5.3 / §5.4 and focus desktop. If yes (e.g. checking on jobs from a phone), they need first-class treatment, not just "doesn't break."
5. **Component library**: build in-house or adopt one (Radix / Headless UI / Mantine / shadcn)? Given the scope of the refactor, adopting saves weeks; the cost is some visual customization.

---

## 10. Verification (how we'll know it worked)

- **New-user time-to-first-job**: before/after — target <3 min including reading explainer.
- **Cognitive load on landing**: count of distinct status indicators visible above the fold. Today: ~24. Target: ~8.
- **Inconsistency surface**: count of distinct green / red / yellow shades in CSS. Today: 4+/3+/2+. Target: 1 each.
- **App.tsx LOC**: today 8,393. Target: <500 (composition only).
- **Browser test stability**: number of selector-based test failures during refactor (`text=...` style breaks). Track via test changes per PR.
- **Screenshot regression**: Phase 1 produces a baseline; later phases compare visually.

---

## Appendix A — Audit screenshots
- `/tmp/otto-ui-01-launcher.png` — Project launcher
- `/tmp/otto-ui-02-tasks.png` — Tasks view (desktop)
- `/tmp/otto-ui-03-health.png` — Health view
- `/tmp/otto-ui-04-newjob.png` — New Job modal (collapsed)
- `/tmp/otto-ui-05-recent-detail.png` — Selected merge run
- `/tmp/otto-ui-06-task-expanded.png` — Task card More disclosure
- `/tmp/otto-ui-07-narrow.png` — 1024px responsive break
- `/tmp/otto-ui-08-logs.png` — Logs pane
- `/tmp/otto-ui-09-code-changes.png` — Diff pane
- `/tmp/otto-ui-10-try-product.png` — Try Product pane
- `/tmp/otto-ui-11-artifacts.png` — Artifacts pane
- `/tmp/otto-ui-12-result.png` — Result (proof markdown) pane
- `/tmp/otto-ui-15-newjob-advanced-open.png` — Advanced options
- `/tmp/otto-ui-18-phase-routing.png` — Phase routing nested form
- `/tmp/otto-ui-19-mobile.png` — 375px mobile

---

## Appendix B — Files touched (estimate)

| Phase | Files | Approx LOC delta |
|---|---|---|
| 1 (quick wins) | `App.tsx`, `styles.css` | -200 / +400 |
| 2 (IA) | `App.tsx`, `styles.css`, `types.ts`, `api.ts` | -400 / +800 |
| 3 (refactor) | new tree under `components/`, `state/`, `hooks/`; `App.tsx` shrinks | -7900 / +8500 (net same) |
| 4 (polish) | branding assets, new copy strings | +200 |
