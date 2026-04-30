# Codex Learnings

Purpose: persistent, project-specific memory for future Codex sessions working
on Otto. Read this before touching Mission Control, web servers, certifier
evidence, or token accounting.

## Repeated Mistakes To Avoid

- Stay in the user-requested worktree. For the current I2P line, use
  `/Users/yuxuan/work/cc-autonomous/.worktrees/codex-provider-i2p` unless the
  user explicitly asks to inspect or merge another tree.
- Do not assume the browser is running the latest code. After web client or
  backend changes, rebuild, restart the server, and verify the served bundle or
  API reflects the new commit. A stale server caused multiple false "fixed"
  claims.
- Do not run web backend tests at the same time as `npm run web:build` when
  bundle freshness is enforced. The build stamp can race the tests and produce
  a stale-bundle failure. Build first, then run tests.
- Use the repo environment for Python tests, usually `.venv/bin/pytest` or the
  repo's `uv` workflow. System `python3 -m pytest` may not have pytest.
- When a user reports a UI bug from another device, verify host binding and the
  exact served port. For MacBook/iPhone testing over Tailscale, the server must
  listen on `0.0.0.0`, not only `127.0.0.1`.
- Do not rely on screenshots or happy-path API checks as "E2E tested." For
  Mission Control UI, exercise the actual user flow in the live browser,
  especially modal submission, queue start/stop, run detail, landing, logs, and
  proof/evidence views.
- If a simple UI change takes too long, look for duplicated rendering paths or
  stale bundle/server issues before changing the same label/style repeatedly.
- Avoid opening new long-lived exec sessions casually. Poll existing sessions
  and let build/test commands exit; the workspace has repeatedly hit process
  limits.
- For background servers started by agents/certifiers, ensure they are killed
  or started with bounded output. A leaked Flask server previously wrote huge
  Claude SDK background output files under `/private/tmp`.
- When testing video proof, inspect the produced report like a user: does the
  video actually demonstrate the build intent, including non-happy paths or
  downloaded/exported artifacts where relevant, instead of a generic
  walkthrough?

## Web Build And Test Order

Recommended order for Mission Control web changes:

1. Make scoped source changes.
2. Run `npm run web:typecheck`.
3. Run `npm run web:build` so `otto/web/static/` and `build-stamp.json` match
   source.
4. Run focused Python/web tests with `.venv/bin/pytest ...`.
5. Restart the running `otto web` server if user-facing verification matters.
6. Verify from the actual client/device/URL the user is using when remote
   browser behavior is in question.

## Token Accounting Memory

Provider token fields are not interchangeable:

- Claude/Anthropic reports `cache_creation_input_tokens` and
  `cache_read_input_tokens` as additive token classes. Total token traffic
  includes normal input, cache creation, cache read, output, and reasoning.
- Codex/OpenAI-style `cached_input_tokens` is a subset of input tokens, not an
  extra bucket to add on top of input.
- In mixed aggregates, treat cache-read tokens plus provider-reported cached
  input subsets as `cached`; treat uncached input, cache creation, output, and
  reasoning as `fresh`.
- User-facing spend should prefer tokens over money and should be compact:
  `fresh + cached · hit%`.
- Example from a real standard-cert run:
  `76K fresh + 1.8M cached · 96% hit`.
- Cache hit rate should be computed from cache-eligible input traffic, not from
  output or reasoning tokens.

## Communication Standard

- If verification was partial, say exactly what was and was not exercised.
- If the server was not restarted, say so before telling the user to test.
- If a result depends on generated static assets, mention whether the bundle was
  rebuilt.
- Prefer one clear next action over a long list when the user is debugging live.
