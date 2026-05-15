"""Deterministic repair loop for v5/v6 integration preflight failures."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from otto.safe_slug import short_hash
from otto.setup_gitignore import is_common_build_artifact_path, is_otto_owned_path
from otto.v5_clean_verify import (
    CleanOracleIssue,
    CleanOracleResult,
    CleanOracleStepResult,
    verify_from_clean_oracle,
)


@dataclass(frozen=True)
class RepairBudget:
    wall_clock_s: float = 1800.0
    cost_usd: float | None = None
    agent_turns: int = 1
    oracle_invocations: int = 4
    idle_s: float | None = None
    diff_churn: int | None = None
    closeout_agent_turns: int = 0
    provider_max_turns: int | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "wall_clock_s": self.wall_clock_s,
            "cost_usd": self.cost_usd,
            "agent_turns": self.agent_turns,
            "oracle_invocations": self.oracle_invocations,
            "idle_s": self.idle_s,
            "diff_churn": self.diff_churn,
            "closeout_agent_turns": self.closeout_agent_turns,
            "provider_max_turns": self.provider_max_turns,
        }

    @classmethod
    def from_jsonable(cls, raw: dict[str, Any] | None) -> "RepairBudget":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            wall_clock_s=float(raw.get("wall_clock_s") or 1800.0),
            cost_usd=(
                float(raw["cost_usd"])
                if isinstance(raw.get("cost_usd"), (int, float))
                else None
            ),
            agent_turns=max(0, int(raw.get("agent_turns") or 0)),
            oracle_invocations=max(0, int(raw.get("oracle_invocations") or 0)),
            idle_s=(
                float(raw["idle_s"])
                if isinstance(raw.get("idle_s"), (int, float))
                else None
            ),
            diff_churn=(
                int(raw["diff_churn"])
                if isinstance(raw.get("diff_churn"), int)
                else None
            ),
            closeout_agent_turns=max(0, int(raw.get("closeout_agent_turns") or 0)),
            provider_max_turns=(
                int(raw["provider_max_turns"])
                if isinstance(raw.get("provider_max_turns"), int)
                else None
            ),
        )


@dataclass
class RepairPacket:
    repair_unit: dict[str, Any]
    acceptance_oracle: dict[str, Any]
    latest_oracle_result: dict[str, Any]
    product_contract: dict[str, Any]
    integration_context: dict[str, Any]
    attempt_history: list[dict[str, Any]]
    current_state: dict[str, Any]
    budget: RepairBudget
    packet_dir: Path
    agent_session_id: str = ""

    @property
    def packet_path(self) -> Path:
        return self.packet_dir / "repair_packet.json"

    @property
    def events_path(self) -> Path:
        return self.packet_dir / "repair_packet.events.jsonl"

    @property
    def repair_unit_id(self) -> str:
        return str(self.repair_unit.get("id") or self.packet_dir.name)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "repair_unit": self.repair_unit,
            "acceptance_oracle": self.acceptance_oracle,
            "latest_oracle_result": self.latest_oracle_result,
            "product_contract": self.product_contract,
            "integration_context": self.integration_context,
            "attempt_history": self.attempt_history,
            "current_state": self.current_state,
            "budget": self.budget.to_jsonable(),
            "packet_dir": str(self.packet_dir),
            "agent_session_id": self.agent_session_id,
            "_written_at": _iso_now(),
        }

    @classmethod
    def from_jsonable(cls, payload: dict[str, Any], *, packet_path: Path) -> "RepairPacket":
        packet_dir = Path(str(payload.get("packet_dir") or packet_path.parent))
        return cls(
            repair_unit=dict(payload.get("repair_unit") or {}),
            acceptance_oracle=dict(payload.get("acceptance_oracle") or {}),
            latest_oracle_result=dict(payload.get("latest_oracle_result") or {}),
            product_contract=dict(payload.get("product_contract") or {}),
            integration_context=dict(payload.get("integration_context") or {}),
            attempt_history=list(payload.get("attempt_history") or []),
            current_state=dict(payload.get("current_state") or {}),
            budget=RepairBudget.from_jsonable(payload.get("budget")),
            packet_dir=packet_dir,
            agent_session_id=str(payload.get("agent_session_id") or ""),
        )

    @classmethod
    def load(cls, packet_path: Path) -> "RepairPacket":
        payload = json.loads(Path(packet_path).read_text(encoding="utf-8"))
        return cls.from_jsonable(payload, packet_path=Path(packet_path))

    def persist(self) -> None:
        self.packet_dir.mkdir(parents=True, exist_ok=True)
        with _repair_unit_lock(self.packet_dir, self.repair_unit_id):
            tmp_path = self.packet_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(self.to_jsonable(), indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(self.packet_path)

    def append_event(
        self,
        event_type: str,
        *,
        digest: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event_payload = dict(payload or {})
        event_payload.setdefault("type", event_type)
        event_payload.setdefault("repair_unit_id", self.repair_unit_id)
        _append_repair_event(
            packet_path=self.packet_path,
            packet_dir=self.packet_dir,
            repair_unit_id=self.repair_unit_id,
            digest=digest,
            event=event_payload,
        )

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows

    def capture_scope_baseline(self) -> None:
        worktree = Path(str(self.repair_unit.get("worktree") or "."))
        self.current_state["scope_baseline"] = _scope_baseline(worktree)
        self.persist()


@dataclass(frozen=True)
class OracleRepairResult:
    verdict: Literal["pass", "merge_blocked"]
    summary: str
    agent_session_id: str = ""
    cost_usd: float = 0.0
    agent_turns_used: int = 0
    oracle_invocations: int = 0
    packet_path: str = ""
    composite_gate: dict[str, Any] | None = None
    escalation: dict[str, Any] | None = None


OracleRunner = Callable[[RepairPacket], CleanOracleResult | Awaitable[CleanOracleResult]]
AgentRunner = Callable[..., Awaitable[tuple[str, float, str, dict[str, Any]]]]
CommitHook = Callable[[RepairPacket, CleanOracleResult], tuple[bool, str] | Awaitable[tuple[bool, str]]]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def _repair_unit_lock(packet_dir: Path, repair_unit_id: str) -> Any:
    import fcntl

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", repair_unit_id).strip("-") or "unit"
    lock_dir = packet_dir.parent / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"repair_unit_id={repair_unit_id}\n_written_at={_iso_now()}\n")
        lock_file.flush()
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _append_repair_event(
    *,
    packet_path: Path,
    packet_dir: Path,
    repair_unit_id: str,
    digest: str,
    event: dict[str, Any],
) -> None:
    packet_dir.mkdir(parents=True, exist_ok=True)
    events_path = packet_dir / "repair_packet.events.jsonl"
    with _repair_unit_lock(packet_dir, repair_unit_id):
        seq = 1
        if events_path.exists():
            with events_path.open("r", encoding="utf-8") as fh:
                seq += sum(1 for line in fh if line.strip())
        row = {
            "seq": seq,
            "ts": _iso_now(),
            "digest": digest,
            "event": event,
        }
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def append_repair_packet_oracle_event(
    packet_path: Path,
    result: CleanOracleResult,
    *,
    source: str,
) -> None:
    """Append an oracle_run event for CLI or agent-side oracle invocations."""
    packet_path = Path(packet_path)
    packet_dir = packet_path.parent
    repair_unit_id = packet_dir.name
    if packet_path.exists():
        try:
            packet = RepairPacket.load(packet_path)
            repair_unit_id = packet.repair_unit_id
            packet.latest_oracle_result = result.to_jsonable()
            packet.persist()
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    _append_repair_event(
        packet_path=packet_path,
        packet_dir=packet_dir,
        repair_unit_id=repair_unit_id,
        digest=result.digest,
        event={
            "type": "oracle_run",
            "repair_unit_id": repair_unit_id,
            "source": source,
            "passed": result.passed,
        },
    )


def _git_status_porcelain(worktree: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _porcelain_path(line: str) -> str:
    raw = line[3:] if len(line) > 3 else ""
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[1]
    return raw.strip().strip('"')


def _is_generated_path(path: str) -> bool:
    return is_otto_owned_path(path) or is_common_build_artifact_path(path)


def _path_signature(path: Path) -> tuple[bool, str]:
    if not path.exists() or not path.is_file():
        return (False, "")
    try:
        return (True, short_hash(path.read_bytes().hex(), length=32))
    except OSError:
        return (True, "")


def _scope_baseline(worktree: Path) -> dict[str, tuple[bool, str]]:
    baseline: dict[str, tuple[bool, str]] = {}
    for line in _git_status_porcelain(worktree).splitlines():
        rel = _porcelain_path(line)
        if not rel or _is_generated_path(rel):
            continue
        baseline[rel] = _path_signature(worktree / rel)
    return baseline


def _modified_paths_since_baseline(
    worktree: Path,
    baseline: dict[str, Any] | None,
) -> list[str]:
    paths: list[str] = []
    for line in _git_status_porcelain(worktree).splitlines():
        rel = _porcelain_path(line)
        if not rel or _is_generated_path(rel):
            continue
        if baseline is None:
            paths.append(rel)
            continue
        signature = _path_signature(worktree / rel)
        stored = baseline.get(rel)
        if isinstance(stored, list):
            stored = tuple(stored)
        if stored != signature:
            paths.append(rel)
    return sorted(dict.fromkeys(paths))


def _path_allowed(path: str, allowed_paths: list[str]) -> bool:
    if not allowed_paths:
        return True
    normalized = path.strip("/")
    for allowed in allowed_paths:
        prefix = str(allowed).strip("/")
        if normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _has_conflict_markers(worktree: Path, paths: list[str]) -> bool:
    for rel in paths:
        path = worktree / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "<<<<<<< " in text and "=======" in text and ">>>>>>> " in text:
            return True
    return False


def _has_unmerged_paths(worktree: Path) -> bool:
    for line in _git_status_porcelain(worktree).splitlines():
        status = line[:2]
        if "U" in status or status in {"AA", "DD"}:
            return True
    return False


def _evaluate_composite_gate(packet: RepairPacket, oracle_result: CleanOracleResult) -> dict[str, Any]:
    worktree = Path(str(packet.repair_unit.get("worktree") or "."))
    baseline_raw = packet.current_state.get("scope_baseline")
    baseline = baseline_raw if isinstance(baseline_raw, dict) else None
    changed_since_baseline = _modified_paths_since_baseline(worktree, baseline)
    allowed_paths = [str(path) for path in (packet.repair_unit.get("allowed_paths") or [])]
    scope_policy = str(packet.repair_unit.get("scope_policy") or "unrestricted")
    scope_violations = (
        [
            path for path in changed_since_baseline
            if not _path_allowed(path, allowed_paths)
        ]
        if scope_policy == "allowed_paths"
        else []
    )
    dirty_paths = _modified_paths_since_baseline(worktree, None)
    conflict_markers = _has_conflict_markers(worktree, dirty_paths or changed_since_baseline)
    unmerged = _has_unmerged_paths(worktree)
    gate = {
        "oracle_passed": oracle_result.passed,
        "clean_worktree": not dirty_paths,
        "dirty_paths": dirty_paths,
        "conflict_markers": not conflict_markers,
        "unmerged_paths": not unmerged,
        "scope_ok": not scope_violations,
        "scope_violations": scope_violations,
        "verdict_consistency": True,
        "graph_invariants": True,
    }
    gate["passed"] = all(
        bool(gate[key])
        for key in (
            "oracle_passed",
            "clean_worktree",
            "conflict_markers",
            "unmerged_paths",
            "scope_ok",
            "verdict_consistency",
            "graph_invariants",
        )
    )
    return gate


def _issue_fingerprint_set(result: CleanOracleResult) -> set[str]:
    return {
        json.dumps(
            {
                "kind": issue.kind,
                "step_id": issue.step_id,
                "ports": sorted(issue.ports),
                "paths": sorted(issue.paths),
            },
            sort_keys=True,
        )
        for issue in result.issues
    }


def oracle_progress_reproducible(
    *,
    previous: CleanOracleResult,
    improved: CleanOracleResult,
    reproduced: CleanOracleResult,
    has_owned_path_diff: bool,
) -> bool:
    if not has_owned_path_diff:
        return False
    if improved.digest != reproduced.digest:
        return False
    if improved.passed and not previous.passed:
        return True
    return len(_issue_fingerprint_set(improved)) < len(_issue_fingerprint_set(previous))


def _oracle_result_from_json(payload: dict[str, Any]) -> CleanOracleResult:
    def _severity(raw: Any) -> Literal["warn", "error", "block"]:
        value = str(raw or "block")
        if value in {"warn", "error", "block"}:
            return cast(Literal["warn", "error", "block"], value)
        return "block"

    def _scope(raw: Any) -> Literal["scaffold", "subtree", "full"]:
        value = str(raw or "subtree")
        if value in {"scaffold", "subtree", "full"}:
            return cast(Literal["scaffold", "subtree", "full"], value)
        return "subtree"

    issues = [
        CleanOracleIssue(
            kind=str(issue.get("kind") or "unknown"),
            severity=_severity(issue.get("severity")),
            message=str(issue.get("message") or ""),
            step_id=str(issue.get("step_id") or ""),
            paths=[str(path) for path in (issue.get("paths") or [])],
            ports=[int(port) for port in (issue.get("ports") or [])],
            command_identity=str(issue.get("command_identity") or ""),
            return_code=(
                int(issue["return_code"])
                if isinstance(issue.get("return_code"), int)
                else None
            ),
        )
        for issue in (payload.get("issues") or [])
        if isinstance(issue, dict)
    ]
    steps = [
        CleanOracleStepResult(
            id=str(step.get("id") or ""),
            status=str(step.get("status") or ""),
            return_code=(
                int(step["return_code"])
                if isinstance(step.get("return_code"), int)
                else None
            ),
            command_identity=str(step.get("command_identity") or ""),
            command=[str(part) for part in (step.get("command") or [])],
            cwd=str(step.get("cwd") or ""),
            env={str(k): str(v) for k, v in (step.get("env") or {}).items()},
            started_at=str(step.get("started_at") or ""),
            duration_s=float(step.get("duration_s") or 0.0),
            artifact_paths=[str(path) for path in (step.get("artifact_paths") or [])],
            stdout_tail=str(step.get("stdout_tail") or ""),
            stderr_tail=str(step.get("stderr_tail") or ""),
            reason=str(step.get("reason") or ""),
        )
        for step in (payload.get("steps") or [])
        if isinstance(step, dict)
    ]
    result = CleanOracleResult(
        passed=bool(payload.get("passed")),
        scope=_scope(payload.get("scope")),
        issues=issues,
        steps=steps,
        artifact_path_refs=[str(path) for path in (payload.get("artifact_path_refs") or [])],
        command=[str(part) for part in (payload.get("command") or [])],
        env={str(k): str(v) for k, v in (payload.get("env") or {}).items()},
        digest=str(payload.get("digest") or ""),
        _written_at=str(payload.get("_written_at") or ""),
    )
    return result


def _infra_oracle_result(packet: RepairPacket, message: str) -> CleanOracleResult:
    step = CleanOracleStepResult(
        id="oracle_infra",
        status="failed",
        return_code=1,
        command_identity="clean-verify",
        command=[str(part) for part in (packet.acceptance_oracle.get("command") or [])],
        cwd=str(packet.repair_unit.get("worktree") or "."),
        env={str(k): str(v) for k, v in (packet.acceptance_oracle.get("env") or {}).items()},
        reason=message,
    )
    issue = CleanOracleIssue(
        kind="oracle_infra_error",
        severity="block",
        message=message,
        step_id=step.id,
        command_identity=step.command_identity,
        return_code=step.return_code,
    )
    return CleanOracleResult.from_parts(
        passed=False,
        scope=cast(
            Literal["scaffold", "subtree", "full"],
            str(packet.acceptance_oracle.get("verify_scope") or "subtree"),
        ),
        issues=[issue],
        steps=[step],
        artifact_path_refs=[],
        command=step.command,
        env=step.env,
        project_dir=Path(str(packet.repair_unit.get("worktree") or ".")),
        temp_dir=None,
    )


async def _default_oracle_runner(packet: RepairPacket) -> CleanOracleResult:
    command = [str(part) for part in (packet.acceptance_oracle.get("command") or [])]
    worktree = Path(str(packet.repair_unit.get("worktree") or "."))
    if not command:
        scope = str(packet.acceptance_oracle.get("verify_scope") or "subtree")
        if scope not in {"scaffold", "subtree", "full"}:
            scope = "subtree"
        return verify_from_clean_oracle(
            worktree,
            scope=cast(Literal["scaffold", "subtree", "full"], scope),
        )
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (packet.acceptance_oracle.get("env") or {}).items()})
    timeout = int(packet.acceptance_oracle.get("timeout_s") or 300)
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _infra_oracle_result(packet, f"{type(exc).__name__}: {exc}")
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return _infra_oracle_result(
            packet,
            f"clean-verify emitted non-JSON output (exit {proc.returncode})",
        )
    return _oracle_result_from_json(payload)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _repair_prompt(packet: RepairPacket) -> str:
    return (
        "Repair this worktree so the clean-deploy oracle passes. Preserve the "
        "product contract, P0-P4 merge invariants, and owned-path/scope rules. "
        "Diagnose from the complete evidence packet. Run the oracle as your "
        "acceptance loop. Stop only when the oracle passes or you can produce a "
        "structured escalation record explaining why it cannot within budget.\n\n"
        f"Repair packet: {packet.packet_path}\n"
        f"Oracle command: {json.dumps(packet.acceptance_oracle.get('command') or [])}\n"
        f"Repair unit: {json.dumps(packet.repair_unit, sort_keys=True, default=str)}\n"
    )


def _structured_escalation(
    packet: RepairPacket,
    *,
    reason: str,
    agent_turns_used: int,
    oracle_invocations: int,
    cost_usd: float,
) -> dict[str, Any]:
    return {
        "reason": reason,
        "oracle_command": packet.acceptance_oracle.get("command") or [],
        "oracle_env": packet.acceptance_oracle.get("env") or {},
        "final_oracle_result": packet.latest_oracle_result,
        "all_issues": (packet.latest_oracle_result or {}).get("issues") or [],
        "attempt_timeline": packet.events(),
        "agent_turns_used": agent_turns_used,
        "oracle_invocations": oracle_invocations,
        "cost_usd": cost_usd,
        "files_changed": _modified_paths_since_baseline(
            Path(str(packet.repair_unit.get("worktree") or ".")),
            None,
        ),
        "recommendation": "review_packet",
        "closeout_source": (
            "agent_reserve"
            if packet.budget.closeout_agent_turns > 0
            else "packet"
        ),
        "_written_at": _iso_now(),
    }


async def run_oracle_repair_agent(
    repair_packet: RepairPacket,
    *,
    config: dict[str, Any],
    agent_runner: AgentRunner | None = None,
    oracle_runner: OracleRunner | None = None,
    commit_hook: CommitHook | None = None,
) -> OracleRepairResult:
    """Run or resume one durable repair session for a whole oracle unit."""
    packet = repair_packet
    if packet.packet_path.exists():
        loaded = RepairPacket.load(packet.packet_path)
        # Keep caller-provided budget overrides for tests/new invocations, but
        # replay durable identity/session state from disk.
        loaded.budget = packet.budget
        packet = loaded
    packet.persist()
    worktree = Path(str(packet.repair_unit.get("worktree") or "."))
    started = time.monotonic()
    cost_usd = 0.0
    agent_turns_used = 0
    oracle_invocations = 0
    latest_oracle = _oracle_result_from_json(packet.latest_oracle_result)
    default_oracle = oracle_runner is None

    while True:
        if agent_turns_used >= packet.budget.agent_turns:
            escalation = _structured_escalation(
                packet,
                reason="budget_exhausted",
                agent_turns_used=agent_turns_used,
                oracle_invocations=oracle_invocations,
                cost_usd=cost_usd,
            )
            packet.append_event("repair_escalated", digest=latest_oracle.digest, payload=escalation)
            packet.persist()
            return OracleRepairResult(
                verdict="merge_blocked",
                summary="repair budget exhausted",
                agent_session_id=packet.agent_session_id,
                cost_usd=cost_usd,
                agent_turns_used=agent_turns_used,
                oracle_invocations=oracle_invocations,
                packet_path=str(packet.packet_path),
                escalation=escalation,
            )
        if time.monotonic() - started > packet.budget.wall_clock_s:
            escalation = _structured_escalation(
                packet,
                reason="wall_clock_exhausted",
                agent_turns_used=agent_turns_used,
                oracle_invocations=oracle_invocations,
                cost_usd=cost_usd,
            )
            packet.append_event("repair_escalated", digest=latest_oracle.digest, payload=escalation)
            packet.persist()
            return OracleRepairResult(
                verdict="merge_blocked",
                summary="repair wall-clock budget exhausted",
                agent_session_id=packet.agent_session_id,
                cost_usd=cost_usd,
                agent_turns_used=agent_turns_used,
                oracle_invocations=oracle_invocations,
                packet_path=str(packet.packet_path),
                escalation=escalation,
            )

        from otto.agent import make_agent_options

        if agent_runner is None:
            from otto.agent import run_agent_with_timeout

            async def default_runner(
                prompt: str,
                options: Any,
                **kwargs: Any,
            ) -> tuple[str, float, str, dict[str, Any]]:
                return await run_agent_with_timeout(prompt, options, **kwargs)

            selected_runner: AgentRunner = default_runner
        else:
            selected_runner = agent_runner
        options = make_agent_options(
            worktree,
            config,
            agent_type="build",
            resume=packet.agent_session_id or None,
        )
        max_turns = packet.budget.provider_max_turns or int(config.get("max_turns_per_call") or 1)
        options.max_turns = max(1, max_turns)
        options.cwd = str(worktree)
        prompt = _repair_prompt(packet)
        log_dir = packet.packet_dir / "agent" / f"turn-{agent_turns_used + 1}"
        log_dir.mkdir(parents=True, exist_ok=True)
        text, turn_cost, session_id, breakdown = await selected_runner(
            prompt,
            options,
            log_dir=log_dir,
            phase_name="REPAIR",
            phase_label="oracle-repair",
            timeout=int(min(packet.budget.wall_clock_s, float(config.get("run_budget_seconds") or 3600))),
            project_dir=worktree,
        )
        del text
        cost_usd += float(turn_cost or 0.0)
        agent_turns_used += 1
        if session_id:
            packet.agent_session_id = session_id
        packet.attempt_history.append({
            "type": "agent_turn",
            "turn": agent_turns_used,
            "agent_session_id": packet.agent_session_id,
            "cost_usd": float(turn_cost or 0.0),
            "breakdown": breakdown,
            "_written_at": _iso_now(),
        })
        packet.append_event(
            "agent_turn",
            digest=latest_oracle.digest,
            payload={
                "agent_session_id": packet.agent_session_id,
                "turn": agent_turns_used,
                "cost_usd": float(turn_cost or 0.0),
            },
        )
        packet.persist()

        if oracle_invocations >= packet.budget.oracle_invocations:
            continue
        raw_oracle = oracle_runner(packet) if oracle_runner is not None else _default_oracle_runner(packet)
        latest_oracle = await _maybe_await(raw_oracle)
        oracle_invocations += 1
        packet.latest_oracle_result = latest_oracle.to_jsonable()
        if not default_oracle:
            packet.append_event(
                "oracle_run",
                digest=latest_oracle.digest,
                payload={
                    "source": "controller",
                    "passed": latest_oracle.passed,
                },
            )
        packet.persist()

        if latest_oracle.passed and commit_hook is not None:
            ok, detail = await _maybe_await(commit_hook(packet, latest_oracle))
            packet.append_event(
                "commit",
                digest=latest_oracle.digest,
                payload={"ok": ok, "detail": detail},
            )
            packet.persist()
            if not ok:
                escalation = _structured_escalation(
                    packet,
                    reason="commit_failed",
                    agent_turns_used=agent_turns_used,
                    oracle_invocations=oracle_invocations,
                    cost_usd=cost_usd,
                )
                return OracleRepairResult(
                    verdict="merge_blocked",
                    summary=detail,
                    agent_session_id=packet.agent_session_id,
                    cost_usd=cost_usd,
                    agent_turns_used=agent_turns_used,
                    oracle_invocations=oracle_invocations,
                    packet_path=str(packet.packet_path),
                    escalation=escalation,
                )

        gate = _evaluate_composite_gate(packet, latest_oracle)
        if gate["passed"]:
            return OracleRepairResult(
                verdict="pass",
                summary="clean-deploy oracle and composite gate passed",
                agent_session_id=packet.agent_session_id,
                cost_usd=cost_usd,
                agent_turns_used=agent_turns_used,
                oracle_invocations=oracle_invocations,
                packet_path=str(packet.packet_path),
                composite_gate=gate,
            )
        if latest_oracle.passed:
            return OracleRepairResult(
                verdict="merge_blocked",
                summary="clean-deploy passed but composite repair gate blocked landing",
                agent_session_id=packet.agent_session_id,
                cost_usd=cost_usd,
                agent_turns_used=agent_turns_used,
                oracle_invocations=oracle_invocations,
                packet_path=str(packet.packet_path),
                composite_gate=gate,
            )
