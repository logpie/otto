"""Controller-run UI behavior journey executor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from otto.journey_executor_common import (
    artifact_run_name,
    executor_non_pass_result,
    executor_result_payload,
)
from otto.journey_contracts import VerificationContractError, validate_ui_pass_model
from otto.observability import iso_timestamp, write_json_atomic, write_text_atomic
from otto.safe_slug import safe_slug

UI_EXECUTOR_SOURCE = "ui_executor"
ENFORCED_UI_OBSERVABLE_LOCATOR_KEYS = frozenset({
    "selector",
    "target_selector",
    "testid_selector",
    "testid",
    "data_testid",
    "data-testid",
    "role",
    "name",
    "accessible_name",
    "label",
    "text",
    "expected_text",
    "visible_text",
})
_OBSERVABLE_SELECTOR_KEYS = ("selector", "target_selector", "testid_selector")
_OBSERVABLE_ROLE_KEYS = ("role",)
_OBSERVABLE_NAME_KEYS = ("name", "accessible_name")
_OBSERVABLE_LABEL_KEYS = ("label",)
_OBSERVABLE_TEXT_KEYS = ("text", "expected_text", "visible_text", "name", "accessible_name")


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


@dataclass(frozen=True)
class _DOMObservableState:
    count: int
    signatures: tuple[str, ...]
    scope_found: bool
    scope_label: str
    locator_label: str = "<missing locator>"


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
    if _inside_running_event_loop():
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="otto-ui-journey") as executor:
            return executor.submit(
                _run_ui_journey_executor_sync,
                journeys=journeys,
                base_url=base_url,
                project_dir=project_dir,
                clean_project_dir=clean_project_dir,
                artifact_dir=artifact_dir,
                timeout_s=timeout_s,
            ).result()
    return _run_ui_journey_executor_sync(
        journeys=journeys,
        base_url=base_url,
        project_dir=project_dir,
        clean_project_dir=clean_project_dir,
        artifact_dir=artifact_dir,
        timeout_s=timeout_s,
    )


def _inside_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run_ui_journey_executor_sync(
    *,
    journeys: list[dict[str, Any]],
    base_url: str,
    project_dir: Path,
    clean_project_dir: Path,
    artifact_dir: Path,
    timeout_s: int,
) -> UIJourneyExecutorRun:
    """Run the synchronous Playwright driver outside any caller event loop."""

    del clean_project_dir  # The browser talks to the temp deployment by URL.
    started = iso_timestamp()
    run_dir = artifact_dir / artifact_run_name(started)
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
    locator, control = _action_locator(page, action)
    if locator is None:
        return f"action {action.get('id') or '<unnamed>'} lacks an executable control locator"
    if locator.count() == 0:
        # Literal selector missed — try the deterministic semantic
        # fallback (robust to accessible-name/label drift). Literal-first
        # means zero behavior change when the label already matches.
        sloc, slabel = _semantic_locator(page, action)
        if sloc is not None and sloc.count() > 0:
            locator, control = sloc, slabel
        else:
            return f"required control absent: {control}"
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
    except timeout_error_type:
        return f"required control not visible: {control}"
    try:
        if not bool(locator.is_enabled(timeout=timeout_ms)):
            return f"required control disabled: {control}"
    except TypeError:
        if not bool(locator.is_enabled()):
            return f"required control disabled: {control}"

    for fill in _input_specs(action):
        field, field_label = _input_locator(page, fill)
        value = str(fill.get("value") or "")
        if field is None:
            return f"action input lacks executable locator for {action.get('id') or '<unnamed>'}"
        if field.count() == 0:
            # Literal-miss → deterministic semantic fallback (handles the
            # unassociated-<label> case that broke resume16k journey 1).
            sfield, sflabel = _semantic_locator(page, fill)
            if sfield is not None and sfield.count() > 0:
                field, field_label = sfield, sflabel
            else:
                return f"required input absent: {field_label}"
        field.fill(value, timeout=timeout_ms)

    post_observables = _list_of_dicts(action.get("success_observables"))
    if not post_observables:
        return f"action {action.get('id') or '<unnamed>'} lacks executable post-action observables"
    before_observable_states = [
        _dom_observable_state(page, observable)
        for observable in post_observables
    ]

    before_dom = _dom_fingerprint(page)
    before_network_count = len(network_events)
    expectations = _network_expectations_for_action(action, pass_model)
    missing_network: list[str] = []
    if expectations:
        first = expectations[0]
        try:
            with page.expect_response(
                lambda response: _response_matches(response, first),
                timeout=timeout_ms,
            ):
                locator.click(timeout=timeout_ms)
        except timeout_error_type:
            missing_network.append(_network_expectation_label(first))
    else:
        locator.click(timeout=timeout_ms)

    for expected in expectations[1:]:
        if not _wait_for_network_event(page, network_events, expected, timeout_ms=timeout_ms):
            missing_network.append(_network_expectation_label(expected))

    dom_failure = ""
    for index, observable in enumerate(post_observables):
        dom_failure = _assert_dom_observable(
            page,
            observable,
            timeout_ms=timeout_ms,
            before_state=before_observable_states[index],
            require_delta=True,
        )
        if dom_failure:
            break
    after_dom = _dom_fingerprint(page)
    network_changed = len(network_events) > before_network_count
    dom_changed = after_dom != before_dom
    if not network_changed and not dom_changed:
        return (
            f"click on {control} produced no observed network/DOM effect"
        )
    if missing_network:
        return f"expected network effects were not observed for {control}: {', '.join(missing_network)}"
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
    return executor_result_payload(
        journey,
        source=UI_EXECUTOR_SOURCE,
        status=status,
        detail=detail,
        proof_usable=proof_usable,
        artifact_paths=artifact_paths,
    )


def _non_pass_result(
    journey: dict[str, Any],
    *,
    status: str,
    detail: str,
    proof_usable: bool,
    artifact_paths: list[Path],
) -> dict[str, Any]:
    return executor_non_pass_result(
        journey,
        source=UI_EXECUTOR_SOURCE,
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
    locator, _label = _generic_locator(page, ready_policy, text_keys=("text", "wait_for_text"))
    if locator is not None:
        locator.wait_for(state="visible", timeout=timeout_ms)


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


def _assert_dom_observable(
    page: Any,
    observable: dict[str, Any],
    *,
    timeout_ms: int,
    before_state: _DOMObservableState | None = None,
    require_delta: bool = False,
) -> str:
    selector = _explicit_selector(observable)
    text = str(
        observable.get("text")
        or observable.get("expected_text")
        or observable.get("visible_text")
        or ""
    ).strip()
    if not text:
        ui_effect = str(observable.get("ui_effect") or "").strip()
        text = _extract_assertable_text(ui_effect)
    exact = _exact_match(observable)
    if _has_dom_observable_locator(observable, text=text):
        deadline = time.monotonic() + (timeout_ms / 1000)
        last_state = _dom_observable_state(page, observable, text=text)
        while time.monotonic() < deadline:
            if _observable_state_satisfies(last_state, before_state, require_delta=require_delta):
                return ""
            try:
                page.wait_for_timeout(50)
            except Exception:
                time.sleep(0.05)
            last_state = _dom_observable_state(page, observable, text=text)
        if not last_state.scope_found:
            return f"scope not found: {last_state.scope_label}"
        if text and last_state.count == 0:
            return f"text {text!r} not visible inside {last_state.scope_label}"
        if selector and last_state.count == 0:
            return f"selector not visible inside {last_state.scope_label}: {selector}"
        if last_state.count == 0:
            return (
                f"locator not visible inside {last_state.scope_label}: "
                f"{last_state.locator_label}"
            )
        if require_delta:
            return f"no new scoped observable appeared inside {last_state.scope_label}"

    if selector:
        root, scope_label = _scope_root(page, observable)
        if root is None:
            return f"scope not found: {scope_label}"
        locator = root.locator(selector).first
        if locator.count() == 0:
            return f"selector not found: {selector}"
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            return f"selector not visible: {selector}: {type(exc).__name__}: {exc}"
        if text:
            content = str(locator.text_content(timeout=timeout_ms) or "")
            if not _text_matches(content, text, exact=exact):
                return f"text {text!r} not visible inside {selector}"
        return ""
    root, scope_label = _scope_root(page, observable)
    if root is None:
        return f"scope not found: {scope_label}"
    locator, label = _generic_locator(
        root,
        observable,
        text_keys=("text", "expected_text", "visible_text", "name", "accessible_name"),
    )
    if locator is not None:
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"locator not visible: {label}: {type(exc).__name__}: {exc}"
    if text:
        try:
            root.get_by_text(text, exact=exact).first.wait_for(state="visible", timeout=timeout_ms)
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


def _network_event_matches(event: _NetworkEvent, expected: dict[str, Any]) -> bool:
    method = str(expected.get("method") or "").upper()
    path = str(expected.get("path") or "")
    status = expected.get("status")
    if method and event.method.upper() != method:
        return False
    if path and event.path != path:
        return False
    if status not in (None, ""):
        try:
            if event.status != int(status):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _wait_for_network_event(
    page: Any,
    network_events: list[_NetworkEvent],
    expected: dict[str, Any],
    *,
    timeout_ms: int,
) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if any(_network_event_matches(event, expected) for event in network_events):
            return True
        try:
            page.wait_for_timeout(50)
        except Exception:
            time.sleep(0.05)
    return any(_network_event_matches(event, expected) for event in network_events)


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


def _network_expectation_label(expected: dict[str, Any]) -> str:
    method = str(expected.get("method") or "*").upper()
    path = str(expected.get("path") or "*")
    status = str(expected.get("status") or "*")
    return f"{method} {path} status={status}"


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _input_specs(action: dict[str, Any]) -> list[dict[str, Any]]:
    raw = action.get("inputs") or action.get("fields") or action.get("fill")
    if isinstance(raw, dict):
        return [{"selector": selector, "value": value} for selector, value in raw.items()]
    return _list_of_dicts(raw)


_SEMANTIC_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Role compatibility for semantic resolution. The journey's intended role
# and the built control's actual role need not be identical — a "button"
# intent legitimately resolves to a link styled as a button, a "textbox"
# to a searchbox/combobox, etc. Missing role on either side is neutral.
_SEMANTIC_ROLE_SYNONYMS: dict[str, set[str]] = {
    "button": {"button", "link", "menuitem", "tab"},
    "link": {"link", "button"},
    "textbox": {"textbox", "searchbox", "combobox"},
    "searchbox": {"searchbox", "textbox", "combobox"},
    "combobox": {"combobox", "textbox", "listbox"},
    "checkbox": {"checkbox", "switch"},
    "switch": {"switch", "checkbox"},
}


def _semantic_tokens(text: Any) -> list[str]:
    return _SEMANTIC_TOKEN_RE.findall(str(text or "").lower())


def _semantic_role_compatible(intent_role: str, cand_role: str) -> bool:
    ir = (intent_role or "").strip().lower()
    cr = (cand_role or "").strip().lower()
    if not ir or not cr:
        return True
    if ir == cr:
        return True
    return cr in _SEMANTIC_ROLE_SYNONYMS.get(ir, {ir})


def _semantic_match(
    intent: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    min_recall: float = 0.6,
    margin: float = 0.15,
) -> dict[str, Any] | None:
    """Deterministically resolve a journey step's intended control to the
    best-matching page element, robust to accessible-name/label drift.

    Phase 1 of the Hybrid journey resolver (see
    plan-hybrid-journey-resolver.md). The brittle root cause: compile-spec
    emits literal Playwright selectors; independent leaf agents build the
    UI with their own reasonable wording ("Create workspace" vs the
    journey's "Create your workspace"; aria-label "Search query" vs
    "Search"; an unassociated ``<label>Name</label>``). A strict string
    match then fails a functionally-correct product.

    ``intent`` = the step's {role, name|label|text} treated as fuzzy
    INTENT, not exact match. ``candidates`` = page elements, each
    {role, name, label, text, placeholder, value, aria_label, selector}.

    Scores by token-recall of the intent phrase within each candidate's
    accessible text, gated by role compatibility. Returns the best
    candidate ONLY if it clears ``min_recall`` AND is unambiguous (beats
    the runner-up by ``margin``); otherwise None — fail CLOSED, never
    guess. This keeps the no-false-pass invariant: a wrong/ambiguous
    resolution yields "absent" (a real failure), never a fake pass; and
    the post-action assertion stays deterministic regardless.
    """
    want = str(
        intent.get("name") or intent.get("label") or intent.get("text") or ""
    ).strip()
    want_toks = _semantic_tokens(want)
    if not want_toks or not candidates:
        return None
    want_set = set(want_toks)
    want_phrase = " ".join(want_toks)
    irole = str(intent.get("role") or "").strip().lower()
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for cand in candidates:
        if not _semantic_role_compatible(irole, str(cand.get("role") or "")):
            continue
        cand_text = " ".join(
            str(cand.get(k) or "")
            for k in ("name", "label", "text", "placeholder", "value", "aria_label")
        )
        cand_toks = _semantic_tokens(cand_text)
        cand_set = set(cand_toks)
        if not cand_set:
            continue
        recall = len(want_set & cand_set) / len(want_set)
        # Full-phrase containment is a strong specificity signal but only
        # ever co-occurs with recall == 1.0, so it sharpens the margin /
        # tie-break without ever rescuing a low-recall match.
        bonus = 0.25 if want_phrase and want_phrase in " ".join(cand_toks) else 0.0
        scored.append((recall + bonus, len(cand_set), cand))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    best_score, _, best = scored[0]
    if best_score < min_recall:
        return None
    if len(scored) > 1 and best_score - scored[1][0] < margin:
        return None  # ambiguous — fail closed, do not guess
    return best


# DOM snapshot of interactive controls + their accessible names. Crucially
# this also recovers labels for inputs whose <label> is NOT programmatically
# associated (no for=/id, not wrapping) by walking preceding siblings — the
# exact resume16k journey-1 killer (`required input absent: label='Name'`).
_CANDIDATE_JS = r"""
() => {
  const SEL = 'button,a[href],input,textarea,select,[role=button],[role=link],[role=textbox],[role=searchbox],[role=combobox],[role=menuitem],[role=tab],[role=checkbox],[role=switch],[contenteditable=""],[contenteditable=true]';
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^a-zA-Z0-9_-]/g,'\\$&');
  const cssPath = (el) => {
    const td = el.getAttribute && el.getAttribute('data-testid');
    if (td) return `[data-testid="${td}"]`;
    if (el.id) return `#${esc(el.id)}`;
    const parts = []; let n = el;
    while (n && n.nodeType === 1 && parts.length < 6) {
      let s = n.nodeName.toLowerCase();
      const p = n.parentElement;
      if (p) { const sib = Array.from(p.children).filter(c => c.nodeName === n.nodeName);
        if (sib.length > 1) s += `:nth-of-type(${sib.indexOf(n)+1})`; }
      parts.unshift(s); n = n.parentElement;
    }
    return parts.join(' > ');
  };
  const roleOf = (el) => {
    const r = el.getAttribute && el.getAttribute('role'); if (r) return r;
    const t = el.nodeName.toLowerCase();
    if (t === 'button') return 'button';
    if (t === 'a') return 'link';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') { const ty = (el.getAttribute('type')||'text').toLowerCase();
      if (ty === 'search') return 'searchbox';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (['button','submit','reset','image'].includes(ty)) return 'button';
      return 'textbox'; }
    return '';
  };
  const labelFor = (el) => {
    const lb = el.getAttribute && el.getAttribute('aria-labelledby');
    if (lb) { const r = document.getElementById(lb); if (r) return (r.textContent||'').trim(); }
    if (el.id) { const l = document.querySelector(`label[for="${esc(el.id)}"]`); if (l) return (l.textContent||'').trim(); }
    let p = el.parentElement;
    while (p) { if (p.nodeName === 'LABEL') return (p.textContent||'').trim(); p = p.parentElement; }
    let prev = el.previousElementSibling;
    while (prev) {
      if (['LABEL','SPAN','DIV','P','LEGEND'].includes(prev.nodeName)) {
        const t = (prev.textContent||'').trim();
        if (t && t.length <= 60) return t;
      }
      prev = prev.previousElementSibling;
    }
    return '';
  };
  const out = [];
  for (const el of Array.from(document.querySelectorAll(SEL))) {
    if (out.length >= 250) break;
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    if (rect && rect.width === 0 && rect.height === 0) continue;
    const ty = (el.getAttribute && (el.getAttribute('type')||'')).toLowerCase();
    const submitVal = (el.nodeName === 'INPUT' && ['button','submit','reset'].includes(ty)) ? (el.value||'') : '';
    out.push({
      role: roleOf(el),
      name: ((el.getAttribute && el.getAttribute('aria-label')) || submitVal || (el.textContent||'').trim()).slice(0,80),
      label: labelFor(el),
      text: (el.textContent||'').trim().slice(0,80),
      placeholder: (el.getAttribute && el.getAttribute('placeholder')) || '',
      value: (el.value !== undefined && el.value !== null ? String(el.value) : '').slice(0,80),
      aria_label: (el.getAttribute && el.getAttribute('aria-label')) || '',
      selector: cssPath(el),
    });
  }
  return out;
}
"""


def _intent_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The journey step's literal selector strings, reinterpreted as fuzzy
    INTENT for semantic resolution (Hybrid resolver Phase 1)."""
    pick = lambda *keys: next(  # noqa: E731
        (str(spec[k]).strip() for k in keys if str(spec.get(k) or "").strip()),
        "",
    )
    return {
        "role": pick("role", "control_role", "field_role", "input_role"),
        "name": pick(
            "name", "accessible_name", "control_name", "button_name", "field_name", "input_name"
        ),
        "label": pick("label", "control_label", "field_label", "input_label"),
        "text": pick("text", "visible_text", "button_text", "expected_text"),
    }


def _semantic_locator(
    page: Any, spec: dict[str, Any]
) -> tuple[Any | None, str]:
    """Literal-miss fallback: snapshot the live page's interactive
    controls and deterministically resolve the step's intent to the best
    one (robust to accessible-name/label drift). Returns
    (locator, "semantic:<selector>") or (None, "")."""
    intent = _intent_from_spec(spec)
    if not (intent["name"] or intent["label"] or intent["text"]):
        return None, ""
    try:
        cands = page.evaluate(_CANDIDATE_JS)
    except Exception:  # noqa: BLE001 — never let resolution crash the journey
        return None, ""
    if not isinstance(cands, list) or not cands:
        return None, ""
    match = _semantic_match(intent, [c for c in cands if isinstance(c, dict)])
    if not match:
        return None, ""
    selector = str(match.get("selector") or "").strip()
    if not selector:
        return None, ""
    try:
        return page.locator(selector).first, f"semantic:{selector}"
    except Exception:  # noqa: BLE001
        return None, ""


def _action_locator(page: Any, action: dict[str, Any]) -> tuple[Any | None, str]:
    locator, label = _generic_locator(
        page,
        action,
        selector_keys=(
            "selector",
            "control_selector",
            "click_selector",
            "button_selector",
            "target_selector",
        ),
        role_keys=("role", "control_role"),
        name_keys=("name", "accessible_name", "control_name", "button_name"),
        label_keys=("label", "control_label"),
        text_keys=("text", "visible_text", "button_text"),
    )
    if locator is not None:
        return locator, label
    name = str(action.get("name") or action.get("accessible_name") or "").strip()
    if name:
        return page.get_by_role("button", name=name).first, f"role=button name={name!r}"
    return None, "<missing control locator>"


def _input_locator(page: Any, spec: dict[str, Any]) -> tuple[Any | None, str]:
    return _generic_locator(
        page,
        spec,
        selector_keys=("selector", "field_selector", "input_selector", "target_selector"),
        role_keys=("role", "field_role", "input_role"),
        name_keys=("name", "accessible_name", "field_name", "input_name"),
        label_keys=("label", "field_label", "input_label"),
        text_keys=("placeholder", "text"),
    )


def _generic_locator(
    page: Any,
    spec: dict[str, Any],
    *,
    selector_keys: tuple[str, ...] = ("selector", "target_selector", "testid_selector"),
    role_keys: tuple[str, ...] = ("role",),
    name_keys: tuple[str, ...] = ("name", "accessible_name"),
    label_keys: tuple[str, ...] = ("label",),
    text_keys: tuple[str, ...] = ("text", "visible_text", "expected_text"),
) -> tuple[Any | None, str]:
    selector = _explicit_selector(spec, selector_keys=selector_keys)
    if selector:
        return page.locator(selector).first, selector
    role = _first_text(spec, role_keys)
    name = _first_text(spec, name_keys)
    exact = _exact_match(spec)
    if role:
        kwargs: dict[str, Any] = {"exact": exact}
        if name:
            kwargs["name"] = name
        return page.get_by_role(role, **kwargs).first, (
            f"role={role!r} name={name!r}" if name else f"role={role!r}"
        )
    label = _first_text(spec, label_keys)
    if label:
        return page.get_by_label(label, exact=exact).first, f"label={label!r}"
    text = _first_text(spec, text_keys)
    if text:
        return page.get_by_text(text, exact=exact).first, f"text={text!r}"
    return None, "<missing locator>"


def _observable_locator(
    page: Any,
    spec: dict[str, Any],
    *,
    text: str | None = None,
    first: bool = False,
) -> tuple[Any | None, str]:
    selector = _explicit_selector(spec, selector_keys=_OBSERVABLE_SELECTOR_KEYS)
    if selector:
        locator = page.locator(selector)
        return (locator.first if first else locator), selector
    role = _first_text(spec, _OBSERVABLE_ROLE_KEYS)
    name = _first_text(spec, _OBSERVABLE_NAME_KEYS)
    exact = _exact_match(spec)
    if role:
        kwargs: dict[str, Any] = {"exact": exact}
        if name:
            kwargs["name"] = name
        locator = page.get_by_role(role, **kwargs)
        label = f"role={role!r} name={name!r}" if name else f"role={role!r}"
        return (locator.first if first else locator), label
    label = _first_text(spec, _OBSERVABLE_LABEL_KEYS)
    if label:
        locator = page.get_by_label(label, exact=exact)
        return (locator.first if first else locator), f"label={label!r}"
    visible_text = str(text or "").strip() or _first_text(spec, _OBSERVABLE_TEXT_KEYS)
    if visible_text:
        locator = page.get_by_text(visible_text, exact=exact)
        return (locator.first if first else locator), f"text={visible_text!r}"
    return None, "<missing locator>"


def _has_dom_observable_locator(spec: dict[str, Any], *, text: str) -> bool:
    return bool(
        _explicit_selector(spec, selector_keys=_OBSERVABLE_SELECTOR_KEYS)
        or _first_text(spec, _OBSERVABLE_ROLE_KEYS)
        or _first_text(spec, _OBSERVABLE_NAME_KEYS)
        or _first_text(spec, _OBSERVABLE_LABEL_KEYS)
        or text
    )


def _explicit_selector(
    spec: dict[str, Any],
    *,
    selector_keys: tuple[str, ...] = ("selector", "target_selector", "testid_selector"),
) -> str:
    selector = _first_text(spec, selector_keys)
    if selector:
        return selector
    testid = _first_text(spec, ("testid", "data_testid", "data-testid"))
    if testid:
        return f"[data-testid='{_css_attr_value(testid)}']"
    return ""


def _scope_selector(spec: dict[str, Any]) -> str:
    selector = _first_text(
        spec,
        (
            "container_selector",
            "scope_selector",
            "within_selector",
            "region_selector",
        ),
    )
    if selector:
        return selector
    testid = _first_text(spec, ("container_testid", "scope_testid", "region_testid"))
    if testid:
        return f"[data-testid='{_css_attr_value(testid)}']"
    return ""


def _scope_root(page: Any, spec: dict[str, Any]) -> tuple[Any | None, str]:
    selector = _scope_selector(spec)
    if not selector:
        return page, "page"
    locator = page.locator(selector).first
    if locator.count() == 0:
        return None, selector
    return locator, selector


def _exact_match(spec: dict[str, Any]) -> bool:
    return spec.get("exact") is not False


def _first_text(spec: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(spec.get(key) or "").strip()
        if value:
            return value
    return ""


def _css_attr_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


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


def _dom_observable_state(
    page: Any,
    observable: dict[str, Any],
    *,
    text: str | None = None,
) -> _DOMObservableState:
    scope_selector = _scope_selector(observable)
    expected_text = str(
        text
        if text is not None
        else observable.get("text")
        or observable.get("expected_text")
        or observable.get("visible_text")
        or ""
    ).strip()
    if not expected_text:
        expected_text = _extract_assertable_text(str(observable.get("ui_effect") or ""))
    exact = _exact_match(observable)
    scope_label = scope_selector or "page"
    root, _root_label = _scope_root(page, observable)
    if root is None:
        return _DOMObservableState(
            count=0,
            signatures=(),
            scope_found=False,
            scope_label=scope_label,
        )
    locator, locator_label = _observable_locator(root, observable, text=expected_text)
    if locator is None:
        return _DOMObservableState(
            count=0,
            signatures=(),
            scope_found=True,
            scope_label=scope_label,
            locator_label=locator_label,
        )
    raw = locator.evaluate_all(
        """(elements, {text, exact}) => {
          const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
          const matchesText = (value) => {
            if (!text) return true;
            const normalized = normalize(value);
            return exact ? normalized === text : normalized.includes(text);
          };
          const visible = (el) => {
            if (!el || !(el instanceof Element)) return false;
            const style = window.getComputedStyle(el);
            if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };
          const textFor = (el) => (
            el.innerText || el.textContent || el.getAttribute('aria-label') ||
            el.getAttribute('title') || el.getAttribute('alt') || el.value || ''
          );
          const signatures = elements
            .filter((el) => visible(el) && matchesText(textFor(el)))
            .map((el) => {
              const attrs = [
                  'id', 'class', 'role', 'aria-label', 'aria-labelledby',
                  'aria-describedby', 'data-testid', 'name', 'type'
                ]
                .map((name) => `${name}=${el.getAttribute(name) || ''}`)
                .join('|');
              const value = 'value' in el ? String(el.value || '') : '';
              const checked = 'checked' in el ? String(Boolean(el.checked)) : '';
              const disabled = 'disabled' in el ? String(Boolean(el.disabled)) : '';
              return `${el.tagName}|${attrs}|value=${value}|checked=${checked}|disabled=${disabled}|${normalize(textFor(el))}|${String(el.outerHTML || '').slice(0, 500)}`;
            });
          return { count: signatures.length, signatures };
        }""",
        {
            "text": expected_text,
            "exact": exact,
        },
    )
    if not isinstance(raw, dict):
        return _DOMObservableState(
            count=0,
            signatures=(),
            scope_found=False,
            scope_label=scope_label,
            locator_label=locator_label,
        )
    raw_signatures = raw.get("signatures")
    signatures = raw_signatures if isinstance(raw_signatures, list) else []
    return _DOMObservableState(
        count=int(raw.get("count") or 0),
        signatures=tuple(str(item) for item in signatures),
        scope_found=True,
        scope_label=scope_label,
        locator_label=locator_label,
    )


def _observable_state_satisfies(
    state: _DOMObservableState,
    before_state: _DOMObservableState | None,
    *,
    require_delta: bool,
) -> bool:
    if state.count <= 0:
        return False
    if not require_delta or before_state is None:
        return True
    if state.count > before_state.count:
        return True
    before_signatures = set(before_state.signatures)
    return any(signature not in before_signatures for signature in state.signatures)


def _text_matches(content: str, expected: str, *, exact: bool) -> bool:
    normalized_content = " ".join(content.split())
    normalized_expected = " ".join(expected.split())
    if exact:
        return normalized_content == normalized_expected
    return normalized_expected in normalized_content


def _extract_assertable_text(value: str) -> str:
    text = value.strip()
    quoted = re.search(r"['\"]([^'\"]{2,})['\"]", text)
    if quoted:
        return quoted.group(1)
    return ""


def _git_diff_dirty(project_dir: Path) -> str:
    if not (project_dir / ".git").exists():
        return ""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"git diff failed: {type(exc).__name__}: {exc}"
    return proc.stdout or proc.stderr or ""
