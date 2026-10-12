"""Regression for ClickUp's verified automatic bare-URL link exports."""

from __future__ import annotations

import json

from tests.conftest import MockClickUpAPI
from tests.test_task_markdown_contract import LIST_ID, expect_create, invoke


def test_markdown_create_accepts_clickup_bare_url_autolinking(mock_api: MockClickUpAPI) -> None:
    sent = "## Goal\n\nNotes: https://example.invalid/docs/one\nhttps://example.invalid/tasks/two\n"
    exported = (
        "## Goal\n\nNotes: [https://example.invalid/docs/one](https://example.invalid/docs/one)\n"
        "[https://example.invalid/tasks/two](https://example.invalid/tasks/two)"
    )
    expect_create(mock_api, sent, exported)
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
    assert json.loads(result.stdout)["result"]["task"]["markdown_description"] == exported
