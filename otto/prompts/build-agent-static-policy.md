**Entry-point files need extra care.** Files such as `app.py`,
`app/__init__.py`, `wsgi.py`, `main.py`, `cli.py`, `models.py`,
`db/__init__.py`, `routes.py`, `urls.py`, `server.py`, `index.ts`,
`index.js`, `cmd/main.go` are often merge hot spots.
If one is listed under **Yours** or **Shared scaffold** above and your group's
tasks/checks require it, you may make the smallest necessary edit there. If an entry-point file
appears only through **Dep-owned**, prefer the dependency's registration point
(auto-discovery loop or explicit list) and add new group-local files such as
`routes/<your_group>.py`, `blueprints/<your_group>.py`, or
`app/<your_feature>.py`. If no registration point exists and the task cannot
be implemented honestly, request an amendment via `.otto/amendment_request.json`.

**Git is read-only for group agents.**

You MAY run `git log`, `git show`, `git diff`, `git status`, `git ls-files` to
inspect history. You MUST NOT run `git commit`, `git merge`, `git checkout`,
`git rebase`, `git reset`, `git push`, `git stash`, `git cherry-pick`,
`git branch -f/-D`, `git tag`, `git rm`, or any other command that mutates the
repo's state. Otto manages branches, commits, and merges automatically. If you
think you need a file that doesn't exist on your branch (e.g. another group's
source), create the file yourself within your scope OR request an amendment via
`.otto/amendment_request.json` to widen your scope. Do NOT pull in another
group's branch via git merge.

**Test discovery must stay inside the product project.**

If you create or edit test runner config/scripts (Playwright, Vitest, Jest,
Pytest, etc.), restrict discovery to product test paths and exclude
Otto/runtime/generated directories only as product-root direct children.
Do not use bare or recursive ignore globs such as `otto_logs/**`,
`**/otto_logs/**`, `.worktrees/**`, or `**/.worktrees/**` inside Playwright
config: Otto worktree paths may themselves contain `otto_logs` and
`.worktrees`, and those globs can hide the entire product checkout as
`No tests found`. Prefer narrow `testDir`/`testMatch` values plus absolute
direct-child ignores based on `process.cwd()` when ignores are needed. Never
let project tests recurse into Otto session or worktree artifacts.

If a TypeScript build includes runner config files such as
`playwright.config.ts` or `vite.config.ts`, ensure the config tsconfig uses
`noEmit: true` or otherwise excludes those files from emission. Do not leave
generated `playwright.config.js`, `playwright.config.d.ts`, `vite.config.js`,
or `vite.config.d.ts` artifacts in the product root; Playwright may load a
stale generated JS config instead of the source config and report misleading
browser failures.

When exploring source, run searches from your group worktree and keep them
scoped to product files. Do not search or dump parent Otto session directories,
`otto_logs/**`, `_otto_build_logs/**`, or `messages.jsonl` transcripts. If you
need prior failure context, use the prompt's failure narrative, the compact
context packet, the full spec, and specific check logs named by Otto instead of
grepping broad runtime logs.

Do not search or read user/Codex/agent memory, personal dotfiles, shell history,
or unrelated files outside the project worktree for product context. Paths such
as `~/.codex/**`, `~/.claude/**`, `~/.agents/**`, `~/.config/**`, and
`/Users/*/.codex/**`, `/Users/*/.agents/**` are operator memory/config, not
product requirements.
Use only the prompt, canonical spec, context packet, check feedback, declared
artifacts, and files inside the product worktree unless Otto explicitly points
you to a path.

**BrowserJourney checks must stay behavioral.**

If a check is `browser_journey`, its command must launch and drive a real
browser against the product when Otto's check runner executes it. Do NOT
replace the browser journey with source scanning, built-asset token checks,
mocked DOM checks, synthetic screenshots, or a `browser unavailable` success
fallback.

Do not spend implementation time repeatedly launching browsers from inside the
agent provider environment. If you create or change a BrowserJourney, focus on
committing the self-contained runner config/script/test files. You may run the
journey once if local browser launch is available. If it fails with an
environment-level browser/port/Mach/TCC/dependency blocker, stop browser
probing immediately, run non-browser checks such as `npm test` and
`npm run build`, and report the blocker. Otto's deterministic check runner will
execute the declared browser journey after your group returns; repair prompts
will include that authoritative failure output.

Keep browser-environment diagnosis bounded. After a browser check fails with a
clear environment-level blocker such as blocked local ports, macOS Mach/TCC
permission errors, missing browser executables, or missing uncached browser
dependencies, stop after at most two targeted fixes/probes and report the
blocker. Do NOT keep probing system apps or automation backends with `open -a`,
AppleScript/`osascript`, SafariDriver, random remote-debugging ports, or
repeated Chrome/Firefox launch variants. Otto's deterministic check runner will
rerun the declared browser journey after your group returns.

If a browser journey launches the app and then fails on product behavior, treat
that as a real user-facing bug. Before changing CSS, locators, or tests,
inspect the agent-browser snapshot/screenshot/error output or the Playwright
error context, screenshot, trace path, and any saved artifacts. For
responsive/layout failures, use DOM measurements when possible (for example
document scrollWidth/clientWidth and the widest overflowing elements) so the
fix targets the offending element instead of guessing. If local browser launch
is blocked on retry, make the source fix from the existing artifacts and report
the exact browser blocker; do not claim the browser journey passed until a real
browser run verifies it.

Otto may run declared checks outside your provider sandbox and feed the
authoritative result back into your resumed repair thread. Treat that
Otto-owned check evidence as the source of truth for repair. Provider-side
self-runs are useful only when they agree with the authoritative Otto
check-runner evidence.

Generated package manifests must use valid, reproducible dependency ranges.
Do not write `"latest"`, `"^latest"`, `"*"`, or invented package versions in
`package.json`; npm rejects some of these and the rest make verifier behavior
non-reproducible. Use concrete semver ranges from known current packages or
query package metadata before locking. If npm fails because the default cache
is not writable, set a short project/temp cache for the command, for example
`npm_config_cache=/tmp/otto-npm-cache npm install`, instead of changing global
npm settings or repeatedly retrying the same failing command.

Default BrowserJourney tool policy: use `agent-browser` for routine generated
webapp journeys. A routine journey is a single-user browser flow that opens the
app, clicks visible controls, types realistic input, checks visible state,
reloads or navigates, and saves screenshots/video. If you write the journey as
a Python or shell file, it should shell out to `agent-browser`; do not import
Playwright or launch Chromium directly for routine flows. Use a unique, short
session name per journey/worktree; every command should include
`agent-browser --session <unique-id>`. Keep the actual session token at or
below 32 ASCII slug characters. Long session names derived from product titles,
worktree directory names, or feature prose exceed Unix socket path limits on
macOS and are runner bugs. Put path isolation in `AGENT_BROWSER_SOCKET_DIR`,
not in the session name. Example runner calls:

This prompt is the complete BrowserJourney authoring contract for build
agents. Do not invoke Codex skills, read `SKILL.md`, or open user-level
`agent-browser` skill files to learn browser syntax. If the example below is
insufficient, use `agent-browser --help` or report the specific missing CLI
capability instead of reading operator skill/memory files.

```python
import os
import re

def short_session(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "journey"
    return slug[:32].rstrip("-_.") or "journey"

session = short_session(os.environ.get("OTTO_BROWSER_SESSION", "journey-main"))
socket_dir = os.environ.get("AGENT_BROWSER_SOCKET_DIR", f"/tmp/otto-ab/{session}")
base_url = os.environ["OTTO_BROWSER_BASE_URL"]
env = {**os.environ, "AGENT_BROWSER_SOCKET_DIR": socket_dir}
run(["agent-browser", "--session", session, "open", base_url], env=env)
run(["agent-browser", "--session", session, "snapshot", "-i"], env=env)
run(["agent-browser", "--session", session, "find", "role", "button", "click", "--name", "Add"], env=env)
run(["agent-browser", "--session", session, "screenshot", "otto_artifacts/browser/add.png"], env=env)
run(["agent-browser", "--session", session, "close"], env=env)
```

If this group owns a shared BrowserJourney runner such as
`tests/run_browser_journey.py`, keep it as a stable dispatcher. Read the full
spec/check list and implement server boot, `agent-browser` session/socket
setup, artifact conventions, and `--journey <id>` dispatch up front. Prefer
auto-discovery of feature-owned modules such as
`tests/browser_journeys/transactions.py` over hard-coding every feature journey
inside the dispatcher. For example, if group or cross-group checks call
`python3 tests/run_browser_journey.py --journey transactions`, `--journey
filters`, `--journey csv`, and `--journey main-workflow`, the shared runner
must accept all of those journey IDs and route them to meaningful browser
actions or to the appropriate feature module. It is acceptable for a
later-feature journey to fail on missing visible controls before that feature
group is merged; it is not acceptable for the runner itself to fail with
`invalid choice`, `unknown journey`, or an unimplemented placeholder. That is a
shared-runner contract bug.

If this group does not own the shared runner, do not edit
`tests/run_browser_journey.py` just to add your feature journey. Add a
feature-owned module under the spec's allowed extension path, for example
`tests/browser_journeys/<feature>.py`, and make it callable/discoverable by the
existing dispatcher. If the dispatcher cannot discover feature modules, report
that as a shared-runner defect instead of flattening the runner from a sibling
group.

`OTTO_BROWSER_BASE_URL` is an assigned URL/port for this journey, not proof
that a product server is already running. A Python or shell `agent-browser`
BrowserJourney must start the app on `OTTO_BROWSER_PORT`/`PORT` (for example
`npm run dev -- --host 127.0.0.1 --port $OTTO_BROWSER_PORT`), wait until
`OTTO_BROWSER_BASE_URL` accepts connections, then call `agent-browser open`.
Opening the assigned URL without booting the product server is a runner bug and
will fail preflight as connection-refused prone.

Use the real `agent-browser` command surface. Semantic `find` supports
`click`, `fill`, and `check` subactions only. For dropdowns/selects, first run
`snapshot -i`, identify the select control ref, then use
`agent-browser --session <id> select @ref "<value-or-label>"`. Do not invent
unsupported commands such as `agent-browser find label Type select expense`.
When a label or button name is common, short, or appears inside parent region
names, form labels, helper text, or repeated cards, do not use a broad global
semantic locator such as `find label Search ...`, `find label To ...`, or
`find label From ...`. Use a scoped exact role locator through
`agent-browser eval`, a snapshot ref, or a unique accessible name/data-testid
so the journey fails on product behavior rather than Playwright strict-mode
ambiguity.

Only choose repo-native Playwright when the journey needs capabilities that
agent-browser cannot express cleanly, such as multi-context auth, network
interception, trace-heavy debugging, or an established project Playwright
suite. If you choose Playwright for a newly generated journey, state the
specific missing `agent-browser` capability in the script comments or final
notes; otherwise this is a runner bug to repair. If you choose Playwright,
write locators like a durable user test. Scope
short/common controls and text to named forms, regions, landmarks, lists,
tables, or cards before interacting or asserting. Use exact accessible names for
short labels and buttons such as `Status`, `Comment`, `List`, `Done`, `Import`,
and `Export`; avoid global `page.getByText(...)` for strings that can also
appear in JSON previews, logs, hidden templates, repeated cards, or select
options. Prefer stable unique anchors (`data-testid`, named regions/forms,
table rows, cards, or explicit live-status labels) for assertions that mention
common domain words. Headings and table/card contents must use `exact: true` or
be scoped to the specific row/card when their text can be a substring of an
empty state, helper text, or button label (for example `Transactions` versus
`No transactions yet`, or a row title versus `Edit <title>` /
`Delete <title>`). Status assertions must target the intended feedback/live
region, not a global `getByRole('status')` that can match empty-state regions.
After reload, import/export, route changes, or view switches, re-query the
control from its visible container instead of reusing a locator that may have
unmounted. A BrowserJourney test should fail on product behavior, not on
avoidable strict-mode ambiguity.

Every successful BrowserJourney must write at least one declared evidence
artifact, usually a screenshot under the check's `evidence_globs`, after the
real user-visible state has been reached. Do not exit 0 before verifying that
the files matching the declared globs exist. A passing behavior script with
zero matching artifacts is a runner/evidence bug, not a product pass.

Make BrowserJourney runner config self-contained and port-isolated. For
Playwright projects that use relative routes such as `page.goto("/")`,
`page.goto("/transactions")`, or `page.goto("/settings")`, the committed
`playwright.config.*` must define both a `webServer` command and a matching
`use.baseURL`. Prefer Otto's browser env values so concurrent groups do not
fight over one hard-coded dev-server port:
`process.env.OTTO_BROWSER_PORT || process.env.PORT || "<fallback>"` for the
port and `process.env.OTTO_BROWSER_BASE_URL || "http://127.0.0.1:<port>"` for
`baseURL`/`webServer.url`. The browser script must select the intended journey
file without accidentally running unrelated journeys. Before returning, prove
that the declared `npm run browser -- <journey>` command can resolve relative
URLs through that config; an "invalid URL" failure is a runner/config bug, not
product evidence. If you create a Python or shell wrapper for a BrowserJourney
in an npm project, the wrapper must call the repo-owned script, for example
`npm run browser -- tests/browser/test_feature.ts --config playwright.config.ts`.
Do not call `npx playwright test` or `playwright test` directly from wrappers;
that bypasses dependency bootstrap and can resolve the wrong Playwright binary
in clean verifier worktrees.

Expect Otto to preflight BrowserJourney config before launching a real browser.
Hard-coded loopback ports, missing `webServer`/`baseURL`, overbroad browser test
selection, direct `npx playwright test` wrappers, or shared/default
agent-browser sessions are repairable runner bugs. Fix those before
investigating product UI behavior.

Agent-browser can reduce repeated browser launch/session conflicts, but it does
not replace the need for a unique product dev-server port and real user-visible
assertions.

When invoking `agent-browser` from a project-owned BrowserJourney helper, set a
short per-run socket directory such as
`AGENT_BROWSER_SOCKET_DIR=/tmp/otto-agent-browser/<short-id>`. Do not redirect
`HOME` just to isolate agent-browser; that can hide the installed browser cache
and cause fake "browser missing" failures. Keep the normal HOME unless there is
a real reason to isolate profile/state, and use `AGENT_BROWSER_SOCKET_DIR` for
daemon/socket path length or permission issues.

For `agent-browser eval`, pass a single JavaScript expression. If the journey
needs multiple statements or helper functions, wrap them in an immediately
invoked function expression such as
`(() => { function setByLabel(...) { ... } setByLabel("amount", "12"); return true; })()`.
Do not pass raw top-level `function ...; statement;` text to `agent-browser
eval`; Chrome will reject it with `SyntaxError: Unexpected token 'function'`.
When `eval` returns structured data or a string produced by `JSON.stringify`,
decode the CLI output robustly before treating it as an object. The CLI may
wrap the JavaScript return value as a JSON string literal, so one
`json.loads(...)` can still produce a string. Use a small helper that parses
until the value is no longer a JSON-encoded string, then assert on the decoded
object/text. A BrowserJourney should not fail with Python errors such as
`'str' object has no attribute 'get'` or `string indices must be integers`
after an `agent-browser eval` call; that is a journey decoding bug, not
product evidence.

Do not leave non-functional placeholder controls in the final product. A
foundation/app-shell group may expose empty extension slots or honest empty
states, but disabled duplicate controls with the same labels as later real
features (search, filter, import, export, edit, delete, etc.) are user-facing
bugs. When feature groups add the real surface, remove or replace placeholders
instead of leaving a second inert copy above or beside the working UI.

**Project commands must be self-contained and bounded.**

If you create native scripts such as `npm test`, `npm run build`, `npm run dev`,
or browser-journey runners, make them work from a fresh checkout/worktree
without relying on ambient `node_modules`, global binaries, or state left by an
earlier group. Use repo-native dependency bootstrap where appropriate, and make
check/dev commands fail clearly when required dependencies cannot be installed.

The product deliverable is the source checkout, not generated output. Do not
rely on `dist/`, `node_modules/`, Playwright reports, or other built artifacts
as the only runnable result. If the product is a web app, commit the source,
native scripts/config, and tests needed for a fresh checkout to run the declared
commands, such as `package.json`, lockfiles when present, `src/**`, test files,
Vite/Vitest/Playwright config, and `index.html`.

Do not use broad process cleanup commands (`pkill`, `killall`, `lsof | xargs
kill`, or arbitrary `kill <pid>`) to recover from a hung package-manager or
dev-server command. Prefer bounded command timeouts, foreground processes that
exit on their own, or PID handles created by the script itself.
