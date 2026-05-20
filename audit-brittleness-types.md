# Otto Untyped-Dict Brittleness Audit

## Executive Summary

Otto's codebase contains **708 `isinstance(x, dict)` checks** against only **97 TypedDict/dataclass declarations**, creating a major brittleness vector. When AI editors work with untyped `dict[str, Any]` across module boundaries, they have no way to discover:

- What keys are valid
- What types those keys hold
- What shape downstream consumers expect
- Which keys are optional vs. required

This audit identifies the **5 highest-leverage typed boundaries** that would meaningfully reduce AI-induced bugs.

---

## Top 5 High-Leverage Boundaries

### 1. Task Graph Entry (CRITICAL IMPACT)

**Semantic:** A task entry in `task_graph.json` represents a hierarchical decomposition node with metadata, verdicts, costs, and retry state.

**Current Shape:**
```python
# otto/queue/task_graph.py:185
def get_task(project_dir: Path, task_id: str) -> dict[str, Any] | None:
    graph = read_graph(project_dir)
    return graph["tasks"].get(task_id)
```

**Read Sites:** 20+ distinct locations:
- `v5_runner.py:796` (`_task_owned_paths` reads `"owned_paths"`)
- `v5_runner.py:804` (`_is_foundation_task` reads `"task_role"`, `"intent"`)
- `v5_runner.py:881` (`_foundation_contract_findings` iterates contracts in task)
- `task_graph.py:185-294` (multiple setters/getters reading all keys)

**Proposed TypedDict:**
```python
@dataclass
class TaskEntry:
    task_id: str
    parent_task_id: str | None = None
    intent: str = ""
    verdict: Literal["pass", "partial", "unverified", "merge_blocked", 
                     "pending_children", "catastrophic"] | None = None
    task_role: Literal["foundation", "feature", "contract_amendment", "integration"] = "feature"
    owned_paths: list[str] = field(default_factory=list)
    child_task_ids: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    integration_branch: str | None = None
    foundation_contracts: list[dict[str, Any]] = field(default_factory=list)
    contract_amendment_retry_in_progress: bool = False
```

**Bug Prevention:** AI editors adding a new retry-tracking field won't accidentally shadow `contract_amendment_retry_*` prefix keys scattered elsewhere (currently a risk in `task_graph.py:112-128`).

**Migration Cost:** ~15 call sites; mostly straightforward (readers become typed; graph mutations remain dict construction).

---

### 2. Pipeline Event Payload (HIGH IMPACT)

**Semantic:** The `payload` dict passed to `on_event()` callbacks carries execution milestones (compile_start, lead_start, build_progress, etc.). UI consumers and loggers depend on this shape implicitly.

**Current Shape:**
```python
# v5_runner.py:2178
def _emit(on_event: Any, payload: dict[str, Any]) -> None:
    if on_event is None:
        return
    try:
        on_event(payload)
```

**Call Sites:** 40+ distinct _emit() calls with these payload structures:
- `{event: "project_branch_checked_out", context, from, to}` (v5_runner.py:365)
- `{event: "session_open", session_id}` (v5_runner.py:1317)
- `{event: "compile_start"}` (v5_runner.py:1399)
- `{event: "lead_start", task_id, ...}` (v5_runner.py:1455)
- `{event: "lead_result", task_id, verdict, cost_usd, ...}` (v5_runner.py:1479-1502)

**Proposed Union TypedDict:**
```python
EventPayload = Union[
    TypedDict("SessionOpen", {"event": Literal["session_open"], "session_id": str}),
    TypedDict("CompileStart", {"event": Literal["compile_start"]}),
    TypedDict("LeadStart", {"event": Literal["lead_start"], "task_id": str}),
    TypedDict("LeadResult", {"event": Literal["lead_result"], "task_id": str, 
                             "verdict": str, "cost_usd": float}),
    # ... other variants
]
```

**Bug Prevention:** Event consumers (loggers, UI formatters) receive typed access to expected fields. AI changing event shape doesn't silently break downstreams.

**Migration Cost:** ~40 call sites but highly localized in `v5_runner.py`.

---

### 3. Repair Packet (HIGH IMPACT)

**Semantic:** A repair packet bundles diagnostic context, attempt history, and oracle results for the preflight-repair loop. Already has a *partial* dataclass definition (`RepairPacket` in `v5_preflight_repair.py:86`), but the nested dicts (`repair_unit`, `acceptance_oracle`, etc.) remain untyped.

**Current Shape:**
```python
# v5_preflight_repair.py:86-98
@dataclass
class RepairPacket:
    repair_unit: dict[str, Any]  # ← UNTYPED; contains {id, category, reason, ...}
    acceptance_oracle: dict[str, Any]  # ← UNTYPED; oracle config
    latest_oracle_result: dict[str, Any]  # ← UNTYPED; oracle execution result
    product_contract: dict[str, Any]
    integration_context: dict[str, Any]
    attempt_history: list[dict[str, Any]]  # ← UNTYPED; [{ agent_action, result, cost, ...}]
    current_state: dict[str, Any]
```

**Read Sites:** 15+:
- `v5_preflight_repair.py:204` (loads from JSON, passes to agent)
- `v5_runner.py:_run_child_verify_repair_packet` (consumes for verification)
- `v5_runner.py:_run_plan_amendment_repair_packet` (feeds to plan agent)

**Proposed TypedDicts:**
```python
@dataclass
class RepairUnit:
    id: str
    category: str  # "preflight_failure" | "contract_delta" | ...
    reason: str
    discovered_at: str  # ISO timestamp

@dataclass
class AttemptEntry:
    agent_action: str
    result: str  # "fix_applied" | "inconclusive" | "timeout"
    cost_usd: float
    timestamp: str

@dataclass
class RepairPacket:  # Update existing
    repair_unit: RepairUnit  # Was dict
    acceptance_oracle: dict[str, Any]  # TODO: next audit
    latest_oracle_result: dict[str, Any]
    attempt_history: list[AttemptEntry]  # Was list[dict]
    current_state: dict[str, Any]
```

**Bug Prevention:** The agent that reads repair packets won't mutate unintended nested keys; type hints flag when a repair-unit field is accessed that doesn't exist.

**Migration Cost:** ~8 call sites; mostly `from_jsonable()` / `to_jsonable()` transformations already in place.

---

### 4. Journey Verdict (MEDIUM-HIGH IMPACT)

**Semantic:** A journey verdict dict represents the pass/fail outcome of a behavioral test, with metadata for feature coverage, proof usability, and source tracking. Multiple modules build, read, and aggregate these.

**Current Shape (implicit):**
```python
# journey_verdict_sink.py:103-110, 115-122
{
    "id": str,
    "passed": bool,
    "status": Literal["pass", "fail", "skip", "defer", "unverified"],
    "detail": str,
    "source": str,  # "journey_verdict_sink" | "executor" | ...
    "proof": bool,
    "feature_id": str | None,  # Optional, varies per journey
    "covers_primary_actions": bool | None,
    "verification_level": str,  # "ui" | "api"
}
```

**Read Sites:** 25+:
- `journey_verdict_sink.py:13` (emitter: builds verdicts)
- `render.py` (proof packet renderer reads feature_id, status, passed)
- `cli_improve.py` (aggregator: counts pass/fail)
- `lead.py` (logging: prints status)

**Proposed TypedDict:**
```python
@dataclass
class JourneyVerdict:
    id: str
    passed: bool
    status: Literal["pass", "fail", "skip", "defer", "unverified"]
    detail: str
    source: str
    proof: bool
    feature_id: str | None = None
    covers_primary_actions: bool | None = None
    group_id: str | None = None
    verification_level: str | None = None
```

**Bug Prevention:** The audit in `archive/audits/audit-journey.md` found that metadata keys (`feature_id`, `group_id`) were dropped mid-pipeline. With types, downstream code can't accidentally omit required keys.

**Migration Cost:** ~15 call sites; most are simple list comprehensions that already iterate over verdicts.

---

### 5. Checkpoint Resume State (MEDIUM IMPACT)

**Semantic:** The checkpoint dict persisted to `checkpoint.json` carries session state for resume: costs, agent session IDs, round history, spec phase. Already partially typed as `ResumeState` dataclass, but the `rounds: list[dict[str, Any]]` field remains untyped.

**Current Shape:**
```python
# checkpoint.py:64, 742
rounds: list[dict[str, Any]] = field(default_factory=list)
# Each round dict has: {round_num, cost, agent_response, diagnosis, ...}
```

**Read Sites:** 10+:
- `checkpoint.py:742` (writes checkpoint)
- `cli_improve.py` (reads for round reporting)
- `v5_runner.py` (checks resume state)

**Proposed TypedDict:**
```python
@dataclass
class CheckpointRound:
    round_num: int
    cost_usd: float
    agent_response: str | None = None
    diagnosis: str | None = None
    timestamp: str = ""
    tool_name: str | None = None
    failure_reason: str | None = None
```

**Bug Prevention:** Prevents AI editors from misnaming round fields or accidentally overwriting cost tracking in resume loops.

**Migration Cost:** ~8 call sites; mostly in checkpoint serialization/deserialization already structured.

---

## Cross-Module Impact Analysis

| Boundary | Modules Involved | Estimated Bug Severity | Migration Complexity |
|----------|------------------|------------------------|----------------------|
| Task Graph Entry | v5_runner, task_graph, queue | **HIGH** (contract across 5+ async tasks) | Medium (~15 sites) |
| Event Payload | v5_runner, UI/logging consumers | **HIGH** (contract with external code) | Medium (~40 sites) |
| Repair Packet | preflight_repair, v5_runner | **HIGH** (AI agent input/output) | Low (~8 sites) |
| Journey Verdict | journey_verdict_sink, render, cli, lead | **MEDIUM-HIGH** (multi-module aggregation) | Low (~15 sites) |
| Checkpoint Round | checkpoint, cli_improve, v5_runner | **MEDIUM** (resume critical path) | Low (~8 sites) |

---

## Methodology & Findings

**Search Strategy:**
- Identified 708 `isinstance(x, dict)` checks (grep across otto/)
- Located 97 existing TypedDict/dataclass definitions
- Tracked call patterns: which functions return `dict[str, Any]` and are called from 3+ modules
- Analyzed JSON persistence patterns (task_graph.json, repair-packet.json, checkpoint.json)
- Reviewed multi-module consumers (proof rendering, journey aggregation, event emission)

**Signals Used:**
1. Cross-module dict passing (functions returning dicts used by >3 callers)
2. Implicit schemas (JSON files with consistent key structure)
3. Repetitive key-access patterns (same `get()` calls scattered across files)
4. AI edit risk (dicts modified in one place, consumed elsewhere without type safety)

**Not Prioritized (lower leverage):**
- Individual utility dicts (local scope, single-module use)
- Template dicts that are rarely mutated
- Dicts that are already partially validated at boundaries (e.g., JSON schema validation on input)

---

## Recommendation

**Start with Task Graph Entry.** It's the backbone of hierarchical decomposition; typing it forces clarity on the contract between planner, executor, and verifier. The other four are complementary and should follow in order of cross-module impact.

Each migration should include:
1. Dataclass/TypedDict definition in the source module
2. Conversion layer in constructors/loaders (from_jsonable pattern already in use)
3. Type annotation update on all consumers
4. Unit test confirming type safety (e.g., missing required field fails early)

This would eliminate the majority of silent brittleness in the AI-editing path.
