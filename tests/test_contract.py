from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from clickup_cli import __version__
from clickup_cli.cli import app
from tests.conftest import MockClickUpAPI

AUTH_VALUE = "unit-auth-value"
TASK_ID = "task_123"
LIST_ID = "list_456"
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
WRITE_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}

runner = CliRunner()


def task_payload(
    status: str = "Open",
    *,
    status_type: str = "open",
    task_id: str = TASK_ID,
    name: str = "Synthetic task",
    description: str = "Synthetic task description",
    assignee_ids: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "assignees": [{"id": user_id} for user_id in (assignee_ids or [])],
        "description": description,
        "due_date": None,
        "due_date_time": None,
        "id": task_id,
        "list": {"id": LIST_ID},
        "name": name,
        "status": {"status": status, "type": status_type},
        "tags": [],
        "url": f"https://app.clickup.com/t/{task_id}",
    }


def list_payload(*statuses: tuple[str, str]) -> dict[str, object]:
    return {
        "id": LIST_ID,
        "statuses": [{"status": label, "type": status_type} for label, status_type in statuses],
    }


def invoke(api: MockClickUpAPI, args: list[str], *, json_output: bool = False):  # type: ignore[no-untyped-def]
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


def expect_list_read(api: MockClickUpAPI, *statuses: tuple[str, str]) -> None:
    api.expect(
        "GET",
        f"/api/v2/list/{LIST_ID}",
        headers=READ_HEADERS,
        response_json=list_payload(*statuses),
    )


def test_version_is_credential_free_and_makes_no_request(mock_api: MockClickUpAPI) -> None:
    result = runner.invoke(app, ["--version"], env={})

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "clickup 0.2.0"
    assert mock_api.state.requests == []


def test_release_versions_are_exact() -> None:
    project_file = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))

    assert project["project"]["version"] == "0.2.0"
    assert __version__ == "0.2.0"


def test_whoami_exact_wire_contract_and_json_schema(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        "/api/v2/user",
        headers=READ_HEADERS,
        response_json={
            "user": {"email": "user@example.invalid", "id": 42, "username": "Example User"}
        },
    )

    result = invoke(mock_api, ["auth", "whoami"], json_output=True)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "user": {
                "email": "user@example.invalid",
                "id": "42",
                "username": "Example User",
            }
        },
    }


def test_show_accepts_workspace_url_and_normalizes_json(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload())

    result = invoke(
        mock_api,
        ["task", "show", f"https://app.clickup.com/t/workspace_9/{TASK_ID}"],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "task": {
                "archived": None,
                "assignees": [],
                "attachments": [],
                "description": "Synthetic task description",
                "due_date": None,
                "due_date_ms": None,
                "due_date_time": None,
                "id": TASK_ID,
                "list_id": LIST_ID,
                "list_name": None,
                "name": "Synthetic task",
                "priority": None,
                "start_date": None,
                "start_date_ms": None,
                "start_date_time": None,
                "status": "Open",
                "status_type": "open",
                "tags": [],
                "url": f"https://app.clickup.com/t/{TASK_ID}",
            }
        },
    }


def test_status_exact_wire_contract(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload("In Progress", status_type="custom"))

    result = invoke(mock_api, ["task", "status", TASK_ID])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "In Progress"


def test_set_status_exact_read_validate_write_readback_sequence(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task_read(mock_api, task_payload("Open"))
    expect_list_read(mock_api, ("Open", "open"), ("In Progress", "custom"), ("Done", "closed"))
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"status": "In Progress"},
        response_json=task_payload("In Progress", status_type="custom"),
    )
    expect_task_read(mock_api, task_payload("In Progress", status_type="custom"))

    result = invoke(mock_api, ["task", "set-status", TASK_ID, "in progress"], json_output=True)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == {
        "changed": True,
        "previous_status": "Open",
        "status": "In Progress",
        "task_id": TASK_ID,
    }


def test_invalid_status_fails_before_put_and_lists_valid_labels(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task_read(mock_api, task_payload("Open"))
    expect_list_read(mock_api, ("Open", "open"), ("Done", "closed"))

    result = invoke(mock_api, ["task", "set-status", TASK_ID, "Blocked"])

    assert result.exit_code == 1
    assert "Valid statuses: Open, Done" in result.stderr


def test_idempotent_status_does_not_put(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload("In Progress", status_type="custom"))
    expect_list_read(mock_api, ("Open", "open"), ("In Progress", "custom"))

    result = invoke(mock_api, ["task", "set-status", TASK_ID, "IN PROGRESS"], json_output=True)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False


def test_complete_prefers_completed_over_done_typed_on_hold(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload("Open"))
    expect_list_read(
        mock_api,
        ("Open", "open"),
        ("on hold", "done"),
        ("done", "done"),
        ("completed", "closed"),
    )
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"status": "completed"},
        response_json={},
    )
    expect_task_read(mock_api, task_payload("completed", status_type="closed"))

    result = invoke(mock_api, ["task", "complete", TASK_ID])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"{TASK_ID}: Open -> completed"


def test_complete_prefers_complete_over_done_typed_review(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload("Open"))
    expect_list_read(
        mock_api,
        ("review", "done"),
        ("done", "closed"),
        ("complete", "done"),
    )
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"status": "complete"},
        response_json={},
    )
    expect_task_read(mock_api, task_payload("complete", status_type="done"))

    result = invoke(mock_api, ["task", "complete", TASK_ID])

    assert result.exit_code == 0, result.output


def test_complete_refuses_misleading_terminal_like_statuses(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload("Open"))
    expect_list_read(
        mock_api,
        ("Open", "open"),
        ("on hold", "done"),
        ("review", "closed"),
        ("archived", "closed"),
    )

    result = invoke(mock_api, ["task", "complete", TASK_ID])

    assert result.exit_code == 1
    assert "No semantic completion status" in result.stderr
    assert "on hold, review, archived" in result.stderr


def test_create_minimal_exact_body(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={"name": "Minimal task"},
        response_json={"id": "created_1"},
    )
    mock_api.expect(
        "GET",
        "/api/v2/task/created_1",
        headers=READ_HEADERS,
        response_json=task_payload(task_id="created_1", name="Minimal task", description=""),
    )

    result = invoke(mock_api, ["task", "create", "Minimal task", "--list-id", LIST_ID])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("Created created_1:")


def test_create_populated_exact_body(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={
            "assignees": [12, 34],
            "description": "Details",
            "name": "Populated task",
            "status": "Open",
        },
        response_json={"id": "created_2"},
    )
    mock_api.expect(
        "GET",
        "/api/v2/task/created_2",
        headers=READ_HEADERS,
        response_json=task_payload(
            task_id="created_2",
            name="Populated task",
            description="Details",
            assignee_ids=[12, 34],
        ),
    )

    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Populated task",
            "--list-id",
            LIST_ID,
            "--description",
            "Details",
            "--status",
            "Open",
            "--assignee",
            "12",
            "--assignee",
            "34",
        ],
        json_output=True,
    )

    assert result.exit_code == 0, result.output


def test_delete_requires_confirmation_without_api_request(mock_api: MockClickUpAPI) -> None:
    result = invoke(mock_api, ["task", "delete", TASK_ID])

    assert result.exit_code == 1
    assert "--yes" in result.stderr
    assert mock_api.state.requests == []


def test_delete_exact_wire_contract(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "DELETE",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_status=204,
        response_json=None,
    )

    result = invoke(mock_api, ["task", "delete", TASK_ID, "--yes"], json_output=True)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {"deleted": True, "task_id": TASK_ID},
    }


def test_authorization_value_is_redacted_from_api_errors(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_status=401,
        response_json={"err": f"Rejected value {AUTH_VALUE}"},
    )

    result = invoke(mock_api, ["task", "show", TASK_ID], json_output=True)

    assert result.exit_code == 1
    combined_output = result.stdout + result.stderr
    assert AUTH_VALUE not in combined_output
    assert "[REDACTED]" in combined_output
    assert json.loads(result.stderr) == {
        "error": {
            "message": "ClickUp API returned HTTP 401: Rejected value [REDACTED]",
            "type": "api_error",
        },
        "ok": False,
    }


def test_api_failure_is_concise(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_status=503,
        response_json={"err": "temporarily unavailable"},
    )

    result = invoke(mock_api, ["task", "show", TASK_ID])

    assert result.exit_code == 1
    assert result.stderr.strip() == (
        "Error: ClickUp API returned HTTP 503: temporarily unavailable"
    )


def test_transport_failure_uses_local_server_and_is_concise(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        disconnect=True,
    )

    result = invoke(mock_api, ["task", "show", TASK_ID])

    assert result.exit_code == 1
    assert "ClickUp request failed" in result.stderr
    assert AUTH_VALUE not in result.stdout + result.stderr


def test_status_verification_mismatch_fails(mock_api: MockClickUpAPI) -> None:
    expect_task_read(mock_api, task_payload("Open"))
    expect_list_read(mock_api, ("Open", "open"), ("Done", "closed"))
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body={"status": "Done"},
        response_json={},
    )
    expect_task_read(mock_api, task_payload("Open"))

    result = invoke(mock_api, ["task", "set-status", TASK_ID, "Done"])

    assert result.exit_code == 1
    assert "expected 'Done', received 'Open'" in result.stderr


def test_429_retries_after_bounded_delay(mock_api: MockClickUpAPI) -> None:
    for _ in range(2):
        mock_api.expect(
            "GET",
            f"/api/v2/task/{TASK_ID}",
            headers=READ_HEADERS,
            response_status=429,
            response_json={"err": "slow down"},
            response_headers={"Retry-After": "0"},
        )
    expect_task_read(mock_api, task_payload())

    result = invoke(mock_api, ["task", "show", TASK_ID])

    assert result.exit_code == 0, result.output
