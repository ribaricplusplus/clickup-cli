from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner, Result

import clickup_cli.discovery as discovery
from clickup_cli.cli import app
from clickup_cli.client import ClickUpClient
from clickup_cli.errors import InvalidOperationError
from clickup_cli.errors import ReferenceError as TaskReferenceError
from tests.conftest import MockClickUpAPI

AUTH_VALUE = "discovery-auth-value"
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
WRITE_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}

runner = CliRunner()


def invoke(api: MockClickUpAPI, args: list[str], *, json_output: bool = True) -> Result:
    global_args = ["--base-url", api.base_url]
    if json_output:
        global_args.append("--json")
    return runner.invoke(
        app,
        [*global_args, *args],
        env={"CLICKUP_API_TOKEN": AUTH_VALUE},
    )


def task_payload(
    task_id: str,
    *,
    name: str | None = None,
    list_id: str = "list_1",
    status: str = "Open",
    status_type: str = "open",
    assignees: list[int] | None = None,
    tags: list[str] | None = None,
    due_date: int | None = None,
    due_date_time: bool | None = None,
    parent: str | None = None,
    archived: bool = False,
    description: str = "",
    text_content: str = "",
    markdown_description: str = "",
) -> dict[str, Any]:
    return {
        "archived": archived,
        "assignees": [
            {
                "email": f"user{user_id}@example.invalid",
                "id": user_id,
                "username": f"User {user_id}",
            }
            for user_id in (assignees or [])
        ],
        "description": description,
        "due_date": None if due_date is None else str(due_date),
        "due_date_time": due_date_time,
        "id": task_id,
        "list": {"id": list_id, "name": f"List {list_id}"},
        "markdown_description": markdown_description,
        "name": name or f"Task {task_id}",
        "parent": parent,
        "status": {"status": status, "type": status_type},
        "tags": [{"name": tag} for tag in (tags or [])],
        "text_content": text_content,
        "url": f"https://app.clickup.com/t/{task_id}",
    }


def expect_list_tasks(
    api: MockClickUpAPI,
    list_id: str,
    query: str,
    tasks: list[dict[str, Any]],
    *,
    last_page: bool = True,
) -> None:
    api.expect(
        "GET",
        f"/api/v2/list/{list_id}/task?{query}",
        headers=READ_HEADERS,
        response_json={"last_page": last_page, "tasks": tasks},
    )


def test_discovery_commands_are_visible_without_credentials() -> None:
    root_help = runner.invoke(app, ["--help"], env={"CLICKUP_API_TOKEN": ""})
    task_help = runner.invoke(app, ["task", "--help"], env={"CLICKUP_API_TOKEN": ""})
    workspace_help = runner.invoke(app, ["workspace", "--help"], env={"CLICKUP_API_TOKEN": ""})
    list_help = runner.invoke(app, ["list", "--help"], env={"CLICKUP_API_TOKEN": ""})

    assert root_help.exit_code == 0, root_help.output
    for command in ("workspace", "member", "list"):
        assert command in root_help.stdout
    for command in ("list", "search", "ensure"):
        assert command in task_help.stdout
    assert "tree" in workspace_help.stdout
    assert "statuses" in list_help.stdout


def test_workspace_list_and_members_expose_only_normalized_identity_fields(
    mock_api: MockClickUpAPI,
) -> None:
    workspace_response = {
        "teams": [
            {
                "avatar": "secretly unnecessary",
                "id": "workspace_2",
                "members": [],
                "name": "Zulu",
            },
            {
                "color": "#fff",
                "id": "workspace_1",
                "members": [
                    {
                        "role": 3,
                        "user": {
                            "color": "#000",
                            "email": "z@example.invalid",
                            "id": 22,
                            "profilePicture": "unused",
                            "username": "Zulu User",
                        },
                    },
                    {"user": {"id": 11, "username": "Alpha User"}},
                ],
                "name": "Alpha",
            },
        ]
    }
    mock_api.expect("GET", "/api/v2/team", headers=READ_HEADERS, response_json=workspace_response)
    mock_api.expect("GET", "/api/v2/team", headers=READ_HEADERS, response_json=workspace_response)

    workspace_result = invoke(mock_api, ["workspace", "list"])
    member_result = invoke(mock_api, ["member", "list", "--workspace-id", "workspace_1"])

    assert workspace_result.exit_code == 0, workspace_result.output
    assert json.loads(workspace_result.stdout)["result"] == {
        "workspaces": [
            {"id": "workspace_1", "name": "Alpha"},
            {"id": "workspace_2", "name": "Zulu"},
        ]
    }
    assert member_result.exit_code == 0, member_result.output
    assert json.loads(member_result.stdout)["result"] == {
        "members": [
            {"email": None, "id": "11", "username": "Alpha User"},
            {"email": "z@example.invalid", "id": "22", "username": "Zulu User"},
        ],
        "workspace_id": "workspace_1",
    }


def test_workspace_tree_is_deterministic_and_includes_folderless_lists(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "GET",
        "/api/v2/team",
        headers=READ_HEADERS,
        response_json={"teams": [{"id": "workspace_1", "members": [], "name": "Workspace"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/team/workspace_1/space?archived=false",
        headers=READ_HEADERS,
        response_json={"spaces": [{"id": "space_a", "name": "Alpha"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/team/workspace_1/space?archived=true",
        headers=READ_HEADERS,
        response_json={"spaces": [{"archived": True, "id": "space_z", "name": "Zulu"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_a/folder?archived=false",
        headers=READ_HEADERS,
        response_json={"folders": [{"id": "folder_1", "name": "Folder"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_a/folder?archived=true",
        headers=READ_HEADERS,
        response_json={"folders": []},
    )
    mock_api.expect(
        "GET",
        "/api/v2/folder/folder_1/list?archived=false",
        headers=READ_HEADERS,
        response_json={"lists": [{"id": "list_f", "name": "Folder List"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/folder/folder_1/list?archived=true",
        headers=READ_HEADERS,
        response_json={"lists": []},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_a/list?archived=false",
        headers=READ_HEADERS,
        response_json={"lists": [{"id": "list_root", "name": "Folderless"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_a/list?archived=true",
        headers=READ_HEADERS,
        response_json={"lists": []},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_z/folder?archived=false",
        headers=READ_HEADERS,
        response_json={"folders": []},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_z/folder?archived=true",
        headers=READ_HEADERS,
        response_json={"folders": []},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_z/list?archived=false",
        headers=READ_HEADERS,
        response_json={"lists": []},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_z/list?archived=true",
        headers=READ_HEADERS,
        response_json={"lists": []},
    )

    result = invoke(mock_api, ["workspace", "tree", "workspace_1", "--include-archived"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["workspace"] == {
        "id": "workspace_1",
        "name": "Workspace",
        "spaces": [
            {
                "archived": False,
                "folders": [
                    {
                        "archived": False,
                        "id": "folder_1",
                        "lists": [{"archived": False, "id": "list_f", "name": "Folder List"}],
                        "name": "Folder",
                    }
                ],
                "id": "space_a",
                "lists": [{"archived": False, "id": "list_root", "name": "Folderless"}],
                "name": "Alpha",
            },
            {
                "archived": True,
                "folders": [],
                "id": "space_z",
                "lists": [],
                "name": "Zulu",
            },
        ],
    }


def test_workspace_tree_missing_workspace_is_typed_and_stops_traversal(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect("GET", "/api/v2/team", headers=READ_HEADERS, response_json={"teams": []})

    result = invoke(mock_api, ["workspace", "tree", "missing"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"] == {
        "message": "Workspace missing is not available to the authorized user",
        "type": "resource_not_found",
        "workspace_id": "missing",
    }


def test_list_show_and_statuses_have_stable_minimal_shapes(mock_api: MockClickUpAPI) -> None:
    payload = {
        "archived": False,
        "folder": {"access": True, "id": "folder_1", "name": "Folder"},
        "id": "list_1",
        "name": "Delivery",
        "space": {"access": True, "id": "space_1", "name": "Space"},
        "statuses": [
            {"color": "#fff", "status": "In Progress", "type": "custom"},
            {"color": "#000", "status": "Closed", "type": "closed"},
        ],
    }
    for _ in range(2):
        mock_api.expect("GET", "/api/v2/list/list_1", headers=READ_HEADERS, response_json=payload)

    show_result = invoke(mock_api, ["list", "show", "list_1"])
    statuses_result = invoke(mock_api, ["list", "statuses", "list_1"])

    assert show_result.exit_code == 0, show_result.output
    assert json.loads(show_result.stdout)["result"]["list"] == {
        "archived": False,
        "folder_id": "folder_1",
        "folder_name": "Folder",
        "id": "list_1",
        "name": "Delivery",
        "space_id": "space_1",
        "space_name": "Space",
        "statuses": [
            {"status": "Closed", "type": "closed"},
            {"status": "In Progress", "type": "custom"},
        ],
    }
    assert json.loads(statuses_result.stdout)["result"] == {
        "list_id": "list_1",
        "statuses": [
            {"status": "Closed", "type": "closed"},
            {"status": "In Progress", "type": "custom"},
        ],
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ["task", "list"],
        ["task", "list", "--list-id", "list_1", "--space-id", "space_1"],
        ["task", "list", "--list-id", "../unsafe"],
        ["task", "list", "--list-id", "list_1", "--assignee", "nobody"],
        ["task", "list", "--list-id", "list_1", "--status", " "],
        [
            "task",
            "list",
            "--list-id",
            "list_1",
            "--tag",
            "same",
            "--exclude-tag",
            "SAME",
        ],
        ["task", "search", " ", "--list-id", "list_1"],
        ["task", "search", "query", "--list-id", "list_1", "--due", "next:0d"],
    ],
)
def test_malformed_task_queries_fail_before_network(
    mock_api: MockClickUpAPI, arguments: list[str]
) -> None:
    result = invoke(mock_api, arguments)

    assert result.exit_code != 0
    assert mock_api.state.requests == []


def test_task_list_resolves_me_and_encodes_repeatable_server_filters(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "GET",
        "/api/v2/user",
        headers=READ_HEADERS,
        response_json={"user": {"id": 42}},
    )
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0&subtasks=true&statuses%5B%5D=In+Progress&include_closed=true&"
        "assignees%5B%5D=42&assignees%5B%5D=77&tags%5B%5D=Release+Ready",
        [
            task_payload(
                "task_1",
                status="In Progress",
                status_type="custom",
                assignees=[42],
                tags=["release ready"],
            )
        ],
    )

    result = invoke(
        mock_api,
        [
            "task",
            "list",
            "--list-id",
            "list_1",
            "--assignee",
            "me",
            "--assignee",
            "77",
            "--status",
            "In Progress",
            "--tag",
            "Release Ready",
            "--include-closed",
            "--include-subtasks",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [task["id"] for task in json.loads(result.stdout)["result"]["tasks"]] == ["task_1"]


def test_task_list_applies_default_and_explicit_filters_locally(
    mock_api: MockClickUpAPI,
) -> None:
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0&statuses%5B%5D=Open&assignees%5B%5D=42&tags%5B%5D=wanted",
        [
            task_payload("keep", assignees=[42], tags=["Wanted"]),
            task_payload("wrong_assignee", assignees=[7], tags=["wanted"]),
            task_payload("wrong_status", status="Blocked", assignees=[42], tags=["wanted"]),
            task_payload("excluded", assignees=[42], tags=["wanted", "blocked"]),
            task_payload(
                "closed", status="Closed", status_type="closed", assignees=[42], tags=["wanted"]
            ),
            task_payload("subtask", assignees=[42], tags=["wanted"], parent="parent_1"),
            task_payload("archived", assignees=[42], tags=["wanted"], archived=True),
        ],
    )

    result = invoke(
        mock_api,
        [
            "task",
            "list",
            "--list-id",
            "list_1",
            "--assignee",
            "42",
            "--status",
            "Open",
            "--tag",
            "wanted",
            "--exclude-tag",
            "blocked",
        ],
    )

    assert result.exit_code == 0, result.output
    tasks = json.loads(result.stdout)["result"]["tasks"]
    assert [task["id"] for task in tasks] == ["keep"]


def test_task_list_due_none_is_local_and_include_archived_is_forwarded(
    mock_api: MockClickUpAPI,
) -> None:
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0",
        [task_payload("dated", due_date=1_900_000_000_000, due_date_time=True)],
    )
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0&archived=true",
        [task_payload("none", archived=True)],
    )

    result = invoke(
        mock_api,
        [
            "task",
            "list",
            "--list-id",
            "list_1",
            "--due",
            "none",
            "--include-archived",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [task["id"] for task in json.loads(result.stdout)["result"]["tasks"]] == ["none"]


def test_task_list_today_uses_exact_exclusive_server_bounds(mock_api: MockClickUpAPI) -> None:
    today = datetime.now(UTC).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    start_ms = int(start.timestamp() * 1_000)
    end_ms = start_ms + 86_400_000
    expect_list_tasks(
        mock_api,
        "list_1",
        f"page=0&due_date_gt={start_ms - 1}&due_date_lt={end_ms}",
        [
            task_payload("midnight", due_date=start_ms, due_date_time=True),
            task_payload("tomorrow", due_date=end_ms, due_date_time=True),
        ],
    )

    result = invoke(mock_api, ["task", "list", "--list-id", "list_1", "--due", "today"])

    assert result.exit_code == 0, result.output
    assert [task["id"] for task in json.loads(result.stdout)["result"]["tasks"]] == ["midnight"]


def test_task_list_overdue_and_next_ranges_are_consistent_locally(
    mock_api: MockClickUpAPI,
) -> None:
    today = datetime.now(UTC).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    start_ms = int(start.timestamp() * 1_000)
    expect_list_tasks(
        mock_api,
        "list_1",
        f"page=0&due_date_lt={start_ms}",
        [
            task_payload("past", due_date=start_ms - 1, due_date_time=True),
            task_payload("today", due_date=start_ms, due_date_time=True),
        ],
    )
    expect_list_tasks(
        mock_api,
        "list_1",
        f"page=0&due_date_gt={start_ms - 1}&due_date_lt={start_ms + 172_800_000}",
        [
            task_payload("today", due_date=start_ms, due_date_time=True),
            task_payload("second_day", due_date=start_ms + 86_400_000, due_date_time=True),
            task_payload("boundary", due_date=start_ms + 172_800_000, due_date_time=True),
        ],
    )

    overdue = invoke(mock_api, ["task", "list", "--list-id", "list_1", "--due", "overdue"])
    upcoming = invoke(mock_api, ["task", "list", "--list-id", "list_1", "--due", "next:2d"])

    assert [task["id"] for task in json.loads(overdue.stdout)["result"]["tasks"]] == ["past"]
    assert [task["id"] for task in json.loads(upcoming.stdout)["result"]["tasks"]] == [
        "second_day",
        "today",
    ]


def test_folder_scope_enumerates_lists_before_tasks(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        "/api/v2/folder/folder_1/list?archived=false",
        headers=READ_HEADERS,
        response_json={"lists": [{"id": "list_2", "name": "Two"}]},
    )
    expect_list_tasks(
        mock_api,
        "list_2",
        "page=0",
        [task_payload("task_2", list_id="list_2")],
    )

    result = invoke(mock_api, ["task", "list", "--folder-id", "folder_1"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["tasks"][0]["list_id"] == "list_2"


def test_space_scope_enumerates_folderless_and_folder_lists_and_deduplicates(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "GET",
        "/api/v2/space/space_1/list?archived=false",
        headers=READ_HEADERS,
        response_json={"lists": [{"id": "list_b", "name": "Root"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_1/folder?archived=false",
        headers=READ_HEADERS,
        response_json={"folders": [{"id": "folder_1", "name": "Folder"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/folder/folder_1/list?archived=false",
        headers=READ_HEADERS,
        response_json={"lists": [{"id": "list_a", "name": "Nested"}]},
    )
    expect_list_tasks(
        mock_api,
        "list_a",
        "page=0",
        [task_payload("duplicate", list_id="list_a"), task_payload("task_a", list_id="list_a")],
    )
    expect_list_tasks(
        mock_api,
        "list_b",
        "page=0",
        [task_payload("duplicate", list_id="list_a"), task_payload("task_b", list_id="list_b")],
    )

    result = invoke(mock_api, ["task", "list", "--space-id", "space_1", "--all"])

    assert result.exit_code == 0, result.output
    assert [task["id"] for task in json.loads(result.stdout)["result"]["tasks"]] == [
        "duplicate",
        "task_a",
        "task_b",
    ]


def test_workspace_scope_paginates_until_empty_sorts_and_applies_limit(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "GET",
        "/api/v2/team/workspace_1/task?page=0",
        headers=READ_HEADERS,
        response_json={"tasks": [task_payload("task_z")]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/team/workspace_1/task?page=1",
        headers=READ_HEADERS,
        response_json={"tasks": [task_payload("task_a"), task_payload("task_m")]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/team/workspace_1/task?page=2",
        headers=READ_HEADERS,
        response_json={"tasks": []},
    )

    result = invoke(mock_api, ["task", "list", "--workspace-id", "workspace_1", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert [task["id"] for task in json.loads(result.stdout)["result"]["tasks"]] == [
        "task_a",
        "task_m",
    ]


def test_all_removes_default_result_limit_but_limit_remains_deterministic(
    mock_api: MockClickUpAPI,
) -> None:
    tasks = [task_payload(f"task_{index:03d}") for index in reversed(range(101))]
    for _ in range(2):
        expect_list_tasks(mock_api, "list_1", "page=0", tasks)

    limited = invoke(mock_api, ["task", "list", "--list-id", "list_1"])
    all_results = invoke(mock_api, ["task", "list", "--list-id", "list_1", "--all"])

    limited_tasks = json.loads(limited.stdout)["result"]["tasks"]
    unlimited_tasks = json.loads(all_results.stdout)["result"]["tasks"]
    assert len(limited_tasks) == 100
    assert len(unlimited_tasks) == 101
    assert [task["id"] for task in limited_tasks[:2]] == ["task_000", "task_001"]


def test_repeated_workspace_page_fails_instead_of_looping(mock_api: MockClickUpAPI) -> None:
    for page in range(2):
        mock_api.expect(
            "GET",
            f"/api/v2/team/workspace_1/task?page={page}",
            headers=READ_HEADERS,
            response_json={"tasks": [task_payload("same_task")]},
        )

    result = invoke(mock_api, ["task", "list", "--workspace-id", "workspace_1"])

    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["type"] == "api_error"
    assert "did not advance" in payload["error"]["message"]


def test_workspace_pagination_stops_at_the_hard_page_bound(
    monkeypatch: pytest.MonkeyPatch,
    mock_api: MockClickUpAPI,
) -> None:
    monkeypatch.setattr(discovery, "MAX_TASK_PAGES", 2)
    for page in range(2):
        mock_api.expect(
            "GET",
            f"/api/v2/team/workspace_1/task?page={page}",
            headers=READ_HEADERS,
            response_json={"tasks": [task_payload(f"task_{page}")]},
        )

    result = invoke(mock_api, ["task", "list", "--workspace-id", "workspace_1"])

    assert result.exit_code == 1
    assert "exceeded 2 pages" in json.loads(result.stderr)["error"]["message"]


def test_task_traversal_fails_instead_of_truncating_at_the_safety_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    mock_api: MockClickUpAPI,
) -> None:
    monkeypatch.setattr(discovery, "MAX_TASK_RESULTS", 2)
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0",
        [task_payload("task_1"), task_payload("task_2"), task_payload("task_3")],
    )

    result = invoke(mock_api, ["task", "list", "--list-id", "list_1", "--limit", "2"])

    assert result.exit_code == 1
    assert "safety ceiling of 2 tasks" in json.loads(result.stderr)["error"]["message"]


def test_search_checks_all_content_fields_case_insensitively_and_has_stable_text(
    mock_api: MockClickUpAPI,
) -> None:
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0&include_markdown_description=true",
        [
            task_payload("name", name="Needle in name"),
            task_payload("description", description="Contains NEEDLE"),
            task_payload("text", text_content="needle text"),
            task_payload("markdown", markdown_description="**Needle**"),
            task_payload("miss", description="unrelated"),
        ],
    )

    result = invoke(
        mock_api,
        ["task", "search", "nEeDlE", "--list-id", "list_1"],
        json_output=False,
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "description [Open] Task description",
        "markdown [Open] Task markdown",
        "name [Open] Needle in name",
        "text [Open] Task text",
    ]


def test_exact_name_search_trims_and_does_not_request_markdown(mock_api: MockClickUpAPI) -> None:
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0",
        [
            task_payload("exact", name="  Release Plan  "),
            task_payload("partial", name="Release Plan extra"),
            task_payload("description", description="release plan"),
        ],
    )

    result = invoke(
        mock_api,
        ["task", "search", " release plan ", "--list-id", "list_1", "--exact-name"],
    )

    assert result.exit_code == 0, result.output
    assert [task["id"] for task in json.loads(result.stdout)["result"]["tasks"]] == ["exact"]


def test_deep_workspace_search_enumerates_lists_and_deduplicates_native_ids(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "GET",
        "/api/v2/team/workspace_1/space?archived=false",
        headers=READ_HEADERS,
        response_json={"spaces": [{"id": "space_1", "name": "Space"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_1/list?archived=false",
        headers=READ_HEADERS,
        response_json={"lists": [{"id": "list_1", "name": "Root"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/space/space_1/folder?archived=false",
        headers=READ_HEADERS,
        response_json={"folders": [{"id": "folder_1", "name": "Folder"}]},
    )
    mock_api.expect(
        "GET",
        "/api/v2/folder/folder_1/list?archived=false",
        headers=READ_HEADERS,
        response_json={"lists": [{"id": "list_2", "name": "Nested"}]},
    )
    for list_id in ("list_1", "list_2"):
        expect_list_tasks(
            mock_api,
            list_id,
            "page=0&include_markdown_description=true",
            [task_payload("same", name="Needle", list_id="list_1")],
        )

    result = invoke(
        mock_api,
        ["task", "search", "needle", "--workspace-id", "workspace_1", "--deep"],
    )

    assert result.exit_code == 0, result.output
    assert [task["id"] for task in json.loads(result.stdout)["result"]["tasks"]] == ["same"]
    assert all("/team/workspace_1/task" not in request.path for request in mock_api.state.requests)


def test_ensure_existing_exact_name_is_a_noop_including_closed_subtasks(
    mock_api: MockClickUpAPI,
) -> None:
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0&subtasks=true&include_closed=true",
        [
            task_payload(
                "existing",
                name="  Target Task ",
                status="Closed",
                status_type="closed",
                parent="parent_1",
            )
        ],
    )

    result = invoke(mock_api, ["task", "ensure", " target task ", "--list-id", "list_1"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["created"] is False
    assert payload["task"]["id"] == "existing"
    assert [request.method for request in mock_api.state.requests] == ["GET"]


def test_ensure_ambiguity_fails_closed_with_stable_candidates_and_no_write(
    mock_api: MockClickUpAPI,
) -> None:
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0&subtasks=true&include_closed=true",
        [task_payload("task_b", name="Duplicate"), task_payload("task_a", name=" duplicate ")],
    )

    result = invoke(mock_api, ["task", "ensure", "DUPLICATE", "--list-id", "list_1"])

    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "ambiguous_match"
    assert error["candidate_ids"] == ["task_a", "task_b"]
    assert [task["id"] for task in error["candidates"]] == ["task_a", "task_b"]
    assert [request.method for request in mock_api.state.requests] == ["GET"]


def test_ensure_zero_matches_uses_existing_verified_create_path(mock_api: MockClickUpAPI) -> None:
    expect_list_tasks(
        mock_api,
        "list_1",
        "page=0&subtasks=true&include_closed=true",
        [],
    )
    due_ms = 1_893_542_400_000
    mock_api.expect(
        "POST",
        "/api/v2/list/list_1/task",
        headers=WRITE_HEADERS,
        json_body={
            "assignees": [7, 42],
            "description": "Details",
            "due_date": due_ms,
            "due_date_time": False,
            "name": "New Task",
            "status": "Open",
            "tags": ["One", "Two"],
        },
        response_json={"id": "created_1"},
    )
    mock_api.expect(
        "GET",
        "/api/v2/task/created_1",
        headers=READ_HEADERS,
        response_json=task_payload(
            "created_1",
            name="New Task",
            status="Open",
            assignees=[7, 42],
            tags=["one", "two"],
            due_date=due_ms,
            due_date_time=False,
            description="Details",
        ),
    )

    result = invoke(
        mock_api,
        [
            "task",
            "ensure",
            " New Task ",
            "--list-id",
            "list_1",
            "--description",
            "Details",
            "--status",
            "Open",
            "--assignee",
            "42",
            "--assignee",
            "7",
            "--due-date",
            "2030-01-02",
            "--tag",
            "One",
            "--tag",
            "Two",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["created"] is True
    assert payload["task"]["id"] == "created_1"
    assert [request.method for request in mock_api.state.requests] == ["GET", "POST", "GET"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["task", "ensure", " ", "--list-id", "list_1"],
        ["task", "ensure", "Task", "--list-id", "../unsafe"],
        ["task", "ensure", "Task", "--list-id", "list_1", "--tag", " "],
        ["task", "ensure", "Task", "--list-id", "list_1", "--due-date", "tomorrow"],
    ],
)
def test_malformed_ensure_inputs_make_no_write_or_read(
    mock_api: MockClickUpAPI, arguments: list[str]
) -> None:
    result = invoke(mock_api, arguments)

    assert result.exit_code != 0
    assert mock_api.state.requests == []


def test_task_response_shape_failure_is_typed(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        "/api/v2/list/list_1/task?page=0",
        headers=READ_HEADERS,
        response_json={"unexpected": []},
    )

    result = invoke(mock_api, ["task", "list", "--list-id", "list_1"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "api_error"


def test_new_client_paths_validate_ids_and_pages_before_network(
    mock_api: MockClickUpAPI,
) -> None:
    with ClickUpClient(token=AUTH_VALUE, base_url=mock_api.base_url) as client:
        operations: tuple[Callable[[], object], ...] = (
            lambda: client.get_spaces("../unsafe", archived=False),
            lambda: client.get_folders("../unsafe", archived=False),
            lambda: client.get_space_lists("../unsafe", archived=False),
            lambda: client.get_folder_lists("../unsafe", archived=False),
            lambda: client.get_list_tasks("../unsafe", page=0),
            lambda: client.get_workspace_tasks("../unsafe", page=0),
        )
        for operation in operations:
            with pytest.raises(TaskReferenceError):
                operation()
        with pytest.raises(InvalidOperationError):
            client.get_list_tasks("list_1", page=-1)

    assert mock_api.state.requests == []


def test_empty_task_list_text_is_stable(mock_api: MockClickUpAPI) -> None:
    expect_list_tasks(mock_api, "list_1", "page=0", [])

    result = invoke(mock_api, ["task", "list", "--list-id", "list_1"], json_output=False)

    assert result.exit_code == 0, result.output
    assert result.stdout == "No tasks\n"
