# Otto MC — wireframes

Text-based screen sketches. Used as scaffolding for structural agreement
before any visual design pass. Vocabulary aligns with
[`research.md`](../research.md): Intent, Spec, Feature, Group, Guardrail,
Stage, Audit, Proof, Run.

Conventions:

- `[ ]` = button
- `[*]` = primary button
- `( )` = radio / chip
- `▾` = expanded section
- `▸` = collapsed section
- `✓` `⚠` `✗` `⊘` = pass / partial / blocked / out-of-scope
- `——` = horizontal divider

---

## Screen 1 — Project landing (launcher mode)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Otto                                          [+ New project]  [⚙]   │
├──────────────────────────────────────────────────────────────────────┤
│  Projects                                                            │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  ● p7-shortener        URL shortener with auth, click analytics│  │
│  │    last run: passed · 6/6 features · 3/5 quality · 22m ago     │  │
│  │                                                  [Open project]│  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  ● p9-kanban           Kanban board with cards and columns     │  │
│  │    last run: passed · 5/5 features · 4/5 quality · 1h ago      │  │
│  │                                                  [Open project]│  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  ◌ p10-docflow         Document review and approval workflow   │  │
│  │    last run: running · stage: building (5/8 groups landed)     │  │
│  │                                                  [Open project]│  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

Notes:
- Dot color = aggregate health (green / amber / red).
- Brown indicator = real user-owned dirty files (Otto-owned paths
  excluded by `is_otto_owned_path`).
- Click `[Open project]` → screen 2.
- No "queue runner" toggle. No "watcher" indicator. No "git clean"
  badge for Otto-owned paths.

---

## Screen 2 — Project view (Runs list)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Otto · p7-shortener                                  [+ New run]  [⚙]│
├──────────────────────────────────────────────────────────────────────┤
│  Runs                                          [Filters ▾] [Refresh] │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ ✓ landed   shrtnr — URL shortener with auth, …                 │  │
│  │           6/6 features · 5/5 capabilities · q 3/5              │  │
│  │           wall 21:43 · cost $3.01 · finished 2h ago            │  │
│  │           Built in 6 groups                                    │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ ⚠ partial  add image upload to /editor                         │  │
│  │           4/5 features · 1 blocked: image-upload               │  │
│  │           wall 8:14 · cost $0.92 · finished 1d ago             │  │
│  │           Built in 1 group (Editor surface)                    │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ ◌ running   shortener: add admin dashboard                     │  │
│  │           stage: auditing (4/4 groups landed)                  │  │
│  │           elapsed 14:32 · cost so far $1.40                    │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ ⏸ awaiting spec review   real-time chat extension              │  │
│  │           Spec compiled 4m ago · 8 features · 3 groups         │  │
│  │           [Review spec]                                        │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

Notes:
- One row per Run. Status pill drives left edge.
- Counts in vocabulary: features (verdict-bearing), capabilities (if
  we keep this distinction), quality, wall, cost.
- Click row → drawer (screen 3).
- "Awaiting spec review" rows are clickable to go straight to
  spec-review screen (screen 4).
- No "Tasks panel," no "Health" tab.

---

## Screen 3 — Run drawer (default — landed Run)

```
┌──────────────────────────────────────────┬─────────────────────────────┐
│ Runs                                     │ Run detail · landed · ✕     │
│  ✓ landed   shrtnr — URL shortener…      │                             │
│  ⚠ partial  add image upload to /editor  │ Outcome  Landed             │
│  ◌ running  shortener: add admin dash    │ Verdict  6 of 6 features    │
│  ⏸ awaiting spec review …                │ Quality  3/5                │
│                                          │ Wall · Cost  21:43 · $3.01  │
│                                          │                             │
│                                          │ Intent                      │
│                                          │ # `shrtnr` — URL shortener  │
│                                          │ with auth, click analytics, │
│                                          │ and admin                   │
│                                          │ ▸ View full intent          │
│                                          │                             │
│                                          │ [* Open proof packet ]      │
│                                          │ [ View spec ]               │
│                                          │ [ Logs ]   [ Files ]        │
│                                          │ ──────────────────────      │
│                                          │ Features                    │
│                                          │  ✓ Public landing page      │
│                                          │     home renders, form      │
│                                          │     validates URLs          │
│                                          │     ▸ evidence (3 items)    │
│                                          │                             │
│                                          │  ✓ Auth (register/login)    │
│                                          │     bcrypt, sessions ok     │
│                                          │     ▸ evidence (4 items)    │
│                                          │                             │
│                                          │  ✓ Click analytics          │
│                                          │  ✓ Personal dashboard       │
│                                          │  ✓ Admin overview           │
│                                          │  ✓ DB foundation            │
│                                          │ ──────────────────────      │
│                                          │ Guardrails                  │
│                                          │  ⊘ No anonymous deletion    │
│                                          │  ⊘ No external CDN          │
│                                          │     all 2 verified           │
│                                          │ ──────────────────────      │
│                                          │ ▾ Groups (6)                │
│                                          │   foundation                │
│                                          │     2 features · 4:12 · $0.32│
│                                          │     [diff] [logs]           │
│                                          │   public                    │
│                                          │     1 feature · 3:08 · $0.41│
│                                          │     [diff] [logs]           │
│                                          │   auth                      │
│                                          │     1 feature · 5:54 · $0.71│
│                                          │   …                         │
│                                          │ ──────────────────────      │
│                                          │ ▾ Stage timeline            │
│                                          │   compile  ✓ 0:48 · $0.12   │
│                                          │   build    ✓ 12:30 · $1.90  │
│                                          │   audit    ✓ 4:20 · $0.62   │
│                                          │   render   ✓ 0:08           │
│                                          │   land     ✓ 0:32           │
│                                          │ ──────────────────────      │
│                                          │ ▸ Run metadata              │
│                                          │   id 2026-05-04-153230-…    │
│                                          │   started …  finished …     │
└──────────────────────────────────────────┴─────────────────────────────┘
```

Notes:
- Verdict header is the first thing the user reads.
- Features are the primary surface, expandable per Feature for
  evidence (screenshots, walkthrough segment, deterministic checks).
- Guardrails verified inline.
- Groups expander is one click below Features. Each Group has
  diff + logs links.
- Stage timeline is collapsed by default for passed runs, open by
  default for partial / blocked / running.
- No Build/Certify/Proof/Merge widgets. No "Story" anywhere.
- Action row: **Open proof packet** primary; View spec; Logs; Files.
- "Files" replaces legacy "Code changes" — for a multi-Group Run this
  opens a Group picker; for single-Group this goes straight to the
  diff.

---

## Screen 4 — Spec review (gate paused)

```
┌──────────────────────────────────────────┬─────────────────────────────┐
│ Runs                                     │ Spec review · ✕             │
│  ⏸ awaiting spec review                  │                             │
│     real-time chat extension             │ Intent                      │
│                                          │ A real-time chat extension  │
│                                          │ for the doc editor with     │
│                                          │ presence indicators and     │
│                                          │ message history.            │
│                                          │ [Edit intent]               │
│                                          │ ──────────────────────      │
│                                          │ Groups & Features           │
│                                          │ ──────────────────────      │
│                                          │ ▾ Chat surface              │
│                                          │   ☑ Send message            │
│                                          │     room-based; persist     │
│                                          │     [edit] [remove]         │
│                                          │   ☑ Message history         │
│                                          │     last 100 messages       │
│                                          │     [edit] [remove]         │
│                                          │   ☑ Typing indicator        │
│                                          │     [edit] [remove]         │
│                                          │   [+ add feature]           │
│                                          │ ▾ Presence                  │
│                                          │   ☑ Online status           │
│                                          │     active/idle/offline     │
│                                          │   ☐ Avatars                 │
│                                          │     unchecked = removed     │
│                                          │   [+ add feature]           │
│                                          │   [edit group] [remove]     │
│                                          │   [split]                   │
│                                          │ [+ add group]               │
│                                          │ ──────────────────────      │
│                                          │ Guardrails                  │
│                                          │  ⊘ No video calling         │
│                                          │  ⊘ No file uploads via chat │
│                                          │  ⊘ No external WebSocket svc│
│                                          │  [+ add guardrail]          │
│                                          │ ──────────────────────      │
│                                          │ Suggestions from compile    │
│                                          │  • Notification on @mention │
│                                          │    [accept] [dismiss]       │
│                                          │  • Read receipts            │
│                                          │    [accept] [dismiss]       │
│                                          │ ──────────────────────      │
│                                          │ [* Approve & build ]        │
│                                          │ [ Recompile ]   [ Abort ]   │
└──────────────────────────────────────────┴─────────────────────────────┘
```

Notes:
- User reads Otto's plan as a checklist of Features grouped by Group.
- Unchecking a Feature drops it. Editing opens an inline form.
- "Add feature" prompts for free-text description; on save Otto runs a
  micro-compile to assign it to a Group (or spawn one).
- Guardrails are pinned constraints — added by user as free text or
  selected from compile suggestions.
- "Suggestions from compile" are Features the LLM proposed but kept
  separate because user didn't ask for them — explicit accept/reject.
- "Recompile" regenerates Spec from updated intent + accepted edits.
- "Abort" exits the gate without building.

---

## Screen 4a — Spec editor (Markdown view, the writer's view)

```
┌──────────────────────────────────────────┬─────────────────────────────┐
│ Runs                                     │ Spec · doc-editor · v3 · ✕  │
│  ⏸ awaiting spec review                  │                             │
│     doc-editor                           │ [● Markdown ] [ Form ]   ⌃ ⌃│
│                                          │ ──────────────────────      │
│                                          │ # Doc editor                │
│                                          │                             │
│                                          │ A doc editor for engineering│
│                                          │ teams. Markdown rendering,  │
│                                          │ inline comments, image      │
│                                          │ upload. Built around real-  │
│                                          │ world review workflows.     │
│                                          │                             │
│                                          │ ## Project kind             │
│                                          │ webapp · Flask + SQLite     │
│                                          │                             │
│                                          │ ## Features                 │
│                                          │                             │
│                                          │ ### Editor surface          │
│                                          │                             │
│                                          │ #### Markdown rendering     │
│                                          │                             │
│                                          │ Pages render `.md` files as │
│                                          │ HTML. CommonMark spec       │
│                                          │ compliance for headings,    │
│                                          │ lists, code blocks…         │
│                                          │                             │
│                                          │ #### Save / load            │
│                                          │ Author can save drafts and  │
│                                          │ reopen them…                │
│                                          │                             │
│                                          │   [edit raw markdown ↗]     │
│                                          │ ──────────────────────      │
│                                          │ Status                      │
│                                          │ ✓ Parsed cleanly            │
│                                          │ ✓ 6 features, 2 groups      │
│                                          │ ✓ All ids stable from prev  │
│                                          │ ⚠ "Image upload" group_id   │
│                                          │   moved from "comments" →   │
│                                          │   "editor-surface" (file    │
│                                          │   overlap detected)         │
│                                          │ ──────────────────────      │
│                                          │ [* Approve & build ]        │
│                                          │ [ Save draft ] [ Recompile ]│
│                                          │ [ Diff vs v2 ] [ Abort ]    │
└──────────────────────────────────────────┴─────────────────────────────┘
```

Notes:
- Default view when opening spec gate.
- Shows a Markdown render with HTML-comment metadata stripped.
- "Edit raw markdown" opens an inline textarea editor.
- Status sidebar shows parse health + Otto's auto-mechanics warnings
  (e.g. group_id reassignment).
- Save draft / Approve / Recompile / Abort buttons drive the gate.

## Screen 4b — Spec editor (Form view, the structured editor)

```
┌──────────────────────────────────────────┬─────────────────────────────┐
│ Runs                                     │ Spec · doc-editor · v3 · ✕  │
│                                          │                             │
│                                          │ [ Markdown ] [● Form ]   ⌃ ⌃│
│                                          │ ──────────────────────      │
│                                          │ Intent                      │
│                                          │ ┌─────────────────────────┐ │
│                                          │ │ A doc editor for        │ │
│                                          │ │ engineering teams…      │ │
│                                          │ └─────────────────────────┘ │
│                                          │                             │
│                                          │ Project kind   (●) webapp   │
│                                          │                ( ) cli      │
│                                          │                ( ) library  │
│                                          │                ( ) api      │
│                                          │ ──────────────────────      │
│                                          │ Groups & Features           │
│                                          │ ──────────────────────      │
│                                          │ ▾ Editor surface       [⋮]  │
│                                          │   files: routes/editor.py,  │
│                                          │     templates/, static/,    │
│                                          │     models.py               │
│                                          │   depends on: (none)        │
│                                          │                             │
│                                          │   ☑ Markdown rendering [⋮]  │
│                                          │      Pages render .md files │
│                                          │      as HTML…               │
│                                          │      [edit] [acceptance]    │
│                                          │      evidence: BrowserJourney│
│                                          │      RepoTestCheck          │
│                                          │                             │
│                                          │   ☑ Save / load        [⋮]  │
│                                          │   ☑ Image upload       [⋮]  │
│                                          │   [+ add feature]           │
│                                          │                             │
│                                          │ ▾ Comments             [⋮]  │
│                                          │   files: routes/comments.py,│
│                                          │     templates/comments.html,│
│                                          │     models.py (shared)      │
│                                          │   depends on: editor-surface│
│                                          │                             │
│                                          │   ☑ Line-anchored comments  │
│                                          │   ☑ Threaded replies (1 lvl)│
│                                          │   ☑ Resolve thread          │
│                                          │   [+ add feature]           │
│                                          │                             │
│                                          │ [+ add group]               │
│                                          │ ──────────────────────      │
│                                          │ Guardrails                  │
│                                          │  ⊘ No video upload    [✕]   │
│                                          │  ⊘ No real-time collab[✕]   │
│                                          │  ⊘ No mobile UI       [✕]   │
│                                          │  ⊘ No external CDN    [✕]   │
│                                          │  [+ add guardrail]          │
│                                          │ ──────────────────────      │
│                                          │ Suggestions Otto considered │
│                                          │  ○ Notifications on @mention│
│                                          │    [accept] [dismiss]       │
│                                          │  ○ Read receipts            │
│                                          │    [accept] [dismiss]       │
│                                          │  ○ Export to PDF            │
│                                          │    [accept] [dismiss]       │
│                                          │ ──────────────────────      │
│                                          │ [* Approve & build ]        │
│                                          │ [ Recompile ] [ Abort ]     │
└──────────────────────────────────────────┴─────────────────────────────┘
```

Notes:
- `[⋮]` per Group/Feature: rename, move-to-group, delete, set
  evidence kinds, set acceptance detail.
- Unchecking a Feature drops it from the Spec entirely.
- "Suggestions" are Features Otto considered but didn't include —
  user-explicit accept/dismiss to add or kill.
- Toggling between Markdown / Form keeps the same Spec object — both
  views render from `spec.json` and write back to it.

## Screen 4c — Add Feature modal

```
┌─────────────────────────────────────────────┐
│ Add feature                            [✕]  │
├─────────────────────────────────────────────┤
│ Group         Editor surface          [▾]   │
│                                             │
│ Name          ┌───────────────────────────┐ │
│               │ Search inside document    │ │
│               └───────────────────────────┘ │
│                                             │
│ Description                                 │
│ ┌─────────────────────────────────────────┐ │
│ │ Full-text search across the user's      │ │
│ │ documents. Result list shows title +    │ │
│ │ matching line; click to open at line.   │ │
│ └─────────────────────────────────────────┘ │
│                                             │
│ Acceptance detail (optional)                │
│ ┌─────────────────────────────────────────┐ │
│ │ Search "lorem"; ≥1 result for fixture   │ │
│ │ document containing the word; click;    │ │
│ │ opens the document at the line.         │ │
│ └─────────────────────────────────────────┘ │
│                                             │
│ Evidence kinds                              │
│  ☑ BrowserJourney   ☑ ApiProbe              │
│  ☐ StateInvariant   ☑ RepoTestCheck         │
│                                             │
│ ▸ Otto suggestion                           │
│   "I'd put this in a new group `search`     │
│    because the file overlap with editor-    │
│    surface is minimal. Files would be:      │
│    routes/search.py, templates/search.html, │
│    static/search.js."                       │
│   [Accept Otto's grouping] [Edit grouping]  │
│                                             │
│ [* Add ]   [ Cancel ]                       │
└─────────────────────────────────────────────┘
```

Notes:
- Otto runs a micro-compile in the background to suggest the right
  Group based on file overlap with existing Groups.
- User can accept the suggestion or override (move to existing Group,
  spawn new Group with custom name).

## Screen 4d — Spec diff (vN → v(N+1))

```
┌─────────────────────────────────────────────────────────────────────┐
│ Spec diff · v2 → v3                                          [✕]    │
├─────────────────────────────────────────────────────────────────────┤
│ Intent                                                              │
│ - A doc editor with markdown and inline comments.                   │
│ + A doc editor for engineering teams. Markdown rendering,           │
│ + inline comments, image upload. Built around real-world…           │
│                                                                     │
│ Features                                                            │
│ + Image upload                  (new — added by user)               │
│   group: editor-surface                                             │
│   evidence: BrowserJourney, ApiProbe, StateInvariant                │
│                                                                     │
│ - Notifications on @mention     (removed — was in suggestions)      │
│                                                                     │
│ Guardrails                                                          │
│ + No mobile-specific UI         (new — added by user)               │
│ + No external CDN               (new — added by user)               │
│                                                                     │
│ Groups                                                              │
│   editor-surface                                                    │
│ +   owned_paths: + static/editor.js  (new file from image-upload)   │
│                                                                     │
│ Re-dispatch impact                                                  │
│ ⚠  editor-surface group will rebuild (1 new feature + new files)    │
│ ✓  comments group unchanged — won't rebuild                         │
│                                                                     │
│ [Approve diff & build]   [Edit more]   [Abort]                      │
└─────────────────────────────────────────────────────────────────────┘
```

Notes:
- Triggered by [Diff vs v2] button on screen 4a.
- Shows what changed between Spec versions, plus what re-dispatch
  this implies.
- "Re-dispatch impact" is the key brownfield UX: user sees exactly
  what's about to re-execute.

## Screen 5 — Run drawer (running, mid-Build)

```
┌──────────────────────────────────────────┬─────────────────────────────┐
│ Runs                                     │ Run detail · running · ✕    │
│  ◌ running   shortener: add admin dash   │                             │
│                                          │ Outcome  Building           │
│                                          │ Stage    Build (4/4 groups  │
│                                          │          landed; auditing   │
│                                          │          starts in ~2 min)  │
│                                          │ Elapsed  14:32              │
│                                          │ Cost so far  $1.40 (cap $5) │
│                                          │                             │
│                                          │ Intent                      │
│                                          │ Add an admin dashboard …    │
│                                          │ ──────────────────────      │
│                                          │ [ Pause ] [ Abort ]         │
│                                          │ ──────────────────────      │
│                                          │ Groups (4)                  │
│                                          │  ✓ admin-routes  landed     │
│                                          │     2 features · 3:21       │
│                                          │  ✓ admin-views   landed     │
│                                          │     1 feature  · 4:08       │
│                                          │  ◌ admin-actions building…  │
│                                          │     1 feature · running     │
│                                          │     [tail logs]             │
│                                          │  ⏸ admin-search  blocked    │
│                                          │     waiting on admin-routes │
│                                          │ ──────────────────────      │
│                                          │ ▾ Stage timeline            │
│                                          │   compile ✓ 0:42            │
│                                          │   build   ◌ 13:22 (running) │
│                                          │   audit     pending         │
│                                          │   render    pending         │
│                                          │   land      pending         │
│                                          │ ──────────────────────      │
│                                          │ ▸ Live event feed           │
│                                          │   [tail full session log]   │
└──────────────────────────────────────────┴─────────────────────────────┘
```

Notes:
- Same drawer as screen 3, just with "Running" verdict header and
  partial data.
- "Open proof packet" button absent (no proof yet); replaced with
  Pause / Abort.
- Group rows show real-time status; click any one to tail its
  narrative log.
- Cost vs cap visible inline.

---

## Screen 6 — Per-Feature drilldown

Triggered by clicking `▸ evidence (4 items)` next to a Feature in
screen 3. Or by visiting `/sessions/<id>/features/<feature-id>` directly.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Otto · p7-shortener · run 2026-05-04-… · feature auth                │
│                                                          [share URL] │
├──────────────────────────────────────────────────────────────────────┤
│ Auth (register / login)                          ✓ passed            │
│                                                                      │
│ "User can register with email + password (bcrypt). Login sets a      │
│  session cookie. Logout clears it. Tested with 3 fixtures and a      │
│  brute-force resistance check (rate-limited after 5 fails)."         │
│                                                                      │
│ Built in group  auth                                                 │
│ Files changed   routes/auth.py, models.py, templates/login.html      │
│ Repair attempts 0                                                    │
│ ──────────────────────────────────────────────────────────────────── │
│ Walkthrough segment                                  [open in audit] │
│                                                                      │
│ ┌────────────────┐  ┌────────────────┐  ┌────────────────┐           │
│ │ register page  │  │ form filled    │  │ logged-in home │           │
│ │ [screenshot]   │  │ [screenshot]   │  │ [screenshot]   │           │
│ └────────────────┘  └────────────────┘  └────────────────┘           │
│   00:42  GET /register     00:48  POST /register     00:51  GET /    │
│                                                                      │
│ Saved DOM                                                            │
│  • register-success.html  (after submit)                             │
│  • login-form.html        (form rendered)                            │
│ ──────────────────────────────────────────────────────────────────── │
│ Deterministic checks                                                 │
│  ✓ ApiProbe POST /register  201 Created, body matches schema         │
│  ✓ StateInvariant  bcrypt hash format in users.password_hash         │
│  ✓ BrowserJourney register → login → access /dashboard               │
│  ✓ RepoTestCheck pytest tests/test_auth.py  3 passed                 │
│ ──────────────────────────────────────────────────────────────────── │
│ Audit narrative                                                      │
│  "I navigated to /register. The form had email + password fields.    │
│   I filled in test@example.com / hunter2. Submit succeeded; I was    │
│   redirected to /. The session cookie was set. I logged out via      │
│   POST /logout; cookie cleared. I tried logging back in with the     │
│   same credentials — succeeded. I tried 6 wrong passwords; the 6th   │
│   was rate-limited as expected by the spec."                         │
│ ──────────────────────────────────────────────────────────────────── │
│ Spec context                                                         │
│  This Feature was generated by Compile from the original intent.     │
│  Original spec line: "Auth: simple username/password with bcrypt;    │
│  sessions in cookies."                                               │
│                                                                      │
│ [ Re-audit this feature ]                                            │
└──────────────────────────────────────────────────────────────────────┘
```

Notes:
- Sharable URL — user can paste this in a PR description as proof
  this Feature works.
- Walkthrough segment is the audit walkthrough sliced to just this
  Feature's tagged actions.
- Deterministic checks list per-check pass/fail with output excerpts.
- "Re-audit this feature" runs `otto run --rerun-audit <session-id>
  --feature auth` against current code (cheap if walkthrough scope is
  one Feature).

---

## Screen 7 — Re-audit dialog

Triggered by `[ Re-audit this feature ]` from screen 6, or
`otto run --rerun-audit` flag at CLI.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Re-audit                                                       [✕]   │
├──────────────────────────────────────────────────────────────────────┤
│ Re-audit                                                             │
│  ( ) The whole product (all features)                                │
│  (●) Just selected features:                                         │
│      ☑ Auth (register / login)                                       │
│      ☐ Public landing page                                           │
│      ☐ Click analytics                                               │
│      ☐ Personal dashboard                                            │
│      ☐ Admin overview                                                │
│      ☐ DB foundation                                                 │
│                                                                      │
│ Run against                                                          │
│  ( ) The original session's code (HEAD at 2026-05-04 15:32)          │
│  (●) The current code on main (HEAD now)                             │
│                                                                      │
│ Estimated cost  $0.20 (1 feature, focused walkthrough)               │
│ Estimated wall  ~2 min                                               │
│                                                                      │
│ [* Start re-audit ]   [ Cancel ]                                     │
└──────────────────────────────────────────────────────────────────────┘
```

Notes:
- Re-audit is the killer primitive: cheap, scoped, against arbitrary
  code state.
- Result is its own session under `otto_logs/sessions/<new-id>/` with
  its own Proof packet — links back to the parent session.

---

## Screen 8 — New run dialog

Triggered by `[+ New run]` on screen 2.

```
┌──────────────────────────────────────────────────────────────────────┐
│ New run                                                       [✕]    │
├──────────────────────────────────────────────────────────────────────┤
│ Intent                                                               │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Build me a doc editor with markdown rendering and inline     │   │
│  │ comments. No video. No real-time presence.                   │   │
│  │                                                              │   │
│  └──────────────────────────────────────────────────────────────┘   │
│  [ Load from intent.md ]                                             │
│                                                                      │
│ Project kind   ( ) cli  ( ) library  (●) webapp  ( ) api             │
│                                                                      │
│ ▸ Advanced                                                           │
│    Provider (default: claude / sonnet)                               │
│    Cost cap (default: $5)                                            │
│    Skip spec review  ☐                                               │
│    Audit walkthrough per feature  ☐                                  │
│    Pre-merge audit groups (default: none)                            │
│                                                                      │
│ [* Start ]   [ Cancel ]                                              │
└──────────────────────────────────────────────────────────────────────┘
```

Notes:
- Intent textbox is pre-filled from `intent.md` if it exists.
- "Skip spec review" checkbox makes the user's plan ownership explicit:
  if they checked it, Otto runs straight through Compile.
- Advanced collapsed by default; covers the configurable knobs.

---

## Information density rules

1. **Verdict above evidence.** Every screen leads with the conclusion
   ("6/6 features," "passed," "3/5 quality"); evidence is below or one
   click away.

2. **Features above Groups.** Always. Groups are an implementation
   surface.

3. **Pass / partial / blocked / out-of-scope** distinguish at a glance.
   Every Feature gets a glyph; aggregations show counts.

4. **Cost visible always when >0.** Both per-Group and per-Run. Users
   need to know what each Run cost without clicking.

5. **One-click to evidence.** Click any Feature → see screenshots +
   walkthrough excerpt + checks. Two clicks to deep evidence (audit
   narrative, raw walkthrough log).

6. **No backwards compatibility cruft surfaced.** The new MC never
   shows "story," "capability," "certifier," "Build/Certify/Proof"
   widgets. Legacy runs render in the legacy panel until Phase C
   deletes both.

---

## What's still ambiguous (resolve in design pass)

- **Feature ordering.** Within a Group, alphabetical? Spec order?
  Status (failed first)? Probably status-first for partial/blocked
  runs, spec-order for passed runs.
- **Group ordering.** Same question, same answer: dep-order (foundation
  before consumers) for mid-Run; status-first when summarizing.
- **Quality findings rendering.** Severity-tagged? Inline near related
  Feature? Separate "Polish needed" section?
- **Empty Run state.** First-ever Run, no history — what does the
  Project view look like?
- **Multi-project history view.** When a user has 12 projects, how do
  they spot the one with a failing Run?
- **Real-time updates.** WebSocket vs polling. State.jsonl tailing vs
  server push.
- **Mobile.** Probably out of scope but worth flagging — none of these
  layouts collapse cleanly under 768px.

These are visual/interaction-design questions, not structural ones.
Defer until structural agreement (research.md + this file) is signed
off.
