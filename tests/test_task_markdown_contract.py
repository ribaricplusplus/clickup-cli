"""Task Markdown contracts over real localhost HTTP sockets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from clickup_cli.cli import app
from clickup_cli.client import ClickUpClient
from clickup_cli.errors import InvalidOperationError
from tests.conftest import MockClickUpAPI
from tests.test_task_mutations_contract import LIST_ID as LIST_ID
from tests.test_task_mutations_contract import TASK_ID as TASK_ID
from tests.test_task_mutations_contract import task_payload as task_payload

AUTH = "markdown-contract-auth"
READ = {"Accept": "application/json", "Authorization": AUTH}
WRITE = {**READ, "Content-Type": "application/json"}
TASK_PATH = f"/api/v2/task/{TASK_ID}"
MD_PATH = TASK_PATH + "?include_markdown_description=true"
runner = CliRunner()


def invoke(api: MockClickUpAPI, args: list[str]) -> Result:
    return runner.invoke(
        app,
        ["--json", "--base-url", api.base_url, *args],
        env={"CLICKUP_API_TOKEN": AUTH},
    )


def expect_create(api: MockClickUpAPI, sent: str, returned: object) -> None:
    api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE,
        json_body={"name": "Example", "markdown_content": sent},
        response_json={"id": TASK_ID},
    )
    api.expect(
        "GET",
        MD_PATH,
        headers=READ,
        response_json={
            **task_payload(name="Example", description="Goal"),
            "markdown_description": returned,
        },
    )


def test_create_markdown_writes_only_markdown_content_and_keeps_readback(
    mock_api: MockClickUpAPI,
) -> None:
    expect_create(mock_api, "## Goal", "## Goal")
    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Example",
            "--list-id",
            LIST_ID,
            "--description",
            "## Goal",
            "--description-format",
            "markdown",
        ],
    )
    assert result.exit_code == 0, result.output
    task = json.loads(result.stdout)["result"]["task"]
    assert task["description"] == "Goal"
    assert task["markdown_description"] == "## Goal"
    assert task["description_format"] == "markdown"
    assert [request.method for request in mock_api.state.requests] == ["POST", "GET"]


def test_create_accepts_provider_bullet_spacing_without_losing_structure(
    mock_api: MockClickUpAPI,
) -> None:
    sent = (
        "# Goal\r\n\r\n**Bold** and `code` [source](https://example.invalid/a).\r\n\r\n"
        "- First item\r\n- Second item\r\n\r\n> Quote\r\n"
    )
    returned = (
        "# Goal\n\n**Bold** and `code` [source](https://example.invalid/a).\n\n"
        "*   First item\n*   Second item\n\n> Quote"
    )
    expect_create(mock_api, sent, returned)
    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Example",
            "--list-id",
            LIST_ID,
            "--description",
            sent,
            "--description-format",
            "markdown",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["markdown_description"] == returned


def expect_markdown_task(api: MockClickUpAPI, description: str, markdown: object) -> None:
    api.expect(
        "GET",
        MD_PATH,
        headers=READ,
        response_json={**task_payload(description=description), "markdown_description": markdown},
    )


def test_update_repairs_plain_literal_heading_then_identical_update_is_noop(
    mock_api: MockClickUpAPI,
) -> None:
    expect_markdown_task(mock_api, "## Goal", r"\## Goal")
    mock_api.expect(
        "PUT",
        TASK_PATH,
        headers=WRITE,
        json_body={"markdown_content": "## Goal"},
        response_json={},
    )
    expect_markdown_task(mock_api, "Goal", "## Goal")
    expect_markdown_task(mock_api, "Goal", "## Goal")
    args = [
        "task",
        "update",
        TASK_ID,
        "--description",
        "## Goal",
        "--description-format",
        "markdown",
    ]
    first = invoke(mock_api, args)
    second = invoke(mock_api, args)
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    changed = json.loads(first.stdout)["result"]
    unchanged = json.loads(second.stdout)["result"]
    assert changed["changed"] is True
    assert changed["fields"] == ["description"]
    assert unchanged["changed"] is False
    assert unchanged["task"]["description_format"] == "markdown"
    assert unchanged["task"]["markdown_description"] == "## Goal"
    assert [request.method for request in mock_api.state.requests] == ["GET", "PUT", "GET", "GET"]


@pytest.mark.parametrize("description_format", ["markdown", "plain"])
@pytest.mark.parametrize("value", ["", " "])
@pytest.mark.parametrize("command", ["create", "update"])
def test_logical_empty_description_uses_single_space_wire(
    mock_api: MockClickUpAPI,
    description_format: str,
    value: str,
    command: str,
) -> None:
    markdown = description_format == "markdown"
    key = "markdown_content" if markdown else "description"
    path = MD_PATH if markdown else TASK_PATH
    if command == "update":
        mock_api.expect(
            "GET",
            path,
            headers=READ,
            response_json={**task_payload(), "markdown_description": "Old description"},
        )
    body = {key: " "}
    if command == "create":
        body["name"] = "Example"
    mock_api.expect(
        "POST" if command == "create" else "PUT",
        f"/api/v2/list/{LIST_ID}/task" if command == "create" else TASK_PATH,
        headers=WRITE,
        json_body=body,
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET",
        path,
        headers=READ,
        response_json={**task_payload(name="Example", description=""), "markdown_description": ""},
    )
    args = ["task", command, "Example" if command == "create" else TASK_ID]
    if command == "create":
        args += ["--list-id", LIST_ID]
    result = invoke(
        mock_api,
        [*args, "--description", value, "--description-format", description_format],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["description"] == ""


def test_ensure_markdown_file_creates_then_existing_match_is_unchanged(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
) -> None:
    path = tmp_path / "description.md"
    path.write_text("## Goal", encoding="utf-8")
    search_path = f"/api/v2/list/{LIST_ID}/task?page=0&subtasks=true&include_closed=true"
    mock_api.expect(
        "GET",
        search_path,
        headers=READ,
        response_json={"tasks": [], "last_page": True},
    )
    expect_create(mock_api, "## Goal", "## Goal")
    # An existing name is returned unchanged, even if the requested body/format differ.
    mock_api.expect(
        "GET",
        search_path,
        headers=READ,
        response_json={
            "tasks": [task_payload(name="Example", description="Keep original")],
            "last_page": True,
        },
    )
    args = [
        "task",
        "ensure",
        "Example",
        "--list-id",
        LIST_ID,
        "--description-file",
        str(path),
        "--description-format",
        "markdown",
    ]
    first = invoke(mock_api, args)
    second = invoke(mock_api, args)
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    assert json.loads(first.stdout)["result"]["task"]["description_format"] == "markdown"
    found = json.loads(second.stdout)["result"]
    assert found["created"] is False
    assert found["task"]["description"] == "Keep original"
    assert "description_format" not in found["task"]
    assert [request.method for request in mock_api.state.requests] == ["GET", "POST", "GET", "GET"]


@pytest.mark.parametrize("json_output", [True, False])
def test_task_show_can_retrieve_readable_markdown_description(
    mock_api: MockClickUpAPI,
    json_output: bool,
) -> None:
    expect_markdown_task(mock_api, "Goal", "## Goal\n\n**Bold**")
    args = [
        "--base-url",
        mock_api.base_url,
        "task",
        "show",
        TASK_ID,
        "--description-format",
        "markdown",
    ]
    if json_output:
        args.insert(0, "--json")
    result = runner.invoke(app, args, env={"CLICKUP_API_TOKEN": AUTH})
    assert result.exit_code == 0, result.output
    if json_output:
        task = json.loads(result.stdout)["result"]["task"]
        assert task["description"] == "Goal"
        assert task["markdown_description"] == "## Goal\n\n**Bold**"
        assert task["description_format"] == "markdown"
    else:
        assert "Description (Markdown):\n## Goal\n\n**Bold**" in result.stdout


@pytest.mark.parametrize("operation", ["create", "update"])
def test_transport_rejects_both_description_wire_fields_before_network(
    mock_api: MockClickUpAPI,
    operation: str,
) -> None:
    with ClickUpClient(token=AUTH, base_url=mock_api.base_url) as client:
        with pytest.raises(InvalidOperationError, match=r"description.*markdown_content"):
            if operation == "create":
                client.create_task(
                    LIST_ID, "Example", description="Plain", markdown_content="**MD**"
                )
            else:
                client.update_task(TASK_ID, {"description": "Plain", "markdown_content": "**MD**"})
    assert mock_api.state.requests == []


@pytest.mark.parametrize("value", [123, False, ["text"]])
def test_transport_rejects_non_text_markdown_before_network(
    mock_api: MockClickUpAPI,
    value: object,
) -> None:
    with ClickUpClient(token=AUTH, base_url=mock_api.base_url) as client:
        with pytest.raises(InvalidOperationError, match="Markdown content must be text"):
            client.create_task(LIST_ID, "Example", markdown_content=value)  # type: ignore[arg-type]
    assert mock_api.state.requests == []
