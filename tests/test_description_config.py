"""Token-free task description configuration and precedence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clickup_cli.config import resolve_settings
from clickup_cli.errors import ConfigurationError
from tests.conftest import MockClickUpAPI
from tests.test_task_markdown_contract import (
    LIST_ID,
    MD_PATH,
    READ,
    TASK_ID,
    TASK_PATH,
    WRITE,
    expect_create,
    invoke,
    task_payload,
)


def test_description_format_settings_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'default_profile = "work"\n[profiles.work]\ndescription_format = "markdown"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CLICKUP_DESCRIPTION_FORMAT", raising=False)
    assert resolve_settings(None, None, None, config_path=config).description_format == "markdown"
    monkeypatch.setenv("CLICKUP_DESCRIPTION_FORMAT", "plain")
    assert resolve_settings(None, None, None, config_path=config).description_format == "plain"
    assert (
        resolve_settings(
            None,
            None,
            None,
            description_format="markdown",
            config_path=config,
        ).description_format
        == "markdown"
    )
    monkeypatch.delenv("CLICKUP_DESCRIPTION_FORMAT")
    assert (
        resolve_settings(
            None,
            None,
            None,
            config_path=tmp_path / "missing.toml",
        ).description_format
        == "plain"
    )


def test_cli_create_uses_environment_description_format(
    mock_api: MockClickUpAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLICKUP_DESCRIPTION_FORMAT", "markdown")
    expect_create(mock_api, "## Goal", "## Goal")
    result = invoke(
        mock_api,
        ["task", "create", "Example", "--list-id", LIST_ID, "--description", "## Goal"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["description_format"] == "markdown"


@pytest.mark.parametrize("command", ["create", "ensure", "update"])
@pytest.mark.parametrize("source", ["profile", "environment", "cli", "cli-plain"])
def test_cli_description_format_precedence_for_each_write_command(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    source: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config/clickup-cli/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'default_profile = "work"\n[profiles.work]\ndescription_format = "markdown"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CLICKUP_DESCRIPTION_FORMAT", raising=False)
    if source != "profile":
        monkeypatch.setenv(
            "CLICKUP_DESCRIPTION_FORMAT", "markdown" if source == "cli-plain" else "plain"
        )
    markdown = source not in {"environment", "cli-plain"}
    read_path = MD_PATH if markdown else TASK_PATH
    body = {"markdown_content" if markdown else "description": "## Goal"}
    args = ["task", command, TASK_ID if command == "update" else "Example"]
    if command == "update":
        mock_api.expect(
            "GET",
            read_path,
            headers=READ,
            response_json={**task_payload(), "markdown_description": "Old description"},
        )
    else:
        args += ["--list-id", LIST_ID]
        body["name"] = "Example"
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
        json_body=body,
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET",
        read_path,
        headers=READ,
        response_json={
            **task_payload(name="Example", description="Goal" if markdown else "## Goal"),
            "markdown_description": "## Goal",
        },
    )
    args += ["--description", "## Goal"]
    if source == "cli":
        args += ["--description-format", "markdown"]
    elif source == "cli-plain":
        args += ["--description-format", "plain"]
    result = invoke(mock_api, args)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("command", ["create", "ensure", "update", "show"])
@pytest.mark.parametrize("value", ["html", "Markdown", "", " markdown"])
def test_invalid_cli_description_format_fails_before_network(
    mock_api: MockClickUpAPI,
    command: str,
    value: str,
) -> None:
    args = ["task", command, TASK_ID if command in {"update", "show"} else "Example"]
    if command in {"create", "ensure"}:
        args += ["--list-id", LIST_ID]
    result = invoke(mock_api, [*args, "--description-format", value])
    assert result.exit_code == 1
    assert (
        "Description format must be markdown or plain"
        in json.loads(result.stderr)["error"]["message"]
    )
    assert mock_api.state.requests == []


@pytest.mark.parametrize("value", ["html", "Markdown", ""])
def test_invalid_environment_description_format_fails_before_network(
    mock_api: MockClickUpAPI,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("CLICKUP_DESCRIPTION_FORMAT", value)
    result = invoke(mock_api, ["task", "show", TASK_ID])
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "configuration_error"
    assert mock_api.state.requests == []


@pytest.mark.parametrize("value", ['"html"', '"Markdown"', '""', "true", "123"])
def test_invalid_profile_description_format_is_rejected(
    tmp_path: Path,
    value: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f"[profiles.work]\ndescription_format = {value}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        resolve_settings("work", None, None, config_path=config)


def test_batch_descriptions_remain_plain_even_with_markdown_default(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLICKUP_DESCRIPTION_FORMAT", "markdown")
    path = tmp_path / "manifest.jsonl"
    path.write_text(
        json.dumps({"task": TASK_ID, "set": {"description": "## Goal"}}) + "\n", encoding="utf-8"
    )
    # Full batch preflight, then the focused plain mutation/readback.
    for _ in range(2):
        mock_api.expect("GET", TASK_PATH, headers=READ, response_json=task_payload())
    mock_api.expect(
        "PUT", TASK_PATH, headers=WRITE, json_body={"description": "## Goal"}, response_json={}
    )
    mock_api.expect(
        "GET", TASK_PATH, headers=READ, response_json=task_payload(description="## Goal")
    )
    result = invoke(mock_api, ["task", "batch", "apply", str(path), "--yes"])
    assert result.exit_code == 0, result.output
    assert [req.method for req in mock_api.state.requests] == ["GET", "GET", "PUT", "GET"]
