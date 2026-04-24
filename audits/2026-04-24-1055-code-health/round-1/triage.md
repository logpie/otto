# Triage

| Candidate | Status | Resolution |
| --- | --- | --- |
| Codex stdout line exceeds asyncio stream limit | fixed | Added 16 MiB Codex subprocess reader limit and regression assertion. |
| `origin/fix/...` default branch detected as only final path segment | fixed | Preserve the branch path after `refs/remotes/origin/`. |
| Mission Control landing target defaults to `main` when no `otto.yaml` exists | fixed | Use `load_config()` so UI and CLI target detection match. |
| Filtered run detail regression did not include unrelated filters | fixed | Updated test to call `/api/runs/{id}?type=merge&query=unmatched`. |
| Broad `except Exception` in service/config touched area | invalid | Pre-existing defensive behavior, not caused by this patch. |
| 16 MiB stream limit might be unbounded memory risk | invalid | Bounded constant; observed crash was 579 KiB and the limit is scoped to Codex subprocess stdout. |
