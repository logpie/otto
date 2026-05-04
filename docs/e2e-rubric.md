# Otto E2E Evaluation Rubric

This rubric defines my standards for judging an end-to-end Otto run. It exists
because past audits had false positives — I want to make criteria explicit
**before** running, not adapt them post-hoc to make a run look better.

A run is judged across five dimensions. **A run only PASSES if every dimension
passes.** No partial credit; if I'm tempted to add caveats, that's a FAIL.

## 1. Compile honesty

The compiled spec must be specific enough that two independent build agents
could not drift on structure.

| Check | Criteria |
|---|---|
| Validator warnings | No "tasks too vague", "no checks declared", "tasks field empty", "cross_slice_checks missing" warnings. Other warnings (e.g., missing optional structure fields) reviewed case-by-case. |
| Task concreteness | Every `slice.tasks[i]` names a specific file path, API shape, data structure, or visible behavior — not "implement the feature" prose. |
| Owned paths declared | Every slice has at least one entry in `owned_paths` OR explicitly justifies why not. |
| Spec round-trip | `spec.json` parses back to the same object. No silent coercions reported. |

## 2. Build honesty

Every slice's branch must reflect what the slice actually contributed.

| Check | Criteria |
|---|---|
| Per-slice branches | `git branch --list 'i2p/*/*'` shows N branches for N PASSING slices. |
| Per-slice commits | Each slice branch has at least one commit beyond its parent ref (`git log <parent>..<slice-branch>` non-empty), unless the slice was vacuous (no `tasks` and no `owned_paths`). |
| No phantom REDUNDANT | Slices that declared `tasks` or `owned_paths` did NOT report REDUNDANT (would mean over-reach by another slice). |
| State journal matches reality | `slice.merge.landed` events' commit hashes resolve in `git log --all`. `replay()` reports zero `unreconciled_landed_ids` and zero `duplicate_hash_landed_ids`. |
| Scope warnings investigated | Any `scope.warning` events have been examined. Either justified (legitimate cross-cut) or fixed. Silent acceptance fails. |

## 3. Merge honesty

Real merges, not commit-in-shared-worktree fictions.

| Check | Criteria |
|---|---|
| Real merge commits | `git log --merges main` shows one merge per PASSING slice. Commit message format `i2p(slice_id): merge slice branch ...`. |
| Two parents per merge | `git log -1 --pretty=format:%P <merge-commit>` returns two parent hashes (not one — fast-forward indicates the merge model is broken). |
| Conflict-repair on slice branch | If any merge conflicted, the repair commit lives on the SLICE branch (`git log --format=%h <branch> | head -3` includes a `i2p(...): repair...` commit), not on main. |
| Dep order respected | A dependent slice's merge commit must be reachable from `main^{commit}` only AFTER its dep's merge commit. |

## 4. Audit honesty

The audit must report symptoms accurately, never patch.

| Check | Criteria |
|---|---|
| Verdict matches reality | If I run the product manually, behavior matches what audit verdict claims. PASSED means the contract test really passes; PARTIAL means there are real gaps; BLOCKED means the product genuinely doesn't work. |
| Capability verdicts populated | `audit_result.capability_verdicts` non-empty, each with status/detail/evidence_refs. |
| Contract test result honest | `contract_test_passed` reflects the actual `test_command` exit code. If `False`, `contract_test_detail` cites the failure. |
| Audit didn't write code | `git log --author=audit` is empty. The audit agent's permission_mode is `bypassPermissions` (asserted at runtime). |
| Fix-loop integrity | If audit attempted fixes, each `audit.attempt.finished` event records the verdict per attempt. Final verdict reflects whether fixes landed (no silent PASSED upgrade after a failed fix). |

## 5. Product quality (the real test)

Otto's claim is "intent to product." The product must actually work.

| Check | Criteria |
|---|---|
| Files specified in structure exist | Every `routes[i].path` has a corresponding handler in code. Every `components[i].name` exists where declared. Every `data_models[i]` entity has a real schema. |
| Imports resolve | `python -c "import <main_module>"` succeeds. No `ModuleNotFoundError`. |
| Test command passes | `otto.yaml`'s `test_command` exits 0 against the merged product. |
| Webapp: pages render | If webapp, dev server starts, key routes return 200, declared `key_text` actually appears in HTML. |
| CLI: invoked as documented | If CLI, the documented invocation runs without error and produces output matching declared behavior. |
| API: endpoints respond | If API, declared endpoints accept declared request shapes and return declared response shapes. |
| No leftover stubs | No `raise NotImplementedError`, no `# TODO: implement`, no `pass  # stub`. |
| Sensible code | Not just enough to pass tests — code that a junior engineer wouldn't be embarrassed to commit. |

## Rubric application

For each E2E run, I'll write `e2e-results/<project-id>/judgment.md` containing:

1. The project intent
2. Per-dimension pass/fail with specific evidence (commit hashes, file paths, command outputs)
3. Overall verdict (PASS / FAIL)
4. If FAIL: root cause analysis — is this an Otto design bug or a one-off
   LLM artifact? Otto bugs trigger a fix in the codebase before the next run.

## Project escalation ladder

Microfeed-scale (~5-8 slices, 1 framework, 1 datastore) is a **checkpoint**,
not a ceiling. Pass it, then escalate. Each tier adds a dimension Otto must
generalize across.

| Tier | Scale | Adds | Example projects |
|---|---|---|---|
| **T1 Smoke** | 1-3 slices | basic harness | Flask todo, log-line filter CLI |
| **T2 Microfeed-class** | 5-8 slices, single framework | DB + auth + forms | Bookmark manager, RSS reader, paste-bin |
| **T3 Multi-component** | 8-12 slices, 2+ runtimes | frontend + backend boundary | React + FastAPI app, CLI + library duo, static site generator with templating engine |
| **T4 Real-app scale** | 12-20 slices, multiple integrations | external services, queues, background work | Slack-clone (channels, real-time, presence), URL shortener with analytics, image-resize service with S3-like storage, multi-tenant SaaS skeleton |
| **T5 Complex systems** | 20+ slices, distributed concerns | concurrency, persistence migrations, caching, retries, graceful degradation | Job-board with search/email/admin/queue, Kanban with WebSocket sync + offline mode, e-commerce checkout with payment-stub + inventory + order state machine |
| **T6 Production-shaped** | greenfield in unfamiliar stack | non-Python/JS, polyglot, infra-as-code | Go HTTP service with Postgres + migrations, Rust CLI with structured plugins, Elixir Phoenix LiveView app |

**Escalation rule**: a tier is "passed" only when 2+ projects in that tier
score full PASS on every rubric dimension. One pass is luck; two is signal.
Fail at any tier → root-cause, fix, re-run that tier (don't skip ahead).

**Diversity within tier**: I will not run two projects of the same shape
(e.g., two Flask CRUD apps) at the same tier. Diversity exposes generalization
gaps that overfit hides.

**No tier is the ceiling.** When T6 passes, I'll invent T7 (e.g., debugger
fix or real refactor against a 50k LOC repo, not greenfield). The point is
to find Otto's actual ceiling, not stop early.

## Anti-patterns I'll catch myself doing

- "It mostly worked" → FAIL (no partial credit)
- "The audit said passed so it passed" → check the audit's reasoning against reality
- "The slice didn't write that file because of X" → was X declared in the spec? if not, FAIL
- Skipping dimensions because they "obviously pass" → run the check anyway
- Adapting the rubric mid-run → if the rubric is wrong, fix it BEFORE the next project, not during this one
