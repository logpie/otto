"""Controller-run API behavior journey executors."""

from __future__ import annotations

import functools
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable

from otto.journey_contracts import (
    VerificationContractError,
    validate_api_pass_model,
)
from otto.journey_executor_common import (
    artifact_run_name,
    executor_non_pass_result,
    executor_pass_result,
    journey_id,
)
from otto.observability import iso_timestamp, write_json_atomic, write_text_atomic
from otto.safe_slug import safe_slug

API_EXECUTOR_SOURCE = "api_executor"
ENFORCED_HTTP_STRONG_ASSERTIONS = frozenset({
    "expect_json",
    "expect_body_contains",
    "extract",
    "expect_json_path",
    "expect_header",
})
ENFORCED_SERVICE_STRONG_ASSERTIONS = frozenset({
    "expect_json",
    "expect_body_contains",
    "expect_json_path",
})
APIProbeExecutor = Callable[..., dict[str, Any]]


@dataclass
class APIJourneyExecutorRun:
    executor_results: list[dict[str, Any]] = field(default_factory=list)
    artifact_paths: list[Path] = field(default_factory=list)


def run_api_journey_executor(
    *,
    journeys: list[dict[str, Any]],
    project_dir: Path,
    artifact_dir: Path,
    base_url: str | None = None,
    timeout_s: int = 30,
) -> APIJourneyExecutorRun:
    """Run API-level journeys through typed controller executors."""

    started = iso_timestamp()
    run_dir = artifact_dir / artifact_run_name(started)
    run_dir.mkdir(parents=True, exist_ok=True)
    executor_results: list[dict[str, Any]] = []
    artifact_paths: list[Path] = [run_dir]
    for journey in journeys:
        result = _run_one_journey(
            journey=journey,
            project_dir=project_dir,
            run_dir=run_dir,
            base_url=base_url,
            timeout_s=timeout_s,
        )
        executor_results.append(result)
        artifact_paths.extend(
            Path(path)
            for path in result.get("artifact_paths") or []
            if isinstance(path, str)
        )

    summary_path = run_dir / "api-executor-summary.json"
    write_json_atomic(
        summary_path,
        {
            "_written_at": iso_timestamp(),
            "source": API_EXECUTOR_SOURCE,
            "executor_results": executor_results,
        },
        trailing_newline=True,
    )
    artifact_paths.append(summary_path)
    return APIJourneyExecutorRun(
        executor_results=executor_results,
        artifact_paths=artifact_paths,
    )


def _run_one_journey(
    *,
    journey: dict[str, Any],
    project_dir: Path,
    run_dir: Path,
    base_url: str | None,
    timeout_s: int,
) -> dict[str, Any]:
    jid = journey_id(journey)
    journey_dir = run_dir / safe_slug(jid, max_len=48)
    journey_dir.mkdir(parents=True, exist_ok=True)
    probe_kind = str(journey.get("probe_kind") or "").strip()
    executor = PROBE_EXECUTORS.get(probe_kind)
    if executor is None:
        return _non_pass_result(
            journey,
            status="unverified",
            detail=f"unsupported api probe_kind {probe_kind!r}",
            proof_usable=False,
            artifact_paths=[],
        )
    try:
        validate_api_pass_model(journey, path=f"behavior_journeys[{jid}]")
        pass_model = journey.get("pass_model")
        if not isinstance(pass_model, dict):
            raise VerificationContractError(
                "verification_contract_missing",
                f"behavior_journeys[{jid}].pass_model",
                "api journey is missing declarative pass_model",
            )
        return executor(
            journey=journey,
            pass_model=pass_model,
            project_dir=project_dir,
            journey_dir=journey_dir,
            base_url=base_url,
            timeout_s=timeout_s,
        )
    except VerificationContractError as exc:
        return _non_pass_result(
            journey,
            status="unverified",
            detail=f"{exc.code}: {exc.message}",
            proof_usable=False,
            artifact_paths=[],
        )
    except Exception as exc:  # noqa: BLE001
        return _non_pass_result(
            journey,
            status="unverified",
            detail=f"api executor error: {type(exc).__name__}: {exc}",
            proof_usable=False,
            artifact_paths=[],
        )


def _run_http_api_journey(
    *,
    journey: dict[str, Any],
    pass_model: dict[str, Any],
    project_dir: Path,
    base_url: str | None,
    journey_dir: Path,
    timeout_s: int,
) -> dict[str, Any]:
    del project_dir
    if not base_url:
        return _non_pass_result(
            journey,
            status="unverified",
            detail="http_api journey requires base_url",
            proof_usable=False,
            artifact_paths=[],
        )
    steps = pass_model.get("steps") or pass_model.get("api_steps")
    if not isinstance(steps, list) or not steps:
        return _non_pass_result(
            journey,
            status="unverified",
            detail="http_api pass_model requires a non-empty steps list",
            proof_usable=False,
            artifact_paths=[],
        )

    cookie_jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    state: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            return _write_http_result(
                journey,
                journey_dir,
                events,
                status="unverified",
                detail=f"http_api step {index} is not an object",
                proof_usable=False,
            )
        rendered = _render_templates(raw_step, state)
        method = str(rendered.get("method") or "GET").upper()
        path = str(rendered.get("path") or "").strip()
        if not path:
            return _write_http_result(
                journey,
                journey_dir,
                events,
                status="unverified",
                detail=f"http_api step {index} missing path",
                proof_usable=False,
            )
        url = _join_url(base_url, path)
        headers = _string_map(rendered.get("headers"))
        data = _request_body(rendered, headers)
        expected_status = int(rendered.get("expect_status") or rendered.get("status") or 200)
        started = time.monotonic()
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            with opener.open(request, timeout=max(1, timeout_s)) as response:
                status_code = int(response.status)
                response_headers = _headers_to_dict(response.headers)
                response_text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            response_headers = _headers_to_dict(exc.headers)
            response_text = exc.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            events.append(_http_event(rendered, url, 0, "", time.monotonic() - started, error=str(exc.reason)))
            return _write_http_result(
                journey,
                journey_dir,
                events,
                status="fail",
                detail=f"{method} {path} connection error: {exc.reason}",
                proof_usable=True,
            )
        except TimeoutError as exc:
            events.append(_http_event(rendered, url, 0, "", time.monotonic() - started, error=str(exc)))
            return _write_http_result(
                journey,
                journey_dir,
                events,
                status="fail",
                detail=f"{method} {path} timed out",
                proof_usable=True,
            )
        duration_s = time.monotonic() - started
        event = _http_event(rendered, url, status_code, response_text, duration_s)
        events.append(event)
        if status_code != expected_status:
            return _write_http_result(
                journey,
                journey_dir,
                events,
                status="fail",
                detail=f"{method} {path} status={status_code}, expected {expected_status}",
                proof_usable=True,
            )
        json_payload, json_error = _parse_json(response_text)
        failure = _assert_http_body(
            rendered,
            response_text,
            response_headers,
            json_payload,
            json_error,
        )
        if failure:
            return _write_http_result(
                journey,
                journey_dir,
                events,
                status="fail",
                detail=f"{method} {path} {failure}",
                proof_usable=True,
            )
        extract = rendered.get("extract")
        if isinstance(extract, dict):
            if json_error:
                return _write_http_result(
                    journey,
                    journey_dir,
                    events,
                    status="fail",
                    detail=f"{method} {path} extract needs JSON response: {json_error}",
                    proof_usable=True,
                )
            for key, expr in extract.items():
                value, missing = _json_path(json_payload, str(expr))
                if missing:
                    return _write_http_result(
                        journey,
                        journey_dir,
                        events,
                        status="fail",
                        detail=f"{method} {path} extract {key!r} missing at {expr!r}",
                        proof_usable=True,
                    )
                state[str(key)] = value

    return _write_http_result(
        journey,
        journey_dir,
        events,
        status="pass",
        detail="stateful http_api pass-model satisfied",
        proof_usable=True,
    )


def _run_cli_journey(
    *,
    journey: dict[str, Any],
    pass_model: dict[str, Any],
    project_dir: Path,
    journey_dir: Path,
    base_url: str | None,
    timeout_s: int,
) -> dict[str, Any]:
    del base_url
    command = _command_list(pass_model.get("command"))
    if command is None:
        return _non_pass_result(
            journey,
            status="unverified",
            detail="cli_command pass_model requires command as a non-empty string list",
            proof_usable=False,
            artifact_paths=[],
        )
    env = {**os.environ, **_string_map(pass_model.get("env"))}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"cli_command timed out after {timeout_s}s",
            proof_usable=True,
            artifact_paths=[],
        )
    except OSError as exc:
        return _non_pass_result(
            journey,
            status="unverified",
            detail=f"cli_command could not start: {exc}",
            proof_usable=False,
            artifact_paths=[],
        )

    stdout_path = journey_dir / "stdout.log"
    stderr_path = journey_dir / "stderr.log"
    write_text_atomic(stdout_path, proc.stdout or "")
    write_text_atomic(stderr_path, proc.stderr or "")
    artifact_paths = [stdout_path, stderr_path]
    expected_exit = int(pass_model.get("expect_exit_code") or pass_model.get("exit_code") or 0)
    if proc.returncode != expected_exit:
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"cli_command exit_code={proc.returncode}, expected {expected_exit}",
            proof_usable=True,
            artifact_paths=artifact_paths,
        )
    failure = _assert_text_contains(proc.stdout or "", pass_model.get("stdout_contains"), "stdout")
    failure = failure or _assert_text_contains(proc.stderr or "", pass_model.get("stderr_contains"), "stderr")
    failure = failure or _assert_fs_effects(project_dir, pass_model.get("fs_effects"))
    if failure:
        return _non_pass_result(
            journey,
            status="fail",
            detail=failure,
            proof_usable=True,
            artifact_paths=artifact_paths,
        )
    return _pass_result(
        journey,
        detail=f"cli_command satisfied in {time.monotonic() - started:.3f}s",
        artifact_paths=artifact_paths,
    )


def _run_library_journey(
    *,
    journey: dict[str, Any],
    pass_model: dict[str, Any],
    project_dir: Path,
    journey_dir: Path,
    base_url: str | None,
    timeout_s: int,
) -> dict[str, Any]:
    del base_url
    module = str(pass_model.get("module") or "").strip()
    function = str(pass_model.get("function") or "").strip()
    if not module or not function:
        return _non_pass_result(
            journey,
            status="unverified",
            detail="library_call pass_model requires module and function",
            proof_usable=False,
            artifact_paths=[],
        )
    payload = {
        "module": module,
        "function": function,
        "args": pass_model.get("args") or [],
        "kwargs": pass_model.get("kwargs") or {},
    }
    script = (
        "import importlib, json, os, sys, traceback\n"
        "sys.path.insert(0, os.getcwd())\n"
        "payload = json.loads(os.environ['OTTO_LIBRARY_CALL'])\n"
        "try:\n"
        "    mod = importlib.import_module(payload['module'])\n"
        "    fn = getattr(mod, payload['function'])\n"
        "    result = fn(*payload.get('args', []), **payload.get('kwargs', {}))\n"
        "    print(json.dumps({'ok': True, 'return': result}, default=str))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'ok': False, 'error_type': type(exc).__name__, 'error': str(exc)}))\n"
    )
    try:
        proc = subprocess.run(
            ["python3", "-c", script],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env={**os.environ, "OTTO_LIBRARY_CALL": json.dumps(payload)},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"library_call timed out after {timeout_s}s",
            proof_usable=True,
            artifact_paths=[],
        )
    stdout_path = journey_dir / "library-call.jsonl"
    write_text_atomic(stdout_path, proc.stdout or "")
    artifact_paths = [stdout_path]
    if proc.returncode != 0:
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"library_call subprocess exit_code={proc.returncode}",
            proof_usable=True,
            artifact_paths=artifact_paths,
        )
    try:
        observed = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"library_call produced malformed result: {exc}",
            proof_usable=True,
            artifact_paths=artifact_paths,
        )
    expect_raises = str(pass_model.get("expect_raises") or "").strip()
    if expect_raises:
        if observed.get("ok") is False and observed.get("error_type") == expect_raises:
            return _pass_result(journey, detail="library_call raised expected exception", artifact_paths=artifact_paths)
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"library_call did not raise expected {expect_raises}: {observed}",
            proof_usable=True,
            artifact_paths=artifact_paths,
        )
    if observed.get("ok") is not True:
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"library_call raised {observed.get('error_type')}: {observed.get('error')}",
            proof_usable=True,
            artifact_paths=artifact_paths,
        )
    if "expect_return" in pass_model and observed.get("return") != pass_model.get("expect_return"):
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"library_call return={observed.get('return')!r}, expected {pass_model.get('expect_return')!r}",
            proof_usable=True,
            artifact_paths=artifact_paths,
        )
    return _pass_result(journey, detail="library_call satisfied", artifact_paths=artifact_paths)


def _run_service_health_journey(
    *,
    journey: dict[str, Any],
    pass_model: dict[str, Any],
    project_dir: Path,
    journey_dir: Path,
    base_url: str | None,
    timeout_s: int,
) -> dict[str, Any]:
    del base_url
    command = _command_list(pass_model.get("start_command") or pass_model.get("command"))
    health_url = str(pass_model.get("health_url") or "").strip()
    if command is None or not health_url:
        return _non_pass_result(
            journey,
            status="unverified",
            detail="service_health pass_model requires start_command and health_url",
            proof_usable=False,
            artifact_paths=[],
        )
    env = {**os.environ, **_string_map(pass_model.get("env"))}
    proc: subprocess.Popen[str] | None = None
    output = ""
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        deadline = time.monotonic() + max(1, timeout_s)
        last_error = ""
        expected_status = int(pass_model.get("expect_status") or 200)
        expect_body = str(pass_model.get("expect_body_contains") or "")
        expect_json = pass_model.get("expect_json")
        expect_json_path = pass_model.get("expect_json_path")
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(health_url, timeout=1) as response:
                    status_code = int(response.status)
                    response_text = response.read().decode("utf-8", errors="replace")
                json_payload, json_error = _parse_json(response_text)
                body_matches = not expect_body or expect_body in response_text
                json_matches = True
                if expect_json is not None:
                    json_matches = not json_error and not _json_subset_mismatch(json_payload, expect_json)
                json_path_failure = _assert_json_path(json_payload, json_error, expect_json_path)
                if status_code == expected_status and body_matches and json_matches and not json_path_failure:
                    log_path = _collect_service_output(proc, journey_dir)
                    return _pass_result(
                        journey,
                        detail=f"service_health satisfied at {health_url}",
                        artifact_paths=[log_path],
                    )
                last_error = f"status={status_code}, payload did not match"
                if json_path_failure:
                    last_error = json_path_failure
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = str(exc)
            time.sleep(0.1)
        log_path = _collect_service_output(proc, journey_dir)
        return _non_pass_result(
            journey,
            status="fail",
            detail=f"service_health did not become healthy: {last_error or 'process exited'}",
            proof_usable=True,
            artifact_paths=[log_path],
        )
    except OSError as exc:
        return _non_pass_result(
            journey,
            status="unverified",
            detail=f"service_health could not start: {exc}",
            proof_usable=False,
            artifact_paths=[],
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                output = proc.communicate(timeout=2)[0] or output
            except subprocess.TimeoutExpired:
                proc.kill()
                output = proc.communicate(timeout=2)[0] or output
            if output:
                write_text_atomic(journey_dir / "service-output.log", output)


PROBE_EXECUTORS: dict[str, APIProbeExecutor] = {
    "http_api": _run_http_api_journey,
    "cli_command": _run_cli_journey,
    "library_call": _run_library_journey,
    "service_health": _run_service_health_journey,
}


def _collect_service_output(proc: subprocess.Popen[str], journey_dir: Path) -> Path:
    if proc.poll() is None:
        try:
            proc.terminate()
            output = proc.communicate(timeout=2)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            output = proc.communicate(timeout=2)[0] or ""
    else:
        output = proc.communicate(timeout=2)[0] or ""
    path = journey_dir / "service-output.log"
    write_text_atomic(path, output)
    return path


def _write_http_result(
    journey: dict[str, Any],
    journey_dir: Path,
    events: list[dict[str, Any]],
    *,
    status: str,
    detail: str,
    proof_usable: bool,
) -> dict[str, Any]:
    path = journey_dir / "http-events.json"
    write_json_atomic(
        path,
        {"_written_at": iso_timestamp(), "events": events},
        trailing_newline=True,
    )
    if status == "pass":
        return _pass_result(journey, detail=detail, artifact_paths=[path])
    return _non_pass_result(
        journey,
        status=status,
        detail=detail,
        proof_usable=proof_usable,
        artifact_paths=[path],
    )


_pass_result = functools.partial(executor_pass_result, source=API_EXECUTOR_SOURCE)
_non_pass_result = functools.partial(executor_non_pass_result, source=API_EXECUTOR_SOURCE)


def _command_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    command = [item for item in value if isinstance(item, str) and item]
    if len(command) != len(value):
        return None
    return command


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _headers_to_dict(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    items = headers.items() if hasattr(headers, "items") else []
    return {str(key).lower(): str(value) for key, value in items}


def _request_body(step: dict[str, Any], headers: dict[str, str]) -> bytes | None:
    if "json" in step:
        headers.setdefault("Content-Type", "application/json")
        return json.dumps(step.get("json")).encode("utf-8")
    if "body" in step:
        body = step.get("body")
        return body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    return None


def _join_url(base_url: str, path: str) -> str:
    if urllib.parse.urlparse(path).scheme:
        return path
    return base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)


def _http_event(
    step: dict[str, Any],
    url: str,
    status_code: int,
    response_text: str,
    duration_s: float,
    *,
    error: str = "",
) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    return {
        "_written_at": iso_timestamp(),
        "step_id": str(step.get("id") or ""),
        "method": str(step.get("method") or "GET").upper(),
        "url": url,
        "path": parsed.path,
        "status": status_code,
        "duration_s": duration_s,
        "response_preview": response_text[:500],
        "error": error,
    }


def _parse_json(text: str) -> tuple[Any, str]:
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _assert_http_body(
    step: dict[str, Any],
    response_text: str,
    response_headers: dict[str, str],
    json_payload: Any,
    json_error: str,
) -> str:
    contains = step.get("expect_body_contains")
    if isinstance(contains, str) and contains and contains not in response_text:
        return f"body did not contain {contains!r}"
    expected_json = step.get("expect_json")
    if expected_json is not None:
        if json_error:
            return f"expected JSON response but parse failed: {json_error}"
        mismatch = _json_subset_mismatch(json_payload, expected_json)
        if mismatch:
            return mismatch
    header_failure = _assert_headers(response_headers, step.get("expect_header"))
    if header_failure:
        return header_failure
    json_path_failure = _assert_json_path(json_payload, json_error, step.get("expect_json_path"))
    if json_path_failure:
        return json_path_failure
    return ""


def _assert_headers(response_headers: dict[str, str], expected: Any) -> str:
    for assertion in _header_assertions(expected):
        name = assertion["name"]
        actual = response_headers.get(name.lower())
        if actual is None:
            return f"header {name} missing"
        if "equals" in assertion and actual != assertion["equals"]:
            return f"header {name} expected {assertion['equals']!r}, got {actual!r}"
        if "contains" in assertion and assertion["contains"] not in actual:
            return f"header {name} did not contain {assertion['contains']!r}"
    return ""


def _header_assertions(expected: Any) -> list[dict[str, str]]:
    if expected in (None, "", [], {}):
        return []
    if isinstance(expected, str):
        raw = expected.strip()
        if ":" in raw:
            name, value = raw.split(":", 1)
            return [{"name": name.strip(), "equals": value.strip()}]
        return [{"name": raw}]
    if isinstance(expected, list):
        assertions: list[dict[str, str]] = []
        for item in expected:
            assertions.extend(_header_assertions(item))
        return assertions
    if isinstance(expected, dict):
        explicit_name = str(expected.get("name") or expected.get("header") or expected.get("key") or "").strip()
        if explicit_name:
            assertion = {"name": explicit_name}
            if "equals" in expected:
                assertion["equals"] = str(expected.get("equals"))
            elif "value" in expected:
                assertion["equals"] = str(expected.get("value"))
            if "contains" in expected:
                assertion["contains"] = str(expected.get("contains"))
            return [assertion]
        return [
            {"name": str(name).strip(), "equals": str(value)}
            for name, value in expected.items()
            if str(name).strip()
        ]
    return []


def _assert_json_path(json_payload: Any, json_error: str, expected: Any) -> str:
    for assertion in _json_path_assertions(expected):
        expr = assertion["path"]
        if json_error:
            return f"expected JSON path {expr} but parse failed: {json_error}"
        value, missing = _json_path(json_payload, expr)
        if missing:
            return f"JSON path {expr} missing"
        if "equals" in assertion and value != assertion["equals"]:
            return f"JSON path {expr} expected {assertion['equals']!r}, got {value!r}"
        if "contains" in assertion and assertion["contains"] not in str(value):
            return f"JSON path {expr} did not contain {assertion['contains']!r}"
    return ""


def _json_path_assertions(expected: Any) -> list[dict[str, Any]]:
    if expected in (None, "", [], {}):
        return []
    if isinstance(expected, str):
        return [{"path": expected.strip()}]
    if isinstance(expected, list):
        assertions: list[dict[str, Any]] = []
        for item in expected:
            assertions.extend(_json_path_assertions(item))
        return assertions
    if isinstance(expected, dict):
        explicit_path = str(expected.get("path") or expected.get("json_path") or "").strip()
        if explicit_path:
            assertion: dict[str, Any] = {"path": explicit_path}
            if "equals" in expected:
                assertion["equals"] = expected.get("equals")
            elif "value" in expected:
                assertion["equals"] = expected.get("value")
            if "contains" in expected:
                assertion["contains"] = str(expected.get("contains"))
            return [assertion]
        return [
            {"path": str(path).strip(), "equals": value}
            for path, value in expected.items()
            if str(path).strip()
        ]
    return []


def _json_subset_mismatch(observed: Any, expected: Any, path: str = "$") -> str:
    if isinstance(expected, dict):
        if not isinstance(observed, dict):
            return f"{path} expected object, got {type(observed).__name__}"
        for key, expected_value in expected.items():
            if key not in observed:
                return f"{path}.{key} missing"
            mismatch = _json_subset_mismatch(observed[key], expected_value, f"{path}.{key}")
            if mismatch:
                return mismatch
        return ""
    if isinstance(expected, list):
        if observed != expected:
            return f"{path} expected {expected!r}, got {observed!r}"
        return ""
    if observed != expected:
        return f"{path} expected {expected!r}, got {observed!r}"
    return ""


def _json_path(payload: Any, expr: str) -> tuple[Any, bool]:
    if expr == "$":
        return payload, False
    if not expr.startswith("$."):
        return None, True
    current = payload
    for part in expr[2:].split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return None, True
    return current, False


def _render_templates(value: Any, state: dict[str, Any]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, item in state.items():
            rendered = rendered.replace("{{" + key + "}}", str(item))
        return rendered
    if isinstance(value, dict):
        return {key: _render_templates(item, state) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_templates(item, state) for item in value]
    return value


def _assert_text_contains(text: str, expected: Any, label: str) -> str:
    if expected is None:
        return ""
    if isinstance(expected, str):
        expected_items = [expected]
    elif isinstance(expected, list):
        expected_items = [str(item) for item in expected]
    else:
        return f"{label}_contains must be a string or list"
    for item in expected_items:
        if item not in text:
            return f"{label} did not contain {item!r}"
    return ""


def _assert_fs_effects(project_dir: Path, raw_effects: Any) -> str:
    if raw_effects is None:
        return ""
    if not isinstance(raw_effects, list):
        return "fs_effects must be a list"
    for effect in raw_effects:
        if not isinstance(effect, dict):
            return "fs_effects entries must be objects"
        rel = str(effect.get("path") or "").strip()
        if not rel:
            return "fs_effects entry missing path"
        path = project_dir / rel
        expected_exists = effect.get("exists")
        if expected_exists is not None and path.exists() is not bool(expected_exists):
            return f"filesystem effect {rel!r} exists={path.exists()}, expected {bool(expected_exists)}"
        contains = effect.get("contains")
        if isinstance(contains, str):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                return f"filesystem effect {rel!r} could not be read: {exc}"
            if contains not in text:
                return f"filesystem effect {rel!r} did not contain {contains!r}"
    return ""
