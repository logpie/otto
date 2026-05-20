# v6 Performance and Quality Planning Research

Date: 2026-05-14
Scope: v5 pipeline only. No i2p monolithic path changes. No live run started.

## What Exists Today

- `docs/v5-v6-punch-list.md` already contains the 10 target entries and the current diagnosis. The relevant sections are the decisions broadcast entry, `script_valid`, integration preflight injection, and the wall-clock performance entries.
- `otto/v5_runner.py` already:
  - starts the root v5 pipeline with `compile_flat_spec()`,
  - runs architect scaffold preflight via `check_scaffold_compiles()` after an architect pass,
  - propagates architect install dirs into `project_dir`,
  - symlinks install dirs into child worktrees,
  - ensures Playwright browsers once when `@playwright/test` is detected,
  - sets up an integration worktree before invoking the integration agent.
- `_run_integration()` currently runs `smoke_clean_deploy(project_dir, ...)` before resolving the integration worktree and only logs emitted issues. That matches the punch list failure: the integration agent does not receive the smoke result and the smoke check may inspect `project_dir` rather than the merged integration branch.
- `otto/v5_clean_verify.py` is the central clean verification primitive. It currently supports `scope="scaffold"` and `scope="subtree"`, installs and builds manifests, parses declared ports from CHARTER prose, and starts `start.sh` only in subtree/full scope.
- `otto/v5_preflight.py` maps `verify_from_clean()` failures to preflight-visible `PreflightIssue`s. `check_scaffold_compiles()` currently skips `start.sh`; `smoke_clean_deploy()` maps install/build/start failures to clean deploy issue kinds.
- `otto/v5_verification_plan.py` is a runner-side deterministic downgrade layer. It currently runs the structured IA/spec matrix for every Lead verdict regardless of leaf versus integration scope.
- `otto/lead.py` renders prompts and reads verdicts. The prompt renderer can pass child summaries into the integration prompt, but it has no channel for preflight result data yet. Verdict parsing accepts arbitrary JSON keys, so adding `decisions_appended` can remain backward-compatible.
- `otto/prompts/lead.md` already tells children to read `CHARTER.md` and `decisions.md`, and Step 4 tells them to append boundary decisions. The punch list evidence says children are not doing it consistently.
- `otto/spec_compile_flat.py` compiles structured v5 spec JSON with retries on validation/lint failure. It currently writes `spec/spec.json` and input provenance but has no intent-hash cache and no explicit compile metrics artifact.
- `otto/agent.py` and `otto/logstream.py` already write `messages.jsonl` with `phase_start`, assistant events including `elapsed_s`, usage payloads, and `phase_end`. That is enough to derive time-to-first-assistant-token and output token counts, but not currently summarized for compile attempts.

## Evidence Read

- sc6 compile evidence:
  - `v5-itracker-sc6-213910/otto_logs/sessions/2026-05-14-043911-298f0f/spec/compile-agent/messages.jsonl`
  - compile duration: `597.168s`
  - token usage: `total_tokens=200167`, `output_tokens=44928`, `cached_input_tokens=155230`, `cost_usd=0.9146`
  - first assistant event elapsed at about `218.244s`
- sc6 generated artifacts:
  - `spec/spec.json`: about `61830` bytes
  - `CHARTER.md`: `902` lines, about `36837` bytes
  - `decisions.md`: `13` lines, only architect entries were observed
  - `start.sh` uses `${service^^}`, which fails on macOS bash 3.2 at runtime
- sc4 comparison artifacts:
  - `CHARTER.md`: `1138` lines, about `43636` bytes
  - `spec/spec.json`: about `61485` bytes

## Constraints And Open Questions

- All implementation must stay in v5. Do not touch the legacy i2p monolithic path.
- The validation plan should avoid another paid full iTracker run per batch. Use unit tests, prompt snapshot tests, deterministic temp repos, and replay against existing sc4/sc6 artifacts wherever possible.
- A single final live or near-live validation should be reserved until Class A correctness and Class B wall-clock changes are both shipped.
- Provider routing for spec compile is a policy decision. Metrics and caps can ship first; flipping compile default to Codex should require explicit confirmation after evidence.
- `codex-gate` Plan Gate and Implementation Gate are required before non-trivial implementation batches, but this kickoff is plan-only and does not start implementation.

