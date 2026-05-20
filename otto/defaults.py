"""Single source of truth for constants Otto uses at runtime.

Every constant below is imported directly by the call-sites that need
it (e.g. ``from otto.defaults import DEFAULT_REPAIR_AGENT_WALL_CLOCK_S``).
Per-run overrides live in ``otto.config`` (``otto.yaml`` + CLI flags);
defaults here are the values used when nothing overrides them.

Subprocess/network timeouts (e.g. ``requests.get(timeout=5)``) are NOT
considered configurable budgets — they're transport-layer concerns.
The "no magic numbers" rule targets retry/budget/audit knobs that
appear in 2+ call-sites and would silently diverge on rename.
"""

from __future__ import annotations


# Brownfield-compile preamble (A6.1). Caps on what the Python helper
# bundles into the prompt before handing the agent its discovery tools
# (Read/Glob/Grep). Not user-tunable — these target prompt-budget
# concerns, not retry/audit knobs. Public so spec_compile.py and tests
# can import them directly.
BROWNFIELD_PREAMBLE_MAX_FILES = 200
BROWNFIELD_PREAMBLE_MAX_LINES_PER_FILE = 200

# Seed stage (A1.5-seed). Per-fixture wall-clock cap on the project-owned
# seed script (e.g. `scripts/otto/seed_user.py`). Transport-layer concern,
# not a retry/budget knob — a single fixture install should never exceed
# this; if it does, the project's seed script is broken.
SEED_PER_FIXTURE_TIMEOUT_S = 60

# Tree-level cost cap (USD) for a v5 run. Enforced in v5_runner._process_children
# as the "refuse new dispatches" gate. Exposed via `otto run --tree-budget-usd`
# at the CLI; this default lives here so the CLI and the runner agree without
# the constant being declared twice.
DEFAULT_TREE_BUDGET_USD = 25.0

# Port-cleanup subprocess timeout. Used for lsof/kill probes against ephemeral
# ports during preflight. 2s is long enough for a healthy host, short enough
# to fail fast on a stuck system.
PORT_CLEANUP_TIMEOUT_S = 2

# UI / browser polling heartbeat. Used by the Mission Control event watchers
# and the journey UI executor to throttle their event-source polling.
UI_POLL_INTERVAL_S = 0.05

# Repair-agent wall-clock ceiling. Each individual repair-agent invocation
# (foundation gate, integration smoke, child upward-merge-after-failure)
# gets at most this many seconds before the runner cancels it and records a
# timeout. Bumped from 1200 → 3600 (60 min) after the 2026-05-20 linkboard
# e2e showed two consecutive 1199s timeouts cutting off real fix work
# mid-flight ("at the 1199s wall, then discarded" comment in v5/repair.py).
# Set per-run via `repair_wall_clock_s` in otto.yaml or a stage-specific
# `<stage>_repair_wall_clock_s` (see v5/preflight_oracle._repair_budget_from_config).
DEFAULT_REPAIR_AGENT_WALL_CLOCK_S = 3600.0

# Whole-run wall-clock budget. Sets the `run_budget_seconds` ceiling that
# `otto run` enforces — when exceeded, the dispatch loop drains in-flight
# work and the verdict goes to `partial`. CLI `--budget` overrides; yaml
# `run_budget_seconds` overrides. The fallback used to be a 3600 literal
# duplicated across 6 sites (v5_runner / lead / v5_preflight_repair); now
# centralized here. CLI default tracked this fallback when the literal
# was 600 vs. the doc-stated 3600 — fixed by routing CLI through the
# same constant.
DEFAULT_RUN_BUDGET_S = 3600

# Repair-agent shape — number of agent turns per repair invocation and
# number of oracle re-runs per repair invocation. Both used as the
# `default_agent_turns` / `default_oracle_invocations` argument to
# v5/preflight_oracle._repair_budget_from_config across the 5 repair
# phases (foundation gate, integration smoke, child upward-merge,
# subtree propagation, integration repair). Plan-amendment repair is a
# documented outlier at oracle_invocations=1 because it has no oracle
# re-run loop — see comment at v5/repair.py:1327.
DEFAULT_REPAIR_AGENT_TURNS = 1
DEFAULT_REPAIR_ORACLE_INVOCATIONS = 3

# Oracle stage timeout — wall-clock cap (seconds) on a single oracle
# stage invocation (e.g. one clean-verify pass). Repair-loop retries
# get fresh stages, so this is per-stage, not per-repair.
DEFAULT_ORACLE_STAGE_TIMEOUT_S = 300

# Clean-verify deploy / build-step ceiling (seconds). One application
# install + dev-server-up cycle in the clean-verify oracle. Distinct
# from the longer repair-agent ceiling because clean-verify is just
# "boot it and hit `/`", not a fix loop.
DEFAULT_CLEAN_VERIFY_TIMEOUT_S = 120

# After clean-verify install, give the dev server this many additional
# seconds to bind its declared ports before the gate gives up.
DEFAULT_PORT_WAIT_S = 12

# Autopilot rate-limit window (seconds). Used by mission_control's
# autopilot to count actions/pilot-calls per rolling hour. If the
# window changes, both the `since` cutoff AND the `window_seconds`
# field on the budget-status payload must move together; this constant
# keeps them in sync.
AUTOPILOT_RATE_LIMIT_WINDOW_S = 3600
