# Adversarial review: Otto against "build me a web browser"

*Reviewer: adversarial senior design review. Framing: a Chromium-style browser — rendering engine, networking stack, JS runtime, multi-process architecture, UI chrome, extensions, sandboxing. Not a webpage. A product.*

---

## 1. Decomposition test — what does Compile produce?

### What the LLM would likely generate

Given `otto run "build me a web browser"`, the compile agent would probably produce something like:

```
Groups & Features
  Networking
    ✓ HTTP/HTTPS request handling
    ✓ DNS resolution
    ✓ Cookie + session management
  Rendering
    ✓ HTML parser
    ✓ CSS layout engine
    ✓ DOM tree construction
  JavaScript
    ✓ JS engine (parsing + execution)
    ✓ DOM API bindings
    ✓ Event loop
  Browser chrome
    ✓ Address bar / navigation
    ✓ Tab management
    ✓ Bookmarks
  Storage
    ✓ History
    ✓ LocalStorage / IndexedDB
  Extensions
    ✓ Extension loader
    ✓ WebExtensions API subset
  Security
    ✓ Sandboxing
    ✓ Content security policy
    ✓ Same-origin enforcement

Guardrails
  ✗ No video calling
  ✗ No mobile-specific layout
```

That's roughly 20–25 Features in 7–8 Groups. Looks structured. But the decomposition immediately exposes a load-bearing assumption that collapses at this scale.

### Where the model breaks down

**The ownership overlap problem is catastrophic, not incidental.** The research doc's rule for merging into one Group is file-ownership overlap. But in a browser, everything overlaps everything:

- The JS engine overlaps with the DOM API, which overlaps with the rendering pipeline, which overlaps with the networking stack (fetch, XHR), which overlaps with the security sandbox. These are not independent features with separate `owned_paths`. The design assumes you can partition a codebase into N non-overlapping file sets. A browser's entire architecture is a rebuttal to that assumption.

**The Groups are not feasible execution units.** A Group's dispatch plan is "one agent, one branch, one focused implementation." But "implement the HTML parser" requires 50,000+ lines of C/C++/Rust, decades of spec compliance work, and deep integration with the layout engine that is being built simultaneously in a different Group on a different branch. The agent cannot implement a real HTML parser in one agent loop. It will implement a toy stub. The same applies to every Group here.

**The file-overlap merge algorithm produces a single mega-Group.** Once the compile agent runs its ownership analysis, every Group will overlap with every other Group — the networking stack needs headers shared with the JS engine, the DOM binds to both the HTML parser and the layout engine, the security sandbox wraps all of them. The algorithm collapses everything into one Group, defeating the parallelism benefit entirely. The compile validator has no way to detect "this is a systems-programming product that cannot be decomposed at this granularity."

**The `project_kind` enum is blind here.** `{webapp, cli, library, api}` — a browser fits none of these cleanly. It's a platform. The kind field and all downstream prompt engineering assumes one of these four categories. The compile prompt almost certainly specializes for Flask/Node/Python-style webapps. It will generate a webapp-flavored plan ("routes, templates, models") for a product that has no such structure.

---

## 2. Audit test — can the audit stage verify a browser?

### Evidence kinds that could work

**RepoTestCheck** — if the build agent wrote any unit tests at all, these run. But they will test toy implementations: `assert parse_html("<b>bold</b>") == [...]`. They tell you the toy parser exists, not that it handles real-world HTML.

**ApiProbe** — there is no HTTP API to probe. A browser's "API" is browser-internal (IPC between renderer and browser process). No endpoint to curl. ApiProbe produces zero signal here.

**StateInvariant** — conceivably useful for "does the history database contain the right schema" or "does the cookie jar persist across restarts." But these are peripheral storage invariants, not correctness of the core engine.

**BrowserJourney** — the audit agent would open a headless browser (likely Playwright-controlled Chromium) to walk the product. But the product *is* a browser. This is recursive and undefined. The audit tooling is designed to test webapps by driving a browser. It cannot drive another browser to test it. The walkthrough mechanism breaks entirely.

### What cannot be verified, and whether the verdict is honest

The audit cannot verify:
- Rendering correctness (CSS box model, flexbox, float, z-index stacking)
- JavaScript spec compliance (ECMAScript conformance)
- Security boundary enforcement (same-origin, sandbox escapes)
- Multi-process isolation (crash isolation between renderer and browser process)
- Extension API compatibility
- Memory safety (the most critical property of a browser)
- Network protocol correctness (TLS, HTTP/2, WebSockets)

A browser that "passes audit" would be a browser that: (a) has some toy HTML renderer that renders a single static page, (b) can navigate somewhere in a chromeless UI, (c) has passing unit tests for toy stubs. The audit would call this `passed` on all Features because the walkthrough segments — screenshots of a Tkinter or Qt window showing an address bar — would technically show "navigation works." The verdict would be dishonest. Not because the auditor is lying, but because the auditor's evidence standard (screenshots + API probes + test pass/fail) is structurally inadequate for this product class.

This is the core failure: **the audit design assumes a web-hosted product with an HTTP API surface.** The BrowserJourney is a Playwright wrapper. The ApiProbe hits HTTP endpoints. Neither instrument applies to a systems-level desktop application.

---

## 3. Proof test — "JavaScript engine works"

### What the per-Feature proof packet would actually contain

```
Feature: JavaScript engine works
Verdict: passed (?)

Built in group: javascript
Files changed: js_engine.py, js_lexer.py, js_parser.py, js_interpreter.py

Walkthrough segment:
  [screenshot: address bar showing "data:text/html,<script>document.title='test'</script>"]
  [screenshot: title bar showing "test"]

Deterministic checks:
  ✓ RepoTestCheck  pytest tests/test_js_engine.py  12 passed
  ✓ BrowserJourney: navigate to data: URL with inline script, verify title change
  — ApiProbe: n/a (no HTTP API)
  — StateInvariant: n/a

Audit narrative:
  "I navigated to a data: URL containing a simple script. The document title
   changed as expected. The JavaScript engine appears functional."
```

### Is this useful?

No. It is almost certainly **false confidence**. The "JavaScript engine" in this proof is a toy Python interpreter that handles `document.title = 'test'` and nothing else. A real JS engine must handle:
- The full ECMAScript spec (1,000+ pages)
- JIT compilation or at least efficient tree-walking
- Garbage collection
- Asynchronous execution (Promises, async/await)
- Web APIs (setTimeout, fetch, addEventListener)
- DOM bindings at every level

The proof packet's 12 passing tests prove the toy stubs work. They do not prove anything about a JavaScript engine. A user reading this proof would be misled. The Proof is structurally incapable of surfacing "we built a stub, not a real implementation" because the audit walkthrough has no test oracle for "real" vs "toy."

This is not an edge case — it is what the proof packet will always produce for any sufficiently complex sub-system that cannot be fully exercised by a browser-click-and-screenshot loop.

---

## 4. UI/UX test — walkthrough as a browser-builder

### Spec review screen (Screen 4)

The spec review screen shows 7 Groups with ~25 Features in a scrollable checklist. This breaks in several ways:

**The Feature granularity is wrong for this product.** "HTML parser," "CSS layout engine," "JS engine" are not Features in the sense the design intends — a Feature is "a unit of value the user asked for or accepted." These are subsystems. A user who asked for "a web browser" did not ask for an HTML parser as a separable deliverable. The checklist would look like a spec for a browser's internal architecture, not a product the user asked for. There is no way to intelligently check/uncheck these items — they're all required and they all depend on each other.

**The file-path display in Form view is nonsense.** The Form view shows `files: routes/editor.py, templates/…` style paths for each Group. A browser codebase would have `src/render/html_parser.cc`, `src/net/url_loader.cc`, `src/v8/…`. The path display is decorative. Users cannot reason about "owned_paths" for a browser — it's 3+ million lines of C++.

**The "Suggestions from compile" carousel would dominate.** For a product this open-ended, the LLM would generate a long tail of features it considered: WebRTC, WebAssembly, DevTools, PDF viewer, password manager, sync, etc. The suggestions panel would contain 15–20 items that the user must individually accept or dismiss before building. This is not a minor UX nuisance — it's a decision-overload moment at the worst possible time.

**Guardrails become a responsibility transfer.** For a doc editor, "no video upload" is a reasonable guardrail. For a browser, the guardrails you'd need to write to make the scope honest are things like: "No real CSS layout engine — use a native webview." "No real JS engine — embed QuickJS." "No real networking — use libcurl via FFI." These are not guardrails; they are architecture decisions. The guardrail UI asks the user to enumerate scope limits on a product with unbounded scope. Nobody knows all the things a browser might try to implement until it's already building.

### Run drawer during build (Screen 5)

**Group status display becomes incomprehensible at this scale.** Screen 5 works for 4 Groups. For 7–8 Groups with deep dependencies, the status list would look like:

```
✓ networking      landed     2 features · 8:41
◌ html-parser     building…  3 features · running
⏸ css-layout      blocked    waiting on html-parser
⏸ js-engine       blocked    waiting on html-parser
⏸ dom-bindings    blocked    waiting on html-parser, css-layout, js-engine
⏸ browser-chrome  blocked    waiting on dom-bindings
⏸ security        blocked    waiting on all of the above
```

Seven Groups, six blocked, one building. The user is watching a wall of "blocked" status for the entire duration of the build (potentially hours). The live event feed linked at the bottom would be streaming hundreds of lines of agent output about partial HTML parser implementation. The drawer has no mechanism to convey "this is taking 3 hours because the dependency chain is linear."

**Cost display would trigger abort.** The design shows `Cost so far $1.40 (cap $5)` for a simple admin dashboard. Building 7 Groups of a browser at $0.50–$2.00 per Group would hit the default $5 cap before finishing the second Group. The user would either hit the cap wall (causing a partial, non-functional product) or have to pre-configure `--total-cost-usd 50` without knowing why. The cap mechanism is not surfaced during the spec review gate, only during the run.

---

## 5. Where the design holds, where it breaks

### Where it holds

The design is genuinely well-thought-out for its actual target: **greenfield webapps with 5–15 self-contained features** — a URL shortener, a Kanban board, a doc editor, a chat app. For these products:

- The Feature/Group decomposition is sensible and produces feasible execution units.
- The BrowserJourney audit mechanism works (there's an HTTP server to navigate).
- Per-Feature proof packets contain real signal (register flow screenshots, API probe results).
- The spec review screen is correctly sized — 5–10 Features fit in one screen without scroll-induced confusion.
- The run drawer for 3–5 Groups is readable and actionable.

The vocabulary is clean. The retry layer architecture (check loop vs audit loop) is correctly designed. The Proof granularity (per-Feature mini-packets with evidence refs) is the right shape. The spec markdown/JSON dual-artifact design is solid.

### Where it breaks — and the load-bearing assumptions

**Assumption 1: Files can be partitioned into N non-overlapping sets.** The entire Group dispatch model rests on `owned_paths` being disjoint. This assumption holds for webapps (each feature owns a route file + template + test). It fails for any product with a layered architecture (browsers, compilers, databases, operating systems).

**Assumption 2: The product has an HTTP API surface the audit can navigate.** BrowserJourney assumes a browser is driving a webapp. ApiProbe assumes there are HTTP endpoints to curl. StateInvariant assumes there's a readable persistence layer. All three are structured around the webapp model. This assumption fails for any product that is itself a platform, a library, an embedded system, or a desktop application with no HTTP layer.

**Assumption 3: A "Feature" is a user-visible product capability verifiable via browser interaction.** This is implicit throughout — the proof packet's primary evidence is a walkthrough segment (screenshots + DOM snapshots). For subsystem features (a memory allocator, a JIT compiler, a network stack), there is no browser interaction that proves correctness. The Feature/verdict model degrades to "did the unit tests pass?" with a verdant screenshot of something tangentially related.

**Assumption 4: Compile can produce a sensible Spec from free-text intent.** For a doc editor, the LLM can derive a reasonable Spec because the problem space is well-bounded and the LLM has seen hundreds of similar apps. For "build me a web browser," the LLM will either (a) generate an impossibly ambitious spec or (b) secretly scope it down to a toy webview wrapper without telling the user. There is no mechanism in the Compile stage to say "this intent is out of scope for the product type I can build." The spec review gate becomes a negotiation about scope that neither party is equipped to conduct.

**Assumption 5: Build time is bounded by the check loop cap.** The design caps check-loop retries at 3 per Group. For a complex subsystem, 3 attempts produce increasingly broken stubs. There's no concept of "this Group is architecturally intractable at this complexity level." The system will run 3 attempts, declare the Group blocked, and move on — leaving the user with a partial product where "JS engine" is a stub that handles two test cases.

---

## 6. Specific suggestions

### Suggestion 1: Add a compile-time complexity gate

Compile should estimate the complexity of the derived Spec and warn the user — or hard-block — when it detects a product that is likely out of scope. The gate criteria:

- Number of cross-cutting dependencies (if >N% of Groups depend on all others, the product is not decomposable).
- Estimated total LOC for implementation (if the LLM estimates >50k lines for a single Group, flag it).
- Absence of a webapp API surface (if `project_kind` cannot be determined or the product has no HTTP layer, warn that audit coverage will be degraded).

This gate does not need to be perfect. It needs to prevent the user from investing $40 in a run that will produce toy stubs and a misleading "partial" verdict.

### Suggestion 2: Introduce `project_kind: platform` (or just be explicit about unsupported kinds)

The four project kinds (`webapp, cli, library, api`) define what the audit toolchain supports. Add documentation — and a Compile-time message — that says: "Otto builds products where Claude Code can write the full implementation and a browser-based walkthrough can verify it. Systems-level products (browsers, compilers, databases, kernels) are out of scope." This is not admitting defeat. It is honest scoping that prevents wasted runs.

### Suggestion 3: Make audit coverage honest in the Proof

The verdict header says "6/6 features passed." For a browser, this would mean "6/6 toy stubs passed their toy tests." The design already has the concept of `partial` — but partial implies "we tried and got most of it." The Proof has no way to express "we implemented stubs, not the real thing." Consider adding a `coverage_confidence: low | medium | high` field to each Feature verdict, where low means "evidence came only from unit tests with no integration walkthrough." This makes the proof honest without changing the verdict model.

### Suggestion 4: Spec review screen needs a "complexity warning" rail

For a browser-scale spec, the spec review screen should show a visible rail warning: "This intent has N Groups with M total Features and estimated wall time >2 hours. Otto works best for products with 5–10 features. Consider narrowing scope." This warning belongs in the spec review gate, not after a 3-hour failed run.

### Suggestion 5: The plan should explicitly state scope constraints

The research.md document should include a section titled "What Otto is not designed for" with specific examples. This is not the same as a Guardrail (which is user-supplied). It is a design boundary that the plan authors have implicitly assumed but not stated. Stating it explicitly prevents future sessions from re-discovering these limits through failed runs, and gives the user the right context at the spec review gate.

### Suggestion 6: Cost cap should be surfaced at spec review, not mid-run

Currently, the cost cap is configurable via `--total-cost-usd` or the Advanced section of the New Run dialog. But the user cannot estimate cost before approving the spec. The spec review screen should show a rough estimated cost range based on Group count and estimated complexity. If that estimate exceeds the default cap, the spec review gate should say so: "This run is estimated to cost $12–$18. Your current cap is $5. Adjust the cap or reduce scope before building."

---

## Summary judgment

The design is fit-for-purpose for the product class it was implicitly designed for: **greenfield webapps of modest scope**. The vocabulary, retry architecture, proof granularity, and UI information hierarchy are all correct for that class.

The design is not fit-for-purpose for systems-level products, platforms, or any product where (a) owned_paths cannot be cleanly partitioned, (b) there is no HTTP API surface for the audit to traverse, or (c) a "passing" Feature proof would require more than a browser-click-and-screenshot walkthrough to be credible.

A browser is the adversarial maximum of this problem. The plan should say so, explicitly, and add the compile-time complexity gate and scope documentation to prevent users from running $40 of tokens to build a toy with a misleading verdict.
