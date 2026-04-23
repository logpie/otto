# TUI Mission Control — Status

Last updated: 2026-04-23

## Branch state

- Branch: `parallel-otto` (worktree at `.worktrees/parallel-otto`)
- 46 commits ahead of `99a53ccfe` (pre-TUI i2p merge baseline)
- 883 unit tests passing
- Design doc: `docs/design/tui-mission-control.md` (1347 lines, Plan-Gate APPROVED 3 rounds)
- Per-phase + final implementation gate trail: `review.md`

## What's done

### Substrate (Phase 1)

- `otto/runs/` package — canonical run registry, history v2 primitive, atomic repair, schema
- `otto/paths.py` — wrappers for runs/, live/, gc/, session and merge command channels
- Atomic publishers (build / improve / certify / spec) write live records with heartbeat (2s) and finalize on terminal
- Queue watcher allocates `attempt_run_id` pre-spawn via O_CREAT|O_EXCL, injects via `OTTO_RUN_ID` env, mirrors child progress, performs startup repair
- Merge orchestrator publishes live record + heartbeat + finalize, consumes durable cancel commands
- All 5 design gate exits (A simultaneous-registration, B unacked-replay, C suspend-grace, D mixed-version, E history-corruption) provably tested

### Universal viewer (Phase 2)

- `otto/tui/mission_control.py` — Textual app with 3 panes: Live Runs / History / Detail+Logs
- `otto/tui/mission_control_model.py` — reader-only model polling registry + tailing logs
- `otto/tui/adapters/{atomic,queue,merge}.py` — per-domain adapters (row label, history summary, artifact list, action capability, detail renderer)
- Reader-derived LAGGING/STALE overlays (suspend grace, dead writer detection)
- History pagination + dedupe by `dedupe_key`/`run_id`
- Legacy queue compat: synthesizes rows from `.otto-queue-state.json` for old watchers; marked legacy queue mode
- Refresh cadence: 500ms active / 1.5s idle; mtime-based file cache for perf
- New `otto dashboard` CLI; `otto queue dashboard` becomes queue-filtered Mission Control wrapper

### Mutations (Phase 3)

- `otto/tui/mission_control_actions.py` — action runner
- `c` cancel: durable envelope to queue / atomic / merge command channels; SIGTERM(pgid) fallback after `max(4s, 2*heartbeat)` only after writer-identity revalidation
- `r` resume: shells `otto queue resume` / `otto build --resume` / `otto improve <subcmd> --resume`
- `R` retry/requeue: reconstructed argv from `source.argv` (atomic) or task definition (queue); collision modal
- `x` remove/cleanup: `otto queue rm` / `otto queue cleanup` / new `otto cleanup <run_id>`
- `m`/`M` merge selected/all: `otto merge <ids>` / `otto merge --all`
- `e` open file in $EDITOR: spawned per selected artifact row
- Worker-thread dispatch via Textual `run_worker` — TUI never blocks
- Multi-select via `space`; `m` operates on the selected set or current row
- Long-running shell-outs surface late non-zero exits via completion watcher

### History + editor hooks (Phase 4)

- All terminalization paths (atomic, queue, merge, certify) write directly via `append_history_snapshot()`
- v2 row schema with intent_path / spec_path / session_dir / manifest_path / summary_path / checkpoint_path / primary_log_path / extra_log_paths / artifacts; old fields preserved
- `build_terminal_snapshot()` only emits paths that exist on disk at snapshot time
- PageUp/PageDown app-priority for History pagination; `[`/`]` also
- `f` outcome cycle, `t` type cycle, `/` substring filter modal
- $EDITOR flow with clear errors when unset
- Registry GC (`gc_terminal_records`) wired at TUI / queue / atomic / merge startup; tombstones to `cross-sessions/runs/gc/`

### Polish (Phase 5)

- `?` opens comprehensive help modal; focus-aware footer
- Detail log search: `Ctrl-F` or `/` when Detail focused; `n`/`N` navigate; matches highlighted
- Theming via `otto/theme.py`: status colors (running=green / failed=red / cancelled=yellow / stale=dim / lagging=orange)
- Performance: mtime-based registry cache; perf tests for 20 concurrent runs

### Code-health audit + fix pass

- 4 parallel agents (bugs, dead code, dedup, AI slop) audited the new ~1800 LOC
- All CRITICAL + IMPORTANT findings fixed by Codex (12 fixes)
- Dedup: `build_terminal_snapshot()` (was 4 hand-built copies), `publisher_for()` factory (was 3 boilerplates), `ArtifactRef.from_path()`, adapter `execute()` mixin
- Dead code: 6 unused LEGACY_* constants, duplicate `_boot_id`, premature `gc_terminal_records` alias

### otto-as-user skill upgrades

- SKILL.md refreshed for Mission Control framing; compact keybind table; new artifact paths section; failure modes updated for durable cancel + SIGTERM fallback
- New daily-tier `U2`: Mission Control basic flow with PTY + asciinema (~$0.50)
- Re-recorded `B4` against new TUI ($0.35)
- New nightly `N9`: realistic operator session — Pilot drives MissionControlApp through standalone build (success) + 2 queue tasks (1 success, 1 cancelled mid-flight) + open in $EDITOR + multi-select merge
- N9 batch-discovery refactor: soft-asserts collect all failures per run; auto-mine post-run audit on `otto_logs/`; subprocess stderr captured per phase
- `OTTO_DEBUG_FAST=1` env var: adds `--fast` to N9 LLM builds for cheap debug iteration (~3min/$0.50 vs ~10min/$3)

## Real Otto bugs found and fixed via N9 dogfooding

12 separate bugs surfaced over 12 takes:

1. dirty index refused build → harness uses `--allow-dirty`
2. standalone build leaves repo on its branch → `git checkout main` before merge
3. queue concurrency=1 made task 2 invisible within SLA → `--concurrent 2`
4. snapshot-passing race in adapter compat dedup
5. `manifest.json` referenced but missing → centralized path-existence check in `build_terminal_snapshot()`
6. queue cleanup deleted artifacts referenced in history → preservation+rewrite in `otto/queue/artifacts.py`
7. cancelled-paused build didn't write terminal history → snapshot in cancel-pause path
8. `python -m otto.cli` did nothing — no `__main__` guard → added guard
9. `_otto_cli_argv` resolved through venv symlink → drop `.resolve()` (siblings are pre-symlink)
10. `otto merge` refused on untracked `.otto-queue-commands.acks.jsonl` runtime file → use shared `repo_preflight_issues()` + `setup_gitignore` adds the patterns
11. hidden oracle too strict on `manifest_path` → relaxed for cancelled / `--fast` / merge rows
12. fixture pip-install (`name = "otto"` in LLM-built CLI) shadowed `.venv/bin/otto` → harness startup guard + per-scenario isolated venv + packaging-intent warning

## What's left

### Required before merge to main

- [ ] **Full-fidelity N9 pass** (no `OTTO_DEBUG_FAST`) — proves the workflow under the real Otto certifier loop. Cost ~$3, ~10-15min.
- [ ] **Manual TUI smoke** — open `otto dashboard` in a real terminal for ~30s, verify rendering matches Pilot snapshots. Pure-rendering bugs (color, layout glitches, focus indicators) won't show up in Pilot tests.

### Nice-to-have before merge

- [ ] Push branch + open PR
- [ ] Run nightly `N1, N2, N4, N8` to confirm the substrate didn't regress those (cost ~$8, ~60min)
- [ ] Move sample asciinema cast `bench-results/as-user/samples/B4-mission-control.cast` to a tracked location (currently gitignored)

### Post-merge follow-ups

- [ ] Adapter `make_action` helpers — Codex partially extracted in audit pass; finish the rest (NOTE-grade)
- [ ] Open Questions in design doc still pending: should `otto` with no args open Mission Control? When to deprecate `otto queue dashboard`?
- [ ] Performance pass on N=50+ concurrent runs (current perf tests cover 20)
- [ ] N10 deletion was clean but the `n9_mission_control_workflow` fixture name now feels stale — could rename to `n9_realistic_operator_session`

### Known limitations (by design, not bugs)

- Mission Control has NO daemon — closing the TUI stops the operator surface, but background CLI runs continue. Tmux/screen recommended for SSH disconnect persistence.
- `merge --resume` is disabled with reason; deferred until merge resume CLI exists.
- Standalone certify has no resume; disabled with reason.
- Pilot drives in-process (not subprocess'd Textual) — the realistic-session test proves the substrate but doesn't catch shell-out terminal rendering quirks.

## Quick reference

**Open TUI:**
```bash
otto dashboard
```

**Run realistic E2E nightly (full fidelity):**
```bash
.venv/bin/python scripts/otto_as_user_nightly.py --scenario N9
```

**Same but cheap debug iteration:**
```bash
OTTO_DEBUG_FAST=1 .venv/bin/python scripts/otto_as_user_nightly.py --scenario N9
```

**All nightlies:**
```bash
.venv/bin/python scripts/otto_as_user_nightly.py
```

**Daily smoke (Mission Control basic + queue + merge dashboard scenarios):**
```bash
.venv/bin/python scripts/otto_as_user.py --scenario U2,B1,B2,B3,B4,D1,D5
```
