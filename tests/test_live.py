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
from clickup_cli.domain import (
    list_statuses,
    parse_due_date,
    task_assignee_ids,
    task_due_date,
    task_status,
)
from clickup_cli.errors import APIError

pytestmark = pytest.mark.live


def _invoke(runner: CliRunner, base_url: str, *args: str) -> dict[str, Any]:
    result = runner.invoke(
        app,
        ["--base-url", base_url, "--json", *args],
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
    run_id = uuid.uuid4()

    with ClickUpClient(token=token, base_url=base_url) as client:
        try:
            identity = _invoke(runner, base_url, "auth", "whoami")
            assert identity["user"]["id"] is not None
            user_id = int(identity["user"]["id"])

            created = _invoke(
                runner,
                base_url,
                "task",
                "create",
                f"clickup-cli-live-{run_id}",
                "--list-id",
                list_id,
                "--description",
                "Temporary task created by the opt-in live CLI test.",
            )
            task_id = str(created["task"]["id"])

            shown = _invoke(runner, base_url, "task", "show", task_id)
            assert shown["task"]["id"] == task_id
            assert shown["task"]["list_id"] == list_id

            comment_text = f"Disposable clickup-cli comment {run_id}"
            added_comment = _invoke(
                runner,
                base_url,
                "task",
                "comment",
                "add",
                task_id,
                comment_text,
            )
            assert added_comment["comment"]["text"] == comment_text
            comments = _invoke(runner, base_url, "task", "comment", "list", task_id)
            assert any(
                comment["id"] == added_comment["comment"]["id"] and comment["text"] == comment_text
                for comment in comments["comments"]
            )

            date_only = parse_due_date("2030-01-02")
            date_result = _invoke(
                runner,
                base_url,
                "task",
                "due-date",
                "set",
                task_id,
                "2030-01-02",
            )
            assert date_result["changed"] is True
            assert date_result["due_date"] == date_only.display
            assert date_result["due_date_time"] is False
            assert (
                task_due_date(client.get_task(task_id)).milliseconds == date_result["due_date_ms"]
            )

            timed = parse_due_date("2030-01-02T15:04:05+01:00")
            timed_result = _invoke(
                runner,
                base_url,
                "task",
                "due-date",
                "set",
                task_id,
                "2030-01-02T15:04:05+01:00",
            )
            assert timed_result["changed"] is True
            assert timed_result["due_date"] == "2030-01-02T14:04:05Z"
            assert timed_result["due_date_time"] is True
            assert task_due_date(client.get_task(task_id)).milliseconds == timed.milliseconds

            cleared = _invoke(runner, base_url, "task", "due-date", "clear", task_id)
            assert cleared["changed"] is True
            assert task_due_date(client.get_task(task_id)).milliseconds is None

            if user_id in task_assignee_ids(client.get_task(task_id)):
                initially_unassigned = _invoke(
                    runner,
                    base_url,
                    "task",
                    "unassign",
                    task_id,
                    str(user_id),
                )
                assert initially_unassigned["assigned"] is False
                assert user_id not in task_assignee_ids(client.get_task(task_id))

            assigned = _invoke(runner, base_url, "task", "assign", task_id, str(user_id))
            assert assigned["changed"] is True
            assert assigned["assigned"] is True
            assert user_id in task_assignee_ids(client.get_task(task_id))

            unassigned = _invoke(runner, base_url, "task", "unassign", task_id, str(user_id))
            assert unassigned["changed"] is True
            assert unassigned["assigned"] is False
            assert user_id not in task_assignee_ids(client.get_task(task_id))

            current_result = _invoke(runner, base_url, "task", "status", task_id)
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
                "task",
                "set-status",
                task_id,
                alternative,
            )
            assert changed["changed"] is True
            assert changed["status"] == alternative
            assert task_status(client.get_task(task_id)) == alternative

            completed = _invoke(runner, base_url, "task", "complete", task_id)
            assert completed["changed"] is True
            assert completed["status"].casefold() in {"completed", "complete", "done", "closed"}

            readback = client.get_task(task_id)
            assert task_status(readback) == completed["status"]
            status_payload = readback.get("status")
            assert isinstance(status_payload, dict)
            assert str(status_payload.get("type", "")).casefold() in {"done", "closed"}

            final_status = _invoke(runner, base_url, "task", "status", task_id)
            assert final_status["status"] == completed["status"]

            deleted = _invoke(runner, base_url, "task", "delete", task_id, "--yes")
            assert deleted == {"deleted": True, "task_id": task_id}
            with pytest.raises(APIError, match="HTTP 404"):
                client.get_task(task_id)
            print(f"live_cleanup task_id={task_id} post_delete=HTTP_404")
            task_id = None
        finally:
            if task_id is not None:
                try:
                    client.delete_task(task_id)
                except APIError as exc:
                    if "HTTP 404" not in str(exc):
                        raise
