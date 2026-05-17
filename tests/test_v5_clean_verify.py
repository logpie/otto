"""Tests for v5 clean-state verification."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from otto.v5_clean_verify import _parse_declared_ports, verify_from_clean


def _write_shell(path: Path, text: str, *, executable: bool = True) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_script_valid_accepts_root_shell_script_with_clean_bash_n(tmp_path: Path) -> None:
    _write_shell(
        tmp_path / "start.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\necho ok\n",
    )

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is True
    assert "script_valid:bash_n:start.sh" in result.steps_run


def test_script_valid_rejects_missing_shebang(tmp_path: Path) -> None:
    _write_shell(tmp_path / "start.sh", "echo ok\n")

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is False
    assert result.failure_kind == "script_valid_failed"
    assert "missing a shebang" in (result.failure_message or "")


def test_script_valid_rejects_non_executable_shell_script(tmp_path: Path) -> None:
    _write_shell(
        tmp_path / "start.sh",
        "#!/usr/bin/env bash\necho ok\n",
        executable=False,
    )

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is False
    assert result.failure_kind == "script_valid_failed"
    assert "not executable" in (result.failure_message or "")


def test_script_valid_rejects_bash_n_syntax_error(tmp_path: Path) -> None:
    _write_shell(
        tmp_path / "start.sh",
        "#!/usr/bin/env bash\nif true; then\n  echo missing-fi\n",
    )

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is False
    assert result.failure_kind == "script_valid_failed"
    assert "bash -n start.sh failed" in (result.failure_message or "")


def test_script_valid_rejects_bash4_case_expansion_on_bash3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("otto.v5_clean_verify._host_bash_major", lambda _bash: 3)
    _write_shell(
        tmp_path / "start.sh",
        "#!/usr/bin/env bash\nservice=api\necho \"${service^^}\"\n",
    )

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is False
    assert result.failure_kind == "script_valid_failed"
    assert "bash-4-only" in (result.failure_message or "")


def test_script_valid_catches_sc6_start_sh_bash4_pattern(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sc6 used ${service^^} in the PORT_CONFLICT branch; bash -n missed it."""
    monkeypatch.setattr("otto.v5_clean_verify._host_bash_major", lambda _bash: 3)
    port = _free_port()
    _write_shell(
        tmp_path / "start.sh",
        f"""#!/usr/bin/env bash
set -e
API_PORT=${{API_PORT:-{port}}}
check_port() {{
  local port=$1
  local service=$2
  if lsof -iTCP:"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "PORT_CONFLICT: $service port $port is already in use. Set ${{service^^}}_PORT to a different value."
    exit 1
  fi
}}
check_port "$API_PORT" "API"
sleep 30
""",
    )

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is False
    assert result.failure_kind == "script_valid_failed"
    assert "${var^^}" in (result.failure_message or "")


def test_script_valid_dynamic_port_conflict_probe_catches_branch_error(
    tmp_path: Path,
) -> None:
    port = _free_port()
    (tmp_path / "CHARTER.md").write_text(
        "| Service | Env var | Default |\n"
        "| --- | --- | --- |\n"
        f"| REST API | `API_PORT` | `{port}` |\n",
        encoding="utf-8",
    )
    _write_shell(
        tmp_path / "start.sh",
        f"""#!/usr/bin/env bash
set -e
API_PORT=${{API_PORT:-{port}}}
if lsof -iTCP:"$API_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  missing_conflict_helper
  exit 1
fi
sleep 30
""",
    )

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is False
    assert result.failure_kind == "script_valid_failed"
    assert "PORT_CONFLICT branch" in (result.failure_message or "")
    assert "command not found" in (result.failure_message or "")


def test_script_valid_dynamic_port_conflict_probe_accepts_clean_branch(
    tmp_path: Path,
) -> None:
    port = _free_port()
    (tmp_path / "CHARTER.md").write_text(
        "| Service | Env var | Default |\n"
        "| --- | --- | --- |\n"
        f"| REST API | `API_PORT` | `{port}` |\n",
        encoding="utf-8",
    )
    _write_shell(
        tmp_path / "start.sh",
        f"""#!/usr/bin/env bash
set -e
API_PORT=${{API_PORT:-{port}}}
if lsof -iTCP:"$API_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "PORT_CONFLICT: API port $API_PORT is already in use. Set API_PORT to a different value."
  exit 1
fi
sleep 30
""",
    )

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is True
    assert "script_valid:port_conflict_probe" in result.steps_run


def test_parse_declared_ports_reads_charter_table_and_start_sh_defaults(
    tmp_path: Path,
) -> None:
    port = _free_port()
    (tmp_path / "CHARTER.md").write_text(
        "| Service | Env var | Default |\n"
        "| --- | --- | --- |\n"
        f"| REST API | `API_PORT` | `{port}` |\n",
        encoding="utf-8",
    )
    _write_shell(
        tmp_path / "start.sh",
        f"#!/usr/bin/env bash\nFRONTEND_PORT=${{FRONTEND_PORT:-{port + 1}}}\n",
    )

    assert _parse_declared_ports(tmp_path) == [port, port + 1]


def test_legacy_project_without_shell_scripts_still_passes(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("legacy\n", encoding="utf-8")

    result = verify_from_clean(tmp_path, scope="scaffold", timeout_s=3)

    assert result.passed is True
    assert result.steps_run == []
