"""Deterministic repair loop for v5/v6 integration preflight failures."""

from __future__ import annotations

import inspect
import json
import os
import re
import signal
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from otto.safe_slug import safe_slug, short_hash


RepairAction = Literal["auto_fix", "agent", "escalate"]
TerminalState = Literal["continued", "escalated"]


@dataclass(frozen=True)
class AgentRepairRequest:
    failure_kind: str
    issue: dict[str, Any]
    worktree_path: Path
    session_dir: Path
    attempt_index: int
    workspace_paths: tuple[str, ...] = ()
    instruction: str = ""


@dataclass(frozen=True)
class AgentRepairResult:
    ok: bool
    cost_usd: float = 0.0
    summary: str = ""


@dataclass(frozen=True)
class RepairAttemptResult:
    failure_kind: str
    action: RepairAction
    outcome: str
    continue_run: bool
    cost_usd: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class RepairLoopResult:
    terminal_state: TerminalState
    preflight_payload: dict[str, Any]
    attempts: tuple[RepairAttemptResult, ...] = field(default_factory=tuple)

    @property
    def repaired(self) -> bool:
        return bool(self.attempts) and self.terminal_state == "continued"

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "terminal_state": self.terminal_state,
            "repaired": self.repaired,
            "attempts": [
                {
                    "failure_kind": attempt.failure_kind,
                    "action": attempt.action,
                    "outcome": attempt.outcome,
                    "cost_usd": attempt.cost_usd,
                    "detail": attempt.detail,
                }
                for attempt in self.attempts
            ],
        }


AgentRepairCallable = Callable[[AgentRepairRequest], AgentRepairResult | Awaitable[AgentRepairResult]]
AutoRepairCallable = Callable[[Path, dict[str, Any]], dict[str, Any]]


class PreflightRepairController:
    """Repair preflight failures with deterministic shortcuts plus agent default."""

    def __init__(
        self,
        *,
        session_dir: Path,
        worktree_path: Path,
        original_budget_usd: float | None = None,
        agent_repair: AgentRepairCallable | None = None,
        port_cleanup: AutoRepairCallable | None = None,
        filename_repair: AutoRepairCallable | None = None,
        chmod_repair: AutoRepairCallable | None = None,
        max_attempts_per_kind: int = 2,
        max_total_attempts: int = 3,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.worktree_path = Path(worktree_path)
        del original_budget_usd  # Back-compat only; this loop is attempt-capped.
        self.agent_repair = agent_repair
        self.port_cleanup = port_cleanup or self._cleanup_ports
        self.filename_repair = filename_repair or self._repair_overlong_paths
        self.chmod_repair = chmod_repair or self._repair_permissions
        self.max_attempts_per_kind = max_attempts_per_kind
        self.max_total_attempts = max_total_attempts
        self._attempts_by_kind: defaultdict[str, int] = defaultdict(int)
        self._total_attempts = 0
        self._seen_fingerprints: set[str] = set()

    @property
    def log_path(self) -> Path:
        return self.session_dir / "preflight-repair.jsonl"

    async def repair_until_clean(
        self,
        run_preflight: Callable[[], dict[str, Any]],
        *,
        initial_payload: dict[str, Any] | None = None,
    ) -> RepairLoopResult:
        """Run preflight, repair one blocking issue at a time, and rerun."""
        attempts: list[RepairAttemptResult] = []
        payload = initial_payload if initial_payload is not None else run_preflight()

        while True:
            issue = _first_blocking_issue(payload)
            if issue is None:
                if attempts:
                    payload.setdefault("repair", RepairLoopResult(
                        terminal_state="continued",
                        preflight_payload=payload,
                        attempts=tuple(attempts),
                    ).to_jsonable())
                return RepairLoopResult(
                    terminal_state="continued",
                    preflight_payload=payload,
                    attempts=tuple(attempts),
                )

            attempt = await self.repair_issue(issue)
            attempts.append(attempt)
            if not attempt.continue_run:
                payload.setdefault("repair", RepairLoopResult(
                    terminal_state="escalated",
                    preflight_payload=payload,
                    attempts=tuple(attempts),
                ).to_jsonable())
                return RepairLoopResult(
                    terminal_state="escalated",
                    preflight_payload=payload,
                    attempts=tuple(attempts),
                )
            payload = run_preflight()

    async def repair_issue(self, issue: dict[str, Any]) -> RepairAttemptResult:
        classification = classify_preflight_issue(issue)
        failure_kind = classification["failure_kind"]
        action = classification["action"]
        fingerprint = failure_fingerprint(issue, failure_kind=failure_kind)

        if fingerprint in self._seen_fingerprints:
            return self._escalate(
                issue,
                failure_kind=failure_kind,
                action=action,
                reason="repeated_fingerprint",
                fingerprint=fingerprint,
            )
        if self._total_attempts >= self.max_total_attempts:
            return self._escalate(
                issue,
                failure_kind=failure_kind,
                action=action,
                reason="total_attempt_cap",
                fingerprint=fingerprint,
            )
        if self._attempts_by_kind[failure_kind] >= self.max_attempts_per_kind:
            return self._escalate(
                issue,
                failure_kind=failure_kind,
                action=action,
                reason="kind_attempt_cap",
                fingerprint=fingerprint,
            )
        self._seen_fingerprints.add(fingerprint)
        self._attempts_by_kind[failure_kind] += 1
        self._total_attempts += 1

        if action == "auto_fix":
            return self._auto_fix(issue, classification, fingerprint)
        return await self._agent_fix(issue, classification, fingerprint)

    def _auto_fix(
        self,
        issue: dict[str, Any],
        classification: dict[str, Any],
        fingerprint: str,
    ) -> RepairAttemptResult:
        failure_kind = classification["failure_kind"]
        try:
            if failure_kind == "port_busy":
                detail = self.port_cleanup(self.worktree_path, issue)
            elif failure_kind == "filename_too_long":
                detail = self.filename_repair(self.worktree_path, issue)
            elif failure_kind == "permission_chmod":
                detail = self.chmod_repair(self.worktree_path, issue)
            else:
                return self._escalate(
                    issue,
                    failure_kind=failure_kind,
                    action="auto_fix",
                    reason="missing_auto_fix",
                    fingerprint=fingerprint,
                )
        except Exception as exc:  # noqa: BLE001
            return self._escalate(
                issue,
                failure_kind=failure_kind,
                action="auto_fix",
                reason=f"auto_fix_exception:{type(exc).__name__}: {exc}",
                fingerprint=fingerprint,
            )

        self._append_log(
            event="repair_attempt",
            issue=issue,
            failure_kind=failure_kind,
            action="auto_fix",
            outcome="repaired",
            fingerprint=fingerprint,
            detail=detail,
        )
        return RepairAttemptResult(
            failure_kind=failure_kind,
            action="auto_fix",
            outcome="repaired",
            continue_run=True,
            detail=json.dumps(detail, sort_keys=True),
        )

    async def _agent_fix(
        self,
        issue: dict[str, Any],
        classification: dict[str, Any],
        fingerprint: str,
    ) -> RepairAttemptResult:
        failure_kind = classification["failure_kind"]
        if self.agent_repair is None:
            return self._escalate(
                issue,
                failure_kind=failure_kind,
                action="agent",
                reason="missing_agent_repair_callback",
                fingerprint=fingerprint,
            )

        request = AgentRepairRequest(
            failure_kind=failure_kind,
            issue=dict(issue),
            worktree_path=self.worktree_path,
            session_dir=self.session_dir,
            attempt_index=self._total_attempts,
            workspace_paths=tuple(classification.get("workspace_paths") or ()),
            instruction=str(classification.get("instruction") or ""),
        )
        raw_result = self.agent_repair(request)
        result = await raw_result if inspect.isawaitable(raw_result) else raw_result

        outcome = "repaired" if result.ok else "agent_failed"
        self._append_log(
            event="repair_attempt",
            issue=issue,
            failure_kind=failure_kind,
            action="agent",
            outcome=outcome,
            fingerprint=fingerprint,
            cost_usd=result.cost_usd,
            workspace_paths=list(request.workspace_paths),
            summary=result.summary,
        )
        return RepairAttemptResult(
            failure_kind=failure_kind,
            action="agent",
            outcome=outcome,
            continue_run=result.ok,
            cost_usd=float(result.cost_usd or 0.0),
            detail=result.summary,
        )

    def _escalate(
        self,
        issue: dict[str, Any],
        *,
        failure_kind: str,
        action: RepairAction,
        reason: str,
        fingerprint: str,
    ) -> RepairAttemptResult:
        self._append_log(
            event="repair_escalated",
            issue=issue,
            failure_kind=failure_kind,
            action=action,
            outcome="escalated",
            reason=reason,
            fingerprint=fingerprint,
            attempts_by_kind=self._attempts_by_kind.get(failure_kind, 0),
            total_attempts=self._total_attempts,
        )
        return RepairAttemptResult(
            failure_kind=failure_kind,
            action=action,
            outcome="escalated",
            continue_run=False,
            detail=reason,
        )

    def _append_log(self, *, event: str, **payload: Any) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "_written_at": _iso_now(),
            "event": event,
            **payload,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def _cleanup_ports(self, worktree_path: Path, _issue: dict[str, Any]) -> dict[str, Any]:
        from otto.v5_clean_verify import _parse_declared_ports

        ports = _parse_declared_ports(worktree_path)
        killed: dict[int, list[int]] = {}
        for port in ports:
            pids = _pids_for_port(port)
            owned = [
                pid for pid in pids
                if _is_otto_owned_process(pid, worktree_path)
            ]
            if not owned:
                continue
            for pid in owned:
                _terminate_pid(pid)
            killed[port] = owned
        return {"killed_ports": sorted(killed), "killed_pids": killed}

    def _repair_overlong_paths(self, worktree_path: Path, _issue: dict[str, Any]) -> dict[str, Any]:
        renamed: list[dict[str, str]] = []
        for path in _iter_paths_deep_first(worktree_path):
            try:
                rel = path.relative_to(worktree_path)
            except ValueError:
                continue
            if any(part in {".git", ".worktrees", "node_modules", ".venv"} for part in rel.parts):
                continue
            new_name = _safe_existing_component(path.name)
            if new_name == path.name:
                continue
            target = path.with_name(new_name)
            if target.exists():
                target = path.with_name(_dedupe_existing_name(target))
            path.rename(target)
            renamed.append({"from": str(path), "to": str(target)})
        return {"renamed": renamed}

    def _repair_permissions(self, worktree_path: Path, issue: dict[str, Any]) -> dict[str, Any]:
        message = str(issue.get("message") or "")
        raw_paths = list(issue.get("paths") or []) + _extract_likely_paths(message)
        changed: list[str] = []
        for raw_path in raw_paths:
            rel = str(raw_path).strip()
            if not rel or rel.startswith("/") or ".." in Path(rel).parts:
                continue
            path = worktree_path / rel
            if path.suffix != ".sh" or not path.is_file():
                continue
            path.chmod(path.stat().st_mode | 0o111)
            changed.append(rel)
        return {"chmod_x": sorted(set(changed))}


def _first_blocking_issue(payload: dict[str, Any]) -> dict[str, Any] | None:
    for issue in payload.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if issue.get("severity") in ("block", "error"):
            return issue
    if payload.get("passed") is False:
        return {
            "kind": str(payload.get("error") or "preflight_failed"),
            "severity": "block",
            "message": str(payload.get("error") or "preflight failed without issue detail"),
        }
    return None


def classify_preflight_issue(issue: dict[str, Any]) -> dict[str, Any]:
    """Choose a deterministic shortcut, otherwise hand the issue to an agent."""
    kind = str(issue.get("kind") or "").strip()
    message = str(issue.get("message") or "")
    lowered = f"{kind}\n{message}".lower()

    if "port_busy" in kind or "already bound" in lowered or "port already" in lowered:
        return {"failure_kind": "port_busy", "action": "auto_fix"}
    if (
        "filename_too_long" in kind
        or "file name too long" in lowered
        or "errno 63" in lowered
    ):
        return {"failure_kind": "filename_too_long", "action": "auto_fix"}
    likely_paths = tuple(
        dict.fromkeys([str(path) for path in (issue.get("paths") or [])] + _extract_likely_paths(message))
    )
    if (
        ("not executable" in lowered or "permission denied" in lowered)
        and any(str(path).endswith(".sh") for path in likely_paths)
    ):
        return {
            "failure_kind": "permission_chmod",
            "action": "auto_fix",
            "workspace_paths": likely_paths,
        }
    return {
        "failure_kind": kind or "preflight_failed",
        "action": "agent",
        "workspace_paths": likely_paths,
        "instruction": (
            "Inspect the failure, git status, and nearby files. Make the "
            "smallest repair that lets the preflight pass without changing "
            "provider routing or product scope."
        ),
    }


def failure_fingerprint(issue: dict[str, Any], *, failure_kind: str) -> str:
    message = str(issue.get("message") or "")
    normalized = re.sub(r"/private/[^ \n]+|/tmp/[^ \n]+", "<tmp-path>", message)
    normalized = re.sub(r"\d+\.\d+s", "<duration>", normalized)
    return short_hash(f"{failure_kind}\n{normalized}", length=12)


def _extract_likely_paths(message: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(
        r"([A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|json|toml|yaml|yml|sh|md))(?::|\(|\s|$)",
        message,
    ):
        path = match.group(1).strip()
        if path not in paths:
            paths.append(path)
    return paths


def _iter_paths_deep_first(root: Path) -> list[Path]:
    if not root.exists():
        return []
    paths = list(root.rglob("*"))
    return sorted(paths, key=lambda path: len(path.parts), reverse=True)


def _safe_existing_component(name: str) -> str:
    if len(name.encode("utf-8")) <= 120 and re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return name
    stem = safe_slug(Path(name).stem or name, max_len=40)
    suffix = Path(name).suffix
    candidate = f"{stem}{suffix}" if suffix and len(suffix) <= 12 else stem
    return candidate[:120].strip("-._") or safe_slug(name, max_len=48)


def _dedupe_existing_name(path: Path) -> str:
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 100):
        candidate = f"{stem}-{index}{suffix}"
        if not (path.parent / candidate).exists():
            return candidate
    return f"{stem}-{short_hash(time.time_ns())}{suffix}"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pids_for_port(port: int) -> list[int]:
    import subprocess

    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return []
    pids: list[int] = []
    for raw in out.split():
        try:
            pids.append(int(raw))
        except ValueError:
            continue
    return pids


def _is_otto_owned_process(pid: int, worktree_path: Path) -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
        cwd = Path(proc.cwd()).resolve()
        worktree = worktree_path.resolve()
        if cwd == worktree or worktree in cwd.parents:
            return True
        cmdline = " ".join(proc.cmdline())
        return str(worktree) in cmdline and "otto" in cmdline.lower()
    except (psutil.Error, OSError, RuntimeError):
        return False


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        return
