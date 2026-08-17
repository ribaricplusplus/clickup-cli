from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from clickup_cli.cli import app, main

runner = CliRunner()


@pytest.mark.parametrize(
    ("arguments", "message_fragment"),
    [
        (["--json", "task", "set-status", "task_123"], "Missing argument 'STATUS'"),
        (["--json", "task", "create", "Synthetic task"], "Missing option '--list-id'"),
        (
            [
                "--json",
                "task",
                "create",
                "Synthetic task",
                "--list-id",
                "list_456",
                "--assignee",
                "not-an-integer",
            ],
            "Invalid value for '--assignee'",
        ),
    ],
)
def test_json_usage_errors_are_machine_readable(
    arguments: list[str],
    message_fragment: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(arguments, prog_name="clickup")
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "usage_error"
    assert message_fragment in payload["error"]["message"]


def test_new_task_operations_are_discoverable_without_credentials() -> None:
    task_help = runner.invoke(app, ["task", "--help"], env={"CLICKUP_API_TOKEN": ""})

    assert task_help.exit_code == 0, task_help.output
    for command in ("comment", "due-date", "assign", "unassign"):
        assert command in task_help.stdout

    comment_help = runner.invoke(app, ["task", "comment", "--help"], env={"CLICKUP_API_TOKEN": ""})
    due_date_help = runner.invoke(
        app, ["task", "due-date", "--help"], env={"CLICKUP_API_TOKEN": ""}
    )

    assert comment_help.exit_code == 0, comment_help.output
    assert "add" in comment_help.stdout
    assert "list" in comment_help.stdout
    assert due_date_help.exit_code == 0, due_date_help.output
    assert "set" in due_date_help.stdout
    assert "clear" in due_date_help.stdout
