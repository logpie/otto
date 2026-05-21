"""Regression: ephemeral port env values must not leak into agent-facing
repair packets. The orchestrator allocates fresh ephemeral ports per
clean-deploy probe (51751, 52351, etc.); the agent shouldn't see those
specific numbers because (a) they have no semantic meaning to the agent's
repair work, and (b) the agent might pattern-match on them incorrectly
("the probe used port 51751, so I'll use 51750" — broken assumption).

Scrub policy: env KEY is kept (`API_PORT`, `FRONTEND_PORT`, etc — agent
needs to know the contract declares these), VALUE is replaced with a
sentinel. Applied recursively across the packet so per-step env's also
get scrubbed.
"""

from __future__ import annotations

from otto.v5_preflight_repair import (
    _is_ephemeral_port_env_key,
    _scrub_ephemeral_ports_in_packet,
)


def test_scrubs_top_level_env_port_values() -> None:
    payload = {
        "env": {
            "API_PORT": "51751",
            "FRONTEND_PORT": "51752",
            "PATH": "/usr/bin:/bin",
            "HOME": "/Users/yuxuan",
        }
    }
    _scrub_ephemeral_ports_in_packet(payload)
    assert payload["env"]["API_PORT"] == "<ephemeral, scrubbed>"
    assert payload["env"]["FRONTEND_PORT"] == "<ephemeral, scrubbed>"
    # Non-port env vars untouched:
    assert payload["env"]["PATH"] == "/usr/bin:/bin"
    assert payload["env"]["HOME"] == "/Users/yuxuan"


def test_scrubs_nested_step_envs() -> None:
    payload = {
        "steps": [
            {"id": "build", "env": {"BACKEND_PORT": "51892"}},
            {
                "id": "verify",
                "env": {"DB_PORT": "51893", "OTHER": "ok"},
                "command": ["uvicorn", "app.main:app", "--port", "51892"],
            },
        ]
    }
    _scrub_ephemeral_ports_in_packet(payload)
    assert payload["steps"][0]["env"]["BACKEND_PORT"] == "<ephemeral, scrubbed>"
    assert payload["steps"][1]["env"]["DB_PORT"] == "<ephemeral, scrubbed>"
    assert payload["steps"][1]["env"]["OTHER"] == "ok"
    # We only scrub env-dict values, not command-array literals;
    # the command is the oracle's invocation, which the agent may
    # legitimately need to interpret. Scrubbing argv values would
    # require deeper structural understanding.
    assert "51892" in str(payload["steps"][1]["command"])


def test_preserves_env_keys() -> None:
    """Agent needs to know that `API_PORT` is a parameterized contract
    variable; just not the specific ephemeral value the probe picked."""
    payload = {"env": {"API_PORT": "51751"}}
    _scrub_ephemeral_ports_in_packet(payload)
    assert "API_PORT" in payload["env"]  # key preserved
    assert payload["env"]["API_PORT"] != "51751"  # value scrubbed


def test_is_ephemeral_port_env_key_recognizes_common_shapes() -> None:
    assert _is_ephemeral_port_env_key("API_PORT")
    assert _is_ephemeral_port_env_key("FRONTEND_PORT")
    assert _is_ephemeral_port_env_key("BACKEND_PORT")
    assert _is_ephemeral_port_env_key("DB_PORT")
    assert _is_ephemeral_port_env_key("PORT")
    assert _is_ephemeral_port_env_key("PORT_API")
    assert _is_ephemeral_port_env_key("port_frontend")  # case-insensitive
    # Not port-shaped:
    assert not _is_ephemeral_port_env_key("PATH")
    assert not _is_ephemeral_port_env_key("HOME")
    assert not _is_ephemeral_port_env_key("DATABASE_URL")
    assert not _is_ephemeral_port_env_key("API_TOKEN")


def test_handles_empty_or_missing_env() -> None:
    """Scrub doesn't crash on payloads without `env`."""
    payload: dict = {"other": "data"}
    _scrub_ephemeral_ports_in_packet(payload)
    assert payload == {"other": "data"}

    payload2: dict = {"steps": [{"id": "x"}, {"env": None}]}
    _scrub_ephemeral_ports_in_packet(payload2)
    # No exception; env=None step left as-is
    assert payload2["steps"][1]["env"] is None
