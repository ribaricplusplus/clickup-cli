from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from clickup_cli.cli import app
from tests.conftest import MockClickUpAPI, MultipartPart

AUTH_VALUE = "attachment-auth-secret"
TASK_ID = "task_123"
LIST_ID = "list_456"
ATTACHMENT_ID = "attachment-1.txt"
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
WRITE_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}

runner = CliRunner()


def attachment_payload(
    attachment_id: str = ATTACHMENT_ID,
    *,
    title: str = "report.txt",
    url: str = "https://attachments.example.invalid/report.txt",
    size: int | None = 7,
    date: int | None = 1_900_000_000_000,
    extension: str | None = "txt",
) -> dict[str, object]:
    return {
        "date": date,
        "extension": extension,
        "id": attachment_id,
        "size": size,
        "title": title,
        "url": url,
    }


def task_payload(
    *,
    task_id: str = TASK_ID,
    name: str = "Attachment task",
    attachments: list[dict[str, object]] | None = None,
    archived: bool = False,
    priority: int | None = None,
    start_date: int | None = None,
    start_date_time: bool | None = None,
) -> dict[str, Any]:
    return {
        "archived": archived,
        "assignees": [],
        "attachments": attachments or [],
        "description": "Attachment coverage",
        "due_date": None,
        "due_date_time": None,
        "id": task_id,
        "list": {"id": LIST_ID, "name": "Local list"},
        "name": name,
        "priority": (
            None
            if priority is None
            else {
                "id": str(priority),
                "priority": {1: "urgent", 2: "high", 3: "normal", 4: "low"}[priority],
            }
        ),
        "start_date": None if start_date is None else str(start_date),
        "start_date_time": start_date_time,
        "status": {"status": "Open", "type": "open"},
        "tags": [],
        "url": f"https://app.clickup.com/t/{task_id}",
    }


def invoke(api: MockClickUpAPI, args: list[str]) -> Result:
    return runner.invoke(
        app,
        ["--base-url", api.base_url, "--json", *args],
        env={"CLICKUP_API_TOKEN": AUTH_VALUE},
    )


def expect_task(
    api: MockClickUpAPI,
    payload: dict[str, Any],
    *,
    task_id: str = TASK_ID,
) -> None:
    api.expect(
        "GET",
        f"/api/v2/task/{task_id}",
        headers=READ_HEADERS,
        response_json=payload,
    )


def expect_upload(
    api: MockClickUpAPI,
    *,
    task_id: str,
    filename: str,
    body: bytes,
    response_status: int = 200,
    response_json: object = None,
) -> None:
    api.expect(
        "POST",
        f"/api/v2/task/{task_id}/attachment",
        headers=READ_HEADERS,
        multipart_body=(
            MultipartPart(
                name="attachment",
                filename=filename,
                content_type="application/octet-stream",
                body=body,
            ),
        ),
        response_status=response_status,
        response_json=response_json,
    )


def test_attachment_list_normalizes_exact_stable_shape(mock_api: MockClickUpAPI) -> None:
    complete = attachment_payload()
    sparse: dict[str, object] = {"id": "attachment-2", "name": "fallback-name"}
    expect_task(mock_api, task_payload(attachments=[complete, sparse]))

    result = invoke(mock_api, ["task", "attachment", "list", TASK_ID])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "attachments": [
                {
                    "date": 1_900_000_000_000,
                    "extension": "txt",
                    "id": ATTACHMENT_ID,
                    "size": 7,
                    "title": "report.txt",
                    "url": "https://attachments.example.invalid/report.txt",
                },
                {
                    "date": None,
                    "extension": None,
                    "id": "attachment-2",
                    "size": None,
                    "title": "fallback-name",
                    "url": None,
                },
            ],
            "task_id": TASK_ID,
        },
    }


def test_attachment_upload_exact_multipart_and_verified_readback(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    upload_path = tmp_path / "local.bin"
    upload_path.write_bytes(b"exact multipart bytes\x00\xff")
    created = attachment_payload(title="wire-name.dat", size=None, date=None, extension="dat")
    expect_upload(
        mock_api,
        task_id=TASK_ID,
        filename="wire-name.dat",
        body=b"exact multipart bytes\x00\xff",
        response_json=created,
    )
    expect_task(mock_api, task_payload(attachments=[created]))

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "upload",
            TASK_ID,
            str(upload_path),
            "--name",
            "wire-name.dat",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["attachment"]["id"] == ATTACHMENT_ID
    assert payload["attachment"]["title"] == "wire-name.dat"
    assert (
        mock_api.state.requests[0]
        .headers["content-type"]
        .startswith("multipart/form-data; boundary=")
    )


def test_attachment_upload_rejects_non_file_before_network(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    result = invoke(
        mock_api,
        ["task", "attachment", "upload", TASK_ID, str(tmp_path)],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_operation"
    assert mock_api.state.requests == []


def test_attachment_upload_rejects_unsafe_name_before_network(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    upload_path = tmp_path / "local.bin"
    upload_path.write_bytes(b"content")

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "upload",
            TASK_ID,
            str(upload_path),
            "--name",
            "../unsafe.bin",
        ],
    )

    assert result.exit_code == 1
    assert mock_api.state.requests == []


def test_attachment_upload_readback_mismatch_is_typed(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    upload_path = tmp_path / "report.txt"
    upload_path.write_text("content", encoding="utf-8")
    created = attachment_payload()
    expect_upload(
        mock_api,
        task_id=TASK_ID,
        filename="report.txt",
        body=b"content",
        response_json=created,
    )
    mismatched = attachment_payload(title="different-title.txt")
    expect_task(mock_api, task_payload(attachments=[mismatched]))

    result = invoke(
        mock_api,
        ["task", "attachment", "upload", TASK_ID, str(upload_path)],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "attachment_uploaded_but_unverified"
    assert error["attachment_id"] == ATTACHMENT_ID


def test_attachment_upload_api_error_redacts_token(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    upload_path = tmp_path / "report.txt"
    upload_path.write_text("content", encoding="utf-8")
    expect_upload(
        mock_api,
        task_id=TASK_ID,
        filename="report.txt",
        body=b"content",
        response_status=401,
        response_json={"err": f"Rejected {AUTH_VALUE}"},
    )

    result = invoke(
        mock_api,
        ["task", "attachment", "upload", TASK_ID, str(upload_path)],
    )

    assert result.exit_code == 1
    assert AUTH_VALUE not in result.stdout + result.stderr
    assert "[REDACTED]" in result.stderr


def test_create_with_multiple_attachments_creates_once_then_uploads_sequentially(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.bin"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    created_task_id = "created_1"
    first = attachment_payload(
        "first-id.txt",
        title="first.txt",
        url="https://attachments.example.invalid/first",
    )
    second = attachment_payload(
        "second-id.bin",
        title="second.bin",
        url="https://attachments.example.invalid/second",
        extension="bin",
    )
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={"name": "Created with files"},
        response_json={"id": created_task_id},
    )
    expect_task(
        mock_api,
        task_payload(task_id=created_task_id, name="Created with files"),
        task_id=created_task_id,
    )
    expect_upload(
        mock_api,
        task_id=created_task_id,
        filename="first.txt",
        body=b"first",
        response_json=first,
    )
    expect_task(
        mock_api,
        task_payload(
            task_id=created_task_id,
            name="Created with files",
            attachments=[first],
        ),
        task_id=created_task_id,
    )
    expect_upload(
        mock_api,
        task_id=created_task_id,
        filename="second.bin",
        body=b"second",
        response_json=second,
    )
    expect_task(
        mock_api,
        task_payload(
            task_id=created_task_id,
            name="Created with files",
            attachments=[first, second],
        ),
        task_id=created_task_id,
    )

    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Created with files",
            "--list-id",
            LIST_ID,
            "--attach",
            str(first_path),
            "--attach",
            str(second_path),
        ],
    )

    assert result.exit_code == 0, result.output
    attachments = json.loads(result.stdout)["result"]["task"]["attachments"]
    assert [attachment["id"] for attachment in attachments] == [
        "first-id.txt",
        "second-id.bin",
    ]
    assert [request.method for request in mock_api.state.requests] == [
        "POST",
        "GET",
        "POST",
        "GET",
        "POST",
        "GET",
    ]


def test_create_attachment_failure_returns_typed_partial_outcome(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first = attachment_payload("first-id", title="first.txt")
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={"name": "Partially attached"},
        response_json={"id": TASK_ID},
    )
    expect_task(mock_api, task_payload(name="Partially attached"))
    expect_upload(
        mock_api,
        task_id=TASK_ID,
        filename="first.txt",
        body=b"first",
        response_json=first,
    )
    expect_task(
        mock_api,
        task_payload(name="Partially attached", attachments=[first]),
    )
    expect_upload(
        mock_api,
        task_id=TASK_ID,
        filename="second.txt",
        body=b"second",
        response_status=400,
        response_json={"err": "rejected"},
    )

    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Partially attached",
            "--list-id",
            LIST_ID,
            "--attach",
            str(first_path),
            "--attach",
            str(second_path),
        ],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "created_but_attachment_failed"
    assert error["task_id"] == TASK_ID
    assert error["failed_path"] == str(second_path)
    assert error["uploaded_attachment_ids"] == ["first-id"]


def test_create_with_attachment_wraps_known_id_readback_failure(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    upload_path = tmp_path / "requested.txt"
    upload_path.write_bytes(b"requested")
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={"name": "Readback failed"},
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=READ_HEADERS,
        response_status=503,
        response_json={"err": "temporarily unavailable"},
    )

    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Readback failed",
            "--list-id",
            LIST_ID,
            "--attach",
            str(upload_path),
        ],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "created_but_attachment_failed"
    assert error["task_id"] == TASK_ID
    assert error["failed_path"] == str(upload_path)
    assert error["uploaded_attachment_ids"] == []


def test_create_validates_every_attachment_before_creating_task(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    valid_path = tmp_path / "valid.txt"
    valid_path.write_bytes(b"valid")

    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Must not create",
            "--list-id",
            LIST_ID,
            "--attach",
            str(valid_path),
            "--attach",
            str(tmp_path / "missing.txt"),
        ],
    )

    assert result.exit_code == 1
    assert mock_api.state.requests == []


def test_create_attachment_error_redacts_token_in_partial_outcome(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    upload_path = tmp_path / "secret.txt"
    upload_path.write_bytes(b"not secret")
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={"name": "Token redaction"},
        response_json={"id": TASK_ID},
    )
    expect_task(mock_api, task_payload(name="Token redaction"))
    expect_upload(
        mock_api,
        task_id=TASK_ID,
        filename="secret.txt",
        body=b"not secret",
        response_status=401,
        response_json={"err": f"Rejected {AUTH_VALUE}"},
    )

    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Token redaction",
            "--list-id",
            LIST_ID,
            "--attach",
            str(upload_path),
        ],
    )

    assert result.exit_code == 1
    assert AUTH_VALUE not in result.stdout + result.stderr
    assert json.loads(result.stderr)["error"]["type"] == "created_but_attachment_failed"


def test_task_create_is_not_retried_on_rate_limit(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers=WRITE_HEADERS,
        json_body={"name": "Do not retry"},
        response_status=429,
        response_json={"err": "slow down"},
        response_headers={"Retry-After": "0"},
    )

    result = invoke(
        mock_api,
        ["task", "create", "Do not retry", "--list-id", LIST_ID],
    )

    assert result.exit_code == 1
    assert len(mock_api.state.requests) == 1


def test_download_uses_only_fetched_attachment_and_never_forwards_auth(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    origin = mock_api.base_url.removesuffix("/api")
    download_url = f"{origin}/files/report"
    attachment = attachment_payload(url=download_url)
    expect_task(mock_api, task_payload(attachments=[attachment]))
    mock_api.expect(
        "GET",
        "/files/report",
        headers={"Accept": "*/*"},
        response_body=b"downloaded bytes",
        response_headers={"Content-Type": "application/octet-stream"},
    )
    output = tmp_path / "report.txt"

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            ATTACHMENT_ID,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output.read_bytes() == b"downloaded bytes"
    assert json.loads(result.stdout)["result"]["size"] == 16
    download_request = mock_api.state.requests[1]
    assert "authorization" not in download_request.headers
    assert AUTH_VALUE not in repr(download_request)


def test_download_follows_safe_local_redirect_without_auth(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    origin = mock_api.base_url.removesuffix("/api")
    attachment = attachment_payload(url=f"{origin}/redirect/start")
    expect_task(mock_api, task_payload(attachments=[attachment]))
    mock_api.expect(
        "GET",
        "/redirect/start",
        headers={"Accept": "*/*"},
        response_status=302,
        response_headers={"Location": "/redirect/final"},
    )
    mock_api.expect(
        "GET",
        "/redirect/final",
        headers={"Accept": "*/*"},
        response_body=b"redirected",
    )
    output = tmp_path / "redirected.txt"

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            ATTACHMENT_ID,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output.read_bytes() == b"redirected"
    assert all("authorization" not in request.headers for request in mock_api.state.requests[1:])


def test_download_refuses_overwrite_without_force_before_attachment_request(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    origin = mock_api.base_url.removesuffix("/api")
    attachment = attachment_payload(url=f"{origin}/files/report")
    expect_task(mock_api, task_payload(attachments=[attachment]))
    output = tmp_path / "existing.txt"
    output.write_bytes(b"keep me")

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            ATTACHMENT_ID,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert output.read_bytes() == b"keep me"
    assert len(mock_api.state.requests) == 1


def test_download_force_atomically_replaces_existing_file(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    origin = mock_api.base_url.removesuffix("/api")
    attachment = attachment_payload(url=f"{origin}/files/replacement")
    expect_task(mock_api, task_payload(attachments=[attachment]))
    mock_api.expect("GET", "/files/replacement", response_body=b"replacement")
    output = tmp_path / "existing.txt"
    output.write_bytes(b"old")

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            ATTACHMENT_ID,
            "--output",
            str(output),
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert output.read_bytes() == b"replacement"


def test_download_failure_cleans_atomic_temp_file(mock_api: MockClickUpAPI, tmp_path: Path) -> None:
    origin = mock_api.base_url.removesuffix("/api")
    attachment = attachment_payload(url=f"{origin}/files/truncated")
    expect_task(mock_api, task_payload(attachments=[attachment]))
    mock_api.expect(
        "GET",
        "/files/truncated",
        response_body=b"abc",
        response_headers={"Content-Length": "10"},
    )
    output = tmp_path / "truncated.txt"

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            ATTACHMENT_ID,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert not output.exists()
    assert list(tmp_path.glob(".clickup-attachment-*")) == []


def test_download_rejects_declared_oversize_before_writing(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    origin = mock_api.base_url.removesuffix("/api")
    attachment = attachment_payload(url=f"{origin}/files/oversize")
    expect_task(mock_api, task_payload(attachments=[attachment]))
    mock_api.expect(
        "GET",
        "/files/oversize",
        response_body=b"not read",
        response_headers={"Content-Length": str(100 * 1024 * 1024 + 1)},
    )
    output = tmp_path / "oversize.txt"

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            ATTACHMENT_ID,
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert "100 MiB" in result.stderr
    assert not output.exists()
    assert list(tmp_path.glob(".clickup-attachment-*")) == []


def test_download_rejects_non_https_nonlocal_url_without_contacting_it(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    attachment = attachment_payload(url="http://example.invalid/file")
    expect_task(mock_api, task_payload(attachments=[attachment]))

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            ATTACHMENT_ID,
            "--output",
            str(tmp_path / "file"),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "attachment_download_failed"
    assert len(mock_api.state.requests) == 1


def test_download_rejects_attachment_not_on_fetched_task(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    expect_task(mock_api, task_payload(attachments=[]))

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            "not-present",
            "--output",
            str(tmp_path / "file"),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "attachment_not_found"
    assert len(mock_api.state.requests) == 1


def test_download_redirects_are_bounded(mock_api: MockClickUpAPI, tmp_path: Path) -> None:
    origin = mock_api.base_url.removesuffix("/api")
    attachment = attachment_payload(url=f"{origin}/redirect/0")
    expect_task(mock_api, task_payload(attachments=[attachment]))
    for index in range(6):
        mock_api.expect(
            "GET",
            f"/redirect/{index}",
            response_status=302,
            response_headers={"Location": f"/redirect/{index + 1}"},
        )

    result = invoke(
        mock_api,
        [
            "task",
            "attachment",
            "download",
            TASK_ID,
            ATTACHMENT_ID,
            "--output",
            str(tmp_path / "file"),
        ],
    )

    assert result.exit_code == 1
    assert "5 redirects" in result.stderr
    assert not (tmp_path / "file").exists()


def test_show_enriches_all_new_stable_fields(mock_api: MockClickUpAPI) -> None:
    start_ms = 1_893_593_045_000
    attachment = attachment_payload()
    expect_task(
        mock_api,
        task_payload(
            attachments=[attachment],
            archived=True,
            priority=1,
            start_date=start_ms,
            start_date_time=True,
        ),
    )

    result = invoke(mock_api, ["task", "show", TASK_ID])

    assert result.exit_code == 0, result.output
    task = json.loads(result.stdout)["result"]["task"]
    assert task == {
        "archived": True,
        "assignees": [],
        "attachments": [
            {
                "date": 1_900_000_000_000,
                "extension": "txt",
                "id": ATTACHMENT_ID,
                "size": 7,
                "title": "report.txt",
                "url": "https://attachments.example.invalid/report.txt",
            }
        ],
        "description": "Attachment coverage",
        "due_date": None,
        "due_date_ms": None,
        "due_date_time": None,
        "id": TASK_ID,
        "list_id": LIST_ID,
        "list_name": "Local list",
        "name": "Attachment task",
        "priority": "urgent",
        "start_date": "2030-01-02T14:04:05Z",
        "start_date_ms": start_ms,
        "start_date_time": True,
        "status": "Open",
        "status_type": "open",
        "tags": [],
        "url": f"https://app.clickup.com/t/{TASK_ID}",
    }
