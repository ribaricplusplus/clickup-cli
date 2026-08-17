from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from clickup_cli.cli import app
from tests.conftest import MockClickUpAPI

AUTH_VALUE = "case-workflow-auth"
TASK_ID = "task_123"
LIST_ID = "list_456"
USER_ID = 42
DATE_ONLY_MS = 1_893_542_400_000
DATE_ONLY_CANONICAL_MS = 1_893_546_000_000
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
WRITE_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}

runner = CliRunner()


def invoke(api: MockClickUpAPI, args: list[str]) -> Result:
    return runner.invoke(
        app,
        ["--base-url", api.base_url, "--json", *args],
        env={"CLICKUP_API_TOKEN": AUTH_VALUE},
    )


def task_payload(*, tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "assignees": [{"email": "bruno@example.invalid", "id": USER_ID, "username": "Bruno"}],
        "description": "Check the linked comment.",
        "due_date": str(DATE_ONLY_CANONICAL_MS),
        "due_date_time": False,
        "id": TASK_ID,
        "list": {"id": LIST_ID, "name": "Todos"},
        "name": "Check Alina's Filtracon / Enso comment",
        "status": {"status": "backlog", "type": "open"},
        "tags": [{"name": tag} for tag in (tags if tags is not None else ["focus"])],
        "url": f"https://app.clickup.com/t/{TASK_ID}",
    }


def comment_payload(comment_id: str, text: str, date: str) -> dict[str, Any]:
    return {
        "comment_text": text,
        "date": date,
        "id": comment_id,
        "resolved": False,
        "user": {"id": USER_ID, "username": "Alina"},
    }


def expected_rich_summary() -> dict[str, Any]:
    return {
        "assignees": [{"email": "bruno@example.invalid", "id": "42", "username": "Bruno"}],
        "description": "Check the linked comment.",
        "due_date": "2030-01-02",
        "due_date_ms": DATE_ONLY_CANONICAL_MS,
        "due_date_time": False,
        "id": TASK_ID,
        "list_id": LIST_ID,
        "list_name": "Todos",
        "name": "Check Alina's Filtracon / Enso comment",
        "status": "backlog",
        "status_type": "open",
        "tags": ["focus"],
        "url": f"https://app.clickup.com/t/{TASK_ID}",
    }


def test_show_includes_due_date_assignees_tags_and_list_name(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=task_payload(),
    )

    result = invoke(mock_api, ["task", "show", TASK_ID])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {"task": expected_rich_summary()},
    }


def test_show_preserves_due_instant_when_time_flag_is_unavailable(
    mock_api: MockClickUpAPI,
) -> None:
    response = task_payload()
    response["due_date_time"] = None
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=response,
    )

    result = invoke(mock_api, ["task", "show", TASK_ID])

    assert result.exit_code == 0, result.output
    task = json.loads(result.stdout)["result"]["task"]
    assert task["due_date"] == "2030-01-02T01:00:00Z"
    assert task["due_date_ms"] == DATE_ONLY_CANONICAL_MS
    assert task["due_date_time"] is None


@pytest.mark.parametrize(
    ("field", "malformed"),
    [("assignees", ["not-an-object"]), ("tags", [{"missing": "name"}])],
)
def test_show_rejects_malformed_rich_metadata(
    mock_api: MockClickUpAPI, field: str, malformed: list[object]
) -> None:
    response = task_payload()
    response[field] = malformed
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=response,
    )

    result = invoke(mock_api, ["task", "show", TASK_ID])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "api_error"


def test_create_accepts_date_only_due_date_and_repeatable_tags_then_reads_back(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={
            "assignees": [USER_ID],
            "description": "Check the linked comment.",
            "due_date": DATE_ONLY_MS,
            "due_date_time": False,
            "name": "Check Alina's Filtracon / Enso comment",
            "status": "backlog",
            "tags": ["focus"],
        },
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=task_payload(),
    )

    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Check Alina's Filtracon / Enso comment",
            "--list-id",
            LIST_ID,
            "--description",
            "Check the linked comment.",
            "--status",
            "backlog",
            "--assignee",
            str(USER_ID),
            "--due-date",
            "2030-01-02",
            "--tag",
            "focus",
            "--tag",
            "FOCUS",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {"task": expected_rich_summary()},
    }
    assert [(request.method, request.path) for request in mock_api.state.requests] == [
        ("POST", f"/api/v2/list/{LIST_ID}/task"),
        ("GET", f"/api/v2/task/{TASK_ID}"),
    ]


def test_create_rejects_invalid_due_date_before_network(mock_api: MockClickUpAPI) -> None:
    result = invoke(
        mock_api,
        ["task", "create", "Task", "--list-id", LIST_ID, "--due-date", "tomorrow"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_due_date"
    assert mock_api.state.requests == []


def test_create_rejects_empty_tag_before_network(mock_api: MockClickUpAPI) -> None:
    result = invoke(
        mock_api,
        ["task", "create", "Task", "--list-id", LIST_ID, "--tag", "   "],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_operation"
    assert mock_api.state.requests == []


def test_create_fails_when_readback_does_not_contain_requested_tag(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={"name": "Tagged task", "tags": ["focus"]},
        response_json={"id": TASK_ID},
    )
    readback = task_payload(tags=[])
    readback["name"] = "Tagged task"
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=readback,
    )

    result = invoke(
        mock_api,
        ["task", "create", "Tagged task", "--list-id", LIST_ID, "--tag", "focus"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "verification_failed"
    assert TASK_ID in result.stderr
    assert "tags" in result.stderr


def test_create_reports_created_task_id_when_readback_fails(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={"name": "Readback failure"},
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_status=503,
        response_json={"err": "temporarily unavailable"},
    )

    result = invoke(
        mock_api,
        ["task", "create", "Readback failure", "--list-id", LIST_ID],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "verification_failed"
    assert TASK_ID in error["message"]
    assert "readback failed" in error["message"]


def test_comment_show_accepts_clickup_deep_link(mock_api: MockClickUpAPI) -> None:
    comment_id = "90150251486276"
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=READ_HEADERS,
        response_json={
            "comments": [comment_payload(comment_id, "Gianni is on fire!", "1893500000000")]
        },
    )

    result = invoke(
        mock_api,
        [
            "task",
            "comment",
            "show",
            f"https://app.clickup.com/t/workspace_9/{TASK_ID}?comment={comment_id}&utm_type=1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "comment": {
                "date": "1893500000000",
                "id": comment_id,
                "resolved": False,
                "text": "Gianni is on fire!",
                "user_id": "42",
                "username": "Alina",
            },
            "task_id": TASK_ID,
        },
    }


def test_comment_show_paginates_until_explicit_comment_id_is_found(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=READ_HEADERS,
        response_json={"comments": [comment_payload("comment_new", "Newer", "200")]},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}/comment?start=200&start_id=comment_new",
        headers=READ_HEADERS,
        response_json={"comments": [comment_payload("comment_old", "Target", "100")]},
    )

    result = invoke(
        mock_api,
        ["task", "comment", "show", TASK_ID, "comment_old"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["comment"]["text"] == "Target"


def test_comment_show_reports_not_found_after_final_page(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}/comment",
        headers=READ_HEADERS,
        response_json={"comments": []},
    )

    result = invoke(
        mock_api,
        ["task", "comment", "show", TASK_ID, "missing_comment"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "comment_not_found"
    assert "missing_comment" in result.stderr
