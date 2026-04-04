from __future__ import annotations

from otto.certifier.tier2 import _exec_http, _extract_ids


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ("" if payload is None else str(payload))

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        body = kwargs.get("json", {})
        if method == "PUT" and body.get("status") == "todo":
            return FakeResponse(400, payload={"error": "Must be one of: TODO, IN_PROGRESS, DONE"})
        if method == "PUT":
            return FakeResponse(200, payload={"data": {"id": "task-1", "status": body["status"]}})
        return FakeResponse(200, payload={"data": [{"id": "task-1", "title": "Existing task"}]})


def test_exec_http_notes_enum_self_heal():
    session = FakeSession()

    step = _exec_http(
        session,
        "http://example.test",
        "PUT",
        {"path": "/api/tasks/task-1", "body": {"status": "todo"}, "expect_status": [200]},
        {},
    )

    assert step.passed is True
    assert step.detail == "200 (self-healed: enum case)"
    assert session.calls[1][2]["json"]["status"] == "TODO"


def test_exec_http_unwraps_data_list_for_follow_up_steps():
    session = FakeSession()

    step = _exec_http(
        session,
        "http://example.test",
        "GET",
        {"path": "/api/tasks", "expect_status": [200]},
        {},
    )

    assert step.passed is True
    assert isinstance(step.data, list)
    assert step.data[0]["id"] == "task-1"

    variables: dict[str, str] = {}
    _extract_ids(step.data, variables)

    assert variables["first_item_id"] == "task-1"
