"""Controller-run UI behavior journey executor."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from otto.journey_contracts import VerificationContractError, validate_ui_pass_model
from otto.observability import iso_timestamp, write_json_atomic, write_text_atomic
from otto.safe_slug import safe_slug

UI_EXECUTOR_SOURCE = "ui_executor"


@dataclass
class UIJourneyExecutorRun:
    executor_results: list[dict[str, Any]] = field(default_factory=list)
    artifact_paths: list[Path] = field(default_factory=list)
    infra_error: str = ""


@dataclass
class _NetworkEvent:
    written_at: str
    method: str
    url: str
    path: str
    status: int | None
    failure: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "_written_at": self.written_at,
            "method": self.method,
            "url": self.url,
            "path": self.path,
            "status": self.status,
            "failure": self.failure,
        }


def run_ui_journey_executor(
    *,
    journeys: list[dict[str, Any]],
    base_url: str,
    project_dir: Path,
    clean_project_dir: Path,
    artifact_dir: Path,
    timeout_s: int,
) -> UIJourneyExecutorRun:
    """Run UI-level journeys through Playwright and return sink-ready results."""

    del clean_project_dir  # The browser talks to the temp deployment by URL.
    started = iso_timestamp()
    run_dir = artifact_dir / _artifact_run_name(started)
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths: list[Path] = [run_dir]
    executor_results: list[dict[str, Any]] = []
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        detail = f"oracle_infra_error: playwright unavailable: {type(exc).__name__}: {exc}"
        result = _infra_results(journeys, detail)
        summary_path = _write_run_summary(run_dir, result, artifact_paths, detail)
        artifact_paths.append(summary_path)
        return UIJourneyExecutorRun(
            executor_results=result,
            artifact_paths=artifact_paths,
            infra_error=detail,
        )

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for journey in journeys:
                journey_result = _run_one_journey(
                    browser=browser,
                    journey=journey,
                    base_url=base_url,
                    run_dir=run_dir,
                    timeout_s=timeout_s,
                    timeout_error_type=PlaywrightTimeoutError,
                )
                executor_results.append(journey_result)
                artifact_paths.extend(
                    Path(path)
                    for path in journey_result.get("artifact_paths") or []
                    if isinstance(path, str)
                )
    except Exception as exc:  # noqa: BLE001
        detail = f"oracle_infra_error: browser driver failed: {type(exc).__name__}: {exc}"
        executor_results = _infra_results(journeys, detail)
        infra_path = run_dir / "oracle-infra-error.json"
        write_json_atomic(
            infra_path,
            {"_written_at": iso_timestamp(), "detail": detail},
            trailing_newline=True,
        )
        artifact_paths.append(infra_path)
        return UIJourneyExecutorRun(
            executor_results=executor_results,
            artifact_paths=artifact_paths,
            infra_error=detail,
        )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    dirty = _git_diff_dirty(project_dir)
    if dirty:
        detail = f"real project git diff is not empty after UI probe: {dirty[:400]}"
        executor_results = [
            _non_pass_result(
                journey,
                status="unverified",
                detail=detail,
                proof_usable=False,
                artifact_paths=[],
            )
            for journey in journeys
        ]

    summary_path = _write_run_summary(run_dir, executor_results, artifact_paths, "")
    artifact_paths.append(summary_path)
    return UIJourneyExecutorRun(
        executor_results=executor_results,
        artifact_paths=artifact_paths,
    )


def _run_one_journey(
    *,
    browser: Any,
    journey: dict[str, Any],
    base_url: str,
    run_dir: Path,
    timeout_s: int,
    timeout_error_type: type[Exception],
) -> dict[str, Any]:
    journey_id = str(journey.get("id") or "<unnamed>").strip() or "<unnamed>"
    journey_dir = run_dir / safe_slug(journey_id, max_len=48)
    journey_dir.mkdir(parents=True, exist_ok=True)
    network_events: list[_NetworkEvent] = []
    console_errors: list[dict[str, Any]] = []
    artifacts: list[Path] = []
    context = None
    page = None
    timeout_ms = max(1000, min(timeout_s, 60) * 1000)
    try:
        validate_ui_pass_model(journey, path=f"behavior_journeys[{journey_id}]")
        pass_model = journey["pass_model"]
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        _wire_page_events(page, network_events, console_errors)
        route = _ready_route(journey, pass_model)
        target_url = base_url.rstrip("/") + route
        response = page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        _wait_for_ready(page, pass_model, timeout_ms=timeout_ms)
        if _page_blank_or_stuck(page):
            return _finalize_journey(
                journey,
                status="fail",
                detail="route stuck/blank: rendered page has no usable text or controls",
                proof_usable=True,
                journey_dir=journey_dir,
                page=page,
                network_events=network_events,
                console_errors=console_errors,
                artifacts=artifacts,
            )
        status = int(getattr(response, "status", 0) or 0) if response else None
        if status is not None and status >= 500:
            return _finalize_journey(
                journey,
                status="fail",
                detail=f"route returned HTTP {status}",
                proof_usable=True,
                journey_dir=journey_dir,
                page=page,
                network_events=network_events,
                console_errors=console_errors,
                artifacts=artifacts,
            )

        actions = pass_model.get("actions") if isinstance(pass_model, dict) else []
        for action in actions if isinstance(actions, list) else []:
            if not isinstance(action, dict):
                continue
            failure = _run_action(
                page,
                action,
                pass_model,
                network_events,
                timeout_ms=timeout_ms,
                timeout_error_type=timeout_error_type,
            )
            if failure:
                return _finalize_journey(
                    journey,
                    status="fail",
                    detail=failure,
                    proof_usable=True,
                    journey_dir=journey_dir,
                    page=page,
                    network_events=network_events,
                    console_errors=console_errors,
                    artifacts=artifacts,
                )

        for assertion in _list_of_dicts(pass_model.get("final_dom_assertions")):
            failure = _assert_dom_observable(page, assertion, timeout_ms=timeout_ms)
            if failure:
                return _finalize_journey(
                    journey,
                    status="fail",
                    detail=f"final DOM assertion failed: {failure}",
                    proof_usable=True,
                    journey_dir=journey_dir,
                    page=page,
                    network_events=network_events,
                    console_errors=console_errors,
                    artifacts=artifacts,
                )
        return _finalize_journey(
            journey,
            status="pass",
            detail="declarative UI pass-model satisfied at runtime",
            proof_usable=True,
            journey_dir=journey_dir,
            page=page,
            network_events=network_events,
            console_errors=console_errors,
            artifacts=artifacts,
        )
    except VerificationContractError as exc:
        return _finalize_journey(
            journey,
            status="unverified",
            detail=f"{exc.code}: {exc.message}",
            proof_usable=False,
            journey_dir=journey_dir,
            page=page,
            network_events=network_events,
            console_errors=console_errors,
            artifacts=artifacts,
        )
    except timeout_error_type as exc:
        return _finalize_journey(
            journey,
            status="fail",
            detail=f"UI probe timed out: {exc}",
            proof_usable=True,
            journey_dir=journey_dir,
            page=page,
            network_events=network_events,
            console_errors=console_errors,
            artifacts=artifacts,
        )
    except Exception as exc:  # noqa: BLE001
        return _finalize_journey(
            journey,
            status="unverified",
            detail=f"oracle_infra_error: {type(exc).__name__}: {exc}",
            proof_usable=False,
            journey_dir=journey_dir,
            page=page,
            network_events=network_events,
            console_errors=console_errors,
            artifacts=artifacts,
        )
    finally:
        for closer in (page, context):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass


def _run_action(
    page: Any,
    action: dict[str, Any],
    pass_model: dict[str, Any],
    network_events: list[_NetworkEvent],
    *,
    timeout_ms: int,
    timeout_error_type: type[Exception],
) -> str:
    selector = _action_selector(action)
    if not selector:
        return f"action {action.get('id') or '<unnamed>'} lacks an executable control selector"
    locator = page.locator(selector).first
    if locator.count() == 0:
        return f"required control absent: {selector}"
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
    except timeout_error_type:
        return f"required control not visible: {selector}"
    try:
        if not bool(locator.is_enabled(timeout=timeout_ms)):
            return f"required control disabled: {selector}"
    except TypeError:
        if not bool(locator.is_enabled()):
            return f"required control disabled: {selector}"

    for fill in _input_specs(action):
        fill_selector = str(fill.get("selector") or "").strip()
        value = str(fill.get("value") or "")
        if not fill_selector:
            return f"action input lacks selector for {action.get('id') or '<unnamed>'}"
        field = page.locator(fill_selector).first
        if field.count() == 0:
            return f"required input absent: {fill_selector}"
        field.fill(value, timeout=timeout_ms)

    before_dom = _dom_fingerprint(page)
    before_network_count = len(network_events)
    expectations = _network_expectations_for_action(action, pass_model)
    observed_expected_network = True
    if expectations:
        first = expectations[0]
        observed_expected_network = False
        try:
            with page.expect_response(
                lambda response: _response_matches(response, first),
                timeout=timeout_ms,
            ):
                locator.click(timeout=timeout_ms)
            observed_expected_network = True
        except timeout_error_type:
            pass
    else:
        locator.click(timeout=timeout_ms)

    for expected in expectations[1:]:
        try:
            page.wait_for_response(
                lambda response: _response_matches(response, expected),
                timeout=timeout_ms,
            )
        except timeout_error_type:
            pass

    post_observables = _list_of_dicts(action.get("success_observables"))
    if not post_observables:
        return f"action {action.get('id') or '<unnamed>'} lacks executable post-action observables"

    dom_failure = ""
    for observable in post_observables:
        dom_failure = _assert_dom_observable(page, observable, timeout_ms=timeout_ms)
        if dom_failure:
            break
    after_dom = _dom_fingerprint(page)
    network_changed = len(network_events) > before_network_count
    dom_changed = after_dom != before_dom
    if not network_changed and not dom_changed:
        return (
            f"click on {selector} produced no observed network/DOM effect"
        )
    if expectations and not observed_expected_network:
        return f"expected network effect was not observed for {selector}"
    if dom_failure:
        return f"post-action observable failed: {dom_failure}"
    return ""


def _finalize_journey(
    journey: dict[str, Any],
    *,
    status: str,
    detail: str,
    proof_usable: bool,
    journey_dir: Path,
    page: Any,
    network_events: list[_NetworkEvent],
    console_errors: list[dict[str, Any]],
    artifacts: list[Path],
) -> dict[str, Any]:
    if page is not None:
        screenshot = journey_dir / "screenshot.png"
        dom = journey_dir / "dom.html"
        try:
            page.screenshot(path=str(screenshot), full_page=True)
            artifacts.append(screenshot)
        except Exception:
            pass
        try:
            write_text_atomic(dom, str(page.content()))
            artifacts.append(dom)
        except Exception:
            pass
    network_log = journey_dir / "network.jsonl"
    console_log = journey_dir / "console-errors.jsonl"
    write_text_atomic(
        network_log,
        "".join(json.dumps(event.to_jsonable(), sort_keys=True) + "\n" for event in network_events),
    )
    write_text_atomic(
        console_log,
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in console_errors),
    )
    artifacts.extend([network_log, console_log])
    result = _result_payload(
        journey,
        status=status,
        detail=detail,
        proof_usable=proof_usable,
        artifact_paths=artifacts,
    )
    verdict_path = journey_dir / "verdict.json"
    result["artifact_paths"].append(str(verdict_path))
    write_json_atomic(verdict_path, result, trailing_newline=True)
    return result


def _result_payload(
    journey: dict[str, Any],
    *,
    status: str,
    detail: str,
    proof_usable: bool,
    artifact_paths: list[Path],
) -> dict[str, Any]:
    return {
        "id": str(journey.get("id") or "<unnamed>").strip() or "<unnamed>",
        "status": status,
        "source": UI_EXECUTOR_SOURCE,
        "proof_usable": proof_usable,
        "detail": detail,
        "artifact_paths": [str(path) for path in artifact_paths],
        "_written_at": iso_timestamp(),
    }


def _non_pass_result(
    journey: dict[str, Any],
    *,
    status: str,
    detail: str,
    proof_usable: bool,
    artifact_paths: list[Path],
) -> dict[str, Any]:
    return _result_payload(
        journey,
        status=status,
        detail=detail,
        proof_usable=proof_usable,
        artifact_paths=artifact_paths,
    )


def _infra_results(journeys: list[dict[str, Any]], detail: str) -> list[dict[str, Any]]:
    return [
        _non_pass_result(
            journey,
            status="unverified",
            detail=detail,
            proof_usable=False,
            artifact_paths=[],
        )
        for journey in journeys
    ]


def _write_run_summary(
    run_dir: Path,
    executor_results: list[dict[str, Any]],
    artifact_paths: list[Path],
    infra_error: str,
) -> Path:
    summary_path = run_dir / "executor-results.json"
    write_json_atomic(
        summary_path,
        {
            "_written_at": iso_timestamp(),
            "source": UI_EXECUTOR_SOURCE,
            "infra_error": infra_error,
            "executor_results": executor_results,
            "artifact_paths": [str(path) for path in artifact_paths],
        },
        trailing_newline=True,
    )
    return summary_path


def _wire_page_events(
    page: Any,
    network_events: list[_NetworkEvent],
    console_errors: list[dict[str, Any]],
) -> None:
    def on_response(response: Any) -> None:
        request = getattr(response, "request", None)
        method = str(getattr(request, "method", "") or "")
        url = str(getattr(response, "url", "") or "")
        network_events.append(
            _NetworkEvent(
                written_at=iso_timestamp(),
                method=method,
                url=url,
                path=urlparse(url).path,
                status=int(getattr(response, "status", 0) or 0),
            )
        )

    def on_request_failed(request: Any) -> None:
        url = str(getattr(request, "url", "") or "")
        failure = getattr(request, "failure", None)
        failure_text = ""
        if callable(failure):
            try:
                raw = failure()
            except Exception:  # noqa: BLE001
                raw = None
            if isinstance(raw, dict):
                failure_text = str(raw.get("errorText") or "")
        network_events.append(
            _NetworkEvent(
                written_at=iso_timestamp(),
                method=str(getattr(request, "method", "") or ""),
                url=url,
                path=urlparse(url).path,
                status=None,
                failure=failure_text,
            )
        )

    def on_console(message: Any) -> None:
        msg_type = str(getattr(message, "type", "") or "")
        if msg_type != "error":
            return
        console_errors.append(
            {
                "_written_at": iso_timestamp(),
                "type": msg_type,
                "text": str(getattr(message, "text", "") or "")[:1000],
            }
        )

    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    page.on("console", on_console)


def _wait_for_ready(page: Any, pass_model: dict[str, Any], *, timeout_ms: int) -> None:
    ready = pass_model.get("ready_policy")
    ready_policy = ready if isinstance(ready, dict) else {}
    wait_for = str(ready_policy.get("wait_for") or "interactive").strip().lower()
    if wait_for == "networkidle":
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    else:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        page.wait_for_function(
            "() => document.readyState === 'interactive' || document.readyState === 'complete'",
            timeout=timeout_ms,
        )
    selector = str(ready_policy.get("selector") or ready_policy.get("wait_for_selector") or "").strip()
    if selector:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
    text = str(ready_policy.get("text") or ready_policy.get("wait_for_text") or "").strip()
    if text:
        page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=timeout_ms)


def _page_blank_or_stuck(page: Any) -> bool:
    snapshot = page.evaluate(
        """() => {
            const text = (document.body?.innerText || '').trim();
            const controls = document.querySelectorAll(
              'button, input, select, textarea, a[href], [role="button"]'
            ).length;
            return { text, controls };
        }"""
    )
    if not isinstance(snapshot, dict):
        return True
    text = str(snapshot.get("text") or "").strip()
    controls = int(snapshot.get("controls") or 0)
    if not text and controls == 0:
        return True
    skeletonish = bool(
        re.fullmatch(r"(loading|loading[.\s]*|loading .*)", text, re.IGNORECASE)
    )
    return skeletonish and controls == 0


def _assert_dom_observable(page: Any, observable: dict[str, Any], *, timeout_ms: int) -> str:
    selector = str(
        observable.get("selector")
        or observable.get("target_selector")
        or observable.get("testid_selector")
        or ""
    ).strip()
    text = str(
        observable.get("text")
        or observable.get("expected_text")
        or observable.get("visible_text")
        or ""
    ).strip()
    if not text:
        ui_effect = str(observable.get("ui_effect") or "").strip()
        text = _extract_assertable_text(ui_effect)
    if selector:
        locator = page.locator(selector).first
        if locator.count() == 0:
            return f"selector not found: {selector}"
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            return f"selector not visible: {selector}: {type(exc).__name__}: {exc}"
        if text:
            content = str(locator.text_content(timeout=timeout_ms) or "")
            if text not in content:
                return f"text {text!r} not visible inside {selector}"
        return ""
    if text:
        try:
            page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"text not visible: {text!r}: {type(exc).__name__}: {exc}"
    return "observable lacks executable DOM assertion"


def _response_matches(response: Any, expected: dict[str, Any]) -> bool:
    request = getattr(response, "request", None)
    method = str(expected.get("method") or "").upper()
    path = str(expected.get("path") or "")
    status = expected.get("status")
    if method and str(getattr(request, "method", "") or "").upper() != method:
        return False
    if path and urlparse(str(getattr(response, "url", "") or "")).path != path:
        return False
    if status not in (None, ""):
        try:
            if int(getattr(response, "status", 0) or 0) != int(status):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _network_expectations_for_action(
    action: dict[str, Any],
    pass_model: dict[str, Any],
) -> list[dict[str, Any]]:
    del pass_model
    expectations = _list_of_dicts(action.get("network_expectations"))
    for observable in _list_of_dicts(action.get("success_observables")):
        if str(observable.get("kind") or "") != "network_and_ui_effect":
            continue
        if observable.get("method") and observable.get("path"):
            expectations.append(
                {
                    "method": observable.get("method"),
                    "path": observable.get("path"),
                    "status": observable.get("status"),
                }
            )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in expectations:
        key = (
            str(item.get("method") or "").upper(),
            str(item.get("path") or ""),
            str(item.get("status") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _input_specs(action: dict[str, Any]) -> list[dict[str, Any]]:
    raw = action.get("inputs") or action.get("fields") or action.get("fill")
    if isinstance(raw, dict):
        return [{"selector": selector, "value": value} for selector, value in raw.items()]
    return _list_of_dicts(raw)


def _action_selector(action: dict[str, Any]) -> str:
    return str(
        action.get("selector")
        or action.get("control_selector")
        or action.get("click_selector")
        or action.get("button_selector")
        or action.get("target_selector")
        or ""
    ).strip()


def _ready_route(journey: dict[str, Any], pass_model: dict[str, Any]) -> str:
    ready = pass_model.get("ready_policy")
    ready_policy = ready if isinstance(ready, dict) else {}
    route = str(ready_policy.get("route") or journey.get("entry_route") or "/").strip()
    if not route.startswith("/"):
        route = "/" + route
    return route


def _dom_fingerprint(page: Any) -> str:
    text = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _extract_assertable_text(value: str) -> str:
    text = value.strip()
    quoted = re.search(r"['\"]([^'\"]{2,})['\"]", text)
    if quoted:
        return quoted.group(1)
    return ""


def _artifact_run_name(started: str) -> str:
    return started.replace(":", "").replace("-", "").replace("T", "-").replace("Z", "")


def _git_diff_dirty(project_dir: Path) -> str:
    if not (project_dir / ".git").exists():
        return ""
    try:
        proc = subprocess.run(
            ["git", "diff", "--", "."],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"git diff failed: {type(exc).__name__}: {exc}"
    return proc.stdout or proc.stderr or ""
