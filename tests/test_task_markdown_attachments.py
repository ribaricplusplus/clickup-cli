"""Markdown create output must survive attachment readbacks."""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import MockClickUpAPI, MultipartPart
from tests.test_task_markdown_contract import (
    LIST_ID,
    MD_PATH,
    READ,
    TASK_PATH,
    expect_create,
    invoke,
    task_payload,
)


def test_markdown_create_with_attachment_keeps_final_markdown_readback(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"note")
    expect_create(mock_api, "## Goal", "## Goal")
    attachment = {"id": "attachment_1", "title": "note.txt"}
    mock_api.expect(
        "POST",
        TASK_PATH + "/attachment",
        headers=READ,
        multipart_body=(
            MultipartPart("attachment", "note.txt", "application/octet-stream", b"note"),
        ),
        response_json=attachment,
    )
    final = {**task_payload(name="Example", description="Goal"), "attachments": [attachment]}
    mock_api.expect("GET", TASK_PATH, headers=READ, response_json=final)
    mock_api.expect(
        "GET", MD_PATH, headers=READ, response_json={**final, "markdown_description": "## Goal"}
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
            "--attach",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    task = json.loads(result.stdout)["result"]["task"]
    assert task["markdown_description"] == "## Goal"
    assert task["description_format"] == "markdown"
    assert task["attachments"][0]["id"] == "attachment_1"
