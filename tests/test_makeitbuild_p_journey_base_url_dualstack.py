"""Regression: the Playwright journey base URL must be reachable when the
dev server binds IPv6/localhost, not a hardcoded IPv4 127.0.0.1.

resume16l (with the P1a+P1b semantic resolver) proved the locator-drift
class is fixed — but then ALL 4 journeys went `unverified ::
oracle_infra_error: Page.goto net::ERR_CONNECTION_REFUSED at
http://127.0.0.1:<ephemeral>/...`. The dual-stack START probe
(809033823) lets clean_deploy's start step PASS via ::1 when start.sh
omits --host 127.0.0.1 (vite -> localhost -> IPv6-first on macOS), but
the journey executor navigated the IPv4-only base URL and could never
connect. `_frontend_base_url` now yields `http://localhost:<port>`;
Chromium resolves localhost dual-stack and connects whichever family
the server bound (how a real browser reaches it). Not gate-weakening:
an unreachable server still fails; this only stops mis-attributing a
running IPv6 server as unreachable.
"""

from __future__ import annotations

from otto.v5_clean_verify import _frontend_base_url


def test_named_frontend_port_uses_localhost_not_ipv4() -> None:
    url = _frontend_base_url([("API_PORT", 8000), ("FRONTEND_PORT", 52501)], [8000, 52501])
    assert url == "http://localhost:52501"
    assert "127.0.0.1" not in url  # IPv4-only hardcode would miss an ::1 bind


def test_fallback_first_port_uses_localhost() -> None:
    url = _frontend_base_url([(None, 4321)], [4321])
    assert url == "http://localhost:4321"
    assert "127.0.0.1" not in url


def test_no_ports_returns_empty() -> None:
    assert _frontend_base_url([], []) == ""


def test_fe_marker_variants_resolve_localhost() -> None:
    for env in ("WEB_PORT", "UI_PORT", "CLIENT_PORT", "FE_PORT"):
        url = _frontend_base_url([(env, 6000), ("API_PORT", 8000)], [6000, 8000])
        assert url == "http://localhost:6000", env
