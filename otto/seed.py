"""Seed stage — applies pre-existing fixtures to the live product before Audit.

Step A1.5-seed of the unified intent-to-product pipeline. Applies the
`Spec.audit_fixtures[]` list to a running product so the Audit stage
doesn't waste budget creating test users, follow graphs, channels, and
sample data on every run.

Per-fixture-kind handlers (`_seed_user`, `_seed_channel`, `_seed_follow`,
`_seed_data`) dispatch on `AuditFixture.kind` and invoke a project-owned
script (e.g. `scripts/otto/seed_user.py`) as a subprocess with the
fixture payload as JSON on stdin. The script is responsible for the
mechanics — Otto's Seed stage owns dispatch, idempotency contract,
and error reporting.

Idempotency is enforced by the project's seed scripts (Otto trusts the
script to dedup on a unique key — username, channel-name, follower
pair, etc.). Re-running the Seed stage with the same Spec must produce
the same product state without duplicates. Tests in
`tests/test_seed.py` verify this contract end-to-end with throwaway
seed scripts that maintain a JSON state file.

Failed seed = `SeedResult(succeeded=False, ...)` with a concrete error
detail. The caller (Run orchestrator, A1.6) treats this as a blocked
Run, NOT a silent proceed-with-empty-state — running an audit against
a half-seeded product produces meaningless verdicts.

Pattern-shaped on `otto/checks.py`:
- pure functions, no class state
- dataclass-shaped result with `succeeded` bool + `detail` summary
- per-kind handlers return `(ok, detail)` tuples; the top-level
  `seed_fixtures` aggregates them into a `SeedResult`
- never raises; subprocess failures, missing scripts, invalid kinds all
  become `succeeded=False` with the cause in `detail`
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from otto.defaults import SEED_PER_FIXTURE_TIMEOUT_S
from otto.observability import iso_timestamp, write_text_atomic
from otto.spec_compile import AuditFixture, Spec
from otto.spec_state import emit as emit_state_event


# Recognised fixture kinds (research §4 audit fixtures).
SUPPORTED_KINDS: tuple[str, ...] = ("user", "channel", "follow", "data")


@dataclass
class SeedResult:
    """Outcome of running the Seed stage for one Run.

    `succeeded` is the binary verdict; `detail` is a one-line summary.
    `fixtures_applied` is the per-fixture record of what ran (kind +
    payload-key + ok), preserved in declared order. `errors` is the list
    of human-readable error messages — one per failing fixture, plus an
    aggregate "stage" entry if dispatch itself broke.

    The Run orchestrator treats `succeeded=False` as a blocked Run;
    Audit must not run against a half-seeded product.
    """

    succeeded: bool
    detail: str
    fixtures_applied: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""


def seed_fixtures(
    spec: Spec,
    project_dir: Path,
    session_dir: Path | None = None,
) -> SeedResult:
    """Apply all `spec.audit_fixtures[]` to the live product.

    Args:
        spec: Compiled Spec; only `audit_fixtures` is consumed.
        project_dir: Project root. Seed scripts live under
            `scripts/otto/seed_<kind>.py` (resolved relative to this).
        session_dir: Optional otto_logs session dir. If provided, a raw
            log of seed dispatch is written to
            `<session_dir>/seed/seed.log` for debugging.

    Returns:
        `SeedResult`. `succeeded=True` iff every declared fixture's
        per-kind handler returned ok. An empty `audit_fixtures` list is
        a no-op success ("nothing to seed"). An invalid `kind` aborts
        the run with `succeeded=False`; subsequent fixtures are NOT
        attempted (fail-fast — partial state is the failure mode this
        stage exists to prevent).

    Never raises. All failure paths surface through `SeedResult.errors`.
    """
    started = iso_timestamp()
    result = SeedResult(succeeded=True, detail="", started_at=started)

    # Stage-timeline lifecycle: emit seed.started at entry, seed.finished
    # at exit (both success and failure paths). The RunView stage timeline
    # listens for these to flip the seed stage out of "pending".
    _emit_seed_event(session_dir, "seed.started", detail="seed stage entered")

    try:
        fixtures = list(spec.audit_fixtures or [])
        if not fixtures:
            result.detail = "no audit_fixtures declared (nothing to seed)"
            _write_seed_log(session_dir, result)
            return result

        log_lines: list[str] = [f"# seed log @ {started}"]
        for index, fixture in enumerate(fixtures):
            kind = (fixture.kind or "").strip()
            handler = _HANDLERS.get(kind)
            if handler is None:
                err = (
                    f"audit_fixtures[{index}]: unsupported kind {kind!r} "
                    f"(supported: {', '.join(SUPPORTED_KINDS)})"
                )
                result.succeeded = False
                result.errors.append(err)
                result.fixtures_applied.append(
                    {"index": index, "kind": kind, "ok": False, "error": err}
                )
                log_lines.append(err)
                break  # fail-fast — don't half-seed

            ok, detail = handler(fixture, project_dir)
            record = {
                "index": index,
                "kind": kind,
                "ok": ok,
                "detail": detail,
                "payload_key": _payload_key(fixture),
            }
            result.fixtures_applied.append(record)
            log_lines.append(f"[{index}] kind={kind} ok={ok} detail={detail}")
            if not ok:
                result.succeeded = False
                result.errors.append(f"audit_fixtures[{index}] (kind={kind}): {detail}")
                break  # fail-fast

        if result.succeeded:
            result.detail = f"applied {len(result.fixtures_applied)} fixture(s)"
        elif not result.detail:
            result.detail = (
                f"seed failed at fixture {len(result.fixtures_applied) - 1} of "
                f"{len(fixtures)}; see errors"
            )

        _write_seed_log(session_dir, result, lines=log_lines)
        return result
    finally:
        _emit_seed_event(
            session_dir,
            "seed.finished",
            detail=result.detail or ("succeeded" if result.succeeded else "failed"),
            succeeded=result.succeeded,
            applied=len(result.fixtures_applied),
        )


def _emit_seed_event(
    session_dir: Path | None,
    kind: str,
    *,
    detail: str = "",
    **extra,
) -> None:
    """Best-effort emit of a seed.* lifecycle event into the spec-state journal.

    The journal lives at <session_dir>/spec-state.jsonl. When session_dir is
    None (legacy callers; tests that omit it), the event is a no-op — the
    Seed stage continues to run without observability.

    Failures (filesystem, permission, etc.) never propagate: seed lifecycle
    observability must not block the actual seed work.
    """
    if session_dir is None:
        return
    try:
        emit_state_event(Path(session_dir), kind, detail=detail, **extra)
    except (OSError, ValueError):
        # ValueError covers unknown-kind sanity checks; OSError covers
        # filesystem failures. Either way, swallow — Seed must not abort
        # because journaling failed.
        return


# ---------------------------------------------------------------------------
# Per-fixture-kind handlers
# ---------------------------------------------------------------------------


def _seed_user(fixture: AuditFixture, project_dir: Path) -> tuple[bool, str]:
    """Seed one test user via `scripts/otto/seed_user.py`.

    Payload schema (project-owned; documented here for the seed-script author):
        {"username": str, "email": str, "password": str, ...}

    The seed script must be idempotent on `username` (or whatever unique
    key the project chooses). Re-applying the same payload should leave
    the product state unchanged.
    """
    return _invoke_seed_script(fixture, project_dir, kind="user")


def _seed_channel(fixture: AuditFixture, project_dir: Path) -> tuple[bool, str]:
    """Seed one test channel via `scripts/otto/seed_channel.py`.

    Payload schema (project-owned):
        {"name": str, "members": list[str], "kind": "public" | "dm", ...}
    """
    return _invoke_seed_script(fixture, project_dir, kind="channel")


def _seed_follow(fixture: AuditFixture, project_dir: Path) -> tuple[bool, str]:
    """Seed one follow-graph edge via `scripts/otto/seed_follow.py`.

    Payload schema (project-owned):
        {"follower": str, "followee": str}
    """
    return _invoke_seed_script(fixture, project_dir, kind="follow")


def _seed_data(fixture: AuditFixture, project_dir: Path) -> tuple[bool, str]:
    """Seed sample data via `scripts/otto/seed_data.py`.

    Payload schema (project-owned, free-form):
        whatever the project's data layer expects — products, posts,
        catalogue entries, etc. The fixture's `payload` is forwarded
        verbatim as JSON on stdin.
    """
    return _invoke_seed_script(fixture, project_dir, kind="data")


_HANDLERS = {
    "user": _seed_user,
    "channel": _seed_channel,
    "follow": _seed_follow,
    "data": _seed_data,
}


# ---------------------------------------------------------------------------
# Subprocess dispatch
# ---------------------------------------------------------------------------


def _invoke_seed_script(
    fixture: AuditFixture,
    project_dir: Path,
    *,
    kind: str,
) -> tuple[bool, str]:
    """Run `scripts/otto/seed_<kind>.py` with payload JSON on stdin.

    Returns (ok, detail). Failure modes:
        - script missing → ok=False, detail mentions path
        - non-zero exit → ok=False, detail=f"exit={code}: <stderr-tail>"
        - timeout → ok=False, detail=f"timed out after {N}s"
        - any OSError → ok=False, detail=f"OSError: {exc}"
    """
    script_path = project_dir / "scripts" / "otto" / f"seed_{kind}.py"
    if not script_path.is_file():
        return False, (
            f"seed script not found: scripts/otto/seed_{kind}.py "
            f"(expected at {script_path})"
        )

    payload_json = json.dumps(fixture.payload or {}, sort_keys=True)
    try:
        completed = subprocess.run(
            ["python3", str(script_path)],
            input=payload_json,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=SEED_PER_FIXTURE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {SEED_PER_FIXTURE_TIMEOUT_S}s"
    except OSError as exc:
        return False, f"OSError: {exc}"

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
        return False, f"exit={completed.returncode}: {stderr_tail[0]}"

    stdout_tail = (completed.stdout or "").strip().splitlines()[-1:] or [""]
    return True, f"exit=0: {stdout_tail[0]}" if stdout_tail[0] else "exit=0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_key(fixture: AuditFixture) -> str:
    """Best-effort short identifier for a fixture, for the log line."""
    payload = fixture.payload or {}
    for k in ("username", "name", "id", "follower", "followee"):
        if k in payload:
            return f"{k}={payload[k]!r}"
    return "(no key)"


def _write_seed_log(
    session_dir: Path | None,
    result: SeedResult,
    *,
    lines: list[str] | None = None,
) -> None:
    """Persist a human-readable seed log under <session_dir>/seed/seed.log.

    Best-effort: failures to write the log do not propagate. If
    `session_dir` is None, no log is written.
    """
    if session_dir is None:
        return
    try:
        seed_dir = Path(session_dir) / "seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        body_lines = list(lines or [])
        body_lines.append("")
        body_lines.append(f"succeeded={result.succeeded}")
        body_lines.append(f"detail={result.detail}")
        if result.errors:
            body_lines.append("errors:")
            for err in result.errors:
                body_lines.append(f"  - {err}")
        write_text_atomic(seed_dir / "seed.log", "\n".join(body_lines) + "\n")
    except OSError:
        # Logging is best-effort; never let it block the Run.
        return


__all__ = ["SeedResult", "SUPPORTED_KINDS", "seed_fixtures"]
