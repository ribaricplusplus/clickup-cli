from __future__ import annotations

import json

import pytest

from clickup_cli.cli import main


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
