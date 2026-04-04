# Plan: Unified Certifier — Single QA System (v3 — post Codex round 2)

## Goal

Replace per-task LLM QA with a unified certifier that is the single source of product truth. No duplicate coverage. Per-task check = just run tests.

```
Build phase:  code → agent's own tests → merge
Certify phase: structural → probes → regression → journeys → diagnosis
Fix phase:    targeted fix tasks → re-certify (all deterministic tiers rerun each round)
```

## Key design decisions (from Codex review)

### qa.py/spec.py: deprecate gradually, not delete
Runner depends on spec+QA for: retry feedback, no-change validation, batch QA in planned mode. Phase 1 skips them in monolithic mode only (`skip_qa=True` when certifier enabled). Phase 2 replaces planned-mode dependencies. Phase 3 removes dead code.

### Tier status: four-state enum, not bool
`passed | failed | blocked | skipped`. "App didn't start" = tier 2 blocked (prerequisite: tier 1 app_start passed). "Library has no server" = tier 2 skipped (not applicable). Each tier declares prerequisites explicitly.

### Per-interaction executors, not stretched web path
Current manifest/preflight/journey_agent assume HTTP. Don't force CLI through them. Instead:
- `HttpExecutor` — existing journey_agent + manifest + preflight (web apps, APIs)
- `CliExecutor` — new: invokes CLI commands, checks stdout/stderr/exit codes
- `LibraryExecutor` — new: imports module, calls functions, checks return values
Tier dispatch selects executor based on `classifier.interaction`. Each executor implements `run_probes()` and `run_journeys()`.

### Graduated tests: parameterized and versioned
Shell scripts with hardcoded `localhost:3000` will rot. Instead:
- Base URL is a parameter: `$BASE_URL/bookmarks` not `http://localhost:3000/bookmarks`
- Each test carries metadata: `{intent_hash, story_id, graduated_at, manifest_fingerprint}`
- Tests invalidated when intent_hash or manifest_fingerprint changes
- Stale tests quarantined (not deleted) — a quarantined test failing is a warning, not critical
- Stored under `otto_logs/certifier/regression-tests/` (otto-owned, not product code)

### State isolation between tiers
- DB/session reset between tier 3 (regression) and tier 4 (journeys)
- For web apps: AppRunner restart between tiers, or separate DB per tier
- All deterministic tiers (1-3) rerun every verification round — only tier 4 does targeted re-verify

### verification.py: native findings, not legacy shim
The fix loop must work with `Finding` objects directly — tier 1-3 failures don't map to journey objects. `verification.py` is cut over to native `CertificationReport` in Phase 1 (it's the primary consumer). The `to_legacy_dict()` shim is only for display (CLI output, telemetry), never for control flow.

### Executor selection: deterministic, no fallback
Drop "try alternate executor" — that creates nondeterminism and double-side-effect risk. One executor per classification, chosen deterministically. If the executor fails, that's a tier 1 finding, not a signal to retry with a different executor. If classifier.interaction is wrong, it's a classifier bug to fix.

### Graduated test fingerprinting: per-test, not per-manifest
Whole-manifest fingerprint is too coarse — a harmless route addition quarantines all tests. Instead: each graduated test fingerprints the specific routes/operations it uses. On re-certification, attempt rebinding (route renamed? try new path). Only quarantine tests whose bound resources are gone entirely.

### CLI journey anti-hang
CLI journey agents need: per-command timeout (30s default), `CI=true TERM=dumb` env, no-TTY execution (`< /dev/null`), killed process groups on timeout, hard rule that all CLI journeys must be non-interactive. These are executor-level constraints, not agent-level.

### Regression test portability
`otto_logs/certifier/` is machine-local — fresh clones lose all graduated tests. Solution: regression bundle export/import. `otto certify --export-regression` produces a portable JSON+scripts bundle. `otto certify --import-regression` loads it. CI can cache/restore the bundle. First-build-on-new-machine starts cold but can import from prior CI run.

**Trust boundary**: imported bundles contain executable shell scripts. Require provenance metadata: source repo identity (remote URL), source commit (ancestry check, not exact match), build_id, certifier version, content hash manifest. Gate on repo identity (remote URL must match). Source commit is provenance for audit — not an exact-match requirement, since the whole point is regression across revisions.

### certifier-reports → otto_logs/certifier
Move all certifier artifacts under `otto_logs/` which is already otto-owned and filtered from agent prompts/commits.

### No-op task acceptance
When coding agent makes no changes and tests pass, the task passes (existing behavior via test_command). The certifier later validates the product works regardless of which tasks had changes. No special no-op path needed — if the product is correct, it certifies.

## What changes

### 1. Skip LLM QA in monolithic mode (Phase 1)

**pipeline.py**: When certifier is enabled (default for `otto build`), pass `skip_qa=True` to build config. Runner skips spec gen + QA agent.

**runner.py**: Respect `skip_qa` flag. Per-task check = `test_command` + `verify` only. Coding loop: code → test → retry if fail → merge. No spec gen thread, no QA agent call.

**qa.py / spec.py**: Unchanged. Still used in planned mode and `otto run` (task-level execution without certifier).

### 2. Expand certifier with per-interaction executors

```python
class Executor(Protocol):
    """Interface for product-type-specific verification."""
    def run_structural(self, project_dir: Path, profile: ProductProfile) -> TierResult: ...
    def run_probes(self, project_dir: Path, manifest: ProductManifest) -> TierResult: ...
    def run_journeys(self, stories: list[UserStory], manifest: ProductManifest,
                     config: dict) -> TierResult: ...

class HttpExecutor:  # web apps + APIs — wraps existing journey_agent + manifest + preflight
class CliExecutor:   # CLI tools — bash invocations, stdout/stderr checks
class LibraryExecutor:  # libraries — import + function calls
```

Executor selection: `classifier.interaction` → executor, deterministically. No fallback to alternate executor. If classification is genuinely ambiguous, classifier returns `unknown` and override is required. Overrides are file-based and persistent (in `otto.yaml`: `certifier_interaction: http`) so autonomous runs (build, verification loop) don't need human-in-the-loop. CLI flag `--interaction` sets it for one run. Interactive TUI/REPL/curses apps classified as `interactive_cli` — unsupported in Phase 1, outcome = BLOCKED with explicit message.

Approved rebinds scoped strictly: `{test_id, old_binding, new_binding}` tuple. Expires on certifier-version or manifest-schema change (to prevent stale blanket approvals).

### 3. Tier status and prerequisites

```python
class TierStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"   # prerequisite tier failed
    SKIPPED = "skipped"   # not applicable for this product type

@dataclass
class TierResult:
    tier: int
    name: str
    status: TierStatus
    findings: list[Finding]
    blocked_by: str | None   # e.g. "tier_1:app_start" 
    skip_reason: str | None
    duration_s: float
    cost_usd: float

@dataclass
class CertificationReport:
    product_type: str
    interaction: str          # "http", "cli", "library"
    tiers: list[TierResult]
    findings: list[Finding]   # aggregated across all tiers
    outcome: CertificationOutcome  # passed/failed/blocked
    cost_usd: float
    duration_s: float

    @property
    def passed(self) -> bool:
        return self.outcome == CertificationOutcome.PASSED

    # Legacy compat — display only (cli.py, telemetry), never control flow
    def to_legacy_dict(self) -> dict:
        return {
            "product_passed": self.passed,
            "journeys": [f.to_journey_dict() for f in self.findings if f.tier == 4],
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
        }

class CertificationOutcome(Enum):
    PASSED = "passed"      # all critical findings resolved
    FAILED = "failed"      # product has critical findings
    BLOCKED = "blocked"    # cannot certify (unknown interaction, unsupported product type)
    # BLOCKED does not generate fix tasks — it's not a product bug
    # Threaded through: BuildResult.outcome, CLI exit code (0/1/2), telemetry
```

### 4. Tier implementation

**Tier 1 — Structural** (no LLM, seconds)
- Files exist: package.json / setup.py / Cargo.toml, main entry point
- Build succeeds: `npm install`, `pip install -e .`, `cargo build`
- Web/API: app starts (AppRunner) — produces `app_start` prerequisite
- CLI: entry point is executable / runnable
- Library: package build + clean-env install + import + basic use smoke (temp venv)
- Agent's tests pass: run `test_command`

**Tier 2 — Probes** (no LLM, seconds)
- Prerequisite: tier 1 app_start (for web/API) or tier 1 entry_point (for CLI)
- Dispatched to executor: HttpExecutor runs HTTP probes, CliExecutor runs CLI probes
- Existing code: adapter.py route discovery + manifest.py runtime probes (for HTTP)

**Tier 3 — Regression** (no LLM, seconds)
- Run graduated tests from prior certifications
- Parameterized: `$BASE_URL`, `$CLI_ENTRYPOINT` injected at runtime
- Metadata checked: skip if intent_hash or manifest_fingerprint changed (quarantine)
- Stored under `otto_logs/certifier/regression-tests/` (otto-owned)
- First build: skipped (no prior tests)

**Tier 4 — Journeys** (LLM, minutes)
- Prerequisite: tier 1 (structural checks passed — product is runnable)
- Dispatched to executor: HttpExecutor uses journey_agent.py, CliExecutor uses CLI journey agent
- CLI journeys: per-command timeout (30s), `CI=true TERM=dumb`, no-TTY (`< /dev/null`), killed process groups, non-interactive only
- Library: skipped (unit tests + regression sufficient)
- Includes break testing
- Produces graduated tests as side effect (parameterized scripts with per-test fingerprint)
- State reset: DB/app restart before tier 4 (isolate from tier 2-3 mutations)

### 5. Test graduation

Journey agent produces parameterized regression tests:

```bash
#!/usr/bin/env bash
# Graduated: "User Creates and Deletes Bookmark" (2026-04-04)
# Metadata: intent_hash=a3f2..., manifest_fp=7c1e..., story_id=bookmark-crud
set -euo pipefail
: "${BASE_URL:?BASE_URL required}"

# Step 1: Create bookmark
RESPONSE=$(curl -sf -X POST "$BASE_URL/bookmarks" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","title":"Test","tags":["test"]}')
ID=$(echo "$RESPONSE" | jq -r '.id')
[ -n "$ID" ] && [ "$ID" != "null" ] || { echo "FAIL: create"; exit 1; }

# Step 2: Verify in list
curl -sf "$BASE_URL/bookmarks" | jq -e ".[] | select(.id == $ID)" > /dev/null \
  || { echo "FAIL: not in list"; exit 1; }

# Step 3: Delete
STATUS=$(curl -sf -o /dev/null -w "%{http_code}" -X DELETE "$BASE_URL/bookmarks/$ID")
[ "$STATUS" = "204" ] || [ "$STATUS" = "200" ] || { echo "FAIL: delete $STATUS"; exit 1; }

echo "PASS: bookmark-crud"
```

**Graduation rules:**
- Only PASSED journey steps graduate
- Metadata: `intent_hash`, `manifest_fingerprint`, `story_id`, `graduated_at`
- Per-test fingerprint: hash of bound routes/operations the test uses (not whole manifest)
- Rebinding: attempt rebinding on re-cert (route renamed → try new path). Every rebind emits a `rebound` warning finding with old→new mapping. Critical regression tests require exact-match binding unless approved in config (`certifier_approved_rebinds` in `otto.yaml` — file-based, persistent for autonomous runs).
- Invalidation: quarantine only when bound resources gone entirely and rebinding fails.
- **Regression health gates**: percentage AND absolute-count thresholds. Critical tests weighted 2x. Default: quarantined >50% OR >5 absolute OR rebound >30% OR >3 absolute (of critical) = tier 3 FAILED. Configurable in otto.yaml. Prevents silent coverage decay on both small and large suites.
- Quarantined test failure = warning (not critical) — may be legitimately outdated
- Fresh test failure = critical regression finding
- Portable: `otto certify --export-regression` / `--import-regression` for CI and fresh clones

### 6. Pipeline integration

**pipeline.py**: Call `run_unified_certifier()`, translate via `report.to_legacy_dict()` for BuildResult.

**verification.py**: 
- Uses `CertificationReport.findings` natively for fix prompts (not legacy journey dicts)
- **Diagnosis compaction**: dedupes findings by root cause. Tier 1 app-start failure = one root finding, downstream tier 2-4 blocked findings suppressed from fix prompt. Caps each round to smallest actionable set. Warnings (quarantined regressions, rebinds) excluded from fix prompt.
- Deterministic tiers (1-3) rerun every round (catch regressions from fixes)
- Tier 4 does targeted re-verify (skip passed stories)
- State reset between rounds

**runner.py**: When `config.get("skip_qa")`, skip spec gen + QA. Keep full path for `otto run`.

### 7. What to keep, what to remove

**Keep (certifier/):**
- classifier.py — product type detection (treat as low-confidence hint)
- adapter.py — code analysis (routes, models, auth, seeds)
- stories.py — story compilation (tier 4 input)
- manifest.py — product manifest (HTTP executor)
- journey_agent.py — tier 4 HTTP executor engine
- baseline.py:AppRunner — app lifecycle management

**Absorb into new modules:**
- preflight.py → tiers.py (tier 1 structural checks)

**Keep alive (Phase 1), deprecate later:**
- qa.py — still used in planned mode + `otto run`
- spec.py — still used in planned mode + `otto run`

**Remove in Phase 2+:**
- intent_compiler.py — replaced by stories.py
- binder.py — journey agent handles binding dynamically
- tier2.py — replaced by journey_agent.py
- pow_report.py — replaced by CertificationReport

**New:**
- certifier/report.py — CertificationReport, TierResult, TierStatus, Finding
- certifier/tiers.py — tier 1 (structural) + tier 2 (probes)
- certifier/regression.py — tier 3: run + manage graduated tests
- certifier/graduation.py — extract regression tests from journey results
- certifier/executor.py — Executor protocol + HttpExecutor + CliExecutor

## Implementation phases

### Phase 1: Unified certifier for web apps (monolithic mode)
- report.py dataclasses
- tiers.py (structural + probes — mostly wrapping existing preflight + manifest)
- __init__.py: `run_unified_certifier()` that runs tiers 1-4, returns CertificationReport
- pipeline.py: pass `skip_qa=True`, use unified certifier
- runner.py: respect `skip_qa` flag
- Legacy compat shim (to_legacy_dict)
- E2E test: bookmark manager

### Phase 2: Test graduation
- graduation.py: extract regression tests from journey agent results
- regression.py: run parameterized tests, manage metadata, quarantine stale
- Journey agent prompt: produce test scripts as side effect
- E2E test: second build uses graduated tests from first

### Phase 3: CLI + library executors
- executor.py: Executor protocol, HttpExecutor (wraps existing), CliExecutor, LibraryExecutor
- stories.py: CLI-aware story compilation prompts
- Tier dispatch based on classifier.interaction
- E2E test: temp converter CLI gets tiers 1+3+4

### Phase 4: Deprecate per-task QA
- Replace planned-mode batch QA with certifier
- Remove spec gen from runner.py entirely
- Deprecate qa.py, spec.py

## Files to modify

- NEW: `otto/certifier/report.py`
- NEW: `otto/certifier/tiers.py`
- NEW: `otto/certifier/regression.py`
- NEW: `otto/certifier/graduation.py`
- NEW: `otto/certifier/executor.py`
- MODIFY: `otto/certifier/__init__.py` — new unified entry point
- MODIFY: `otto/certifier/journey_agent.py` — produce graduated tests
- MODIFY: `otto/pipeline.py` — skip_qa + unified certifier
- MODIFY: `otto/verification.py` — use CertificationReport
- MODIFY: `otto/runner.py` — respect skip_qa
- MODIFY: `otto/git_ops.py` — mark `otto_logs/certifier/` as otto-owned
- KEEP: `otto/qa.py`, `otto/spec.py` (until Phase 4)

## Verify

- [ ] Monolithic build: no LLM QA during build, only test_command
- [ ] Certifier runs all 4 tiers for web apps
- [ ] Tier status: blocked when prerequisite fails, skipped when not applicable
- [ ] App start failure → tier 1 finding, tier 2 blocked, tier 3 still runs, tier 4 blocked
- [ ] Deterministic tiers (1-3) rerun every verification round
- [ ] State reset (DB/app restart) between tier 3 and tier 4
- [ ] Journey agent produces parameterized graduated tests with metadata
- [ ] Graduated tests fingerprinted per-test (bound routes/operations), not whole manifest
- [ ] Stale graduated tests attempt rebinding before quarantine
- [ ] Fix prompts reference all tier findings (not just journeys)
- [ ] Legacy compat: pipeline.py, cli.py, telemetry still work via to_legacy_dict()
- [ ] Certifier artifacts under otto_logs/ (not product-owned certifier-reports/)
- [ ] `otto run` (non-build) still uses qa.py/spec.py in Phase 1
- [ ] Planned mode still uses qa.py/spec.py in Phase 1
- [ ] Cost by tier measured and logged — total lower than current QA + certifier
- [ ] Fresh-environment smoke for CLI: build → install in temp dir → execute
- [ ] Certifier leaves repo clean (no uncommitted files outside otto_logs/)
- [ ] All existing pipeline tests pass
- [ ] verification.py uses native Finding objects for fix tasks (not legacy journey dicts)
- [ ] Executor selection is deterministic — no fallback to alternate executor
- [ ] CLI journeys: per-command timeout, CI=true, TERM=dumb, no-TTY, non-interactive
- [ ] Library tier 1: clean-env install + import + use smoke
- [ ] Regression bundle export/import for CI portability
- [ ] CertificationOutcome threaded through BuildResult, CLI (exit 0/1/2), telemetry
- [ ] BLOCKED outcome: does not generate fix tasks, distinct CLI message + exit code 2
- [ ] Overrides file-based in otto.yaml (certifier_interaction, certifier_approved_rebinds)
- [ ] Regression health gates: quarantined >50% or rebound >30% = tier 3 FAILED
- [ ] Regression import gates on repo identity (remote URL), not exact commit match
- [ ] Rebinds emit warning findings with old→new mapping
- [ ] Interactive TUI/REPL apps classified as unsupported, blocked with message
- [ ] Fix prompt diagnosis compaction: root-cause dedup, blocked findings suppressed
- [ ] E2E: bookmark manager builds and certifies with unified system

## Plan Review

### Round 1 — Codex (11 issues)
- [ISSUE] Web-first assumption in certifier stack — fixed: per-interaction executors (HttpExecutor, CliExecutor, LibraryExecutor)
- [ISSUE] Deleting qa.py/spec.py breaks planned mode — fixed: gradual deprecation, Phase 1 = skip_qa flag only
- [ISSUE] TierResult missing blocked state — fixed: four-state enum (passed/failed/blocked/skipped) with prerequisites
- [ISSUE] Graduated tests will rot (hardcoded ports, stale routes) — fixed: parameterized ($BASE_URL), versioned (intent_hash + manifest_fingerprint), quarantine stale
- [ISSUE] Shared mutable state between tiers — fixed: state reset between tiers, deterministic tiers rerun every round
- [ISSUE] Classifier not reliable enough for tier policy — fixed: treat as low-confidence hint, fallback to alternate executor
- [ISSUE] No-op task acceptance path lost — fixed: test_command pass = task pass, certifier validates product-level correctness
- [ISSUE] Backward compatibility breakage (journeys/product_passed contract) — fixed: to_legacy_dict() shim, gradual consumer cutover
- [ISSUE] certifier-reports/ not otto-owned — fixed: move to otto_logs/certifier/
- [ISSUE] Tier 1 build mutates workspace — fixed: runs in project dir which is already a worktree post-merge
- [ISSUE] CLI tier table contradicts verify checklist — fixed: consistent tier table + verify criteria aligned

### Round 2 — Codex (6 issues)
- [ISSUE] to_legacy_dict() not enough for control flow — fixed: verification.py uses native Finding objects, shim only for display
- [ISSUE] Executor fallback nondeterminism — fixed: deterministic executor selection, no fallback, classifier wrong = bug to fix
- [ISSUE] manifest_fingerprint too coarse for graduation — fixed: per-test fingerprint of bound routes/operations, attempt rebinding before quarantine
- [ISSUE] CLI journeys need anti-hang — fixed: per-command timeout, CI=true, TERM=dumb, no-TTY, killed process groups
- [ISSUE] Regression tests machine-local — fixed: portable export/import bundle for CI
- [ISSUE] Library fresh-env smoke missing — fixed: clean-env install+import+use in tier 1

### Round 3 — Codex (5 issues)
- [ISSUE] Regression import is a trust boundary (executable shell scripts) — fixed: provenance metadata + repo identity check, reject mismatched imports
- [ISSUE] No safe degradation for ambiguous classification — fixed: `unknown` classification + config/CLI override (`--interaction http`), interactive TUI/REPL apps blocked explicitly
- [ISSUE] Automatic rebinding can silently hide regressions — fixed: rebinds emit `rebound` warning finding, critical tests require exact-match binding
- [ISSUE] Interactive TUI/REPL/curses apps unaccounted for — fixed: classified as `interactive_cli`, unsupported in Phase 1, blocked with message
- [ISSUE] Fix prompts noisy with all-tier findings — fixed: diagnosis compaction dedupes by root cause, suppresses derivative blocked findings, caps to smallest actionable set

### Round 4 — Codex (4 issues)
- [ISSUE] Import provenance gate too strict (commit exact match) — fixed: gate on repo identity (remote URL), commit is provenance metadata only
- [ISSUE] Top-level report needs blocked outcome, not just tier-level — fixed: CertificationOutcome enum (passed/failed/blocked), blocked does not generate fix tasks
- [ISSUE] Overrides/approvals need to work in autonomous runs — fixed: file-based in otto.yaml (certifier_interaction, certifier_approved_rebinds), not CLI-only
- [ISSUE] Warning-only handling hollows out regression coverage — fixed: regression health gates (quarantined >50% or rebound >30% = tier 3 FAILED)

### Round 5 — Codex (3 issues)
- [ISSUE] BuildResult/CLI/telemetry flatten outcome to bool — fixed: thread CertificationOutcome through BuildResult, CLI exit code (0/1/2), telemetry
- [ISSUE] certifier_approved_rebinds too broad — fixed: scoped by {test_id, old_binding, new_binding} tuple, expires on certifier-version/manifest-schema change
- [ISSUE] Percentage-only regression gates spurious on small/large suites — fixed: both percentage AND absolute-count thresholds, critical tests weighted 2x
