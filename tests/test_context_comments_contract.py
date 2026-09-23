"""Bounded context and native comment contract against a local HTTP recorder."""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner, Result

from clickup_cli.cli import app
from clickup_cli.domain import summarize_comment
from tests.conftest import MockClickUpAPI

TOKEN = "context-test-token"
TASK = "task_123"
URL = f"https://app.clickup.com/t/work_7/{TASK}"
READ = {"Accept": "application/json", "Authorization": TOKEN}
WRITE = {**READ, "Content-Type": "application/json"}
runner = CliRunner()


def invoke(api: MockClickUpAPI, *args: str, machine: bool = True) -> Result:
    return runner.invoke(
        app,
        ["--base-url", api.base_url, *(["--json"] if machine else []), *args],
        env={"CLICKUP_API_TOKEN": TOKEN},
    )


def task(**fields: Any) -> dict[str, Any]:
    return {
        "id": TASK,
        "name": "Example",
        "status": {"status": "Open", "type": "open"},
        "list": {"id": "list_1", "name": "Inbox"},
        "url": f"https://app.clickup.com/t/{TASK}",
        "due_date": None,
        "attachments": [
            {"id": "a1", "title": "one.txt", "url": "https://attachments.clickup.com/a1"},
            {"id": "a2", "title": "two.txt", "url": "https://attachments.clickup.com/a2"},
        ],
        "parent": "parent_1",
        "subtasks": [{"id": "child_1"}],
        **fields,
    }


def expect_task(api: MockClickUpAPI, **fields: Any) -> None:
    api.expect("GET", f"/api/v2/task/{TASK}", headers=READ, response_json=task(**fields))


def comment(
    identifier: str, text: str = "Hello", segments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "id": identifier,
        "date": "1893500000000",
        "comment_text": text,
        "comment": segments if segments is not None else [{"text": text}],
        "user": {"id": 7, "username": "Writer"},
    }


def test_context_reads_only_one_comment_page_and_reports_unknown_completeness(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(mock_api)
    api_list = {
        "id": "list_1",
        "name": "Inbox",
        "space": {"id": "space_1", "name": "Build"},
        "folder": {"id": "folder_1", "name": "Queue"},
        "statuses": [{"status": "Open", "type": "open"}, {"status": "Done", "type": "closed"}],
    }
    mock_api.expect("GET", "/api/v2/list/list_1", headers=READ, response_json=api_list)
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={"comments": [comment("c1"), comment("c2")]},
    )

    result = invoke(mock_api, "task", "context", URL, "--comments", "1", "--attachments", "1")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["result"]
    assert data["task"]["id"] == TASK
    assert data["parent_id"] == "parent_1" and data["subtask_ids"] == ["child_1"]
    assert data["path"] == ["Build", "Queue", "Inbox"]
    assert data["statuses"] == [
        {"status": "Done", "type": "closed"},
        {"status": "Open", "type": "open"},
    ]
    assert data["comments"]["returned_count"] == 1
    assert data["comments"]["has_more"] is True
    assert data["comments"]["cursor"] is not None
    assert data["attachments"]["returned_count"] == 1
    assert len(data["task"]["attachments"]) == 1
    assert data["attachments"]["has_more"] is True
    assert data["attachments"]["cursor"] is None
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET", "GET"]


def test_context_hides_provider_hidden_folder_and_bounds_comment_pagination(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(mock_api)
    mock_api.expect(
        "GET",
        "/api/v2/list/list_1",
        headers=READ,
        response_json={
            "id": "list_1",
            "name": "Inbox",
            "space": {"id": "s1", "name": "Build"},
            "folder": {"id": "f1", "name": "hidden"},
            "statuses": [{"status": "Open", "type": "open"}],
        },
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={"comments": [comment("c1")]},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment?start=1893500000000&start_id=c1",
        headers=READ,
        response_json={"comments": [comment("c2")]},
    )
    result = invoke(mock_api, "task", "context", TASK, "--comments", "2", "--attachments", "0")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["result"]
    assert data["path"] == ["Build", "Inbox"]
    assert data["list"]["folder_id"] is None
    assert data["comments"]["returned_count"] == 2
    assert data["comments"]["has_more"] is None
    assert data["comments"]["cursor"] == {"start": 1893500000000, "start_id": "c2"}
    assert data["attachments"]["has_more"] is True


def test_native_mention_resolves_exact_workspace_member_and_verifies_rich_segments(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(mock_api, team_id="work_7")
    mock_api.expect(
        "GET",
        "/api/v2/team",
        headers=READ,
        response_json={
            "teams": [
                {
                    "id": "work_7",
                    "members": [
                        {"user": {"id": 42, "username": "Alex", "email": "alex@example.test"}}
                    ],
                }
            ]
        },
    )
    segments = [{"type": "tag", "user": {"id": 42}}, {"text": " Hello team"}]
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK}/comment",
        headers=WRITE,
        json_body={"comment": segments, "notify_all": False},
        response_json={"id": "c1"},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={
            "comments": [
                comment("c-other"),
                comment(
                    "c1",
                    "@Alex Hello team",
                    [{"type": "tag", "user": {"id": 42}, "text": "@Alex"}, {"text": " Hello team"}],
                ),
            ]
        },
    )
    result = invoke(
        mock_api, "task", "comment", "add", URL, "Hello team", "--mention", "alex@example.test"
    )
    assert result.exit_code == 0, result.output
    item = json.loads(result.stdout)["result"]["comment"]
    assert item["id"] == "c1"
    assert item["mentions"] == ["42"]
    assert item["segments"][0]["type"] == "tag"
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET", "POST", "GET"]


def test_edit_rejects_stale_sha_before_put(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api)
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={"comments": [comment("c1", "Before")]},
    )
    result = invoke(
        mock_api, "task", "comment", "edit", URL, "c1", "After", "--expect-sha256", "0" * 64
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "stale_comment_revision"
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET"]


def test_edit_preserves_native_mention_and_verifies_same_comment(mock_api: MockClickUpAPI) -> None:
    before = comment(
        "c1", "@Alex Old", [{"type": "tag", "user": {"id": 42}, "text": "@Alex"}, {"text": " Old"}]
    )
    revision = summarize_comment(before)["revision_sha256"]
    expect_task(mock_api)
    mock_api.expect(
        "GET", f"/api/v2/task/{TASK}/comment", headers=READ, response_json={"comments": [before]}
    )
    segments = [{"type": "tag", "user": {"id": 42}}, {"text": " After"}]
    mock_api.expect(
        "PUT",
        "/api/v2/comment/c1",
        headers=WRITE,
        json_body={"comment": segments},
        response_json={},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={
            "comments": [
                comment("other", "After"),
                comment(
                    "c1",
                    "@Alex After",
                    [{"type": "tag", "user": {"id": 42}, "text": "@Alex"}, {"text": " After"}],
                ),
            ]
        },
    )
    result = invoke(
        mock_api, "task", "comment", "edit", URL, "c1", "After", "--expect-sha256", str(revision)
    )
    assert result.exit_code == 0, result.output
    observed = json.loads(result.stdout)["result"]["comment"]
    assert observed["id"] == "c1" and observed["mentions"] == ["42"]
    assert observed["revision_sha256"] != revision
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET", "PUT", "GET"]


def test_ambiguous_username_rejects_before_post(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, team_id="work_7")
    mock_api.expect(
        "GET",
        "/api/v2/team",
        headers=READ,
        response_json={
            "teams": [
                {
                    "id": "work_7",
                    "members": [
                        {"user": {"id": 42, "username": "Alex"}},
                        {"user": {"id": 43, "username": "alex"}},
                    ],
                }
            ]
        },
    )
    result = invoke(mock_api, "task", "comment", "add", TASK, "Hello", "--mention", "Alex")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "ambiguous_match"
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET"]


def test_numeric_mentions_are_workspace_scoped_and_notify_all_explicit(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(mock_api, team_id="work_7")
    mock_api.expect(
        "GET",
        "/api/v2/team",
        headers=READ,
        response_json={
            "teams": [
                {
                    "id": "work_7",
                    "members": [
                        {"user": {"id": 42, "username": "Alex"}},
                        {"user": {"id": 43, "username": "Bea"}},
                    ],
                },
                {"id": "other", "members": [{"user": {"id": 99, "username": "Else"}}]},
            ]
        },
    )
    segments = [
        {"type": "tag", "user": {"id": 42}},
        {"type": "tag", "user": {"id": 43}},
        {"text": " Hi"},
    ]
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK}/comment",
        headers=WRITE,
        json_body={"comment": segments, "notify_all": True},
        response_json={"id": "c1"},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={
            "comments": [
                comment(
                    "c1",
                    "@Alex @Bea Hi",
                    [
                        {"type": "tag", "user": {"id": 42}, "text": "@Alex"},
                        {"type": "tag", "user": {"id": 43}, "text": "@Bea"},
                        {"text": " Hi"},
                    ],
                )
            ]
        },
    )
    result = invoke(
        mock_api,
        "task",
        "comment",
        "add",
        TASK,
        "Hi",
        "--mention",
        "42",
        "--mention",
        "43",
        "--notify-all",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["comment"]["mentions"] == ["42", "43"]


def test_notify_all_without_mentions_does_not_require_workspace_member_lookup(
    mock_api: MockClickUpAPI,
) -> None:
    expect_task(mock_api)
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK}/comment",
        headers=WRITE,
        json_body={"comment": [{"text": "Update"}], "notify_all": True},
        response_json={"id": "c1"},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={"comments": [comment("c1", "Update")]},
    )
    result = invoke(mock_api, "task", "comment", "add", TASK, "Update", "--notify-all")
    assert result.exit_code == 0, result.output
    assert [r.method for r in mock_api.state.requests] == ["GET", "POST", "GET"]


def test_disconnected_post_is_outcome_unknown_and_not_retried(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, team_id="work_7")
    mock_api.expect(
        "GET",
        "/api/v2/team",
        headers=READ,
        response_json={
            "teams": [{"id": "work_7", "members": [{"user": {"id": 42, "username": "Alex"}}]}]
        },
    )
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK}/comment",
        headers=WRITE,
        json_body={
            "comment": [{"type": "tag", "user": {"id": 42}}, {"text": " Hi"}],
            "notify_all": False,
        },
        disconnect=True,
    )
    result = invoke(mock_api, "task", "comment", "add", TASK, "Hi", "--mention", "42")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "outcome_unknown"
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET", "POST"]


def test_missing_created_comment_readback_keeps_returned_id(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, team_id="work_7")
    mock_api.expect(
        "GET",
        "/api/v2/team",
        headers=READ,
        response_json={
            "teams": [{"id": "work_7", "members": [{"user": {"id": 42, "username": "Alex"}}]}]
        },
    )
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK}/comment",
        headers=WRITE,
        json_body={
            "comment": [{"type": "tag", "user": {"id": 42}}, {"text": " Hi"}],
            "notify_all": False,
        },
        response_json={"id": "c1"},
    )
    mock_api.expect(
        "GET", f"/api/v2/task/{TASK}/comment", headers=READ, response_json={"comments": []}
    )
    result = invoke(mock_api, "task", "comment", "add", TASK, "Hi", "--mention", "42")
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "created_but_unverified" and error["comment_id"] == "c1"
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET", "POST", "GET"]


def test_created_id_survives_failed_rich_readback(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api, team_id="work_7")
    mock_api.expect(
        "GET",
        "/api/v2/team",
        headers=READ,
        response_json={
            "teams": [{"id": "work_7", "members": [{"user": {"id": 42, "username": "Alex"}}]}]
        },
    )
    mock_api.expect(
        "POST",
        f"/api/v2/task/{TASK}/comment",
        headers=WRITE,
        json_body={
            "comment": [{"type": "tag", "user": {"id": 42}}, {"text": " Hi"}],
            "notify_all": False,
        },
        response_json={"id": "c1"},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={"comments": [comment("c1", "Hi", [{"text": "Hi"}])]},
    )
    result = invoke(mock_api, "task", "comment", "add", TASK, "Hi", "--mention", "42")
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "created_but_unverified" and error["comment_id"] == "c1"
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET", "POST", "GET"]


def test_disconnected_edit_put_is_unknown_and_keeps_comment_id(mock_api: MockClickUpAPI) -> None:
    before = comment("c1", "Old")
    revision = summarize_comment(before)["revision_sha256"]
    expect_task(mock_api)
    mock_api.expect(
        "GET", f"/api/v2/task/{TASK}/comment", headers=READ, response_json={"comments": [before]}
    )
    mock_api.expect(
        "PUT",
        "/api/v2/comment/c1",
        headers=WRITE,
        json_body={"comment": [{"text": "After"}]},
        disconnect=True,
    )
    result = invoke(
        mock_api, "task", "comment", "edit", TASK, "c1", "After", "--expect-sha256", str(revision)
    )
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "outcome_unknown" and error["comment_id"] == "c1"
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET", "PUT"]


def test_edit_fails_closed_on_unknown_rich_segment_before_put(mock_api: MockClickUpAPI) -> None:
    before = comment("c1", "Widget Old", [{"type": "widget", "text": "Widget"}, {"text": " Old"}])
    revision = summarize_comment(before)["revision_sha256"]
    expect_task(mock_api)
    mock_api.expect(
        "GET", f"/api/v2/task/{TASK}/comment", headers=READ, response_json={"comments": [before]}
    )
    result = invoke(
        mock_api, "task", "comment", "edit", TASK, "c1", "After", "--expect-sha256", str(revision)
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_operation"
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET"]


def test_context_honors_explicit_pagination_completion(mock_api: MockClickUpAPI) -> None:
    expect_task(mock_api)
    mock_api.expect(
        "GET",
        "/api/v2/list/list_1",
        headers=READ,
        response_json={
            "id": "list_1",
            "name": "Inbox",
            "statuses": [{"status": "Open", "type": "open"}],
        },
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK}/comment",
        headers=READ,
        response_json={"comments": [comment("c1")], "has_more": False},
    )
    result = invoke(mock_api, "task", "context", TASK, "--comments", "10")
    assert result.exit_code == 0, result.output
    page = json.loads(result.stdout)["result"]["comments"]
    assert page["returned_count"] == 1 and page["has_more"] is False
    assert page["cursor"] is None
    assert [r.method for r in mock_api.state.requests] == ["GET", "GET", "GET"]


def test_invalid_context_bounds_make_no_request(mock_api: MockClickUpAPI) -> None:
    result = invoke(mock_api, "task", "context", TASK, "--comments", "101")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_operation"
    assert mock_api.state.requests == []


def test_edit_readback_wrong_text_preserves_comment_id(mock_api: MockClickUpAPI) -> None:
    before = comment("c1", "Old")
    revision = summarize_comment(before)["revision_sha256"]
    expect_task(mock_api)
    mock_api.expect(
        "GET", f"/api/v2/task/{TASK}/comment", headers=READ, response_json={"comments": [before]}
    )
    mock_api.expect(
        "PUT",
        "/api/v2/comment/c1",
        headers=WRITE,
        json_body={"comment": [{"text": "After"}]},
        response_json={},
    )
    mock_api.expect(
        "GET", f"/api/v2/task/{TASK}/comment", headers=READ, response_json={"comments": [before]}
    )
    result = invoke(
        mock_api, "task", "comment", "edit", TASK, "c1", "After", "--expect-sha256", str(revision)
    )
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "edited_but_unverified" and error["comment_id"] == "c1"
