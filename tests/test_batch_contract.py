from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner, Result

import clickup_cli.batch as batch_module
from clickup_cli.batch import (
    MAX_MANIFEST_LINE_BYTES,
    BatchService,
    batch_apply_text,
    batch_plan_text,
    load_manifest,
)
from clickup_cli.cli import app
from clickup_cli.client import ClickUpClient
from clickup_cli.errors import BatchManifestError
from tests.conftest import MockClickUpAPI

AUTH_VALUE = "batch-auth-value"
LIST_ID = "list_456"
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
WRITE_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}

runner = CliRunner()


def task_payload(
    task_id: str,
    *,
    name: str,
    status: str = "Open",
    tags: list[str] | None = None,
    assignees: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "archived": False,
        "assignees": [{"id": user_id} for user_id in (assignees or [])],
        "attachments": [],
        "description": "Description",
        "due_date": None,
        "due_date_time": None,
        "id": task_id,
        "list": {"id": LIST_ID, "name": "Batch List"},
        "name": name,
        "priority": None,
        "start_date": None,
        "start_date_time": None,
        "status": {"status": status, "type": "custom"},
        "tags": [{"name": tag} for tag in (tags or [])],
        "url": f"https://app.clickup.com/t/{task_id}",
    }


def write_manifest(path: Path, lines: list[object]) -> bytes:
    raw = b"".join(
        line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode("utf-8")
        for line in lines
    )
    path.write_bytes(raw)
    return raw


def invoke(api: MockClickUpAPI, arguments: list[str], *, json_output: bool = True) -> Result:
    prefix = ["--base-url", api.base_url]
    if json_output:
        prefix.append("--json")
    return runner.invoke(
        app,
        [*prefix, *arguments],
        env={"CLICKUP_API_TOKEN": AUTH_VALUE},
    )


def expect_task(api: MockClickUpAPI, task_id: str, payload: dict[str, Any]) -> None:
    api.expect(
        "GET",
        f"/api/v2/task/{task_id}",
        headers=READ_HEADERS,
        response_json=payload,
    )


def expect_list(api: MockClickUpAPI) -> None:
    api.expect(
        "GET",
        f"/api/v2/list/{LIST_ID}",
        headers=READ_HEADERS,
        response_json={
            "id": LIST_ID,
            "statuses": [
                {"status": "Open", "type": "open"},
                {"status": "In Progress", "type": "custom"},
            ],
        },
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json\n",
        b"[]\n",
        b'{"task":"task_1","unknown":true}\n',
        b'{"task":"task_1","set":{"unknown":true}}\n',
        b'{"task":"task_1","task":"task_2","set":{"name":"x"}}\n',
        b'{"task":"task_1","set":{}}\n',
        b'{"task":"task_1","set":{"name":"   "}}\n',
        b'{"task":"task_1","set":{"description":null}}\n',
        b'{"task":"task_1","set":{"archived":1}}\n',
        b'{"task":"task_1","set":{"priority":"clear"}}\n',
        b'{"task":"task_1","set":{"priority":"blocker"}}\n',
        b'{"task":"task_1","set":{"due_date":"2026-02-30"}}\n',
        b'{"task":"task_1","set":{"start_date":"2026-08-20T12:00:00"}}\n',
        b'{"task":"task_1","add_tags":["focus","FOCUS"]}\n',
        b'{"task":"task_1","add_tags":["focus"],"remove_tags":["FOCUS"]}\n',
        b'{"task":"task_1","add_assignees":[1,1]}\n',
        b'{"task":"task_1","add_assignees":[1],"remove_assignees":[1]}\n',
        b'{"task":"task_1","add_assignees":[true]}\n',
        b'{"task":"task_1","set":{"name":NaN}}\n',
        b"\xff\n",
    ],
)
def test_strict_schema_rejections_are_typed_and_line_aware(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_bytes(b"\n" + raw)

    with pytest.raises(BatchManifestError) as caught:
        load_manifest(path)

    assert caught.value.error_type == "invalid_batch_manifest"
    assert caught.value.details["line"] == 2
    assert "Manifest line 2" in str(caught.value)


def test_url_normalization_precedes_duplicate_detection(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    write_manifest(
        path,
        [
            {"task": "task_1", "set": {"name": "One"}},
            {
                "task": "https://app.clickup.com/t/workspace_9/task_1",
                "set": {"name": "Two"},
            },
        ],
    )

    with pytest.raises(BatchManifestError) as caught:
        load_manifest(path)

    assert caught.value.details["line"] == 2
    assert "first declared on line 1" in str(caught.value)


def test_manifest_limits_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "manifest.jsonl"

    monkeypatch.setattr(batch_module, "MAX_MANIFEST_BYTES", 8)
    path.write_bytes(b"123456789")
    with pytest.raises(BatchManifestError, match="safety limit"):
        load_manifest(path)

    monkeypatch.setattr(batch_module, "MAX_MANIFEST_BYTES", 1024)
    monkeypatch.setattr(batch_module, "MAX_MANIFEST_LINES", 2)
    path.write_bytes(b"\n\n\n")
    with pytest.raises(BatchManifestError, match="line safety limit"):
        load_manifest(path)

    monkeypatch.setattr(batch_module, "MAX_MANIFEST_LINES", 10)
    monkeypatch.setattr(batch_module, "MAX_MANIFEST_TASKS", 1)
    write_manifest(
        path,
        [
            {"task": "task_1", "set": {"name": "One"}},
            {"task": "task_2", "set": {"name": "Two"}},
        ],
    )
    with pytest.raises(BatchManifestError, match="task safety limit"):
        load_manifest(path)

    monkeypatch.setattr(batch_module, "MAX_MANIFEST_TASKS", 10)
    monkeypatch.setattr(batch_module, "MAX_MANIFEST_LINE_BYTES", 32)
    write_manifest(path, [{"task": "task_1", "set": {"description": "x" * 40}}])
    with pytest.raises(BatchManifestError, match="byte safety limit"):
        load_manifest(path)


def test_parser_uses_safe_deterministic_operation_order(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    write_manifest(
        path,
        [
            {
                "task": "task_1",
                "set": {
                    "archived": True,
                    "start_date": None,
                    "priority": "high",
                    "due_date": None,
                    "status": "open",
                    "description": "After",
                    "name": "Renamed",
                },
                "add_tags": ["focus"],
                "remove_tags": ["cold"],
                "add_assignees": [123],
                "remove_assignees": [456],
            }
        ],
    )

    manifest = load_manifest(path)

    assert [operation.kind for operation in manifest.tasks[0].operations] == [
        "set_name",
        "set_description",
        "set_status",
        "set_due_date",
        "set_priority",
        "set_start_date",
        "remove_tag",
        "add_tag",
        "remove_assignee",
        "add_assignee",
        "set_archived",
    ]

    write_manifest(path, [{"task": "task_1", "set": {"archived": False, "name": "x"}}])
    manifest = load_manifest(path)
    assert [operation.kind for operation in manifest.tasks[0].operations] == [
        "set_archived",
        "set_name",
    ]


def test_plan_is_read_only_caches_statuses_and_reports_deterministic_diffs(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.jsonl"
    raw = write_manifest(
        path,
        [
            b"\n",
            {
                "task": "https://app.clickup.com/t/workspace_9/task_1",
                "set": {"status": "in progress", "due_date": None},
                "add_tags": ["focus"],
                "remove_tags": ["cold"],
                "add_assignees": [123],
                "remove_assignees": [456],
            },
            {"task": "task_2", "set": {"status": "IN PROGRESS"}},
        ],
    )
    expect_task(
        mock_api,
        "task_1",
        task_payload(
            "task_1",
            name="First",
            tags=["cold"],
            assignees=[456],
        ),
    )
    expect_task(
        mock_api,
        "task_2",
        task_payload("task_2", name="Second", status="In Progress"),
    )
    expect_list(mock_api)

    result = invoke(mock_api, ["task", "batch", "plan", str(path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    assert payload["task_count"] == 2
    assert payload["operation_count"] == 7
    assert payload["change_count"] == 5
    assert payload["no_op_count"] == 2
    assert payload["tasks"][0]["line"] == 2
    assert payload["tasks"][0]["task_id"] == "task_1"
    assert payload["tasks"][0]["task_name"] == "First"
    assert [change["operation"] for change in payload["tasks"][0]["changes"]] == [
        "set_status",
        "set_due_date",
        "remove_tag",
        "add_tag",
        "remove_assignee",
        "add_assignee",
    ]
    assert payload["tasks"][0]["changes"][0] == {
        "after": "In Progress",
        "before": "Open",
        "changed": True,
        "field": "status",
        "operation": "set_status",
    }
    assert payload["tasks"][1]["changes"][0]["changed"] is False
    assert [request.method for request in mock_api.state.requests] == ["GET", "GET", "GET"]

    text = batch_plan_text(payload)
    assert text.startswith(f"Manifest SHA-256: {hashlib.sha256(raw).hexdigest()}\n")
    assert "no-op set_status" in text


def test_invalid_status_finishes_task_reads_and_never_writes(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.jsonl"
    write_manifest(
        path,
        [
            {"task": "task_1", "set": {"status": "Blocked"}},
            {"task": "task_2", "set": {"name": "After"}},
        ],
    )
    expect_task(mock_api, "task_1", task_payload("task_1", name="First"))
    expect_task(mock_api, "task_2", task_payload("task_2", name="Second"))
    expect_list(mock_api)

    result = invoke(mock_api, ["task", "batch", "apply", str(path), "--yes"])

    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "invalid_status"
    assert error["line"] == 1
    assert [request.method for request in mock_api.state.requests] == ["GET", "GET", "GET"]


def test_apply_requires_confirmation_before_manifest_or_network(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.jsonl"

    result = invoke(mock_api, ["task", "batch", "apply", str(missing)])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "confirmation_required"
    assert mock_api.state.requests == []


def test_successful_multi_task_apply_preflights_all_tasks_and_reports_noops(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.jsonl"
    raw = write_manifest(
        path,
        [
            {"task": "task_1", "set": {"name": "After"}},
            {"task": "task_2", "set": {"name": "Same"}},
        ],
    )
    before_1 = task_payload("task_1", name="Before")
    before_2 = task_payload("task_2", name="Same")
    after_1 = task_payload("task_1", name="After")
    expect_task(mock_api, "task_1", before_1)
    expect_task(mock_api, "task_2", before_2)
    expect_task(mock_api, "task_1", before_1)
    mock_api.expect(
        "PUT",
        "/api/v2/task/task_1",
        headers=WRITE_HEADERS,
        json_body={"name": "After"},
        response_json={},
    )
    expect_task(mock_api, "task_1", after_1)
    expect_task(mock_api, "task_2", before_2)

    result = invoke(mock_api, ["task", "batch", "apply", str(path), "--yes"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    assert (payload["operation_count"], payload["change_count"], payload["no_op_count"]) == (
        2,
        1,
        1,
    )
    assert payload["tasks"][0]["operations"] == [
        {"changed": True, "field": "name", "operation": "set_name"}
    ]
    assert payload["tasks"][0]["final_task"]["name"] == "After"
    assert payload["tasks"][1]["operations"][0]["changed"] is False
    methods = [request.method for request in mock_api.state.requests]
    assert methods == ["GET", "GET", "GET", "PUT", "GET", "GET"]
    assert methods.index("PUT") > 1
    for request in mock_api.state.requests:
        assert "/comment" not in request.path
        assert "/attachment" not in request.path
        assert "/time_entries" not in request.path
        assert not (request.method == "POST" and f"/list/{LIST_ID}/task" in request.path)
        assert not (request.method == "DELETE" and request.path.endswith("/task/task_1"))

    text = batch_apply_text(payload)
    assert text.startswith(f"Manifest SHA-256: {hashlib.sha256(raw).hexdigest()}\n")
    assert "changed set_name" in text
    assert "no-op set_name" in text


def _expect_failed_first_update(
    api: MockClickUpAPI,
    *,
    leaked_message: str = "synthetic failure",
) -> None:
    before_1 = task_payload("task_1", name="Before")
    before_2 = task_payload("task_2", name="Second")
    expect_task(api, "task_1", before_1)
    expect_task(api, "task_2", before_2)
    expect_task(api, "task_1", before_1)
    api.expect(
        "PUT",
        "/api/v2/task/task_1",
        headers=WRITE_HEADERS,
        json_body={"name": "After"},
        response_status=400,
        response_json={"err": leaked_message},
    )


def test_apply_stops_with_typed_exact_partial_outcome_and_redacts_token(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.jsonl"
    raw = write_manifest(
        path,
        [
            {"task": "task_1", "set": {"name": "After"}},
            {"task": "task_2", "set": {"name": "Later"}},
        ],
    )
    _expect_failed_first_update(mock_api, leaked_message=f"bad {AUTH_VALUE} value")

    result = invoke(mock_api, ["task", "batch", "apply", str(path), "--yes"])

    assert result.exit_code == 1
    assert AUTH_VALUE not in result.stderr
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "batch_partial_failure"
    assert error["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    assert error["completed_task_ids"] == []
    assert error["failed_task_id"] == "task_1"
    assert error["failed_line"] == 1
    assert error["failed_operation"] == "set_name"
    assert error["failure"]["error"]["type"] == "api_error"
    assert "[REDACTED]" in error["failure"]["error"]["message"]
    partial = error["results"][0]
    assert partial["status"] == "failed"
    assert partial["completed_operation_count"] == 0
    assert partial["last_verified_task"]["name"] == "Before"
    assert [request.method for request in mock_api.state.requests] == ["GET", "GET", "GET", "PUT"]


def test_continue_on_error_applies_later_tasks_but_exits_nonzero(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.jsonl"
    write_manifest(
        path,
        [
            {"task": "task_1", "set": {"name": "After"}},
            {"task": "task_2", "set": {"name": "Later"}},
        ],
    )
    _expect_failed_first_update(mock_api)
    before_2 = task_payload("task_2", name="Second")
    after_2 = task_payload("task_2", name="Later")
    expect_task(mock_api, "task_2", before_2)
    mock_api.expect(
        "PUT",
        "/api/v2/task/task_2",
        headers=WRITE_HEADERS,
        json_body={"name": "Later"},
        response_json={},
    )
    expect_task(mock_api, "task_2", after_2)

    result = invoke(
        mock_api,
        ["task", "batch", "apply", str(path), "--yes", "--continue-on-error"],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "batch_partial_failure"
    assert error["completed_task_ids"] == ["task_2"]
    assert len(error["failures"]) == 1
    assert [task["status"] for task in error["results"]] == ["failed", "completed"]
    assert error["results"][1]["final_task"]["name"] == "Later"
    assert [request.method for request in mock_api.state.requests] == [
        "GET",
        "GET",
        "GET",
        "PUT",
        "GET",
        "PUT",
        "GET",
    ]


def test_batch_module_only_exposes_plan_and_apply_service_surface() -> None:
    public_methods = {
        name
        for name in dir(BatchService)
        if not name.startswith("_") and callable(getattr(BatchService, name))
    }

    assert public_methods == {"apply", "plan"}
    assert MAX_MANIFEST_LINE_BYTES > 0


class _InMemoryBatchClient:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task
        self.calls: list[str] = []

    def get_task(self, task_id: str) -> dict[str, Any]:
        assert task_id == self.task["id"]
        self.calls.append("get_task")
        return deepcopy(self.task)

    def get_list(self, list_id: str) -> dict[str, object]:
        assert list_id == LIST_ID
        self.calls.append("get_list")
        return {
            "id": LIST_ID,
            "statuses": [
                {"status": "Open", "type": "open"},
                {"status": "In Progress", "type": "custom"},
            ],
        }

    def update_task_status(self, task_id: str, canonical_status: str) -> None:
        assert task_id == self.task["id"]
        self.calls.append("update_task_status")
        self.task["status"] = {"status": canonical_status, "type": "custom"}

    def update_task(self, task_id: str, fields: dict[str, Any]) -> None:
        assert task_id == self.task["id"]
        self.calls.append("update_task")
        for field, value in fields.items():
            if field == "priority":
                self.task[field] = (
                    None if value is None else {"id": str(value), "priority": {2: "high"}[value]}
                )
            elif field in {"start_date", "due_date"}:
                self.task[field] = None if value is None else str(value)
            else:
                self.task[field] = value

    def update_task_due_date(
        self,
        task_id: str,
        due_date_ms: int | None,
        *,
        due_date_time: bool | None = None,
    ) -> None:
        assert task_id == self.task["id"]
        self.calls.append("update_task_due_date")
        self.task["due_date"] = None if due_date_ms is None else str(due_date_ms)
        self.task["due_date_time"] = due_date_time

    def update_task_tag(self, task_id: str, tag_name: str, *, add: bool) -> None:
        assert task_id == self.task["id"]
        self.calls.append("update_task_tag")
        tags = [tag["name"] for tag in self.task["tags"]]
        if add:
            tags.append(tag_name)
        else:
            tags = [tag for tag in tags if tag.casefold() != tag_name.casefold()]
        self.task["tags"] = [{"name": tag} for tag in tags]

    def update_task_assignees(
        self,
        task_id: str,
        *,
        add: list[int],
        remove: list[int],
    ) -> None:
        assert task_id == self.task["id"]
        self.calls.append("update_task_assignees")
        assignees = {assignee["id"] for assignee in self.task["assignees"]}
        assignees.update(add)
        assignees.difference_update(remove)
        self.task["assignees"] = [{"id": user_id} for user_id in sorted(assignees)]

    def __getattr__(self, name: str) -> Any:
        forbidden = {
            "create_task",
            "delete_task",
            "create_task_comment",
            "upload_task_attachment",
            "create_time_entry",
            "update_time_entry",
            "delete_time_entry",
        }
        if name in forbidden:
            raise AssertionError(f"batch called forbidden client method {name}")
        raise AttributeError(name)


def test_apply_dispatches_every_supported_operation_with_a_readback_and_no_forbidden_calls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.jsonl"
    write_manifest(
        path,
        [
            {
                "task": "task_1",
                "set": {
                    "name": "After",
                    "description": "New description",
                    "status": "in progress",
                    "due_date": "2026-08-20",
                    "priority": "high",
                    "start_date": "2026-08-19T12:30:00+02:00",
                    "archived": True,
                },
                "remove_tags": ["cold"],
                "add_tags": ["focus"],
                "remove_assignees": [456],
                "add_assignees": [123],
            }
        ],
    )
    client = _InMemoryBatchClient(
        task_payload(
            "task_1",
            name="Before",
            tags=["cold"],
            assignees=[456],
        )
    )

    result = BatchService(cast(ClickUpClient, client)).apply(
        load_manifest(path),
        continue_on_error=False,
    )

    assert result["operation_count"] == 11
    assert result["change_count"] == 11
    final_task = cast(dict[str, Any], cast(list[Any], result["tasks"])[0]["final_task"])
    assert final_task["name"] == "After"
    assert final_task["description"] == "New description"
    assert final_task["status"] == "In Progress"
    assert final_task["due_date"] == "2026-08-20"
    assert final_task["priority"] == "high"
    assert final_task["start_date"] == "2026-08-19T10:30:00Z"
    assert final_task["tags"] == ["focus"]
    assert [assignee["id"] for assignee in final_task["assignees"]] == ["123"]
    assert final_task["archived"] is True
    write_calls = [call for call in client.calls if call.startswith("update_")]
    assert len(write_calls) == 11
    for index, call in enumerate(client.calls):
        if call.startswith("update_"):
            assert client.calls[index + 1] == "get_task"
    assert client.calls[-2:] == ["update_task", "get_task"]
