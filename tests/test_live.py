from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from typer.testing import CliRunner

from clickup_cli.cli import app
from clickup_cli.client import ClickUpClient
from clickup_cli.config import resolve_base_url
from clickup_cli.domain import (
    list_statuses,
    parse_due_date,
    summarize_task,
    task_assignee_ids,
    task_due_date,
    task_status,
)
from tests.live_safety import (
    OwnedTask,
    OwnedTimeEntry,
    delete_owned_task,
    delete_owned_time_entry,
    prove_sandbox_destination,
)

pytestmark = pytest.mark.live

_TASK_PARTIAL_TYPES = {"created_but_attachment_failed", "created_but_unverified"}


def _error_payload(stderr: str) -> dict[str, Any]:
    if not stderr:
        return {}
    payload = json.loads(stderr)
    error = payload.get("error")
    return cast(dict[str, Any], error) if isinstance(error, dict) else {}


def _invoke(
    runner: CliRunner,
    base_url: str,
    *args: str,
    owned_tasks: dict[str, OwnedTask] | None = None,
    expected_task: OwnedTask | None = None,
    owned_time_entries: dict[str, OwnedTimeEntry] | None = None,
    expected_time_entry: OwnedTimeEntry | None = None,
    owned_attachments: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = runner.invoke(
        app,
        ["--base-url", base_url, "--json", *args],
    )
    if result.exit_code != 0:
        error = _error_payload(result.stderr)
        error_type = error.get("type")
        task_id = error.get("task_id")
        if (
            error_type in _TASK_PARTIAL_TYPES
            and isinstance(task_id, str)
            and owned_tasks is not None
            and expected_task is not None
        ):
            owned_tasks[task_id] = expected_task
            if owned_attachments is not None:
                raw_attachment_ids = error.get("uploaded_attachment_ids", [])
                if isinstance(raw_attachment_ids, list):
                    for attachment_id in raw_attachment_ids:
                        if isinstance(attachment_id, str):
                            owned_attachments[attachment_id] = task_id
                failed_attachment_id = error.get("failed_attachment_id")
                if isinstance(failed_attachment_id, str):
                    owned_attachments[failed_attachment_id] = task_id
        entry_id = error.get("entry_id")
        if (
            error_type == "created_but_unverified"
            and isinstance(entry_id, str)
            and owned_time_entries is not None
            and expected_time_entry is not None
        ):
            owned_time_entries[entry_id] = expected_time_entry
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["ok"] is True
    return cast(dict[str, Any], payload["result"])


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_live_rate_budget(
    token: str,
    base_url: str,
    *,
    minimum_remaining: int,
) -> None:
    """Wait for ClickUp's documented minute window only when response headers require it."""

    endpoint = f"{base_url.rstrip('/')}/v2/user"
    headers = {"Accept": "application/json", "Authorization": token}
    for attempt in range(2):
        with httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=5.0),
            trust_env=False,
        ) as guard:
            response = guard.get(endpoint)
        raw_remaining = response.headers.get("X-RateLimit-Remaining")
        try:
            remaining = int(raw_remaining) if raw_remaining is not None else None
        except ValueError:
            remaining = None
        if response.status_code == 200 and remaining is not None and remaining >= minimum_remaining:
            return
        if response.status_code not in {200, 429}:
            pytest.fail(f"ClickUp rate-budget probe returned HTTP {response.status_code}")
        raw_retry = response.headers.get("Retry-After")
        raw_reset = response.headers.get("X-RateLimit-Reset")
        try:
            delay = (
                float(raw_retry)
                if raw_retry is not None
                else max(0.0, float(cast(str, raw_reset)) - time.time())
            )
        except (TypeError, ValueError):
            pytest.fail("ClickUp rate-budget probe omitted usable retry/reset headers")
        if delay > 65.0:
            pytest.fail(f"ClickUp requested an unexpectedly long live-test wait: {delay:.1f}s")
        if attempt == 1:
            break
        time.sleep(delay + 1.0)
    pytest.fail(
        f"ClickUp live-test rate budget remained below {minimum_remaining} after one bounded wait"
    )


@pytest.mark.skipif(
    os.environ.get("CLICKUP_LIVE_TEST") != "1",
    reason="set CLICKUP_LIVE_TEST=1 to enable the destructive sandbox live test",
)
def test_live_sandbox_cli_lifecycle(tmp_path: Path) -> None:
    required_names = (
        "CLICKUP_API_TOKEN",
        "CLICKUP_TEST_WORKSPACE_ID",
        "CLICKUP_TEST_SPACE_ID",
        "CLICKUP_TEST_LIST_ID",
        "CLICKUP_TEST_TAG",
    )
    required = {name: os.environ.get(name) for name in required_names}
    missing = [name for name, value in required.items() if not value]
    if missing:
        pytest.fail("Required live-test environment is missing: " + ", ".join(missing))

    token = cast(str, required["CLICKUP_API_TOKEN"])
    workspace_id = cast(str, required["CLICKUP_TEST_WORKSPACE_ID"])
    space_id = cast(str, required["CLICKUP_TEST_SPACE_ID"])
    list_id = cast(str, required["CLICKUP_TEST_LIST_ID"])
    tag_name = cast(str, required["CLICKUP_TEST_TAG"])
    base_url = resolve_base_url(None)
    runner = CliRunner()
    run_marker = str(uuid.uuid4())

    owned_tasks: dict[str, OwnedTask] = {}
    owned_time_entries: dict[str, OwnedTimeEntry] = {}
    owned_attachments: dict[str, str] = {}
    owned_comments: dict[str, str] = {}
    cleanup_failures: list[str] = []

    ensure_owner = OwnedTask(run_marker)
    attachment_owner = OwnedTask(run_marker)
    ensure_name = f"clickup-cli-live-ensure-{run_marker}"
    ensure_description = f"clickup-cli live ensure description {run_marker}"
    attachment_task_name = f"clickup-cli-live-attachment-{run_marker}"
    attachment_task_description = f"clickup-cli live attachment task {run_marker}"
    attachment_bytes = f"clickup-cli live attachment {run_marker}\n".encode()
    attachment_path = tmp_path / f"clickup-cli-live-{run_marker}.txt"
    attachment_path.write_bytes(attachment_bytes)
    download_path = tmp_path / f"clickup-cli-live-downloaded-{run_marker}.txt"
    manifest_path = tmp_path / f"clickup-cli-live-batch-{run_marker}.jsonl"

    with ClickUpClient(
        token=token,
        base_url=base_url,
        max_retry_after=65.0,
    ) as client:
        try:
            # All discovery and exact containment proof happen before the first write.
            identity = _invoke(runner, base_url, "auth", "whoami")
            assert identity["user"]["id"] is not None
            user_id = int(identity["user"]["id"])

            workspaces = _invoke(runner, base_url, "workspace", "list")
            assert any(item.get("id") == workspace_id for item in workspaces["workspaces"])
            tree_result = _invoke(runner, base_url, "workspace", "tree", workspace_id)
            members = _invoke(
                runner,
                base_url,
                "member",
                "list",
                "--workspace-id",
                workspace_id,
            )
            assert any(member.get("id") == str(user_id) for member in members["members"])
            list_result = _invoke(runner, base_url, "list", "show", list_id)
            statuses_result = _invoke(runner, base_url, "list", "statuses", list_id)
            assert statuses_result["list_id"] == list_id
            assert statuses_result["statuses"]
            prove_sandbox_destination(
                cast(dict[str, Any], list_result["list"]),
                cast(dict[str, Any], tree_result["workspace"]),
                workspace_id=workspace_id,
                space_id=space_id,
                list_id=list_id,
            )
            _require_live_rate_budget(token, base_url, minimum_remaining=99)

            ensured = _invoke(
                runner,
                base_url,
                "task",
                "ensure",
                ensure_name,
                "--list-id",
                list_id,
                "--description",
                ensure_description,
                owned_tasks=owned_tasks,
                expected_task=ensure_owner,
            )
            assert ensured["created"] is True
            ensure_task_id = str(ensured["task"]["id"])
            owned_tasks[ensure_task_id] = ensure_owner
            assert ensured["task"]["list_id"] == list_id

            ensured_again = _invoke(
                runner,
                base_url,
                "task",
                "ensure",
                ensure_name,
                "--list-id",
                list_id,
                "--description",
                ensure_description,
                owned_tasks=owned_tasks,
                expected_task=ensure_owner,
            )
            assert ensured_again["created"] is False
            assert ensured_again["task"]["id"] == ensure_task_id

            listed = _invoke(runner, base_url, "task", "list", "--list-id", list_id, "--all")
            assert any(task.get("id") == ensure_task_id for task in listed["tasks"])
            searched = _invoke(
                runner,
                base_url,
                "task",
                "search",
                run_marker,
                "--list-id",
                list_id,
                "--deep",
                "--all",
            )
            assert any(task.get("id") == ensure_task_id for task in searched["tasks"])

            created = _invoke(
                runner,
                base_url,
                "task",
                "create",
                attachment_task_name,
                "--list-id",
                list_id,
                "--description",
                attachment_task_description,
                "--attach",
                str(attachment_path),
                owned_tasks=owned_tasks,
                expected_task=attachment_owner,
                owned_attachments=owned_attachments,
            )
            attachment_task_id = str(created["task"]["id"])
            owned_tasks[attachment_task_id] = attachment_owner
            assert created["task"]["list_id"] == list_id
            assert len(created["task"]["attachments"]) == 1
            attachment_id = str(created["task"]["attachments"][0]["id"])
            owned_attachments[attachment_id] = attachment_task_id

            attachments = _invoke(
                runner,
                base_url,
                "task",
                "attachment",
                "list",
                attachment_task_id,
            )
            assert attachments["task_id"] == attachment_task_id
            assert any(
                item.get("id") == attachment_id and run_marker in str(item.get("title"))
                for item in attachments["attachments"]
            )
            downloaded = _invoke(
                runner,
                base_url,
                "task",
                "attachment",
                "download",
                attachment_task_id,
                attachment_id,
                "--output",
                str(download_path),
            )
            assert downloaded["size"] == len(attachment_bytes)
            assert download_path.read_bytes() == attachment_bytes

            updated_name = f"clickup-cli-live-updated-{run_marker}"
            updated_description = f"clickup-cli live updated description {run_marker}"
            updated = _invoke(
                runner,
                base_url,
                "task",
                "update",
                attachment_task_id,
                "--name",
                updated_name,
                "--description",
                updated_description,
                "--priority",
                "high",
                "--start-date",
                "2030-01-02T15:04:05Z",
            )
            assert updated["changed"] is True
            assert set(updated["fields"]) == {
                "description",
                "name",
                "priority",
                "start_date",
                "start_date_time",
            }
            assert updated["task"]["name"] == updated_name
            assert updated["task"]["description"] == updated_description
            assert updated["task"]["priority"] == "high"
            assert updated["task"]["start_date"] == "2030-01-02T15:04:05Z"

            priority_cleared = _invoke(
                runner, base_url, "task", "priority", "clear", attachment_task_id
            )
            assert priority_cleared["task"]["priority"] is None
            start_cleared = _invoke(
                runner, base_url, "task", "start-date", "clear", attachment_task_id
            )
            assert start_cleared["task"]["start_date"] is None

            tag_added = _invoke(
                runner, base_url, "task", "tag", "add", attachment_task_id, tag_name
            )
            assert tag_added["added"] is True
            assert tag_name.casefold() in {tag.casefold() for tag in tag_added["tags"]}
            tag_removed = _invoke(
                runner, base_url, "task", "tag", "remove", attachment_task_id, tag_name
            )
            assert tag_removed["added"] is False
            assert tag_name.casefold() not in {tag.casefold() for tag in tag_removed["tags"]}

            archived = _invoke(runner, base_url, "task", "archive", attachment_task_id)
            assert archived["task"]["archived"] is True
            unarchived = _invoke(runner, base_url, "task", "unarchive", attachment_task_id)
            assert unarchived["task"]["archived"] is False

            batch_description = f"clickup-cli live batch applied {run_marker}"
            manifest_path.write_text(
                json.dumps(
                    {
                        "task": attachment_task_id,
                        "set": {"description": batch_description, "priority": "normal"},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            assert run_marker in manifest_path.name
            assert run_marker in manifest_path.read_text(encoding="utf-8")
            before_plan = summarize_task(client.get_task(attachment_task_id))
            plan = _invoke(runner, base_url, "task", "batch", "plan", str(manifest_path))
            assert plan["task_count"] == 1
            assert plan["tasks"][0]["task_id"] in owned_tasks
            assert plan["change_count"] == 2
            after_plan = summarize_task(client.get_task(attachment_task_id))
            assert after_plan == before_plan
            applied = _invoke(
                runner,
                base_url,
                "task",
                "batch",
                "apply",
                str(manifest_path),
                "--yes",
            )
            assert applied["task_count"] == 1
            assert applied["tasks"][0]["task_id"] in owned_tasks
            assert applied["change_count"] == 2
            batch_readback = _invoke(runner, base_url, "task", "show", attachment_task_id)
            assert batch_readback["task"]["description"] == batch_description
            assert batch_readback["task"]["priority"] == "normal"
            _require_live_rate_budget(token, base_url, minimum_remaining=99)

            comment_text = f"Disposable clickup-cli comment {run_marker}"
            added_comment = _invoke(
                runner, base_url, "task", "comment", "add", ensure_task_id, comment_text
            )
            comment_id = str(added_comment["comment"]["id"])
            owned_comments[comment_id] = ensure_task_id
            assert added_comment["comment"]["text"] == comment_text
            comments = _invoke(runner, base_url, "task", "comment", "list", ensure_task_id)
            assert any(
                comment.get("id") == comment_id and comment.get("text") == comment_text
                for comment in comments["comments"]
            )
            shown_comment = _invoke(
                runner,
                base_url,
                "task",
                "comment",
                "show",
                f"https://app.clickup.com/t/{ensure_task_id}?comment={comment_id}",
            )
            assert shown_comment["comment"] == added_comment["comment"]

            date_only = parse_due_date("2030-01-02")
            date_result = _invoke(
                runner, base_url, "task", "due-date", "set", ensure_task_id, "2030-01-02"
            )
            assert date_result["changed"] is True
            assert date_result["due_date"] == date_only.display
            assert task_due_date(client.get_task(ensure_task_id)).milliseconds is not None

            timed = parse_due_date("2030-01-02T15:04:05+01:00")
            timed_result = _invoke(
                runner,
                base_url,
                "task",
                "due-date",
                "set",
                ensure_task_id,
                "2030-01-02T15:04:05+01:00",
            )
            assert timed_result["due_date"] == "2030-01-02T14:04:05Z"
            assert task_due_date(client.get_task(ensure_task_id)).milliseconds == timed.milliseconds
            cleared = _invoke(runner, base_url, "task", "due-date", "clear", ensure_task_id)
            assert cleared["changed"] is True
            assert task_due_date(client.get_task(ensure_task_id)).milliseconds is None

            if user_id in task_assignee_ids(client.get_task(ensure_task_id)):
                initially_unassigned = _invoke(
                    runner, base_url, "task", "unassign", ensure_task_id, str(user_id)
                )
                assert initially_unassigned["assigned"] is False
            assigned = _invoke(runner, base_url, "task", "assign", ensure_task_id, str(user_id))
            assert assigned["assigned"] is True
            assert user_id in task_assignee_ids(client.get_task(ensure_task_id))
            unassigned = _invoke(runner, base_url, "task", "unassign", ensure_task_id, str(user_id))
            assert unassigned["assigned"] is False
            assert user_id not in task_assignee_ids(client.get_task(ensure_task_id))

            current_result = _invoke(runner, base_url, "task", "status", ensure_task_id)
            current = str(current_result["status"])
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
            changed = _invoke(runner, base_url, "task", "set-status", ensure_task_id, alternative)
            assert changed["changed"] is True
            assert task_status(client.get_task(ensure_task_id)) == alternative
            completed = _invoke(runner, base_url, "task", "complete", ensure_task_id)
            assert completed["changed"] is True
            assert completed["status"].casefold() in {"completed", "complete", "done", "closed"}
            assert task_status(client.get_task(ensure_task_id)) == completed["status"]

            # Reads are allowed even if a human timer exists; start/stop are deliberately absent.
            _invoke(runner, base_url, "time", "current", "--workspace-id", workspace_id)
            now = datetime.now(UTC).replace(microsecond=0)
            entry_start = now - timedelta(hours=2)
            range_start = now - timedelta(days=1)
            range_end = now + timedelta(days=1)
            manual_description = str(uuid.uuid4())
            expected_entry = OwnedTimeEntry(attachment_task_id, manual_description)
            added_time = _invoke(
                runner,
                base_url,
                "time",
                "add",
                "--workspace-id",
                workspace_id,
                "--start",
                _iso_utc(entry_start),
                "--duration",
                "90s",
                "--task",
                attachment_task_id,
                "--description",
                manual_description,
                owned_time_entries=owned_time_entries,
                expected_time_entry=expected_entry,
            )
            entry_id = str(added_time["entry"]["id"])
            owned_time_entries[entry_id] = expected_entry
            assert added_time["entry"]["task_id"] == attachment_task_id
            assert added_time["entry"]["description"] == manual_description

            updated_time_description = f"{manual_description} updated"
            updated_time = _invoke(
                runner,
                base_url,
                "time",
                "update",
                entry_id,
                "--workspace-id",
                workspace_id,
                "--description",
                updated_time_description,
                "--duration",
                "2m",
            )
            assert updated_time["changed"] is True
            assert updated_time["entry"]["description"] == updated_time_description
            assert updated_time["entry"]["duration_ms"] == 120_000

            time_list = _invoke(
                runner,
                base_url,
                "time",
                "list",
                "--workspace-id",
                workspace_id,
                "--from",
                _iso_utc(range_start),
                "--to",
                _iso_utc(range_end),
                "--task",
                attachment_task_id,
            )
            assert time_list["range_semantics"] == "start-inclusive,end-exclusive"
            assert any(entry.get("id") == entry_id for entry in time_list["entries"])

            delete_owned_time_entry(
                client,
                workspace_id,
                entry_id,
                owned_time_entries,
                owned_tasks,
                sandbox_list_id=list_id,
                delete=lambda target: _invoke(
                    runner,
                    base_url,
                    "time",
                    "delete",
                    target,
                    "--workspace-id",
                    workspace_id,
                    "--yes",
                ),
            )
            owned_time_entries.pop(entry_id)

            for task_id in sorted(tuple(owned_tasks)):
                delete_owned_task(
                    client,
                    task_id,
                    owned_tasks,
                    sandbox_list_id=list_id,
                    delete=lambda target: _invoke(
                        runner, base_url, "task", "delete", target, "--yes"
                    ),
                )
                owned_tasks.pop(task_id)
        finally:
            for entry_id in sorted(tuple(owned_time_entries)):
                try:
                    delete_owned_time_entry(
                        client,
                        workspace_id,
                        entry_id,
                        owned_time_entries,
                        owned_tasks,
                        sandbox_list_id=list_id,
                    )
                except Exception as exc:
                    cleanup_failures.append(f"surviving time-entry ID {entry_id}: {exc}")
                else:
                    owned_time_entries.pop(entry_id)
            for task_id in sorted(tuple(owned_tasks)):
                try:
                    delete_owned_task(
                        client,
                        task_id,
                        owned_tasks,
                        sandbox_list_id=list_id,
                    )
                except Exception as exc:
                    cleanup_failures.append(f"surviving task ID {task_id}: {exc}")
                else:
                    owned_tasks.pop(task_id)
            if cleanup_failures:
                pytest.fail("Live cleanup refused or failed: " + "; ".join(cleanup_failures))
