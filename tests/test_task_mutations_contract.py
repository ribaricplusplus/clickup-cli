from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from clickup_cli.cli import app
from tests.conftest import MockClickUpAPI

AUTH_VALUE = "mutation-auth-value"
TASK_ID = "task_123"
LIST_ID = "list_456"
DATE_ONLY_MS = 1_893_542_400_000
TIMED_MS = 1_893_593_045_000
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
WRITE_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}

runner = CliRunner()


def task_payload(
    *,
    name: str = "Before",
    description: str = "Old description",
    priority: int | None = None,
    start_date: int | None = None,
    start_date_time: bool | None = None,
    archived: bool = False,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "archived": archived,
        "assignees": [],
        "attachments": [],
        "description": description,
        "due_date": None,
        "due_date_time": None,
        "id": TASK_ID,
        "list": {"id": LIST_ID},
        "name": name,
        "priority": (
            None
            if priority is None
            else {
                "id": str(priority),
                "priority": {1: "urgent", 2: "high", 3: "normal", 4: "low"}[priority],
            }
        ),
        "start_date": None if start_date is None else str(start_date),
        "start_date_time": start_date_time,
        "status": {"status": "Open", "type": "open"},
        "tags": [{"name": tag} for tag in (tags or [])],
        "url": f"https://app.clickup.com/t/{TASK_ID}",
    }


def invoke(api: MockClickUpAPI, args: list[str]) -> Result:
    return runner.invoke(
        app,
        ["--base-url", api.base_url, "--json", *args],
        env={"CLICKUP_API_TOKEN": AUTH_VALUE},
    )


def expect_task(api: MockClickUpAPI, payload: dict[str, Any]) -> None:
    api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=payload,
    )


def expect_update(api: MockClickUpAPI, body: dict[str, object]) -> None:
    api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers=WRITE_HEADERS,
        json_body=body,
        response_json={},
    )


def test_combined_update_sends_one_minimal_put_then_one_readback(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(mock_api, task_payload())
    expect_update(
        mock_api,
        {
            "description": "New description",
            "name": "After",
            "priority": 2,
            "start_date": TIMED_MS,
            "start_date_time": True,
        },
    )
    expect_task(
        mock_api,
        task_payload(
            name="After",
            description="New description",
            priority=2,
            start_date=TIMED_MS,
            start_date_time=True,
        ),
    )

    result = invoke(
        mock_api,
        [
            "task",
            "update",
            TASK_ID,
            "--name",
            "After",
            "--description",
            "New description",
            "--priority",
            "high",
            "--start-date",
            "2030-01-02T15:04:05+01:00",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["changed"] is True
    assert payload["fields"] == [
        "description",
        "name",
        "priority",
        "start_date",
        "start_date_time",
    ]
    assert payload["task"]["start_date"] == "2030-01-02T14:04:05Z"
    assert payload["task"]["priority"] == "high"
    assert [request.method for request in mock_api.state.requests] == ["GET", "PUT", "GET"]


def test_update_reads_utf8_description_file_before_network(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    description_file = tmp_path / "description.txt"
    description_file.write_text("Line one\nLine two — UTF-8\n", encoding="utf-8")
    expect_task(mock_api, task_payload())
    expect_update(mock_api, {"description": "Line one\nLine two — UTF-8\n"})
    expect_task(
        mock_api,
        task_payload(description="Line one\nLine two — UTF-8\n"),
    )

    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--description-file", str(description_file)],
    )

    assert result.exit_code == 0, result.output


def test_update_date_only_sets_start_date_time_false(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload())
    expect_update(
        mock_api,
        {"start_date": DATE_ONLY_MS, "start_date_time": False},
    )
    expect_task(
        mock_api,
        task_payload(start_date=DATE_ONLY_MS, start_date_time=False),
    )

    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--start-date", "2030-01-02"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["start_date"] == "2030-01-02"


def test_combined_update_noop_avoids_put(mock_api: MockClickUpAPI) -> None:
    expect_task(
        mock_api,
        task_payload(
            name="Same",
            description="Same description",
            priority=3,
            start_date=DATE_ONLY_MS,
            start_date_time=False,
        ),
    )

    result = invoke(
        mock_api,
        [
            "task",
            "update",
            TASK_ID,
            "--name",
            "Same",
            "--description",
            "Same description",
            "--priority",
            "normal",
            "--start-date",
            "2030-01-02",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False
    assert len(mock_api.state.requests) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["task", "update", TASK_ID],
        [
            "task",
            "update",
            TASK_ID,
            "--start-date",
            "2030-01-02",
            "--clear-start-date",
        ],
        ["task", "update", TASK_ID, "--priority", "blocker"],
        ["task", "update", TASK_ID, "--start-date", "2030-01-02T12:00:00"],
    ],
)
def test_invalid_update_options_fail_before_network(
    mock_api: MockClickUpAPI, args: list[str]
) -> None:
    result = invoke(mock_api, args)

    assert result.exit_code == 1
    assert mock_api.state.requests == []


def test_description_options_are_mutually_exclusive_before_network(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    description_file = tmp_path / "description.txt"
    description_file.write_text("File description", encoding="utf-8")

    result = invoke(
        mock_api,
        [
            "task",
            "update",
            TASK_ID,
            "--description",
            "Inline",
            "--description-file",
            str(description_file),
        ],
    )

    assert result.exit_code == 1
    assert mock_api.state.requests == []


def test_invalid_description_file_fails_before_network(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--description-file", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert mock_api.state.requests == []


def test_update_readback_mismatch_is_typed(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload())
    expect_update(mock_api, {"priority": 1})
    expect_task(mock_api, task_payload(priority=2))

    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--priority", "urgent"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "verification_failed"


def test_start_date_verification_accepts_unobservable_time_flag(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(mock_api, task_payload())
    expect_update(
        mock_api,
        {"start_date": DATE_ONLY_MS, "start_date_time": False},
    )
    readback = task_payload(start_date=DATE_ONLY_MS)
    readback.pop("start_date_time")
    expect_task(mock_api, readback)

    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--start-date", "2030-01-02"],
    )

    assert result.exit_code == 0, result.output


def test_start_date_observable_time_flag_mismatch_fails(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(mock_api, task_payload())
    expect_update(
        mock_api,
        {"start_date": DATE_ONLY_MS, "start_date_time": False},
    )
    expect_task(
        mock_api,
        task_payload(start_date=DATE_ONLY_MS, start_date_time=True),
    )

    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--start-date", "2030-01-02"],
    )

    assert result.exit_code == 1
    assert "expected start_date_time=False, received True" in result.stderr


def test_priority_clear_uses_shared_minimal_verified_update(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload(priority=4))
    expect_update(mock_api, {"priority": None})
    expect_task(mock_api, task_payload())

    result = invoke(mock_api, ["task", "priority", "clear", TASK_ID])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["fields"] == ["priority"]


def test_update_priority_clear_maps_to_null(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload(priority=1))
    expect_update(mock_api, {"priority": None})
    expect_task(mock_api, task_payload())

    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--priority", "clear"],
    )

    assert result.exit_code == 0, result.output


def test_update_clear_start_date_noop_avoids_put(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload())

    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--clear-start-date"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False
    assert len(mock_api.state.requests) == 1


def test_start_date_clear_uses_shared_minimal_verified_update(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(
        mock_api,
        task_payload(start_date=TIMED_MS, start_date_time=True),
    )
    expect_update(mock_api, {"start_date": None})
    expect_task(mock_api, task_payload())

    result = invoke(mock_api, ["task", "start-date", "clear", TASK_ID])

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("command", "current", "requested", "body"),
    [
        ("archive", False, True, {"archived": True}),
        ("unarchive", True, False, {"archived": False}),
    ],
)
def test_archive_lifecycle_exact_minimal_body_and_readback(
    mock_api: MockClickUpAPI,
    command: str,
    current: bool,
    requested: bool,
    body: dict[str, object],
) -> None:
    expect_task(mock_api, task_payload(archived=current))
    expect_update(mock_api, body)
    expect_task(mock_api, task_payload(archived=requested))

    result = invoke(mock_api, ["task", command, TASK_ID])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["archived"] is requested


@pytest.mark.parametrize(("command", "archived"), [("archive", True), ("unarchive", False)])
def test_archive_lifecycle_noop_avoids_put(
    mock_api: MockClickUpAPI, command: str, archived: bool
) -> None:
    expect_task(mock_api, task_payload(archived=archived))

    result = invoke(mock_api, ["task", command, TASK_ID])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False
    assert len(mock_api.state.requests) == 1


def test_archive_readback_mismatch_fails(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload())
    expect_update(mock_api, {"archived": True})
    expect_task(mock_api, task_payload())

    result = invoke(mock_api, ["task", "archive", TASK_ID])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "verification_failed"


def test_tag_add_url_encodes_path_and_verifies_case_insensitively(
    mock_api: MockClickUpAPI,
) -> None:
    tag = "Road Map/β"
    expect_task(mock_api, task_payload(tags=[]))
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK_ID}/tag/Road%20Map%2F%CE%B2",
        headers=WRITE_HEADERS,
        response_json={},
    )
    expect_task(mock_api, task_payload(tags=["road map/\u0392"]))

    result = invoke(mock_api, ["task", "tag", "add", TASK_ID, tag])

    assert result.exit_code == 0, result.output
    assert mock_api.state.requests[1].raw_body == b""


def test_tag_remove_uses_observed_case_and_url_encodes(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload(tags=["Road Map/β", "keep"]))
    mock_api.expect(
        "DELETE",
        f"/api/v2/task/{TASK_ID}/tag/Road%20Map%2F%CE%B2",
        headers=WRITE_HEADERS,
        response_json={},
    )
    expect_task(mock_api, task_payload(tags=["keep"]))

    result = invoke(mock_api, ["task", "tag", "remove", TASK_ID, "ROAD MAP/\u0392"])

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("command", "tags"),
    [("add", ["Focus"]), ("remove", [])],
)
def test_tag_noop_avoids_write(mock_api: MockClickUpAPI, command: str, tags: list[str]) -> None:
    expect_task(mock_api, task_payload(tags=tags))

    result = invoke(mock_api, ["task", "tag", command, TASK_ID, "focus"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False
    assert len(mock_api.state.requests) == 1


def test_tag_readback_mismatch_fails(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload())
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK_ID}/tag/focus",
        headers=WRITE_HEADERS,
        response_json={},
    )
    expect_task(mock_api, task_payload())

    result = invoke(mock_api, ["task", "tag", "add", TASK_ID, "focus"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "verification_failed"


def test_dot_segment_tag_is_percent_encoded_safely(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, task_payload())
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK_ID}/tag/%2E%2E",
        headers=WRITE_HEADERS,
        response_json={},
    )
    expect_task(mock_api, task_payload(tags=[".."]))

    result = invoke(mock_api, ["task", "tag", "add", TASK_ID, ".."])

    assert result.exit_code == 0, result.output
