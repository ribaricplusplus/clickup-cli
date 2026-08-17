from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from clickup_cli.cli import app
from tests.conftest import MockClickUpAPI

AUTH_VALUE = "metadata-auth-value"
TASK_ID = "task_123"
TASK_URL = f"https://app.clickup.com/t/workspace_9/{TASK_ID}"
USER_ID = 42
DATE_ONLY_MS = 1_893_542_400_000
DATE_ONLY_CANONICAL_MS = 1_893_546_000_000
NEXT_DAY_MS = 1_893_628_800_000
TIMED_MS = 1_893_593_045_000
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
WRITE_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}
_MISSING = object()

runner = CliRunner()


def task_payload(
    *,
    due_date: str | int | None = None,
    due_date_time: bool | object = _MISSING,
    assignee_ids: list[int] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "assignees": [
            {"id": user_id, "username": f"User {user_id}"} for user_id in (assignee_ids or [])
        ],
        "due_date": due_date,
        "id": TASK_ID,
        "list": {"id": "list_456"},
        "name": "Synthetic metadata task",
        "status": {"status": "Open", "type": "open"},
        "url": f"https://app.clickup.com/t/{TASK_ID}",
    }
    if due_date_time is not _MISSING:
        payload["due_date_time"] = due_date_time
    return payload


def comment_payload(
    comment_id: str = "comment_7", text: str = "Deterministic comment"
) -> dict[str, Any]:
    return {
        "comment": [{"text": text}],
        "comment_text": text,
        "date": "1893500000000",
        "id": comment_id,
        "resolved": False,
        "user": {"id": USER_ID, "username": "Sandbox User"},
    }


def invoke(api: MockClickUpAPI, args: list[str], *, json_output: bool = True) -> Result:
    global_args = ["--base-url", api.base_url]
    if json_output:
        global_args.append("--json")
    return runner.invoke(
        app,
        [*global_args, *args],
        env={"CLICKUP_API_TOKEN": AUTH_VALUE},
    )


def expect_task_read(api: MockClickUpAPI, response: dict[str, Any]) -> None:
    api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=response,
    )


def assert_read_headers_only(api: MockClickUpAPI, *indexes: int) -> None:
    for index in indexes:
        request = api.state.requests[index]
        assert request.headers["authorization"] == AUTH_VALUE
        assert request.headers["accept"] == "application/json"
        assert "content-type" not in request.headers


def test_comment_list_exact_contract_and_json_shape(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=READ_HEADERS,
        response_json={"comments": [comment_payload()]},
    )

    result = invoke(mock_api, ["task", "comment", "list", TASK_URL])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "comments": [
                {
                    "date": "1893500000000",
                    "id": "comment_7",
                    "resolved": False,
                    "text": "Deterministic comment",
                    "user_id": "42",
                    "username": "Sandbox User",
                }
            ],
            "task_id": TASK_ID,
        },
    }
    assert [(request.method, request.path) for request in mock_api.state.requests] == [
        ("GET", f"/api/v2/task/{TASK_ID}/comment")
    ]
    assert_read_headers_only(mock_api, 0)


def test_comment_add_exact_post_then_readback_contract(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=WRITE_HEADERS,
        json_body={"comment_text": "Deterministic comment", "notify_all": False},
        response_json={"date": 1_893_500_000_000, "hist_id": "history_1", "id": "comment_7"},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=READ_HEADERS,
        response_json={"comments": [comment_payload()]},
    )

    result = invoke(
        mock_api,
        ["task", "comment", "add", TASK_URL, "Deterministic comment"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "comment": {
                "date": "1893500000000",
                "id": "comment_7",
                "resolved": False,
                "text": "Deterministic comment",
                "user_id": "42",
                "username": "Sandbox User",
            },
            "task_id": TASK_ID,
        },
    }
    assert [(request.method, request.path) for request in mock_api.state.requests] == [
        ("POST", f"/api/v2/task/{TASK_ID}/comment"),
        ("GET", f"/api/v2/task/{TASK_ID}/comment"),
    ]
    assert mock_api.state.requests[0].headers["authorization"] == AUTH_VALUE
    assert mock_api.state.requests[0].headers["content-type"] == "application/json"
    assert_read_headers_only(mock_api, 1)


def test_comment_add_rejects_empty_text_before_network(mock_api: MockClickUpAPI) -> None:
    result = invoke(mock_api, ["task", "comment", "add", TASK_ID, "   "])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_operation"
    assert mock_api.state.requests == []


def test_comment_add_fails_when_readback_text_does_not_match(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=WRITE_HEADERS,
        json_body={"comment_text": "Expected text", "notify_all": False},
        response_json={"id": "comment_7"},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=READ_HEADERS,
        response_json={"comments": [comment_payload(text="Different text")]},
    )

    result = invoke(mock_api, ["task", "comment", "add", TASK_ID, "Expected text"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "verification_failed"
    assert "text did not match" in result.stderr


def test_comment_api_error_redacts_token_and_stops_before_readback(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=WRITE_HEADERS,
        json_body={"comment_text": "Rejected comment", "notify_all": False},
        response_status=401,
        response_json={"err": f"Rejected token {AUTH_VALUE}"},
    )

    result = invoke(mock_api, ["task", "comment", "add", TASK_ID, "Rejected comment"])

    assert result.exit_code == 1
    assert AUTH_VALUE not in result.stdout + result.stderr
    assert "[REDACTED]" in result.stderr
    assert len(mock_api.state.requests) == 1


def test_due_date_set_date_only_exact_read_write_readback_contract(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task_read(mock_api, task_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"due_date": DATE_ONLY_MS, "due_date_time": False},
        response_json={},
    )
    expect_task_read(
        mock_api,
        task_payload(due_date=str(DATE_ONLY_CANONICAL_MS), due_date_time=False),
    )

    result = invoke(mock_api, ["task", "due-date", "set", TASK_URL, "2030-01-02"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "changed": True,
            "due_date": "2030-01-02",
            "due_date_ms": DATE_ONLY_CANONICAL_MS,
            "due_date_time": False,
            "previous_due_date_ms": None,
            "task_id": TASK_ID,
        },
    }
    assert [request.method for request in mock_api.state.requests] == ["GET", "PUT", "GET"]
    assert_read_headers_only(mock_api, 0, 2)
    assert mock_api.state.requests[1].headers["authorization"] == AUTH_VALUE
    assert mock_api.state.requests[1].headers["content-type"] == "application/json"


def test_due_date_set_timed_offset_normalizes_to_utc_and_sets_time_flag(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task_read(
        mock_api,
        task_payload(due_date=str(DATE_ONLY_MS), due_date_time=False),
    )
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"due_date": TIMED_MS, "due_date_time": True},
        response_json={},
    )
    expect_task_read(mock_api, task_payload(due_date=str(TIMED_MS), due_date_time=True))

    result = invoke(
        mock_api,
        ["task", "due-date", "set", TASK_ID, "2030-01-02T15:04:05+01:00"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload == {
        "changed": True,
        "due_date": "2030-01-02T14:04:05Z",
        "due_date_ms": TIMED_MS,
        "due_date_time": True,
        "previous_due_date_ms": DATE_ONLY_MS,
        "task_id": TASK_ID,
    }


def test_due_date_clear_sends_only_null_then_verifies_absence(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload(due_date=str(TIMED_MS), due_date_time=True))
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"due_date": None},
        response_json={},
    )
    expect_task_read(mock_api, task_payload())

    result = invoke(mock_api, ["task", "due-date", "clear", TASK_URL])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == {
        "changed": True,
        "due_date": None,
        "due_date_ms": None,
        "due_date_time": None,
        "previous_due_date_ms": TIMED_MS,
        "task_id": TASK_ID,
    }
    assert [request.method for request in mock_api.state.requests] == ["GET", "PUT", "GET"]


def test_due_date_clear_is_noop_when_already_clear(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload())

    result = invoke(mock_api, ["task", "due-date", "clear", TASK_ID])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False
    assert len(mock_api.state.requests) == 1
    assert_read_headers_only(mock_api, 0)


def test_date_only_canonical_same_day_is_noop_when_time_flag_is_observable(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task_read(
        mock_api,
        task_payload(due_date=str(DATE_ONLY_CANONICAL_MS), due_date_time=False),
    )

    result = invoke(mock_api, ["task", "due-date", "set", TASK_ID, "2030-01-02"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False
    assert len(mock_api.state.requests) == 1


@pytest.mark.parametrize(
    "invalid_value",
    ["2030-01-02T15:04:05", "2030-02-30", "not-a-date", "1969-12-31"],
)
def test_invalid_due_dates_fail_before_network(
    mock_api: MockClickUpAPI, invalid_value: str
) -> None:
    result = invoke(mock_api, ["task", "due-date", "set", TASK_ID, invalid_value])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_due_date"
    assert mock_api.state.requests == []


def test_due_date_set_fails_on_readback_value_mismatch(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"due_date": DATE_ONLY_MS, "due_date_time": False},
        response_json={},
    )
    expect_task_read(mock_api, task_payload(due_date=str(NEXT_DAY_MS)))

    result = invoke(mock_api, ["task", "due-date", "set", TASK_ID, "2030-01-02"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "verification_failed"
    assert "expected 2030-01-02, received 2030-01-03" in result.stderr


def test_due_date_set_fails_on_observable_time_flag_mismatch(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task_read(mock_api, task_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"due_date": DATE_ONLY_MS, "due_date_time": False},
        response_json={},
    )
    expect_task_read(
        mock_api,
        task_payload(due_date=str(DATE_ONLY_CANONICAL_MS), due_date_time=True),
    )

    result = invoke(mock_api, ["task", "due-date", "set", TASK_ID, "2030-01-02"])

    assert result.exit_code == 1
    assert "expected due_date_time=False, received True" in result.stderr


def test_assign_exact_read_write_readback_contract_and_json(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"assignees": {"add": [USER_ID], "rem": []}},
        response_json={},
    )
    expect_task_read(mock_api, task_payload(assignee_ids=[USER_ID]))

    result = invoke(mock_api, ["task", "assign", TASK_URL, str(USER_ID)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "assigned": True,
            "assignee_ids": [USER_ID],
            "changed": True,
            "task_id": TASK_ID,
            "user_id": USER_ID,
        },
    }
    assert [request.method for request in mock_api.state.requests] == ["GET", "PUT", "GET"]
    assert_read_headers_only(mock_api, 0, 2)
    assert mock_api.state.requests[1].headers["authorization"] == AUTH_VALUE
    assert mock_api.state.requests[1].headers["content-type"] == "application/json"


def test_unassign_exact_read_write_readback_contract(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload(assignee_ids=[7, USER_ID]))
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"assignees": {"add": [], "rem": [USER_ID]}},
        response_json={},
    )
    expect_task_read(mock_api, task_payload(assignee_ids=[7]))

    result = invoke(mock_api, ["task", "unassign", TASK_ID, str(USER_ID)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == {
        "assigned": False,
        "assignee_ids": [7],
        "changed": True,
        "task_id": TASK_ID,
        "user_id": USER_ID,
    }


@pytest.mark.parametrize(
    ("command", "assignee_ids", "assigned"),
    [("assign", [USER_ID], True), ("unassign", [], False)],
)
def test_assignment_noop_avoids_put(
    mock_api: MockClickUpAPI,
    command: str,
    assignee_ids: list[int],
    assigned: bool,
) -> None:
    expect_task_read(mock_api, task_payload(assignee_ids=assignee_ids))

    result = invoke(mock_api, ["task", command, TASK_ID, str(USER_ID)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["assigned"] is assigned
    assert payload["changed"] is False
    assert len(mock_api.state.requests) == 1


def test_assignment_fails_when_readback_does_not_confirm_change(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task_read(mock_api, task_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"assignees": {"add": [USER_ID], "rem": []}},
        response_json={},
    )
    expect_task_read(mock_api, task_payload())

    result = invoke(mock_api, ["task", "assign", TASK_ID, str(USER_ID)])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "verification_failed"
    assert f"user {USER_ID} was not assigned" in result.stderr


def test_assignment_rejects_nonpositive_user_before_network(mock_api: MockClickUpAPI) -> None:
    result = invoke(mock_api, ["task", "assign", TASK_ID, "0"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_operation"
    assert mock_api.state.requests == []


def test_assignment_fails_closed_on_malformed_assignee_readback(
    mock_api: MockClickUpAPI,
) -> None:
    malformed = task_payload()
    malformed["assignees"] = [{"id": "not-numeric"}]
    expect_task_read(mock_api, malformed)

    result = invoke(mock_api, ["task", "assign", TASK_ID, str(USER_ID)])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "api_error"
    assert len(mock_api.state.requests) == 1
