# Critique — Architecture Debate

Both proposals correctly diagnose the proximate failure: `spec.json` emitted `shared_contracts[browser-quality-contract].paths = ["tests/browser/**", ...]` (owner=foundation) while `transactions-ledger.owned_paths` claimed `tests/browser/test_transactions.*`, `budgets.owned_paths` claimed `tests/browser/test_budgets.*`, `insights-dashboard.owned_paths` claimed `tests/browser/test_insights.*` (lines 698, 750, 798, 967 of the spec.json under finance-true-web session `2026-05-08-044149-c25608`). Every downstream group was *guaranteed* to "violate" foundation's contract by writing its own test file. The 11 `group.attempt.failed` + 3 `group.blocked` events (`spec-state.jsonl` ev-000028..067) were structurally inevitable.

Both proposals avoid the elephant: the finance-dash-claude run (`session 2026-05-08-190129-5ea4dc`) failed at `foundation` itself before any ownership question — a strict-mode locator collision (`getByRole('link', { name: 'Dashboard' }) resolved to 2 elements`, `summary.json` verdict `blocked` at attempt 3 / $2.41). The build agent wrote a `<a>Finance Dashboard</a>` brand link AND a `<a>Dashboard</a>` nav link, then wrote a journey calling `getByRole('link', { name: 'Dashboard' })` non-strict. Same agent, both sides. Neither proposal hardens this.

---

## Proposal A — Dynamic, agent-driven decomposition

**Strongest claim:** "delete the up-front commitment." Lead agent decides decomposition at runtime; `otto.lock_paths` enforces ownership at write time, not by post-hoc audit. This is the right diagnosis of the finance-true-web failure.

### Stress tests

- **Finance-true-web:** A would prevent it. No spec-emitted `shared_contracts`/`owned_paths` means no contradictory partition. The lead either keeps tests in foundation or write-locks per filename.
- **Finance-dash-claude:** A does **nothing**. Same lead writes both `Navbar.tsx` and the journey; same brittle locator. A's `otto.verify` is *build-agent-authored*, the very failure mode proof-of-work was invented to prevent (`project_proof_of_work.md` memory). And A explicitly kills `otto/repair_gates.py` (§4) plus reduces `otto/audit.py` to "verifier shim."
- **Browser engine.** §3 hand-waves `--decomp-depth 4`. How does a depth-3 sub-lead detect a *cousin* sub-lead has touched the same `Layout.cpp`? Glob-refcounted sqlite locks cannot. §8 admits "depth 3+ unverified."
- **Brownfield 100k-LOC Django.** §3: "Lead's first action is Glob+Grep." That burns ~50K tokens before useful work, less reliable than a structured discovery pass.
- **30-line CSV-to-JSON.** `--mode solo` works only if the user knows to pass it. Autopilot leaves `Agent`/`TeamCreate` available; "do not decompose for its own sake" (§7) is a vibe, not a control.
- **$10 then crash.** §6 message-stream checkpoint, but §8 admits "exact recovery contract... is undecided." Stale locks + orphaned worktrees: unfinished.
- **Two MC users.** Not addressed. Sqlite-per-worktree breaks under cross-project arbitration.
- **"Build a chat app."** Sparse intent → lead's untested 3-deep planning carries everything. §8 admits no bench evidence.

### Lazy moves

- "Lead decides" — *based on what?* §3's medium walkthrough has the lead predicting `insights+budgets` couples to `transactions` from intent text. That is *exactly the prediction `spec_compile.py` makes and gets wrong*. Swapping one model's prediction for another is not deletion; it is concealment inside an opaque turn.
- "Otto provides mechanics" — `otto.verify(claims[])` schema undefined. If the lead authors its own claims, it can under-claim and hide gaps. `otto/audit.py` (2950 lines) currently derives checks from spec independently; A loses that.
- "User opts in to supervised" — autopilot default has no plan diff (§2 `--review-gate` default `none`). Discoverability zero.

### Silently kept

- A relocates "agent decides scope from intent text" from offline `spec_compile` to the lead's first turn. The session-resume problem gets *worse* (decision encoded in a message stream, not a JSON spec).
- `otto.lock_paths(agent_id, globs)` is `Slice.owned_paths` renamed and runtime-allocated. Same contract surface, same coordination requirement.
- Keeps `budget.py`, `checkpoint.py`, `resume.py`, `queue/` — all assume a finite structured plan. Recursive subagent-tree resume is untested (admitted §8).

### CC SDK / provider fallback
Codex out-of-credits (real, current): A silent. CC SDK session resume today reuses the same `session_id` per group across attempts (verified — finance-true-web foundation 1→2→3). With dynamic decomposition, what *is* a "session"? Lead's? Each subagent's? Unanswered. Malware-reminder injection on Read for non-Opus-4.6: silent.

---

## Proposal B — Explicit tiered architecture

**Strongest claim:** the bug is a category error — T2 machinery applied to a T1 problem. Adding a 50-line manifest-consistency check (§6) would have caught the finance-dashboard failure pre-flight in <1 second.

### Stress tests

- **Finance-true-web:** B's §6 manifest-consistency check ("no path appears in any `owned_paths` AND a `shared_contracts.paths` owned by another group") would reject the spec pre-flight. *Most concrete shippable fix in either document.* Single PR in `spec_compile.py`.
- **Finance-dash-claude:** B does nothing. T1 still has one lead writing nav and journey; same brittle locator; same repair loop dependency.
- **Browser engine.** T2 manifest exists *as data* — better than A's message-stream burial. But §9 admits if discovery is "compile-spec.md with extra steps" we re-implement today's bug. T2 thesis on a knife edge with no design.
- **Django brownfield.** T3 = existing brownfield path renamed and promoted. Preserves what works.
- **30-line CSV-to-JSON.** T0 = single agent. Clean. Autopilot heuristic fragile but `--tier` overrides.
- **$10 then crash.** §8 table specifies per-tier resume (T1 = lead-checkpoint; T2 = merge_queue state). More concrete than A.
- **Two MC users.** Not addressed, but B is closer to today's Otto so existing queue plumbing transfers.
- **"Build a chat app."** Autopilot → T1. Reasonable default.

### Lazy moves

- "Discovery agent" — the entire T2 case rests here and B punts (§9). No prompt design, no consistency proof.
- "Manifest-consistency check, 50 lines" — wishful. Globs (`tests/browser/**` vs `tests/browser/test_transactions.*`) need real path-spec containment, not string compare. `build.py::detect_scope_violations` already does part of this *post-hoc* and let the contradictory spec through anyway. Lifting to pre-flight is right; the LOC estimate is fantasy.
- "Tier dispatcher rule-based" (§2) — four if-statements. §9 admits fragile. Real intents do not declare "web, CLI, daemon, REST API" with list markers. `--tier` escape valve invisible to users who do not know tiers exist.
- "T2 → T1 fallback for one group" — §4 requires concurrent-write conflict detection Otto does not have. `merge_queue.py` does sequential rebase merge today.
- "Naming" — §9 hedges.

### Silently kept

- B keeps `spec_compile.py` (5190 lines) for T2. The same prompt that produced the contradictory spec stays in production. Manifest check catches the *symptom*; prompt is still wrong.
- Keeps `merge_queue.py` (1983 lines) for T2. Every A-grievance about frozen FIFO dispatch still applies inside T2.
- B's T1 with `otto.spawn_subagent` *is* A-lite. B silently smuggles Proposal A in as a tier without committing to A's lock/ownership semantics.

### Provider fallback / SDK / malware reminder
Silent. B's tier model *could* express provider-aware behavior (T0 falls back on credit exhaust), but does not.

---

## What neither addresses

1. **Self-authored brittle locators** (finance-dash-claude class). Both assume certifier/audit catches it. Neither separates build-agent from test-agent so accidental selector reuse cannot occur.
2. **Mid-run intent amendment.** Multi-week builds will need it. A silent; B mentions tier badges, not amendment.
3. **Cross-module repair** (Feature A's repair needs change in B's owned paths). B's T2→T1 fallback half-addresses; A's "lead re-plans" does not say how released locks interact with in-flight subagents.
4. **`intent.md` drift mid-run.** Both ignore it; runtime intent is snapshotted at session start.
5. **Provider fallback** (Codex out of credits — *current real failure*). Both silent. Lead fallback model? Subagent provider inheritance? Cross-provider cost accounting?
6. **Cost ceiling across nested subagents.** A claims "subagents inherit a budget slice" — CC SDK Agent tool does not natively expose this. B silent.
7. **Determinism.** A makes it strictly worse (runtime decomposition non-deterministic). B's dispatcher is reproducible, T1 lead is not. Neither commits to record/replay.
8. **CC SDK `session_id` resumability.** Verified empirically (finance-true-web foundation 1→2→3 reused same id). A and B both need to specify which session resumes; subagent sessions punted.
9. **Malware-reminder injection on Read for non-Opus-4.6 models.** Affects every subagent Read on non-Opus-4.6 providers. Architecture must declare provider-pinning or accept contamination. Both silent.

---

## Verdict

**Neither is sufficient. B is closer to shippable; A names the deeper bug but is too unfinished.**

**Why B near-term:** §6's manifest-consistency check is the only concrete fix in either document that would have prevented the finance-true-web failure, in a single PR to `spec_compile.py`. B preserves T3 brownfield (the working part of Otto). A discards `spec_compile.py` (5190 lines) + `merge_queue.py` (1983) + repair gates + audit's spec-derived check generation, replacing all with hand-waved tools — losing the audit-derives-checks-from-spec invariant that defends against agent self-attestation (`project_proof_of_work.md`).

**Where A is right:** lock-based prevention beats post-hoc detection. B's manifest check still leaves `detect_scope_violations` post-hoc for genuine concurrent-write conflicts. Ownership *should* be discovered from the code, not predicted from intent text.

### Recommended path

1. **Ship B §6 pre-flight check this week.** In `spec_compile.py` post-validate: for every `shared_contracts[c]`, no `groups[g != c.owner_id].owned_paths` may contain a glob intersecting `c.paths`. Fail compile with a structured error naming the conflicting (group, contract). Implement glob containment correctly (this is the part B understates). Per `feedback_codex_fixes_own_bugs.md`, dispatch this fix to Codex.
2. **Bench T1-style `build_solo` against today's pipeline + manifest check on the i2p suite** before any deeper rewrite. `project_i2p_findings.md` shows otto wins on complex products via *scope accountability* — exactly what A discards.
3. **Defer dynamic decomposition (A) until:** depth-2 lead bench-validated; recursive subagent crash recovery designed; write-time lock semantics spec'd against the actual CC SDK Agent-tool API (returns string output, not a process handle — ownership-on-spawn is not free); provider fallback / session-resume / malware-reminder policy settled.
4. **Reject both proposals' silence on:** finance-dash-claude self-locator-collision class, mid-run intent amendment, cross-provider fallback, cross-subagent budget enforcement, SDK quirks. Independent of the tier-vs-dynamic axis; needs its own design.

**Honest summary: B is a refactor with a bug fix smuggled in. A is a research project. Ship B's pre-flight check now; treat A as v6, not v5.**
