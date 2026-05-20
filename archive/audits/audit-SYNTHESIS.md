# Otto Simplification Audit — Synthesis

**Date:** 2026-05-19
**Inputs:** audit-cli.md, audit-v5.md, audit-journey.md, audit-spec-build.md, audit-scripts-docs.md, audit-tests.md, audit-bugs.md

---

## Headline

The biggest simplification win is **deleting the legacy `otto run` pipeline** (~13,000 LOC across 6 modules + 40–50 legacy tests). It's been deprecated in favor of `otto v5 run` for months — per CLAUDE.md it "does NOT converge on large intents", and the CLI itself prints a deprecation warning when invoked. Outside that, the v5 code is in good shape: no orphan modules, no dead branches from prior pivots, just ~200 LOC of dead helpers and 4 real (mostly contained) bugs.

The memory note about "$25 tree_budget_usd silent cap" turned out to be wrong — it's documented in `--tree-budget-usd` CLI help and is overridable. I'll fix that memory note.

---

## Tier 0 — Mechanical cleanups (safe, no behavior change) — ~400 LOC

| Item | Source | Estimate |
|---|---|---|
| Delete 5 dead `v5_*` helpers (`_ensure_playwright_browsers`, `_is_noise_path`, `_charter_prose_line_count`, `_path_matches_leaf_extension`, `_has_unmerged_paths`) | audit-v5 | ~105 LOC |
| Delete 5 dead cli.py helpers (`_build_locked`, `_load_config_or_exit`, `_validate_brownfield_mode`, `_exit_for_lock_busy`, helper variants) | audit-cli | ~35 LOC |
| Consolidate 3× duplicate `_positive_budget_option` / `_max_turns_option` / `_rounds_option` validators into shared module | audit-cli | ~30 LOC |
| Extract shared v5 helpers (`_git_capture`, `_iso_now`, `_coerce_spec`, `_read_text`) to `v5_common.py` | audit-v5 | ~45 LOC dedup |
| Centralize journey browser-runner detection + verdict aggregation (currently duplicated in `lead_verify.py` and `v5_clean_verify.py`) | audit-journey | ~75 LOC dedup |
| Delete 12 orphan test files with zero test functions (after verifying) | audit-tests | ~3500 LOC tests |
| Update stale "Phase 1.2-A/B" docstrings in v5_preflight, v5_runner, v5_review | audit-v5 | doc only |

**Risk:** Low. Strictly removes unreachable code or moves helpers.

---

## Tier 1 — Bug fixes (correctness wins) — 4 bugs

| Bug | File:Line | Severity | Fix |
|---|---|---|---|
| **BUG-1** — `dir_fd` leaked on `fsync` failure | observability.py:85-88 | High | Nested try/finally |
| **BUG-2** — Race: `next()` lookup over in_flight dict can crash with StopIteration in parallel dispatch | v5_runner.py:6418-6424 | High | Defensive loop with `None` guard |
| **BUG-3** — Resume across phases preserves stale `spec_path` from a prior, incompatible intent | checkpoint.py:524-604 | Medium | Add `spec_phase_version` field |
| **BUG-4** — Budget-cap drain silently swallows cancelled-task exceptions, leaves tasks orphaned in `pending_children` | v5_runner.py:5703-5718 | Medium | Iterate gather() results, log+verdict each |

Per CLAUDE.md ("Codex fixes Codex-found bugs"), these were found by a Claude audit agent, not Codex — so Claude can fix them, but BUG-2 + BUG-4 are concurrency-correctness territory and should be Codex-gated before merge.

---

## Tier 2 — Repo hygiene — gitignore + archive — ~2.2 MB

| Item | Source |
|---|---|
| `.gitignore` additions: `loop-report.md` (175k LOC!), `loop-evidence/`, `e2e-results/`, `.runlogs/otto-as-user-claude/`, `loop-config.json` | audit-scripts-docs |
| Move 25+ stale root `plan-*.md` / `research-*.md` / `DEBUG-*.md` / `debug-*.md` into `archive/` (preserves history but cleans root) | audit-scripts-docs |
| Keep: `plan.md`, `progress.md`, `review.md`, `research.md`, `README.md`, `CLAUDE.md`, `AGENTS.md`, plus current-sprint plans | audit-scripts-docs |

**Risk:** Low. Archiving is reversible; gitignoring tracked files needs a `git rm --cached` first.

---

## Tier 3 — Legacy `otto run` pipeline deletion — ~13,000 LOC

**This is the big simplification.** Per CLAUDE.md, `otto run` is the legacy "groups/features" pipeline that "does NOT converge on large intents — prefer `otto v5 run`". The CLI already warns users on invocation. Behind it sits:

| Module | LOC | Role |
|---|---|---|
| `otto/spec_compile.py` | 5480 | Legacy schema v3 compile (vs `spec_compile_flat.py` schema v4) |
| `otto/build.py` | 3818 | Legacy per-slice build orchestration (vs v5_runner.py) |
| `otto/runner.py` | 1859 | Legacy pipeline orchestrator |
| `otto/audit.py` legacy fix-loop | ~1500 | (keep verdict types, extract to `audit_types.py`) |
| `otto/spec_amend.py` legacy bits | ~300 | Amendment chain (keep `spec_review_routes.py` integration) |
| `otto/spec_warnings.py` | 87 | Used only by `spec_compile.py` |
| `otto/cli_run.py` | 1733 | Legacy CLI surface; replace with thin stub that errors out |
| `otto/seed.py` legacy bits | TBD | |
| **Total non-test** | **~13,000+ LOC** | |
| Legacy tests removed | ~40–50 files | test_cli_run, test_runner, test_build_*, test_spec_compile, test_audit |

**Side effects:**
- `otto run "<intent>"` would error out with "removed, use `otto v5 run`" instead of executing the legacy pipeline
- 5 JSON schema files in `spec_schemas/` removed (v3 schemas; flat compile uses inline v4)
- Resume/replay of pre-v5 sessions becomes impossible (already broken in practice)

**Risk:** Medium. Pure deletion is mechanical, but the surface is wide — needs at least one v5 e2e run post-deletion to confirm no accidental imports.

---

## Tier 4 — Modules not audited yet (need round 2)

Not covered by round 1 — recommend a follow-up audit pass:
- `otto/web/` + `otto/mission_control/` (web UI + Mission Control API)
- `otto/queue/` + `otto/merge_queue.py` + `otto/merge/`
- `otto/certifier/`, `otto/verification/`
- `otto/branching.py`, `otto/worktree.py`, `otto/checkpoint.py`, `otto/resume.py`
- `otto/runs/`, `otto/scaffold_profiles/`
- Smaller utility modules: `redaction.py`, `theme.py`, `display.py`, `markers.py`, `replay.py`, `safe_slug.py`, `setup_*.py`, `pipeline.py`, `mcp_tools.py`

---

## Total potential simplification

| Tier | LOC removed | Risk |
|---|---|---|
| 0 (mechanical) | ~400 prod + ~3500 dead test files | Low |
| 1 (4 bugs) | ~20 lines added (defensive code) | Medium (concurrency) |
| 2 (repo hygiene) | 0 source; 2.2MB tracked artifacts gone | Low |
| 3 (legacy `otto run`) | **~13,000 prod + ~15,000 tests** | Medium |
| **Total** | **~13,400 LOC + ~18,500 test LOC** | |

Otto goes from ~97k → ~84k LOC (-14%) on the production side, with the legacy maze gone.
