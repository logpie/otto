# Brittleness + AI-Sloppiness Synthesis

**Inputs:** `audit-brittleness-{prompts,types,errors,magic}.md` + `audit-ai-sloppiness.md`
**Question asked:** From a software-engineering and "reducing AI sloppiness" point of view, do we still need more refactoring and simplification?

---

## TL;DR

**Yes** — but the remaining work is a different *shape* than what we've done so far.

Parts 1–3 + the v5→run rename reduced **size** (97k → 73k LOC, deleted the legacy pipeline, split monoliths). What they didn't fix is **shape**: the boundaries between modules are still untyped dicts, the error handling still hides bugs, and ~4.5k LOC of prompts are dead text that an AI editor will still load when grepping. The next round should target *shape*, not size.

The biggest single AI-sloppiness risk: **708 `isinstance(x, dict)` checks vs 97 typed declarations** (7:1 ratio). When an AI agent edits this codebase, the dict-passing boundaries hide what data flows where. Adding 5 strategic `TypedDict`s would cover the worst offenders.

---

## What we found, in priority order

### Bucket A — Trivial-to-fix, high LOC cleanup (low risk, ~1 commit)

| Finding | Impact | Effort |
|---|---|---|
| **28 orphan prompts in `otto/prompts/` (4,455 LOC)** — only `lead.md`, `lead-integration.md`, `setup-claude.md` are loaded by live code. The rest are legacy pipeline remnants (compile-spec, build-agent, certifier, audit, etc.) that grep would happily pull into context for an AI editor and confuse it. | High — instant deletion clears agent-context noise | 15 min |
| **$25 `tree_budget_usd` declared in 2 places** (v5_runner.py:1290 + cli_run.py:72), missing from defaults.py | Confusion; "why is it capped when I didn't set the cap?" | 10 min |
| **5 hardcoded `"otto_logs/"` path violations** (CLAUDE.md says all paths must go through `otto/paths.py`) | Drift risk when log layout changes | 20 min |

### Bucket B — Highest-leverage shape fixes (medium risk, ~1 commit each)

| Finding | Why it matters for AI sloppiness | Effort |
|---|---|---|
| **TypedDict for task graph entry** (20+ read sites) | Today, an AI editing the task graph contract sees `dict[str, Any]` and has no idea which keys are required. The `contract_amendment_retry_*` family alone has 6+ keys that AI could shadow or rename in one site and break another. | 1–2 hr |
| **TypedDict for pipeline event payload** (40+ `_emit()` calls in v5_runner.py) | Event shapes vary per `event` type. UI + logger consume them. AI adding a new event has no template. | 2 hr |
| **TypedDict for repair packet schema** (15+ read sites, crosses agent boundary) | Agent reads + writes this. Schema drift breaks the agent silently. | 1 hr |
| **18 silent-return error paths** — `except Exception: ... return None` patterns in v5_runner, config, cli_queue, cli. Operators see "feature absent" but it's actually "feature broke silently". | When an AI's edit breaks something downstream, the operator gets no signal at all. One log line per catch slashes diagnosis time from hours to minutes. | 1–2 hr |
| **Centralize timestamp formatting** (4 variants × 20 sites). Sites use `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())` and `.isoformat()` and `datetime.now(timezone.utc).isoformat()` interchangeably. | AI cargo-cults the wrong one; comparisons silently fail across formats. | 30 min |

### Bucket C — Names that lie + copy-paste twins (medium effort, high leverage)

| Finding | Risk |
|---|---|
| **`_repair_stale_target_and_retry_merge`** — does 4 phases (repair → smoke → union-check → merge), not what the name says. R3 audit already added a docstring; rename remains. | Operators + AI both underestimate cost when this fires |
| **`_carry_prior_repair_packets`** — actually copies + **resets** bookkeeping. The reset side-effect is invisible from the name. | Resets cost-attempt counters mid-run unexpectedly |
| **`_handle_mechanical_preflight_blocker` ↔ `_handle_mechanical_merge_blocker`** — 70% identical | AI fixes one, not the other |
| **`_schedule_foundation_contract_amendment` ↔ `_schedule_smoke_repair_needed`** — 85% identical | Same |
| **3 `_repair_*_once()` siblings** — share ~250 LOC of boilerplate | Adding a new `_repair_X_once` invites the 4th copy |
| **6 JSON-reading variants** — different sizes, different error handling | AI picks the wrong helper for the situation |

### Bucket D — Acknowledged but defer (high risk OR low impact)

- **Long functions** — `_carry_prior_repair_packets` (400 LOC), `_conflict_packet_for_refusal` (327), `_stale_target_gate_feedback` (163). Some are intrinsic (state machines); some splittable. The audit3 caution applies: be careful which "looks splittable" actually isn't.
- **Prompt brittleness in live prompts** (lead.md web-specific guidance applied universally, temporal coupling bug in lead-integration.md). Real but needs live-LLM testing to verify the fix doesn't regress.
- **`MAX_CONTRACT_AMENDMENT_ATTEMPTS` used 6x across modules** — centralized but easy to typo. Lower priority because the central definition is well-named.

---

## What's NOT a problem (after the campaigns)

For reference, the audit found these are in good shape:

- **Retry counts** (`MAX_ARCHITECT_RETRIES`, `_SESSION_ID_MAX_ATTEMPTS`, `RUN_ID_MAX_ATTEMPTS`) — centralized.
- **Size/threshold limits** — in `defaults.py`.
- **Provider/model registry** — `PROVIDER_AGENT_MODEL_DEFAULTS` is the canonical map.
- **Path construction** — mostly goes through `otto/paths.py` (5 violations remaining, otherwise clean).
- **Session ID format** — duplicated in 3 places cosmetically, but the format itself is stable.
- **51+ properly-annotated `# noqa: BLE001`** — best-effort paths that are intentional. The noise from these is small.

The "good shape" list is significant — it means the campaigns are paying off. The remaining issues are concentrated in the dict-passing layer and prompt-text layer, not scattered everywhere.

---

## Verdict on "do we still need more refactoring?"

**Yes, one more focused round.** Targeted scope:

### Round 4 — proposed (estimated 4–6 commits, ~3–5 hours)

1. **Delete the 28 orphan prompts** (Bucket A). Trivial. Slashes 4.5k LOC of dead text that pollutes AI context. (One commit.)
2. **Centralize the 3 worst magic numbers** (Bucket A). $25 tree_budget_usd, timeout=2 port cleanup, sleep(0.05) UI polling. (One commit.)
3. **Add 3 TypedDicts** for the highest-leverage boundaries (Bucket B): task graph entry, pipeline event payload, repair packet. (One commit, possibly split for risk.)
4. **Add logging to the 18 silent-return paths** (Bucket B). One log line each. (One commit.)
5. **Centralize timestamp + JSON helpers** (Bucket C). 4 timestamp variants + 6 JSON readers → 1 each. (One commit.)
6. **Rename + refactor the top 3 names-that-lie + copy-paste twins** (Bucket C). Codex-gated since the renames touch the merge/repair hot paths. (One commit.)

After Round 4, **stop**. The remaining items (long functions, prompt brittleness in live prompts, MAX_*_ATTEMPTS centralization, foundation-contract-amendment removal) are all in BACKLOG.md territory — small individual items, each waiting for a session that wants to pick them up. The codebase will have converged on a tighter, more typed, less AI-trap-friendly shape.

### What NOT to do

- Don't try to add types everywhere. The 708 → 97 ratio doesn't need to flip; just cover the 5 hottest boundaries.
- Don't split functions just because they're long. Some long functions ARE state machines that legitimately need that length.
- Don't rewrite live prompts without real-LLM validation. The prompt brittleness is real but the validation cost is high.
- Don't touch the foundation-contract-amendment system. User has deferred twice (Part 2 + R3).

---

## Cost summary if we execute Round 4

| Tier | Net code | Net risk | What it removes from the AI-edit blast radius |
|---|---|---|---|
| A (orphan prompts + magic numbers) | −4,500 LOC | Near-zero | Dead prompt context, misleading constants |
| B (TypedDicts + error logging) | +200 LOC | Low–Medium | Untyped boundaries on 80 read sites; silent-failure debugging |
| C (centralize + rename) | −300 LOC | Medium | Cargo-cult of timestamp/JSON variants; misleading function names |
| **Total** | **−4,600 LOC + 3 TypedDicts + better names** | Mixed | Substantial AI-friendliness gain per LOC touched |

This is the last big-payoff refactor pass I see in the audit data. After it, future work should be feature work, not cleanup.
