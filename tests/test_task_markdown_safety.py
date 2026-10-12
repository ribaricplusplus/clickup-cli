"""Expanded safety regressions for the established Markdown vertical slices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import MockClickUpAPI
from tests.test_task_markdown_contract import (
    LIST_ID,
    MD_PATH,
    READ,
    TASK_ID,
    TASK_PATH,
    WRITE,
    expect_create,
    expect_markdown_task,
    invoke,
    task_payload,
)


@pytest.mark.parametrize("command", ["create", "update"])
@pytest.mark.parametrize(
    ("sent", "returned"),
    [
        ("## Goal", r"\## Goal"),
        ("## Goal", "Goal"),
        ("- First item", r"\- First item"),
        ("**Bold**", r"\*\*Bold\*\*"),
        ("**Bold**", "Bold"),
        ("[source](https://example.invalid/a)", "source"),
        ("[source](https://example.invalid/a)", "[source](https://example.invalid/b)"),
        ("`a  b`", "`a b`"),
        ("```python\nx = 1\n```", "```python\nx = 2\n```"),
        ("> Quote", "Quote"),
        ("First\n\nSecond", "First"),
        ("## Goal", None),
        ("## Goal", 123),
    ],
)
def test_markdown_verification_rejects_format_link_code_or_content_loss(
    mock_api: MockClickUpAPI,
    command: str,
    sent: str,
    returned: object,
) -> None:
    if command == "create":
        expect_create(mock_api, sent, returned)
        args = ["task", "create", "Example", "--list-id", LIST_ID]
    else:
        expect_markdown_task(mock_api, "Before", "Before")
        mock_api.expect(
            "PUT",
            TASK_PATH,
            headers=WRITE,
            json_body={"markdown_content": sent},
            response_json={},
        )
        # Raw text equality is never a substitute for the structured export.
        expect_markdown_task(mock_api, sent, returned)
        args = ["task", "update", TASK_ID]
    result = invoke(mock_api, [*args, "--description", sent, "--description-format", "markdown"])
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    if command == "create":
        assert (error["type"], error["task_id"]) == ("created_but_unverified", TASK_ID)
        assert [req.method for req in mock_api.state.requests] == ["POST", "GET"]
    else:
        assert error["type"] == "verification_failed"
        assert [req.method for req in mock_api.state.requests] == ["GET", "PUT", "GET"]


@pytest.mark.parametrize("command", ["create", "ensure", "update"])
@pytest.mark.parametrize("value", ["## Goal\n", ""])
def test_markdown_description_file_has_same_wire_contract_as_option(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
    command: str,
    value: str,
) -> None:
    path = tmp_path / "description.md"
    path.write_text(value, encoding="utf-8")
    args = ["task", command, TASK_ID if command == "update" else "Example"]
    if command == "update":
        expect_markdown_task(mock_api, "Before", "Before")
        mock_api.expect(
            "PUT",
            TASK_PATH,
            headers=WRITE,
            json_body={"markdown_content": value or " "},
            response_json={},
        )
        expect_markdown_task(mock_api, "Goal" if value else "", value.rstrip("\n"))
    else:
        args += ["--list-id", LIST_ID]
        if command == "ensure":
            mock_api.expect(
                "GET",
                f"/api/v2/list/{LIST_ID}/task?page=0&subtasks=true&include_closed=true",
                headers=READ,
                response_json={"tasks": [], "last_page": True},
            )
        expect_create(mock_api, value or " ", value.rstrip("\n"))
    result = invoke(
        mock_api, [*args, "--description-file", str(path), "--description-format", "markdown"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["markdown_description"] == value.rstrip("\n")


def test_semantic_markdown_update_accepts_normalization_then_is_noop(
    mock_api: MockClickUpAPI,
) -> None:
    requested = "- First item\n- Second item\n"
    returned = "*   First item\n*   Second item"
    expect_markdown_task(mock_api, "Before", "Before")
    mock_api.expect(
        "PUT",
        TASK_PATH,
        headers=WRITE,
        json_body={"markdown_content": requested},
        response_json={},
    )
    expect_markdown_task(mock_api, "First item\nSecond item", returned)
    expect_markdown_task(mock_api, "First item\nSecond item", returned)
    args = [
        "task",
        "update",
        TASK_ID,
        "--description",
        requested,
        "--description-format",
        "markdown",
    ]
    first, second = invoke(mock_api, args), invoke(mock_api, args)
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    assert json.loads(first.stdout)["result"]["changed"] is True
    assert json.loads(second.stdout)["result"]["changed"] is False


@pytest.mark.parametrize("value", ["", " "])
def test_single_space_markdown_clear_is_noop_for_logical_empty_export(
    mock_api: MockClickUpAPI,
    value: str,
) -> None:
    expect_markdown_task(mock_api, "", value)
    result = invoke(
        mock_api,
        ["task", "update", TASK_ID, "--description", " ", "--description-format", "markdown"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False
    assert json.loads(result.stdout)["result"]["task"]["markdown_description"] == ""


@pytest.mark.parametrize("command", ["create", "ensure", "update"])
def test_markdown_mode_without_description_never_writes_markdown(
    mock_api: MockClickUpAPI,
    command: str,
) -> None:
    args = ["task", command, TASK_ID if command == "update" else "Example"]
    if command == "update":
        mock_api.expect("GET", TASK_PATH, headers=READ, response_json=task_payload())
        args += ["--name", "Example"]
    else:
        args += ["--list-id", LIST_ID]
        if command == "ensure":
            mock_api.expect(
                "GET",
                f"/api/v2/list/{LIST_ID}/task?page=0&subtasks=true&include_closed=true",
                headers=READ,
                response_json={"tasks": [], "last_page": True},
            )
    mock_api.expect(
        "PUT" if command == "update" else "POST",
        TASK_PATH if command == "update" else f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE,
        json_body={"name": "Example"},
        response_json={"id": TASK_ID},
    )
    mock_api.expect("GET", TASK_PATH, headers=READ, response_json=task_payload(name="Example"))
    result = invoke(mock_api, [*args, "--description-format", "markdown"])
    assert result.exit_code == 0, result.output
    assert "description_format" not in json.loads(result.stdout)["result"]["task"]


@pytest.mark.parametrize("command", ["create", "ensure", "update"])
def test_description_input_options_still_exclude_each_other_in_markdown_mode(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
    command: str,
) -> None:
    args = ["task", command, TASK_ID if command == "update" else "Example"]
    if command != "update":
        args += ["--list-id", LIST_ID]
    result = invoke(
        mock_api,
        [
            *args,
            "--description-format",
            "markdown",
            "--description",
            "",
            "--description-file",
            str(tmp_path / "not-read.md"),
        ],
    )
    assert result.exit_code == 1
    assert "exactly one" in json.loads(result.stderr)["error"]["message"]
    assert mock_api.state.requests == []


@pytest.mark.parametrize("failure", ["disconnect", "malformed", "readback"])
def test_markdown_create_never_blindly_retries_and_preserves_known_id(
    mock_api: MockClickUpAPI,
    failure: str,
) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE,
        json_body={"name": "Example", "markdown_content": "## Goal"},
        response_json={"id": TASK_ID},
        disconnect=failure == "disconnect",
        response_body=b"not-json" if failure == "malformed" else None,
    )
    if failure == "readback":
        mock_api.expect(
            "GET",
            MD_PATH,
            headers=READ,
            response_status=503,
            response_json={"err": "Readback unavailable"},
        )
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
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert sum(req.method == "POST" for req in mock_api.state.requests) == 1
    if failure == "readback":
        assert (error["type"], error["task_id"]) == ("created_but_unverified", TASK_ID)
    else:
        assert error["type"] == "outcome_unknown"
