"""Small synchronous ClickUp HTTP client with v2 details kept private."""

from __future__ import annotations

import email.utils
import stat
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote, urlencode

import httpx

from clickup_cli.errors import APIError, InvalidOperationError, TransportError
from clickup_cli.refs import validate_native_id
from clickup_cli.types import JsonObject


class ClickUpClient:
    """Reusable direct API client for the supported deterministic operations."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str,
        timeout: float = 10.0,
        max_rate_limit_retries: int = 2,
        max_retry_after: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._max_rate_limit_retries = max_rate_limit_retries
        self._max_retry_after = max_retry_after
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            headers={"Accept": "application/json", "Authorization": token},
            trust_env=False,
        )

    def __enter__(self) -> ClickUpClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""

        self._http.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}/v2{path}"

    @staticmethod
    def _query_path(path: str, parameters: Sequence[tuple[str, str | int]]) -> str:
        if not parameters:
            return path
        return f"{path}?{urlencode(parameters)}"

    def _redact(self, message: str) -> str:
        return message.replace(self._token, "[REDACTED]") if self._token else message

    def _retry_delay(self, response: httpx.Response) -> float:
        raw_value = response.headers.get("Retry-After", "").strip()
        if raw_value:
            try:
                delay = max(0.0, float(raw_value))
            except ValueError:
                try:
                    retry_at = email.utils.parsedate_to_datetime(raw_value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    now = datetime.fromtimestamp(self._clock(), UTC)
                    delay = max(0.0, (retry_at - now).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = 0.0
        else:
            reset_value = response.headers.get("X-RateLimit-Reset", "").strip()
            try:
                delay = max(0.0, float(reset_value) - self._clock())
            except ValueError:
                delay = 0.0
        return min(delay, self._max_retry_after)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
        files: dict[str, tuple[str, BinaryIO, str]] | None = None,
        json_content_type: bool = False,
    ) -> httpx.Response:
        if json_body is not None and files is not None:
            raise InvalidOperationError("A request cannot contain JSON and multipart bodies")
        headers = (
            {"Content-Type": "application/json"}
            if json_body is not None or json_content_type
            else None
        )
        retry_count = self._max_rate_limit_retries if method in {"GET", "PUT", "DELETE"} else 0
        for attempt in range(retry_count + 1):
            try:
                response = self._http.request(
                    method,
                    self._url(path),
                    headers=headers,
                    json=json_body,
                    files=files,
                )
            except httpx.RequestError as exc:
                safe_message = self._redact(str(exc))
                raise TransportError(f"ClickUp request failed: {safe_message}") from exc
            if response.status_code != 429 or attempt == retry_count:
                break
            delay = self._retry_delay(response)
            response.close()
            self._sleep(delay)

        if response.is_error:
            message = "request failed"
            try:
                payload: Any = response.json()
                if isinstance(payload, dict):
                    candidate = payload.get("err") or payload.get("message")
                    if isinstance(candidate, str) and candidate.strip():
                        message = candidate.strip()
            except ValueError:
                pass
            safe_message = self._redact(message)
            status_code = response.status_code
            response.close()
            raise APIError(
                f"ClickUp API returned HTTP {status_code}: {safe_message}",
                status_code=status_code,
            )
        return response

    def _object_response(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
    ) -> JsonObject:
        response = self._request(method, path, json_body=json_body)
        status_code = response.status_code
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise APIError("ClickUp API returned invalid JSON", status_code=status_code) from exc
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise APIError("ClickUp API returned an unexpected JSON shape", status_code=status_code)
        return payload

    def get_user(self) -> JsonObject:
        return self._object_response("GET", "/user")

    def get_workspaces(self) -> JsonObject:
        return self._object_response("GET", "/team")

    def get_spaces(self, workspace_id: str, *, archived: bool) -> JsonObject:
        workspace_id = validate_native_id(workspace_id, label="WORKSPACE_ID")
        path = self._query_path(
            f"/team/{workspace_id}/space",
            [("archived", str(archived).lower())],
        )
        return self._object_response("GET", path)

    def get_folders(self, space_id: str, *, archived: bool) -> JsonObject:
        space_id = validate_native_id(space_id, label="SPACE_ID")
        path = self._query_path(
            f"/space/{space_id}/folder",
            [("archived", str(archived).lower())],
        )
        return self._object_response("GET", path)

    def get_space_lists(self, space_id: str, *, archived: bool) -> JsonObject:
        space_id = validate_native_id(space_id, label="SPACE_ID")
        path = self._query_path(
            f"/space/{space_id}/list",
            [("archived", str(archived).lower())],
        )
        return self._object_response("GET", path)

    def get_folder_lists(self, folder_id: str, *, archived: bool) -> JsonObject:
        folder_id = validate_native_id(folder_id, label="FOLDER_ID")
        path = self._query_path(
            f"/folder/{folder_id}/list",
            [("archived", str(archived).lower())],
        )
        return self._object_response("GET", path)

    def get_task(self, task_id: str) -> JsonObject:
        task_id = validate_native_id(task_id, label="TASK_ID")
        return self._object_response("GET", f"/task/{task_id}")

    def get_list(self, list_id: str) -> JsonObject:
        list_id = validate_native_id(list_id, label="LIST_ID")
        return self._object_response("GET", f"/list/{list_id}")

    def get_list_tasks(
        self,
        list_id: str,
        *,
        page: int,
        parameters: Sequence[tuple[str, str | int]] = (),
    ) -> JsonObject:
        list_id = validate_native_id(list_id, label="LIST_ID")
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            raise InvalidOperationError("Task page must be a non-negative integer")
        path = self._query_path(f"/list/{list_id}/task", [("page", page), *parameters])
        return self._object_response("GET", path)

    def get_workspace_tasks(
        self,
        workspace_id: str,
        *,
        page: int,
        parameters: Sequence[tuple[str, str | int]] = (),
    ) -> JsonObject:
        workspace_id = validate_native_id(workspace_id, label="WORKSPACE_ID")
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            raise InvalidOperationError("Task page must be a non-negative integer")
        path = self._query_path(f"/team/{workspace_id}/task", [("page", page), *parameters])
        return self._object_response("GET", path)

    def update_task_status(self, task_id: str, canonical_status: str) -> None:
        task_id = validate_native_id(task_id, label="TASK_ID")
        response = self._request("PUT", f"/task/{task_id}", json_body={"status": canonical_status})
        response.close()

    def update_task(self, task_id: str, fields: JsonObject) -> None:
        task_id = validate_native_id(task_id, label="TASK_ID")
        if not fields:
            raise InvalidOperationError("A task update must contain at least one field")
        supported_fields = {
            "archived",
            "description",
            "name",
            "priority",
            "start_date",
            "start_date_time",
        }
        unsupported = sorted(set(fields) - supported_fields)
        if unsupported:
            raise InvalidOperationError("Unsupported task update fields: " + ", ".join(unsupported))
        if "name" in fields and (
            not isinstance(fields["name"], str) or not str(fields["name"]).strip()
        ):
            raise InvalidOperationError("Task name must be non-empty text")
        if "description" in fields and not isinstance(fields["description"], str):
            raise InvalidOperationError("Task description must be text")
        if "priority" in fields:
            priority = fields["priority"]
            if priority is not None and (
                isinstance(priority, bool)
                or not isinstance(priority, int)
                or priority not in range(1, 5)
            ):
                raise InvalidOperationError("Task priority must be 1, 2, 3, 4, or null")
        if "start_date" in fields:
            start_date = fields["start_date"]
            if start_date is not None and (
                isinstance(start_date, bool) or not isinstance(start_date, int) or start_date < 0
            ):
                raise InvalidOperationError(
                    "Task start date must be non-negative milliseconds or null"
                )
        if "start_date_time" in fields:
            if not isinstance(fields["start_date_time"], bool):
                raise InvalidOperationError("start_date_time must be boolean")
            if "start_date" not in fields or fields["start_date"] is None:
                raise InvalidOperationError(
                    "start_date_time requires a non-null start_date in the same update"
                )
        if "archived" in fields and not isinstance(fields["archived"], bool):
            raise InvalidOperationError("Task archived state must be boolean")
        response = self._request("PUT", f"/task/{task_id}", json_body=fields)
        response.close()

    def update_task_tag(self, task_id: str, tag_name: str, *, add: bool) -> None:
        task_id = validate_native_id(task_id, label="TASK_ID")
        if (
            not isinstance(tag_name, str)
            or not tag_name.strip()
            or any(character in tag_name for character in ("\0", "\r", "\n"))
        ):
            raise InvalidOperationError("Tag name cannot be empty")
        encoded_tag = "%2E" * len(tag_name) if tag_name in {".", ".."} else quote(tag_name, safe="")
        method = "POST" if add else "DELETE"
        response = self._request(
            method,
            f"/task/{task_id}/tag/{encoded_tag}",
            json_content_type=True,
        )
        response.close()

    def upload_task_attachment(
        self,
        task_id: str,
        path: Path,
        *,
        upload_name: str,
    ) -> JsonObject:
        task_id = validate_native_id(task_id, label="TASK_ID")
        if (
            not upload_name
            or not upload_name.strip()
            or upload_name in {".", ".."}
            or any(character in upload_name for character in ("/", "\\", "\0", "\r", "\n"))
        ):
            raise InvalidOperationError("Attachment name must be a plain file name")
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise InvalidOperationError(f"Attachment path is not a regular file: {path}")
            with path.open("rb") as handle:
                return self._object_response_with_files(
                    "POST",
                    f"/task/{task_id}/attachment",
                    files={"attachment": (upload_name, handle, "application/octet-stream")},
                )
        except OSError as exc:
            raise InvalidOperationError(f"Could not read attachment file: {path}") from exc

    def _object_response_with_files(
        self,
        method: str,
        path: str,
        *,
        files: dict[str, tuple[str, BinaryIO, str]],
    ) -> JsonObject:
        response = self._request(method, path, files=files)
        status_code = response.status_code
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise APIError("ClickUp API returned invalid JSON", status_code=status_code) from exc
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise APIError("ClickUp API returned an unexpected JSON shape", status_code=status_code)
        return payload

    def get_task_comments(
        self,
        task_id: str,
        *,
        start: int | None = None,
        start_id: str | None = None,
    ) -> JsonObject:
        task_id = validate_native_id(task_id, label="TASK_ID")
        if (start is None) != (start_id is None):
            raise InvalidOperationError("start and start_id must be provided together")
        path = f"/task/{task_id}/comment"
        if start is not None and start_id is not None:
            if isinstance(start, bool) or not isinstance(start, int) or start < 0:
                raise InvalidOperationError("start must be a non-negative integer")
            start_id = validate_native_id(start_id, label="START_ID")
            path = f"{path}?{urlencode({'start': start, 'start_id': start_id})}"
        return self._object_response("GET", path)

    def create_task_comment(self, task_id: str, comment_text: str) -> JsonObject:
        task_id = validate_native_id(task_id, label="TASK_ID")
        if not isinstance(comment_text, str) or not comment_text.strip():
            raise InvalidOperationError("Comment text cannot be empty")
        return self._object_response(
            "POST",
            f"/task/{task_id}/comment",
            json_body={"comment_text": comment_text, "notify_all": False},
        )

    def update_task_due_date(
        self,
        task_id: str,
        due_date_ms: int | None,
        *,
        due_date_time: bool | None = None,
    ) -> None:
        task_id = validate_native_id(task_id, label="TASK_ID")
        if due_date_ms is None:
            body: JsonObject = {"due_date": None}
        else:
            if not isinstance(due_date_ms, int) or isinstance(due_date_ms, bool) or due_date_ms < 0:
                raise InvalidOperationError("Due date milliseconds must be a non-negative integer")
            if not isinstance(due_date_time, bool):
                raise InvalidOperationError("due_date_time is required when setting a due date")
            body = {"due_date": due_date_ms, "due_date_time": due_date_time}
        response = self._request("PUT", f"/task/{task_id}", json_body=body)
        response.close()

    def update_task_assignees(
        self,
        task_id: str,
        *,
        add: list[int],
        remove: list[int],
    ) -> None:
        task_id = validate_native_id(task_id, label="TASK_ID")
        if not add and not remove:
            raise InvalidOperationError("An assignee update must add or remove at least one user")
        if any(
            not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0
            for user_id in [*add, *remove]
        ):
            raise InvalidOperationError("Assignee IDs must be positive integers")
        if set(add) & set(remove):
            raise InvalidOperationError("The same user cannot be added and removed together")
        response = self._request(
            "PUT",
            f"/task/{task_id}",
            json_body={"assignees": {"add": list(add), "rem": list(remove)}},
        )
        response.close()

    def create_task(
        self,
        list_id: str,
        name: str,
        *,
        description: str | None = None,
        status: str | None = None,
        assignees: list[int] | None = None,
        due_date: int | None = None,
        due_date_time: bool | None = None,
        tags: list[str] | None = None,
    ) -> JsonObject:
        list_id = validate_native_id(list_id, label="LIST_ID")
        if not isinstance(name, str) or not name.strip():
            raise InvalidOperationError("Task name cannot be empty")
        body: JsonObject = {"name": name}
        if description is not None:
            body["description"] = description
        if status is not None:
            body["status"] = status
        if assignees:
            if any(
                not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0
                for user_id in assignees
            ):
                raise InvalidOperationError("Assignee IDs must be positive integers")
            body["assignees"] = list(assignees)
        if due_date is not None:
            if isinstance(due_date, bool) or not isinstance(due_date, int) or due_date < 0:
                raise InvalidOperationError("due_date must be a non-negative integer")
            if not isinstance(due_date_time, bool):
                raise InvalidOperationError("due_date_time must be boolean when due_date is set")
            body["due_date"] = due_date
            body["due_date_time"] = due_date_time
        elif due_date_time is not None:
            raise InvalidOperationError("due_date_time cannot be set without due_date")
        if tags:
            if any(not isinstance(tag, str) or not tag.strip() for tag in tags):
                raise InvalidOperationError("Tags must be non-empty strings")
            body["tags"] = list(tags)
        return self._object_response("POST", f"/list/{list_id}/task", json_body=body)

    def delete_task(self, task_id: str) -> None:
        task_id = validate_native_id(task_id, label="TASK_ID")
        response = self._request("DELETE", f"/task/{task_id}")
        response.close()
