# Implementation Gate — 2026-04-20 — log-restructure (Phases 1, 5, 6)

Branch: `worktree-i2p`
Commits reviewed: `9fa6554be`, `abf9313cb`, `0e9d959ae`, plus fix commit.

Scope: per-session `otto_logs/sessions/<id>/` layout, `otto/paths.py`
choke point, streaming `messages.jsonl` + `narrative.log` replacing the
legacy `live.log/agent.log/agent-raw.log` trio.

## Round 1 — Codex

- [CRITICAL] Project lock check-then-write race + never called by CLI
  entrypoints — fixed by Codex (O_EXCL atomic create + wired into
  build/certify/improve, --break-lock flag added)
- [CRITICAL] Split/resume session threading incomplete, one invocation
  fanned into multiple session dirs — fixed by Codex (session_id threaded
  through inner build/certifier calls + split-mode spec data + improve
  --resume run_id)
- [IMPORTANT] Split-mode journal writes still global/legacy paths —
  fixed by Codex (session_id threaded through all journal helpers)
- [IMPORTANT] Stale `paused` pointer after successful build — fixed by
  Codex (write_checkpoint clears paused pointer on status=completed)
- [IMPORTANT] History/memory merge order wrong (legacy entries appeared
  newer than post-refactor) — fixed by Codex (sort by parsed timestamp)
- [NOTE] messages.jsonl dropped structured_output — fixed by Codex
  (serialized when non-None + regression test)
- [NOTE] summary.json half-implemented — fixed by Codex (written at end
  of every completed session, not just --force abandoned path)

## Round 2 — Codex

- [IMPORTANT] LockHandle.release() unlinks any .lock, not its own —
  fixed by Codex (nonce-based ownership check + regression test)
- [NOTE] summary.json written for paused/error runs too — fixed by
  Codex (gated on final_status == "completed" only)

## Round 3 — Codex

- [IMPORTANT] set_session_id() could overwrite a new holder's lock after
  --break-lock — fixed by Codex (same nonce check applied to
  _write_record + regression test)

## Round 4 — Codex

- [IMPORTANT] Check-then-mutate TOCTOU still present in _write_record
  and release — fixed by Codex (switched to kernel-level fcntl.flock;
  `.lock` is now immutable after acquire; set_session_id is a no-op)

## Round 5 — Codex

- [IMPORTANT] fcntl imported at module level broke Windows — fixed by
  Codex (platform guard + Windows best-effort fallback with one-time
  warning; fork-based test marked skip-on-Windows)

## Round 6 — Codex

- [IMPORTANT] Windows fallback kept .lock fd open, preventing
  --break-lock from succeeding — fixed by Codex (Windows branch closes
  fd immediately after acquire; regression test for stale-holder unlink
  of replacement lock)

## Round 7 — Codex

- [IMPORTANT] Windows-fallback release() has intrinsic TOCTOU between
  nonce check and unlink — documented as an accepted limitation in the
  module docstring and at the release site. Unix flock is the
  authoritative correctness path.

## Round 8 — Codex re-reviewed fixes

APPROVED. No remaining critical issues.

## Final state

- 200 tests pass (up from 189 pre-gate)
- Lock: kernel flock on Unix (authoritative); Windows best-effort with
  documented TOCTOU
- Session threading: one invocation = one session dir, end to end
- Journal/report routing: all artifacts under session tree
- summary.json: canonical post-run record for completed sessions
- messages.jsonl: truly lossless (structured_output included)
- history/memory: merged chronologically across new + legacy + archive

## Co-authored

Codex authored all fixes during the gate via `mcp__codex__codex`
workspace-write sessions, per CLAUDE.md's "Codex fixes Codex-found
bugs" rule.
