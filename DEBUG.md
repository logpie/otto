# Mission Control Queue Failure Debug

Date: 2026-04-25

## Observations

- User queued a first follow-up task from the web portal and it failed within seconds.
- The run had no primary session log, no normal proof packet, and the UI only showed a generic failure.
- The project watcher log contained:
  - `Fatal Python error: init_sys_streams: can't initialize sys standard streams`
  - `OSError: [Errno 9] Bad file descriptor`
  - `reaped add-simple-authentication-and-role-cc47fe: failed (exit_code=1)`
- The failure happened after a web-server restart while an older watcher process was still alive.
- Normal completed runs still had proof, diff, artifacts, and logs.

## Hypotheses

### H1: Queue children inherited a bad stdio fd from a long-lived watcher (root)

- Supports: Python died during interpreter startup before Otto session files existed; the watcher outlived the terminal/web process that launched it; the error is `Bad file descriptor` in standard stream initialization.
- Conflicts: none found.
- Test: assert queue subprocesses are spawned with a stable `stdin` rather than inheriting watcher fd 0, and verify failed pre-artifact tasks expose watcher logs.

### H2: The child command was malformed

- Supports: the failure happened immediately after dispatch.
- Conflicts: the error is a Python runtime stdio initialization failure, not an Otto argument parse error; no session log or manifest was created.
- Test: inspect watcher log and run record command fields for malformed argv.

### H3: Mission Control hid an available failure source

- Supports: the watcher log contained the root cause but `/api/runs/{id}/logs` returned no text because the primary session log did not exist.
- Conflicts: normal artifact-backed runs display logs correctly.
- Test: seed a failed queue task with no primary log and a watcher log excerpt, then assert Proof, Artifacts, and Logs expose it.

## Root Cause

A watcher process can survive the terminal or web server that launched it and then spawn task children with an inherited broken fd 0. Python can fail during interpreter startup before Otto writes session logs, leaving Mission Control with only a generic failed queue state.

## Fix

- Queue runner now spawns task children with `stdin=subprocess.DEVNULL`.
- Queue run artifacts include watcher-log fallback paths when a terminal queue task has no primary session log.
- Mission Control derives a concise failure summary from watcher log excerpts and exposes it in Proof, Logs, Artifacts, and API details.
- Regression coverage now seeds this exact pre-artifact failure mode and checks that the UI/API shows the real root cause.

## UI Notes

Comparable build/task UIs put the actionable failure first, then let users drill into logs:

- GitHub Actions expands failed steps and supports line-level log links/search.
- Vercel shows a deployment error summary when logs are unavailable, then points users to build logs when they exist.
- Buildkite uses annotations for concise job-scoped summaries alongside logs and artifacts.

For Otto, this means the Proof packet should lead with: root cause, next action, evidence links, then logs/artifacts as drill-downs. It should not duplicate generic "failed" text in several panels.
