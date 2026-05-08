# Critical Bug Hunt Triage

Date: 2026-05-07
Branch: main

## Fixed in this wave

- Codex app-server permission approvals could echo requested filesystem permissions instead of granting only paths under the configured workspace.
- Codex app-server usage accounting preferred per-turn token deltas over cumulative totals.
- Audit could pass even when deterministic cross-slice checks failed.
- Audit could pass when expected product features were not audited individually.
- Malformed `otto.yaml` was treated as an inconclusive contract check instead of a failed product contract gate.
- Merge queue could commit a dirty integration worktree when a declared slice branch was missing.
- Worktree creation silently ignored `base_ref` for existing branches.
- Queue runner could mark completed jobs failed or cancelled if a success manifest raced with timeout/cancel/shutdown handling.
- Queue child identity checks failed when a live child changed cwd.
- Mission Control showed spec review as pending while the run was actively building and did not expose paused live state correctly.

## Not fixed in this wave

- Merge-all UI availability still needs a product decision around whether all-ready landing should be enabled from Mission Control before backend support is complete.
- Full destructive-reset/clean hardening needs a broader merge-workspace policy so repair can distinguish Otto-owned generated state from user edits.
- Raw Codex CLI bypass-permissions behavior remains legacy-provider compatibility risk and should be handled as a provider policy migration, not a narrow patch.
- App-server dollar-budget accounting still needs provider-reported cost metadata; token totals are now counted correctly.
- Queue cancel/remove orphan cleanup needs an end-to-end Mission Control flow fix.
