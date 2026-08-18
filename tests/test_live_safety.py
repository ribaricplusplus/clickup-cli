from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from clickup_cli.client import ClickUpClient
from tests.conftest import MockClickUpAPI
from tests.live_safety import (
    SANDBOX_LIST_NAME,
    LiveContainmentError,
    OwnedTask,
    OwnedTimeEntry,
    delete_owned_task,
    delete_owned_time_entry,
    prove_sandbox_destination,
)
from tests.test_live import _invoke

AUTH_VALUE = "live-safety-auth-value"
WORKSPACE_ID = "42"
SPACE_ID = "77"
LIST_ID = "88"
TASK_ID = "run_task"
ENTRY_ID = "run_entry"
MARKER = "11111111-2222-3333-4444-555555555555"
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
TIME_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}


def task_payload(
    *,
    list_id: str = LIST_ID,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    return {
        "description": description or f"Owned task description {MARKER}",
        "id": TASK_ID,
        "list": {"id": list_id},
        "name": name or f"Owned task {MARKER}",
    }


def time_entry_payload(*, task_id: str = TASK_ID, description: str | None = None) -> dict[str, Any]:
    return {
        "description": description or f"Manual entry {MARKER}",
        "duration": "90000",
        "id": ENTRY_ID,
        "start": "1787000000000",
        "tags": [],
        "tid": task_id,
        "wid": WORKSPACE_ID,
    }


def test_sandbox_proof_accepts_exact_folder_membership() -> None:
    prove_sandbox_destination(
        {
            "id": LIST_ID,
            "name": SANDBOX_LIST_NAME,
            "space_id": SPACE_ID,
        },
        {
            "id": WORKSPACE_ID,
            "spaces": [
                {
                    "folders": [{"id": "folder", "lists": [{"id": LIST_ID}]}],
                    "id": SPACE_ID,
                    "lists": [],
                }
            ],
        },
        workspace_id=WORKSPACE_ID,
        space_id=SPACE_ID,
        list_id=LIST_ID,
    )


@pytest.mark.parametrize(
    ("list_summary", "tree", "message"),
    [
        (
            {"id": LIST_ID, "name": "Almost Sandbox", "space_id": SPACE_ID},
            {"id": WORKSPACE_ID, "spaces": []},
            "name must be exactly",
        ),
        (
            {"id": LIST_ID, "name": SANDBOX_LIST_NAME, "space_id": SPACE_ID},
            {"id": "wrong", "spaces": []},
            "Workspace ID",
        ),
        (
            {"id": LIST_ID, "name": SANDBOX_LIST_NAME, "space_id": SPACE_ID},
            {
                "id": WORKSPACE_ID,
                "spaces": [{"folders": [], "id": SPACE_ID, "lists": []}],
            },
            "absent from configured Space",
        ),
    ],
)
def test_sandbox_proof_fails_closed(
    list_summary: dict[str, Any], tree: dict[str, Any], message: str
) -> None:
    with pytest.raises(LiveContainmentError, match=message):
        prove_sandbox_destination(
            list_summary,
            tree,
            workspace_id=WORKSPACE_ID,
            space_id=SPACE_ID,
            list_id=LIST_ID,
        )


def test_owned_task_cleanup_fetches_before_delete_and_requires_404(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=task_payload(),
    )
    mock_api.expect(
        "DELETE",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json={},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_status=404,
        response_json={"err": "not found"},
    )

    with ClickUpClient(token=AUTH_VALUE, base_url=mock_api.base_url) as client:
        delete_owned_task(
            client,
            TASK_ID,
            {TASK_ID: OwnedTask(MARKER)},
            sandbox_list_id=LIST_ID,
        )


def test_live_invoke_retains_task_id_from_partial_create_error(
    mock_api: MockClickUpAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = f"Partial task {MARKER}"
    description = f"Partial task description {MARKER}"
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=TIME_HEADERS,
        json_body={"description": description, "name": name},
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_status=503,
        response_json={"err": "readback unavailable"},
    )
    monkeypatch.setenv("CLICKUP_API_TOKEN", AUTH_VALUE)
    ownership: dict[str, OwnedTask] = {}
    owner = OwnedTask(MARKER)

    with pytest.raises(AssertionError):
        _invoke(
            CliRunner(),
            mock_api.base_url,
            "task",
            "create",
            name,
            "--list-id",
            LIST_ID,
            "--description",
            description,
            owned_tasks=ownership,
            expected_task=owner,
        )

    assert ownership == {TASK_ID: owner}


@pytest.mark.parametrize(
    "payload",
    [
        task_payload(list_id="another-list"),
        task_payload(name="marker removed"),
        task_payload(description="marker removed"),
    ],
)
def test_owned_task_cleanup_refuses_without_issuing_delete(
    mock_api: MockClickUpAPI, payload: dict[str, Any]
) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=payload,
    )

    with ClickUpClient(token=AUTH_VALUE, base_url=mock_api.base_url) as client:
        with pytest.raises(LiveContainmentError, match="Refusing task cleanup"):
            delete_owned_task(
                client,
                TASK_ID,
                {TASK_ID: OwnedTask(MARKER)},
                sandbox_list_id=LIST_ID,
            )


def test_owned_time_entry_cleanup_proves_entry_and_task_before_exact_delete(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        response_json={"data": time_entry_payload()},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_json=task_payload(),
    )
    mock_api.expect(
        "DELETE",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        response_json={},
    )

    with ClickUpClient(token=AUTH_VALUE, base_url=mock_api.base_url) as client:
        delete_owned_time_entry(
            client,
            WORKSPACE_ID,
            ENTRY_ID,
            {ENTRY_ID: OwnedTimeEntry(TASK_ID, MARKER)},
            {TASK_ID: OwnedTask(MARKER)},
            sandbox_list_id=LIST_ID,
        )


@pytest.mark.parametrize(
    "payload",
    [
        time_entry_payload(task_id="preexisting-task"),
        time_entry_payload(description="marker removed"),
    ],
)
def test_owned_time_entry_cleanup_refuses_without_issuing_delete(
    mock_api: MockClickUpAPI, payload: dict[str, Any]
) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        response_json={"data": payload},
    )

    with ClickUpClient(token=AUTH_VALUE, base_url=mock_api.base_url) as client:
        with pytest.raises(LiveContainmentError, match="Refusing time-entry cleanup"):
            delete_owned_time_entry(
                client,
                WORKSPACE_ID,
                ENTRY_ID,
                {ENTRY_ID: OwnedTimeEntry(TASK_ID, MARKER)},
                {TASK_ID: OwnedTask(MARKER)},
                sandbox_list_id=LIST_ID,
            )
