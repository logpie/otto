# Plan: Restructure `otto_logs/` — One Session, One Directory

## Goal

Replace the current flat, feature-sharded layout with a per-invocation session
directory. Every `otto build | certify | improve` call produces exactly one
`sessions/<id>/` dir containing all its artifacts (spec, build, certify,
improve). Cross-session indexes live in `cross-sessions/`. Two symlinks
(`latest`, `paused`) give O(1) pointers to what users actually care about.

## Why

**Current layout problems** (observed 2026-04-20):

1. **Naming collision**: two `checkpoint.json` files with different schemas —
   `otto_logs/checkpoint.json` (resume state) vs
   `otto_logs/builds/<id>/checkpoint.json` (build summary).
2. **Misleading names**: `improvement-report.md` appears in every `builds/<id>/`
   dir even when no `otto improve` ran.
3. **Split semantics**: `runs/` = spec-gate artifacts, `builds/` = coding agent
   artifacts. Same invocation produces entries in both. User can't see "what
   happened in session X" from one place.
4. **Orphan dirs**: spec-validation failures leave abandoned `runs/<id>/`
   behind.
5. **Shared certifier dir**: `otto_logs/certifier/` is partly per-run
   (`<run_id>/`) and partly shared (`evidence/<build_id>/`) — inconsistent.
6. **Scattered top-level**: `run-history.jsonl`, `certifier-memory.jsonl`,
   `intent.md`, `checkpoint.json` all bare-top-level with no grouping.

**What good looks like**:
- Open `sessions/<id>/` → see everything from that invocation.
- `readlink latest` → most recent session. One command, zero navigation.
- `readlink paused` → the session that needs `--resume` (empty if none).
- `cross-sessions/` → aggregate indexes. No confusion with per-session state.

## Rejected alternatives

- **Keep `runs/` name, nest everything inside**: rejected. "runs" already
  overloaded in the code (run_id, run-history). Reusing the name with new
  semantics would create stealth bugs in grep-based audits.
- **Flatten: `sessions/<id>-spec.md`, `sessions/<id>-build.log`**: rejected.
  Creates hundreds of sibling files at the sessions/ level, hides the phase
  structure, hard to tar/delete a single session.
- **`state/` as catch-all for non-session files**: rejected. Mixes resume
  state (session-scoped) with history/memory (cross-session). User flagged
  this directly.
- **`index/` instead of `cross-sessions/`**: rejected. "Index of what?" —
  ambiguous. `cross-sessions/` is self-documenting at slight length cost.

## Target structure

```
otto_logs/
├── sessions/
│   └── 2026-04-20-170200-9045bc/     # <yyyy-mm-dd>-<HHMMSS>-<short_hash>
│       ├── intent.txt                # per-session archival copy (project-root
│       │                              # `intent.md` remains the runtime contract)
│       ├── summary.json              # verdict, cost, duration, phases_run, status
│       ├── checkpoint.json           # resume state (only while running/paused)
│       ├── spec/                     # only if --spec used
│       │   ├── spec.md
│       │   ├── spec-v1.md .. spec-vN.md   # regen history
│       │   └── agent.log
│       ├── build/                    # coding agent (always for `otto build`)
│       │   ├── messages.jsonl        # SDK message stream, lossless, streams live (Phase 6)
│       │   └── narrative.log         # human-readable streamed narrative (Phase 6)
│       ├── certify/                  # verification artifacts
│       │   ├── proof-of-work.{html,json,md}
│       │   └── evidence/             # screenshots, videos, transcripts
│       └── improve/                  # only for `otto improve`
│           ├── build-journal.md
│           ├── current-state.md
│           ├── session-report.md     # renamed from improvement-report.md
│           └── rounds/<round_id>/    # per-round evidence (journal.py)
├── latest   → sessions/<id>          # symlink, always points to newest
├── paused   → sessions/<id>          # symlink, only exists if a session is paused
└── cross-sessions/
    ├── history.jsonl                 # 1 line per completed session (was run-history.jsonl)
    └── certifier-memory.jsonl        # 1 line per cert (unchanged content)
```

### Session ID format

Unified as `<yyyy-mm-dd>-<HHMMSS>-<6-hex>`. Replaces:
- `run_id` (spec): `2026-04-20-ada4cf`
- `build_id` (coding): `build-1776729460-78529`

The combined form sorts chronologically and stays unique even for rapid
re-invocations (the 6-hex tail is from `secrets.token_hex(3)`).

### Runtime-input files stay at project root (not moved)

Some project-root files are not logs — they are runtime inputs / git-tracked
artifacts consumed by `otto certify` and `otto improve`. These MUST NOT
move into `sessions/<id>/`:

- `intent.md` — product description, read by `otto/config.py` to resolve
  intent, appended by pipeline.py (line ~1008), **committed to git** as the
  canonical product spec. Stays at project root.
- `otto.yaml` — project config. Unchanged location.
- `CLAUDE.md` — agent instructions. Unchanged location.

`sessions/<id>/intent.txt` is an **archival copy only** — snapshot of the
intent at session start. The root `intent.md` remains authoritative.

### Symlinks vs pointer files — atomic writes, with fallback scan

On macOS/Linux, use `os.symlink` with atomic replace: write a temp symlink,
then `os.replace()` to swap. On Windows (or filesystems rejecting symlinks),
fall back to `latest.txt`/`paused.txt` pointer files, also written atomically
(temp file + `os.replace`).

Centralize both in `paths.py` — no ad-hoc symlink writes elsewhere (the
current `otto/certifier/__init__.py:198` silently swallows symlink failures;
that gets deleted).

```python
def set_pointer(logs_dir: Path, name: str, session_id: str) -> None:
    """Atomically point `name` at sessions/<session_id>. Prefer symlink,
    fall back to a .txt pointer file. Never raises — logs at WARNING."""
    target = f"sessions/{session_id}"
    link = logs_dir / name
    tmp_link = logs_dir / f".{name}.tmp"
    try:
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        os.symlink(target, tmp_link)
        os.replace(tmp_link, link)
        # If a stale .txt pointer exists, remove it.
        (logs_dir / f"{name}.txt").unlink(missing_ok=True)
        return
    except OSError as exc:
        logger.warning("symlink %s failed (%s); using pointer file", name, exc)
    # Fallback: atomic write of .txt file. FIRST remove any stale symlink
    # at `link` — otherwise resolve_pointer (which checks symlink first)
    # will keep returning the pre-failure target.
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
    except OSError:
        pass
    tmp_txt = logs_dir / f".{name}.txt.tmp"
    tmp_txt.write_text(session_id)
    os.replace(tmp_txt, logs_dir / f"{name}.txt")

def resolve_pointer(logs_dir: Path, name: str) -> Path | None:
    """Read `name` pointer. If missing, scan sessions/ for a paused session
    (only for name='paused'). Returns absolute session dir path or None."""
    link = logs_dir / name
    if link.is_symlink() or link.exists():
        try:
            return link.resolve(strict=False)
        except OSError:
            pass
    txt = logs_dir / f"{name}.txt"
    if txt.exists():
        sid = txt.read_text().strip()
        if sid:
            sess = logs_dir / "sessions" / sid
            if sess.exists():
                return sess
    # Fallback: for paused pointer specifically, scan for any resumable
    # session (status in {paused, in_progress}). Hard crashes leave
    # status=in_progress without a clean transition — today's otto treats
    # both as resumable. Prefer paused (clean pause) over in_progress
    # (crash) when both present; tie-break by newest `updated_at`.
    if name == "paused":
        candidates: list[tuple[str, float, Path]] = []
        for s in (logs_dir / "sessions").glob("*/checkpoint.json"):
            try:
                cp = json.loads(s.read_text())
                status = cp.get("status", "")
                if status in ("paused", "in_progress"):
                    updated = cp.get("updated_at", "") or cp.get("started_at", "")
                    # Use the file mtime if updated_at is missing.
                    if not updated:
                        updated_ts = s.stat().st_mtime
                    else:
                        updated_ts = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
                    candidates.append((status, updated_ts, s.parent))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        if not candidates:
            return None
        # Sort: paused first, then by newest updated_ts.
        candidates.sort(key=lambda c: (0 if c[0] == "paused" else 1, -c[1]))
        return candidates[0][2]
    return None
```

### Project-level lock — one otto invocation at a time

The plan does NOT solve concurrent invocations, and the existing code has
shared mutable state (git branch, `intent.md` appends, `improve/YYYY-MM-DD`
branch reuse in `cli_improve.py:24`, `journal.py:23` round counter by
counting dirs). Unique session_ids alone are not enough.

`paths.py` exposes a lock helper. Lock is acquired BEFORE session_id
allocation (session_id=None initially), then updated in place once the ID
is chosen — see Phase 1 for the full API and ordering rationale.

```python
def acquire_project_lock(logs_dir: Path, command: str) -> LockHandle:
    """Acquire the single-invocation lock. Raises LockBusy if held by
    another live PID. Lock file: otto_logs/.lock with
    {pid, command, started_at, session_id=None}. Stale-lock detection:
    if holder PID is gone, auto-release with a WARN."""

class LockHandle:
    def set_session_id(self, session_id: str) -> None:
        """Update the lock record once session_id is allocated."""
    # Context manager — release on exit.
```

CLI entrypoints (build, certify, improve) acquire this lock before any
side-effecting work. Released in `finally`. `--break-lock` escape hatch for
the rare stale-lock case where the stale-PID check wasn't enough.

## Migration strategy

**Greenfield + in-place active-state preservation**. Do NOT archive active
resumable state.

- New code keeps legacy readers live for one release so old-layout projects
  continue to work untouched.
- `--migrate-logs` MUST refuse if a legacy paused/in-progress state is
  present (`otto_logs/checkpoint.json` with `status` in
  `{in_progress, paused}`). User must first `otto build --resume` (or
  `--force`) to terminate the legacy run, THEN migrate.
- Migration mechanics (atomic dir rename into a child path is **not**
  possible, and `mv *` risks catching new-layout dirs from mixed state):
  1. Create sibling `otto_logs.pre-restructure.<timestamp>/` at project
     root (NOT a child of `otto_logs/`).
  2. Move old-layout paths by **explicit allowlist** into the sibling:
     `runs/`, `builds/`, `certifier/`, `improve/`, `rounds/`,
     `checkpoint.json`, `run-history.jsonl`, `certifier-memory.jsonl`,
     **`improvement-report.md`** (written at `otto_logs/` root by
     `cli_improve.py:200`), **`intent.md`**-log entries are project-root
     not otto_logs/ so they are NOT moved. New-layout paths
     (`sessions/`, `cross-sessions/`, `latest`, `paused`, `.lock`) are
     NEVER moved — they may already exist if the user previously
     half-ran without migrating. Any unknown files in `otto_logs/`
     root that don't match the allowlist are left in place and logged
     as "unmigrated (unknown)"; migration does not fail on their
     presence.
  3. Create empty new-layout skeleton: `sessions/`, `cross-sessions/`.
  4. Write a `.migration-warned` stamp so Phase 4 warnings don't fire.
  5. Readers (cli_logs, memory) continue to honor
     `otto_logs.pre-restructure.<timestamp>/run-history.jsonl` and
     `otto_logs.pre-restructure.<timestamp>/certifier-memory.jsonl`
     as fallback sources so archived history/memory is not lost.

Why not in-place convert: migration of existing dirs is brittle (build_id
formats differ, partial runs in flight, symlinks on Windows). Users who
care about history can `otto migrate-logs --keep` (copies rather than
moves) or access the archive.

**Upgrade-safety invariant:** after upgrading to this version, resuming a
legacy paused run MUST work without running `--migrate-logs` first. Legacy
readers stay live. Migration is explicitly user-initiated and gated on a
clean terminal state.

**Verify:**
- `otto history` on an old-layout project — prints pre-migration history
  from `run-history.jsonl`.
- Create legacy paused state (simulated checkpoint with `status=paused`) →
  `otto migrate-logs` refuses with clear message pointing at `--resume`
  or `--force`.
- After `--force` clears the paused state → `otto migrate-logs` succeeds,
  archive at `.pre-restructure/`, new layout initialized.

## Phased implementation

### Phase 1 — Introduce `sessions/` writer, keep old paths readable

Touch only the WRITE side. Readers still accept both.

- Add `otto/paths.py` — single source of truth for path construction:
  ```python
  def session_dir(project_dir: Path, session_id: str) -> Path
  def spec_dir(project_dir, sid) -> Path  # session_dir/spec
  def build_dir(project_dir, sid) -> Path
  def certify_dir(project_dir, sid) -> Path
  def improve_dir(project_dir, sid) -> Path
  def cross_sessions_dir(project_dir) -> Path
  def set_pointer(logs_dir, name, session_id) -> None  # atomic
  def resolve_pointer(logs_dir, name) -> Path | None   # with scan fallback
  def clear_pointer(logs_dir, name) -> None
  def new_session_id() -> str  # with collision retry
  def acquire_project_lock(logs_dir, command) -> LockHandle
  # plus LockHandle.set_session_id(sid) — see "Project-level lock" section
  ```

- **Project lock acquired BEFORE session_id allocation.** The lock is
  acquired first with `session_id=None` and `command=<cli_cmd>`; once
  allocation succeeds, the lock record is updated in place with the
  session_id. This eliminates the pre-lock race window and makes the API
  natural.
  ```python
  def acquire_project_lock(logs_dir: Path, command: str) -> LockHandle:
      """Exclusive project lock. Returns handle; use as context manager.
      Writes otto_logs/.lock with {pid, started_at, command, session_id=None}.
      Raises LockBusy if another live PID holds the lock. Auto-releases stale
      locks (holder PID is gone)."""

  class LockHandle:
      def set_session_id(self, session_id: str) -> None:
          """Update the lock record in place once session_id is allocated."""
  ```

- **Session ID generation with collision retry** (called *under* the lock,
  so concurrency can't race). `secrets.token_hex(3)` is 24 bits; rapid
  re-invocations could theoretically collide.
  ```python
  def new_session_id(logs_dir: Path) -> str:
      for _ in range(16):
          sid = f"{datetime.utcnow():%Y-%m-%d-%H%M%S}-{secrets.token_hex(3)}"
          if not (logs_dir / "sessions" / sid).exists():
              return sid
      raise RuntimeError("could not allocate unique session_id after 16 tries")
  ```

- **Caller-owned session_id threaded through all writers.** No inner
  writer may mint its own ID. Signatures change:
  - `build_agentic_v3(..., session_id: str)` in pipeline.py
  - `run_agentic_certifier(..., session_id: str)` in certifier
  - `run_certify_fix_loop(..., session_id: str)` in pipeline
  - `run_spec_agent(..., session_id: str)` in spec.py
  Delete local `build_id` / certifier `run_id` generation at
  `pipeline.py:92` and `certifier/__init__.py:90`.

- **Strengthen phase state machine so spec-approved → build is resumable
  without re-running spec.** In `pipeline.py` (currently line ~188), the
  checkpoint written at build entry is missing an explicit `phase`. Change:
  ```python
  # BEFORE entering the build agent call:
  write_checkpoint(project_dir, run_id=session_id, command="build",
                   phase="build", status="in_progress",
                   intent=intent, spec_path=spec_path_or_empty,
                   spec_hash=spec_hash_or_empty, spec_version=spec_version,
                   session_id=sdk_session_id)
  ```
  Resume helper in `cli.py:89` already fast-paths on `spec_phase_completed`
  — verify that reading `phase="build"` causes `--resume` to skip spec
  entirely and re-enter the build agent via SDK `resume=<sdk_session_id>`.
  **Add regression test:** `test_resume_after_spec_approved_then_build_kill`.

- Generate a unified `session_id` at the entrypoint of each CLI command.
- Update writers (spec.py, pipeline.py, certifier/__init__.py, cli_improve.py,
  journal.py, memory.py, checkpoint.py, cli_logs.py) to use `paths.py`
  functions. No direct `"otto_logs/..."` literals.
- Rename in target paths: `checkpoint.json` inside session dir becomes
  `summary.json` (post-run) while keeping `checkpoint.json` (in-flight only).
- **Gate the report writer.** `_write_improvement_report` in
  `pipeline.py:457` currently fires for every build. Improve commands
  use `command in {"improve.bugs", "improve.feature", "improve.target"}`
  (see `otto/cli_improve.py:85`), not bare `"improve"`. Change: pipeline
  accepts an explicit `is_improve_run: bool` argument; only writes the
  markdown report when that flag is true. Writer target becomes
  `sessions/<id>/improve/session-report.md`. For non-improve builds, write
  only `sessions/<id>/summary.json` — no markdown report.
- **Terminal status for non-resumable failures.** On spec-validation
  failure / agent-didn't-write-spec: write `summary.json` with
  `status="failed_nonresumable"` inside the session dir, clear the
  paused pointer, do NOT set `paused → sessions/<id>`. The session dir
  itself remains for forensics but is clearly terminal.
- **Log files stay on their current formats temporarily** (still write
  `live.log`, `agent.log`, `agent-raw.log` inside the new `build/` subdir).
  Phase 6 replaces them with streaming `messages.jsonl` + `narrative.log`.
  This keeps Phase 1 a pure layout change — format rework is isolated.

**Verify:**
- `otto build --spec "x"` on a fresh dir creates `otto_logs/sessions/<id>/`
  with `spec/`, `build/`, `certify/` subdirs populated and
  `otto_logs/latest → sessions/<id>`.
- Stale `otto_logs/checkpoint.json` (root) is never written in the new code
  path.
- `grep -rn '"otto_logs"' otto/ --include="*.py"` returns only
  `otto/paths.py` and config.py's .gitignore writer.
- `otto history` still runs (may emit empty list from new `cross-sessions/`
  until sessions accumulate).
- `test_resume_after_spec_approved_then_build_kill` — approve spec, kill
  agent mid-build, `--resume` → skips spec, continues build from checkpoint.
- `test_session_id_collision_retry` — monkeypatch `secrets.token_hex` to
  return a fixed value; second invocation should retry, not overwrite.
- `test_project_lock_refuses_concurrent` — two CLI invocations race; the
  second fails with a clear error pointing to `--break-lock`.
- `test_pointer_atomic_write` — simulate crash between `os.symlink` and
  `os.replace`; resolve_pointer still returns a sane answer.
- `test_pointer_scan_fallback` — delete `paused` pointer, write a paused
  checkpoint inside a session; resolve_pointer("paused") finds it via scan.
- `test_failed_nonresumable_clears_pointer` — spec validation fails → no
  `paused` pointer, summary.json has `status=failed_nonresumable`.

### Phase 2 — Migrate readers to the new paths

- **`history.jsonl` schema evolution.** Add a `command` field to every
  written entry (`"build" | "certify" | "improve"`) plus `session_id`.
  `otto history` (in `cli_logs.py`) learns to filter by command and render
  columns per-type. Legacy lines (no `command` field) are treated as
  `command="build"` with best-effort display.
- `cli_logs.py` (`otto history`) reads, in order:
  1. `otto_logs/cross-sessions/history.jsonl` (new)
  2. `otto_logs/run-history.jsonl` (legacy, pre-migration)
  3. `otto_logs.pre-restructure.*/run-history.jsonl` (legacy, post-migration)
  Entries merged chronologically, de-duplicated by `session_id` if present.
  Without the archive fallback, migration would silently lose history.
- `memory.py` reads, in the same order:
  1. `otto_logs/cross-sessions/certifier-memory.jsonl`
  2. `otto_logs/certifier-memory.jsonl`
  3. `otto_logs.pre-restructure.*/certifier-memory.jsonl`
  **Schema is additive only** — existing fields (`ts, command,
  certifier_mode, commit, findings, tested, passed, cost`) preserved
  verbatim; nothing renamed. Append-only order preserved.
- Resume logic: `cli.py` reads `resolve_pointer(logs_dir, "paused")`
  which tries symlink → pointer file → scan fallback (paths.py handles
  all three). If none found, reads legacy `otto_logs/checkpoint.json`.
- `--force` clears the paused pointer and marks the session with
  `summary.json: status="abandoned"` (keeps the dir for forensics; no
  `.abandoned/` rename, just a status flag — simpler and consistent with
  the terminal-status pattern from Phase 1).

**Verify:**
- Start a run in old-layout dir, Ctrl+C mid-spec → resume still works
  (reads legacy checkpoint, no migration required).
- Start a run in new-layout dir, Ctrl+C mid-spec → resume reads
  `sessions/<id>/checkpoint.json`.
- `otto history` on a dir with both legacy and new history entries shows
  them merged chronologically.
- Certifier memory across legacy and new files — the certifier prompt
  injection still loads the last 5 entries in order.

### Phase 3 — Remove legacy writer paths (audit only)

**Subsumed into Phase 1 in practice:** grep confirms no writer still emits
legacy paths. Legacy readers (fallback for upgrade safety) remain live
indefinitely — there is no value in forcibly deprecating them. Old
`otto_logs/` directories keep working as read-only history; new runs write
the new layout.

**Dropped sub-tasks (rationale):**
- ~~Startup warning pointing at `--migrate-logs`~~: Phase 4 (migration
  command) is dropped, so a warning with no remediation action is noise.
  Users with old `otto_logs/` incur zero functional cost — nothing to warn
  about.

**Verify:**
- `grep -rn '"runs"\|"builds"\|"run-history"\|"certifier-memory"' otto/
  --include="*.py"` returns only fallback-reader references. (done)

### Phase 4 — DROPPED

Originally proposed `otto migrate-logs` + `.pre-restructure/` archive
+ deprecation warning. Removed from scope:

- **No pain point.** Legacy paths stay readable via Phase 1 fallbacks —
  history, memory, and resume all work on old `otto_logs/` dirs.
- **Migration is risky for no benefit.** Codex flagged: atomic rename into
  own child is impossible; allowlist can miss files; mid-run upgrade can
  strand paused checkpoints. Every one of these risks disappears if we
  never migrate.
- **Users who want cleanup can `rm` legacy subdirs themselves** — simpler
  and safer than a command that pretends to know what's safe.
- **Far future:** if legacy readers ever need to go, a CHANGELOG note +
  one commit that deletes the fallback paths is cleaner than shipping a
  migration command today.

### Phase 5 — Tests and docs (layout) — reduced scope

Because Phase 2/3 folded into Phase 1, what's left here is purely
documentation + one end-to-end test.

- Update `README.md` log-layout section to new structure.
- Update `CLAUDE.md` log-interpretation table for per-session paths.
- Update `docs/architecture.md` diagrams/references.
- Update `reference_otto_cli.md` memory entry.
- Add one end-to-end test: start a run with legacy-layout otto_logs/,
  Ctrl+C mid-spec, `--resume` — should succeed without migration.

**Verify:**
- `pytest tests/ -q` — full suite green.
- `grep -rn "otto_logs/" README.md CLAUDE.md` shows only new-layout paths.

### Phase 6 — Replace log formats with streaming `messages.jsonl` + `narrative.log`

Retire `live.log`, `agent.log`, `agent-raw.log` entirely. Replace with two
files, both streaming (tail-able) during the run:

**`sessions/<id>/build/messages.jsonl`** — complete normalized SDK event stream
(not raw provider bytes — otto's `run_agent_query` normalizes messages before
callbacks; capturing below that would require provider-adapter changes, out
of scope here). Every normalized message event is recorded with full content,
never filtered or truncated.
- One JSON object per SDK message event, newline-delimited
- Schema (per line):
  ```json
  {
    "ts": "2026-04-20T17:02:15.123Z",
    "elapsed_s": 12.5,
    "session_id": "<sdk_session_id>",
    "type": "assistant|user|result|system",
    "blocks": [
      {"type": "text", "text": "..."},
      {"type": "tool_use", "id": "...", "name": "Bash", "input": {...}},
      {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": false},
      {"type": "thinking", "thinking": "..."}
    ],
    "usage": {"input_tokens": 123, "output_tokens": 45, ...},  // result msgs only
    "cost_usd": 0.0012                                          // result msgs only
  }
  ```
- Never truncated. Never filtered. Written as events arrive.
- Consumer: machine tools (`jq`, future `otto replay <session>`, debugging).

**`sessions/<id>/build/narrative.log`** — human-readable chronological story.
- Streaming, one or more lines per event, written as events arrive
- Formatters per event type:
  - **Tool use** — `[+0:19] Write /private/tmp/.../index.html`
  - **Tool result (brief)** — `[+0:19] → 2.1KB written`
  - **Tool result (error)** — `[+1:18] ✗ Exit 1 — Cards render from localStorage on load (FAIL)`
  - **Bash output** — summarize (exit code + first meaningful line + line count), don't dump
  - **Subagent result** — extract prose text from content list, don't render as Python repr
  - **Thinking** — indented multi-line, preserved at word boundaries (no mid-word truncation)
  - **Certifier markers** — highlighted: `[+3:20] ━━━ CERTIFY_ROUND 1 — PASS (5/5) ━━━`
  - **Git commits** — detect from Bash outputs: `[+2:10] ✓ commit 384dc48 "Add kanban board..."`
- Consumer: humans (tail -f during run, cat post-run).

**Implementation:**

1. Add `otto/logstream.py` — two classes:
   - `JsonlMessageWriter(path)` — writes one line per raw SDK message
   - `NarrativeFormatter(path)` — formats events as human-readable lines
   Both open append-mode, flush after each write.

2. Extend `run_agent_query` (`otto/agent.py:564`) to expose a raw
   `on_message(message)` callback that receives the whole SDK message before
   block-level dispatching.

3. Rewrite `make_live_logger` → `make_session_logger(session_dir)` which
   opens both writers and returns the callback dict.

4. Delete the post-run `agent.log` builder in `pipeline.py:249-321`
   (template leakage, same-timestamp bug, no consumer). The narrative log
   replaces it entirely.

5. Delete `agent-raw.log` write entirely. `messages.jsonl` subsumes it.

6. Keep `live.log` as **a symlink/alias to `narrative.log`** for one
   release so existing docs/habits aren't instantly broken. Drop after.

**Verify:**
- Live: `tail -f sessions/<id>/build/narrative.log` during a run shows
  readable event stream; no Python dict repr, no mid-word truncation,
  no template-placeholder leakage.
- Live: `tail -f sessions/<id>/build/messages.jsonl | jq -r '.blocks[].type'`
  prints every event type in order.
- Post-run: `narrative.log` is readable top-to-bottom as a story;
  `messages.jsonl` replays the entire session losslessly.
- No writers in otto/ reference `agent.log` or `agent-raw.log` except the
  Phase 5 legacy-test case (kept deliberately for fallback coverage).
- `pytest tests/ -q` — full suite green.
- Manually: run `otto build --spec "simple counter"`, open
  `narrative.log` — verify a human can understand the run in 30 seconds.

### Phase 7 — Tests and docs (log formats)

- Update `CLAUDE.md` log-interpretation table:
  - Replace `live.log`/`agent.log`/`agent-raw.log` rows with
    `narrative.log` (human) and `messages.jsonl` (machine).
- Update `README.md` log structure section.
- Update `reference_otto_cli.md` memory.
- Add `otto replay <session_id>` command stub? (Future — out of scope here,
  but `messages.jsonl` makes it possible.)

**Verify:**
- No doc in `README.md`/`CLAUDE.md`/`docs/architecture.md` mentions the
  three retired files except in a "migration/retired" note.
- The narrative format is documented with an example.

## External consumers found (must update)

In-repo references to the old paths that need updating:

- `README.md:221, 404-421` — log layout documentation
- `CLAUDE.md:10-59` — debug/log interpretation table
- `docs/architecture.md:160-279` — architecture diagrams
- `tests/test_v3_pipeline.py`, `tests/test_hardening.py`, `tests/test_spec.py`
  — hardcoded paths in assertions
- `otto/cli.py:659` — hardcoded certifier latest-dir path
- `bench/*/results/*.json` — bench fixtures embed old absolute certifier
  paths. Decision: leave bench fixtures alone (they're historical).

No external scripts or CI workflows grepping old paths found. If any exist
outside this repo, they break — accepted per "internal tool, document in
CHANGELOG."

## Risks

| Risk | Mitigation |
|---|---|
| Missed writer still emits old path → half-migrated dir | Phase 1 audit via grep; phase 3 re-audit; single `otto/paths.py` choke point |
| Symlink fragility on Windows / CI | Pointer-file fallback (Phase 1) |
| Resume breakage during rollout | Phase 2 keeps legacy read fallback; covered by Verify |
| Tests hardcode paths | Phase 5 — update in lockstep; run suite at end of each phase |
| Stale user docs reference old paths | Phase 5 — doc sweep |
| Existing `improve` flows in production reference `improvement-report.md` paths | Rename is breaking; document in CHANGELOG; grace period via legacy-read fallback is not applicable (this is a write-side rename). Accepted break — internal tool, no external consumers known. |
| Narrative formatter bugs (e.g., subagent result misparse, tool-arg rendering) leave users with worse logs than today | Phase 6 lands only after Phase 1–5 are green; keep `messages.jsonl` as ground-truth — narrative bugs are cosmetic (the raw stream is always recoverable). Add golden-file tests for formatter output. |
| Streaming JSONL flush overhead on hot loops | Open file in buffered append mode, flush per message. SDK messages arrive at most a few times/sec — negligible overhead. Measured if needed. |
| Callback added to `run_agent_query` for raw messages is a new API surface | Keep it optional (existing callers unchanged). Only session-logger uses it. |
| Upgrade mid-run strands a legacy paused session | Legacy reader stays live one release; `--migrate-logs` refuses if legacy checkpoint shows paused/in_progress (Phase 4 gate) |
| `intent.md` is runtime input, not a log — moving it breaks `otto certify`/`improve` | Keep `intent.md` at project root; `sessions/<id>/intent.txt` is archival-only copy |
| Session_id collision under rapid re-invocation | `new_session_id` retries on directory-exists; capped at 16 attempts |
| Concurrent invocations race on git / intent.md / improve branches / journal round-count | Project lock file enforces single-invocation; `--break-lock` escape hatch; stale-PID auto-detect |
| Spec-approved → build resume falls back into regenerating spec | Persist `phase="build"` explicitly when build starts; add regression test |
| Orphan session dirs from non-resumable failures | Terminal status in `summary.json` (`failed_nonresumable`/`abandoned`); no paused pointer set |
| Pointer file / symlink write torn by crash | Atomic writes via temp + `os.replace`; scan fallback for the `paused` pointer |
| `_write_improvement_report` fires for every build regardless of mode | Gate on `command == "improve"`; delete from non-improve builds |

## Non-goals

- No breaking changes to existing `certifier-memory.jsonl` entry fields
  (additive only — no renames, no removals, append-only order preserved).
- `history.jsonl` gets `command` and `session_id` fields added, but readers
  handle entries without them (treat as `command="build"`) — so legacy
  entries remain consumable.
- Phase 6 intentionally replaces the three human log files with
  `messages.jsonl` + `narrative.log`. That is a format change, explicitly
  in scope.
- No change to the certifier's prompt or the build agent's behavior.
- No new user-facing features. Layout + logging-format improvements only.
- No multi-session concurrency. Enforced explicitly via project lock.

## Open questions

- [NEEDS DECISION: should `sessions/<id>/checkpoint.json` be deleted on
  successful completion, or rename to `.completed` for forensics? Default:
  delete, summary.json is the permanent record.]
- [NEEDS DECISION: when a session `-n 50` improve loop is running for 4
  hours, do we want per-round progress surfaced at the session-dir level
  (`progress.jsonl` appended per round)? Default: no, journal.py's
  per-round dirs are sufficient.]

## Estimated effort (revised after Phase 1 landed)

- Phase 1: ~2h actual (paths.py + writer migration + regression tests) — **DONE**
- Phase 2: absorbed into Phase 1's legacy fallbacks — effectively **DONE**
- Phase 3: audit only, no active work — effectively **DONE**
- Phase 4: **DROPPED** (see rationale above)
- Phase 5: ~1-2 hours (docs sweep + one cross-layout resume test)
- Phase 6: ~3-5 hours (formatters + JSONL writer + golden-file tests)
- Phase 7: folded into Phase 6

**Remaining work: ~4-7 hours across 1-2 sessions.**

Suggested session split:
- Next session: Phase 5 (docs sweep, quick) → Phase 6 (log format redesign,
  the bigger piece with real user-visible payoff).

`/codex-gate` Implementation Gate before merging Phase 6 to main (highest
risk and most user-visible change).

## Plan Review

### Round 1 — Codex

- [HIGH, ISSUE] Upgrade mid-run is unsafe — `--migrate-logs` would archive the
  active paused checkpoint. **Fixed:** `--migrate-logs` refuses when legacy
  checkpoint shows `status in {in_progress, paused}`; legacy readers stay
  live one release; explicit "upgrade-safety invariant" section added.
- [HIGH, ISSUE] `intent.md` is runtime input (read by `config.py`, committed
  by `pipeline.py`), not a log. Can't move to `sessions/<id>/`. **Fixed:**
  new "Runtime-input files stay at project root" section documents
  `intent.md`, `otto.yaml`, `CLAUDE.md` as non-moving. Per-session
  `intent.txt` is archival-only.
- [HIGH, ISSUE] No concurrency lock — two invocations race on git, branch,
  intent.md, journal round count. **Fixed:** Added project lock
  (`otto_logs/.lock` with pid/command/started_at). `--break-lock` escape
  hatch.
- [HIGH, ISSUE] Spec-approved → build resume state machine weak —
  `phase` not written when build starts, so `--resume` re-runs spec.
  **Fixed:** Phase 1 now explicitly writes `phase="build"` before the
  build agent call. Added `test_resume_after_spec_approved_then_build_kill`.
- [MEDIUM, ISSUE] Symlink/pointer design under-specified — no atomic writes,
  no crash-safe fallback scan. **Fixed:** `set_pointer` uses temp +
  `os.replace`. `resolve_pointer` falls back to scanning
  `sessions/*/checkpoint.json` for paused state. Code shown inline.
- [MEDIUM, ISSUE] Inner writers still mint their own IDs (build_id,
  certifier run_id). **Fixed:** Caller-owned `session_id` threaded through
  all writer entry points; inner mint-calls deleted.
- [MEDIUM, ISSUE] history/memory semantics change. **Fixed:** `history.jsonl`
  gets additive `command` and `session_id` fields; `otto history` merges
  legacy + new chronologically. `certifier-memory.jsonl` schema additive-
  only.
- [MEDIUM, ISSUE] Report rename incomplete — `_write_improvement_report`
  fires for every build. **Fixed:** Pipeline accepts explicit
  `is_improve_run` flag; markdown report written only for improve runs.
- [MEDIUM, ISSUE] "Lossless SDK stream" oversold — normalized, not raw.
  **Fixed:** Language changed to "complete normalized SDK event stream";
  note added that raw provider capture is out of scope.
- [LOW, ISSUE] Orphan dirs from non-resumable failures. **Fixed:** Terminal
  status in `summary.json` (`failed_nonresumable` / `abandoned`); no
  paused pointer set.

### Round 2 — Codex

- [ISSUE] `resolve_pointer` only scanned `status=paused`; misses hard-crash
  `status=in_progress`. **Fixed:** Scan covers both; paused preferred;
  tie-break by newest `updated_at`.
- [ISSUE] Migration cannot atomically rename into its own child; `mv *`
  risks catching new-layout dirs. **Fixed:** Sibling
  `otto_logs.pre-restructure.<timestamp>/` at project root; explicit
  allowlist of what moves.
- [ISSUE] Archive fallback for history/memory missing — migration silently
  loses legacy entries. **Fixed:** Readers check 3 paths in order (new /
  legacy-pre / legacy-archived); de-dup by session_id.
- [ISSUE] Report gate used wrong command value (improve commands are
  `improve.bugs`/`improve.feature`/`improve.target`). **Fixed:** Explicit
  `is_improve_run` flag instead of command-string match.
- [ISSUE] Lock / session_id ordering awkward. **Fixed:** Lock acquired
  first with `session_id=None`; `LockHandle.set_session_id()` updates
  record after allocation.
- [ISSUE] Non-goals section internally inconsistent (claimed "no schema
  changes" but plan adds fields and swaps log formats). **Fixed:**
  Rewritten to accurately reflect scope.

### Round 3 — Codex

- [ISSUE] Stale symlink when falling back to `.txt` — resolve_pointer's
  symlink-first check returns pre-failure target. **Fixed:** `set_pointer`
  explicitly unlinks stale symlink before writing `.txt` fallback.
- [ISSUE] Migration allowlist missed top-level `improvement-report.md`
  (written by `cli_improve.py:200`). **Fixed:** Added to allowlist.
  Unknown files logged as "unmigrated (unknown)", not failure.
- [ISSUE] Lock API signature mismatch between two sections. **Fixed:**
  Early section updated to match Phase 1 API.

### Round 4 — Codex

- [ISSUE] One remaining lock signature in Phase 1 function-list still
  stale. **Fixed:** list updated to `acquire_project_lock(logs_dir,
  command)` with comment about `set_session_id`.

### Round 5 — Codex

- APPROVED. No new issues.
