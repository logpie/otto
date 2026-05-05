# Otto non-webapp walkthrough review

Reviewing: `research.md`, `plan.md`, `docs/otto-wireframes.md`.
Three test projects: REST API, Python library, CLI tool.

---

## Project A — REST API ("tasks API")

CRUD for todo items, OAuth2 auth, pagination, search, rate limiting, webhooks. JSON in / JSON out. No browser.

**Compile output / `structure`.** `project_kind=api` exists as an enum value. Good. But `structure` has no defined contract for API projects anywhere in the three documents — the only illustrated example is `webapp · Flask + SQLite`. For an API, `structure` should encode framework, DB, auth scheme, transport. Without a per-kind schema, two Compile runs will emit incompatible blobs.

**Evidence kinds.** `ApiProbe` is the workhorse and fits. `RepoTestCheck` and `StateInvariant` apply. `BrowserJourney` is inapplicable — there is no browser — yet it appears as a checked-by-default option in the Add Feature modal (screen 4c) with no project-kind suppression. A deeper gap: webhook delivery cannot be verified by any of the four kinds. Verifying outbound side-effects requires a listener-and-trigger pattern that `ApiProbe` as specified does not model.

**Audit shape.** The audit agent has no browser to walk. The phrase "walks the product" in research.md §6 carries browser-navigation connotations that will mislead a prompt-following LLM. Quality findings likewise default to visual/UX concerns (the "hero section lacks visual distinction" example from research.md). For an API, quality findings should cover error response shape and contract consistency — none acknowledged by the design.

**Proof packet.** Screen 6 (per-Feature drilldown) leads with three screenshot thumbnails as the primary visual evidence. For an API feature, those thumbnails are blank. `ApiProbe` results appear only in the "Deterministic checks" subsection at the bottom. The proof is structurally present but visually inverted: the persuasive content (request/response traces) is buried; the prominent slot is empty.

**MC drawer.** The Run drawer and Stage timeline transfer cleanly. The evidence kind checkboxes on screens 4b/4c do not filter by project kind — `BrowserJourney` is always shown. The "Open proof packet" CTA implies a browser walkthrough video that does not exist for an API run.

---

## Project B — Python library ("retryable")

Context manager with exponential backoff, configurable predicates, jitter, structured logging. PyPI-publishable. No UI, no server.

**Compile output / `structure`.** `project_kind=library` exists. `structure` is again unspecified for this kind. A library needs: language, packaging tool (hatch/flit/uv), Python version constraint, entry point, type annotation style, test framework. The Compile prompt presumably contains webapp-biased examples that will produce group names like "routes" or "templates" for a library project.

**Evidence kinds.** Only `RepoTestCheck` squarely applies. `ApiProbe` and `BrowserJourney` are entirely inapplicable. `StateInvariant` can be repurposed (in-process postconditions) but is conceptually stretched. Three kinds the design lacks entirely:
- `ImportCheck` — `import retryable` after install verifies the package is actually importable. Fundamental.
- `TypeCheck` — mypy/pyright pass over the package. Library consumers depend on this.
- `DoctestCheck` — docstring examples execute correctly. Highly persuasive to users.

Without these, the library's proof rests almost entirely on its own test suite — no independent verification that the installed artifact is importable or that its public API examples work.

**Audit shape.** There is no runtime surface to walk. An LLM audit agent must execute Python snippets and observe outputs — a REPL session, not a browser walkthrough. The `walkthrough.jsonl` schema (`screenshot?`, `dom_snapshot?`) is purely browser-derived; there is no `code_output` or `exec_result` field. The design does not acknowledge this duality.

**Proof packet.** The screenshot grid is empty. The "Saved DOM" section is inapplicable. What should lead the per-Feature proof for "exponential backoff with jitter" is: (a) a runnable usage example showing the context manager, (b) `RepoTestCheck` output, (c) any type-check results. The current template has no rendering path for code-centric evidence as the primary proof element.

**MC drawer.** The Form view (screen 4b) shows `BrowserJourney` and `ApiProbe` checkboxes for a library feature. Both are meaningless here. The spec editor shows `webapp · Flask + SQLite` under project kind — the `library · Python 3.11+ · hatch · pytest` equivalent is not illustrated and has no defined rendering.

---

## Project C — CLI tool ("git-flow-helper")

Subcommands for managing git feature branches. Reads git state, executes git commands, shells output to terminal.

**Compile output / `structure`.** `project_kind=cli` exists. `structure` should encode: language, CLI framework (Click/Typer/argparse), invocation mode (installed binary vs `python -m`), entry point. Unspecified. The subcommand-to-Group mapping (`start`, `finish`, `list` → one Group each) is clean and the file-overlap merging rule works well here. The structural logic of Compile transfers; the prompt content does not.

**Evidence kinds.** `RepoTestCheck` and `StateInvariant` (e.g., "branch `feature/foo` exists after `start`") apply. `ApiProbe` and `BrowserJourney` do not. The primary missing kind is `CLIProbe`: invoke binary with arguments, capture stdout/stderr/exit code, assert against patterns. This is structurally identical to `ApiProbe` with "subprocess invocation" replacing "HTTP request." Without it, CLI verification has no first-class check kind.

**Audit shape.** This is the strongest non-webapp case. A CLI audit agent invokes subcommands and observes outcomes — a clear, scripted interface. Feature-tagging applies naturally. What the schema lacks: `exit_code`, `stdout`, `stderr` fields in `walkthrough.jsonl`. The audit agent also needs the CLI's invocation path — the equivalent of the web server address — which should live in `structure.entry_point` but is currently unspecified.

**Proof packet.** Screenshot grid is again empty. The right proof lead for "start subcommand creates branch with correct naming" is a terminal transcript block: the invoked command, stdout, and exit code. This is compelling and unambiguous evidence. The current template has no slot for it.

**MC drawer.** The drawer transfers cleanly for CLI. The evidence kind filter issue recurs: `BrowserJourney` and `ApiProbe` are shown for CLI features. The "Open proof packet" primary CTA implies visual content that is absent. An empty screenshot section in the proof packet is actively misleading — it implies capture failed rather than communicating that CLI projects produce a different evidence form.

**Recursive case.** Otto auditing a CLI tool is well-suited and needs no architectural change — only a prompt variant that says "invoke the CLI" rather than "navigate to the URL," and `structure.entry_point` supplying the invocation path.

---

## Generalization verdict

The core model — Intent → Spec → Features/Groups → Build → Audit → Render → Proof — is genuinely project-kind-neutral and transfers cleanly to all three projects. The pipeline stages, retry loops, vocabulary, and session layout are sound. The design is **not** secretly webapp-only in its architecture.

It is, however, webapp-only in its instantiations: every prompt example, every evidence kind, every proof template element, and every wireframe callout was written with a browser app in mind. The `project_kind` enum acknowledges four types; the design only illustrates one. The fixes are specification work and template parameterization, not architectural rework.

---

## Top 5 changes for genuine project-kind agnosticism

**1. Define `structure` contracts per `project_kind`.** The field must have a typed schema per kind: `webapp` → `{framework, db, auth_scheme}`; `api` → `{framework, db, auth_scheme, transport}`; `library` → `{language, packaging_tool, python_version, entry_point}`; `cli` → `{language, cli_framework, invocation, entry_point}`. The `compile.md` prompt must include one example per kind. Without this, Compile emits uncontrolled blobs and the audit agent has no equivalent of the web server URL for non-webapp projects.

**2. Add `CLIProbe`, `ImportCheck`, `TypeCheck` to `otto/checks.py`.** `CLIProbe` is `ApiProbe` with subprocess-invocation replacing HTTP. `ImportCheck` and `TypeCheck` are single-command checks (`python -c "import pkg"`, `mypy pkg/`). These three additions give each non-webapp `project_kind` at least one first-class check kind that directly fits its primary verification mode.

**3. Extend the `walkthrough.jsonl` line schema.** Add `action_kind: "cli_invoke | api_request | browser_navigate | code_exec"`, `command`, `exit_code`, `stdout`, `stderr` as optional fields alongside the existing `screenshot?` and `dom_snapshot?`. Parameterize the `audit.md` prompt by `project_kind` so the LLM uses "invoke" language for CLI, "execute" language for library, and "navigate" language for webapp.

**4. Filter evidence kind UI by `project_kind`.** Screens 4b and 4c must suppress inapplicable check kinds. For `library`: show `RepoTestCheck`, `ImportCheck`, `TypeCheck`, `DoctestCheck`; hide browser/HTTP kinds. For `cli`: show `CLIProbe`, `StateInvariant`, `RepoTestCheck`; hide the rest. Advanced users can always add overrides. Default visibility should not suggest meaningless options.

**5. Add a non-visual proof layout branch in the templates.** `feature-proof.html.j2` must branch on `project_kind`. For `library` and `cli`, the primary evidence block renders code/terminal content — a usage example and test output for a library, a command transcript with exit code and stdout for a CLI. The screenshot grid section must be conditionally absent, not an empty placeholder. An empty three-box screenshot grid implies evidence capture failed; a rendered terminal transcript communicates clearly that this product kind has a different but equally valid evidence form.
