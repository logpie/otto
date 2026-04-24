# Production Dead/Dedup/Slop Hunter

Scope: changed production files only.

## Findings

- No new dead code found.
- No duplicated parsing or config logic introduced.
- No debug prints, TODO/FIXME markers, fake values, or unused helpers introduced.

## Notes

- `CODEX_STDIO_LIMIT_BYTES` is intentionally named and imported by tests; it is
  not a stray constant.
- The branch-ref parsing in `detect_default_branch()` is small and local to the
  only git ref shape the function reads, so extracting a helper would add
  indirection without reducing duplication.
