# v6.6 Consolidation Research

## Existing Surfaces

- `docs/v5-v6-punch-list.md` defines the governing principle: trust the agent, keep deterministic auto-fixes tiny, and route ambiguous failures to coding-agent repair instead of expanding classifiers.
- `otto/v5_runner.py` owns v5 orchestration, branch hygiene, child dispatch, integration preflight observation, and bottom-up integration.
- `otto/v5_preflight_repair.py` already defaulted many non-port/non-filename failures to an agent, but still carried USD-based repair budget code and lacked the chmod deterministic shortcut.
- `otto/lead.py` renders `lead.md` / `lead-integration.md`, reads `verdict.json`, already recovered some noncanonical verdicts, and ran one canonical rewrite attempt for malformed verdict files.
- `otto/v5_verification_plan.py` still enforced runner-side decisions.md heuristics by detecting changed shared/wire/type paths and downgrading verdicts without a matching `decisions.md` entry.
- `otto/prompts/lead.md` mentioned decomposition overhead and decisions.md, but did not receive runtime facts like `max_parallel`, elapsed budget, queue state, or spec profile.
- `otto/prompts/lead-integration.md` received thin child summaries and preflight JSON inline; no durable integration packet existed.

## Constraints

- Do not touch i2p monolithic path or provider routing.
- No live Otto runs.
- Time budgets only in new runtime context. Do not add USD-based planning inputs.
- No new hard validators or path heuristics.
- Leave user-dirty files alone unless explicitly edited for this task.

## Implementation Implications

- `_checkout_v5_branch_clean` should produce a repair-loop payload for dirty/checkout failures, and callers should invoke the existing `PreflightRepairController` instead of letting a `RuntimeError` escape.
- The repair controller can simplify by keeping only deterministic `port_busy`, `filename_too_long`, and chmod fixes; everything else should use the agent path unless attempt/fingerprint caps stop it.
- Verdict canonicalization should accept common provider-shaped verdicts (`status`, `result`, `outcome`, `passed`, `success`, `ok`, `verdict: passed`) and produce clearer failure reasons when `verdict.json` exists but cannot be parsed or mapped.
- Runtime decomp facts should be passed to `lead.md` as JSON context, not enforced by runner rules.
- Integration context should become a session artifact: `<integration_session_dir>/integration_packet.json`, then the prompt should instruct the integration agent to read it first.
- Decisions.md should remain prompt context. Runner-side changed-path matching should be deleted from verification.
