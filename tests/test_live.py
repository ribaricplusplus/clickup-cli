from __future__ import annotations

import json
import os
import uuid
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from clickup_cli.cli import app
from clickup_cli.client import ClickUpClient
from clickup_cli.config import resolve_base_url
from clickup_cli.domain import list_statuses, task_status
from clickup_cli.errors import APIError

pytestmark = pytest.mark.live


def _invoke(runner: CliRunner, base_url: str, token: str, *args: str) -> dict[str, Any]:
    result = runner.invoke(
        app,
        ["--base-url", base_url, "--json", *args],
        env={"CLICKUP_API_TOKEN": token},
    )
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["ok"] is True
    return cast(dict[str, Any], payload["result"])


@pytest.mark.skipif(
    os.environ.get("CLICKUP_LIVE_TEST") != "1",
    reason="set CLICKUP_LIVE_TEST=1 to enable the destructive sandbox live test",
)
def test_live_sandbox_cli_lifecycle() -> None:
    token = os.environ.get("CLICKUP_API_TOKEN")
    list_id = os.environ.get("CLICKUP_TEST_LIST_ID")
    if not token or not list_id:
        pytest.fail("CLICKUP_API_TOKEN and CLICKUP_TEST_LIST_ID are required")
    assert token is not None
    assert list_id is not None

    base_url = resolve_base_url(None)
    runner = CliRunner()
    task_id: str | None = None

    with ClickUpClient(token=token, base_url=base_url) as client:
        try:
            identity = _invoke(runner, base_url, token, "auth", "whoami")
            assert identity["user"]["id"] is not None

            created = _invoke(
                runner,
                base_url,
                token,
                "task",
                "create",
                f"clickup-cli-live-{uuid.uuid4()}",
                "--list-id",
                list_id,
                "--description",
                "Temporary task created by the opt-in live CLI test.",
            )
            task_id = str(created["task"]["id"])

            shown = _invoke(runner, base_url, token, "task", "show", task_id)
            assert shown["task"]["id"] == task_id
            assert shown["task"]["list_id"] == list_id

            current_result = _invoke(runner, base_url, token, "task", "status", task_id)
            current = str(current_result["status"])
            assert task_status(client.get_task(task_id)) == current

            statuses = list_statuses(client.get_list(list_id))
            alternative = next(
                (
                    status.label
                    for status in statuses
                    if status.label.casefold() != current.casefold()
                    and (status.status_type or "").casefold() not in {"done", "closed"}
                ),
                None,
            )
            assert alternative is not None, (
                "the live sandbox List needs a second nonterminal status for set-status testing"
            )

            changed = _invoke(
                runner,
                base_url,
                token,
                "task",
                "set-status",
                task_id,
                alternative,
            )
            assert changed["changed"] is True
            assert changed["status"] == alternative
            assert task_status(client.get_task(task_id)) == alternative

            completed = _invoke(runner, base_url, token, "task", "complete", task_id)
            assert completed["changed"] is True
            assert completed["status"].casefold() in {"completed", "complete", "done", "closed"}

            readback = client.get_task(task_id)
            assert task_status(readback) == completed["status"]
            status_payload = readback.get("status")
            assert isinstance(status_payload, dict)
            assert str(status_payload.get("type", "")).casefold() in {"done", "closed"}

            final_status = _invoke(runner, base_url, token, "task", "status", task_id)
            assert final_status["status"] == completed["status"]

            deleted = _invoke(runner, base_url, token, "task", "delete", task_id, "--yes")
            assert deleted == {"deleted": True, "task_id": task_id}
            with pytest.raises(APIError, match="HTTP 404"):
                client.get_task(task_id)
            task_id = None
        finally:
            if task_id is not None:
                try:
                    client.delete_task(task_id)
                except APIError as exc:
                    if "HTTP 404" not in str(exc):
                        raise
