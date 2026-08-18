"""Small synchronous ClickUp HTTP client with v2 details kept private."""

from __future__ import annotations

import email.utils
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from clickup_cli.errors import APIError, InvalidOperationError, TransportError
from clickup_cli.refs import validate_native_id, validate_numeric_id
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
        json_content_type: bool = False,
    ) -> httpx.Response:
        headers = (
            {"Content-Type": "application/json"}
            if json_body is not None or json_content_type
            else None
        )
        for attempt in range(self._max_rate_limit_retries + 1):
            try:
                response = self._http.request(
                    method,
                    self._url(path),
                    headers=headers,
                    json=json_body,
                )
            except httpx.RequestError as exc:
                safe_message = self._redact(str(exc))
                raise TransportError(f"ClickUp request failed: {safe_message}") from exc
            if response.status_code != 429 or attempt == self._max_rate_limit_retries:
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
        json_content_type: bool = False,
    ) -> JsonObject:
        response = self._request(
            method,
            path,
            json_body=json_body,
            json_content_type=json_content_type,
        )
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

    def get_task(self, task_id: str) -> JsonObject:
        task_id = validate_native_id(task_id, label="TASK_ID")
        return self._object_response("GET", f"/task/{task_id}")

    def get_list(self, list_id: str) -> JsonObject:
        list_id = validate_native_id(list_id, label="LIST_ID")
        return self._object_response("GET", f"/list/{list_id}")

    def update_task_status(self, task_id: str, canonical_status: str) -> None:
        task_id = validate_native_id(task_id, label="TASK_ID")
        response = self._request("PUT", f"/task/{task_id}", json_body={"status": canonical_status})
        response.close()

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

    @staticmethod
    def _positive_user_id(user_id: int, *, label: str = "ASSIGNEE") -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise InvalidOperationError(f"{label} must be a positive integer")
        return user_id

    def get_time_entries(
        self,
        workspace_id: str,
        *,
        start_ms: int,
        end_ms: int,
        assignee: int | None = None,
        task_id: str | None = None,
        space_id: str | None = None,
        folder_id: str | None = None,
        list_id: str | None = None,
        billable: bool | None = None,
    ) -> JsonObject:
        workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or start_ms < 0
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            raise InvalidOperationError("Time range must use non-negative increasing milliseconds")
        locations = [task_id, space_id, folder_id, list_id]
        if sum(value is not None for value in locations) > 1:
            raise InvalidOperationError("Only one time-entry location filter may be used")

        query: dict[str, str | int] = {"start_date": start_ms, "end_date": end_ms}
        if assignee is not None:
            query["assignee"] = self._positive_user_id(assignee)
        if task_id is not None:
            query["task_id"] = validate_native_id(task_id, label="TASK_ID")
        if space_id is not None:
            query["space_id"] = validate_numeric_id(space_id, label="SPACE_ID")
        if folder_id is not None:
            query["folder_id"] = validate_numeric_id(folder_id, label="FOLDER_ID")
        if list_id is not None:
            query["list_id"] = validate_numeric_id(list_id, label="LIST_ID")
        if billable is not None:
            if not isinstance(billable, bool):
                raise InvalidOperationError("Billable filter must be boolean")
            query["is_billable"] = "true" if billable else "false"
        return self._object_response(
            "GET",
            f"/team/{workspace_id}/time_entries?{urlencode(query)}",
            json_content_type=True,
        )

    def get_current_time_entry(
        self, workspace_id: str, *, assignee: int | None = None
    ) -> JsonObject:
        workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        path = f"/team/{workspace_id}/time_entries/current"
        if assignee is not None:
            path = f"{path}?{urlencode({'assignee': self._positive_user_id(assignee)})}"
        return self._object_response("GET", path, json_content_type=True)

    def start_time_entry(
        self,
        workspace_id: str,
        *,
        task_id: str | None = None,
        description: str | None = None,
        billable: bool | None = None,
    ) -> JsonObject:
        workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        body: JsonObject = {}
        if task_id is not None:
            body["tid"] = validate_native_id(task_id, label="TASK_ID")
        if description is not None:
            if not isinstance(description, str):
                raise InvalidOperationError("Description must be a string")
            body["description"] = description
        if billable is not None:
            if not isinstance(billable, bool):
                raise InvalidOperationError("Billable state must be boolean")
            body["billable"] = billable
        return self._object_response(
            "POST", f"/team/{workspace_id}/time_entries/start", json_body=body
        )

    def stop_time_entry(self, workspace_id: str) -> JsonObject:
        workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        return self._object_response(
            "POST",
            f"/team/{workspace_id}/time_entries/stop",
            json_content_type=True,
        )

    def create_time_entry(
        self,
        workspace_id: str,
        *,
        start_ms: int,
        duration_ms: int,
        task_id: str | None = None,
        description: str | None = None,
        billable: bool | None = None,
    ) -> JsonObject:
        workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
            raise InvalidOperationError("Start must be non-negative integer milliseconds")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise InvalidOperationError("Duration must be positive integer milliseconds")
        body: JsonObject = {"start": start_ms, "duration": duration_ms}
        if task_id is not None:
            body["tid"] = validate_native_id(task_id, label="TASK_ID")
        if description is not None:
            if not isinstance(description, str):
                raise InvalidOperationError("Description must be a string")
            body["description"] = description
        if billable is not None:
            if not isinstance(billable, bool):
                raise InvalidOperationError("Billable state must be boolean")
            body["billable"] = billable
        return self._object_response("POST", f"/team/{workspace_id}/time_entries", json_body=body)

    def get_time_entry(self, workspace_id: str, entry_id: str) -> JsonObject:
        workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        entry_id = validate_native_id(entry_id, label="ENTRY_ID")
        return self._object_response(
            "GET",
            f"/team/{workspace_id}/time_entries/{entry_id}",
            json_content_type=True,
        )

    def update_time_entry(self, workspace_id: str, entry_id: str, *, body: JsonObject) -> None:
        workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        entry_id = validate_native_id(entry_id, label="ENTRY_ID")
        allowed = {"description", "tags", "start", "end", "tid", "billable", "duration"}
        if "tags" not in body or not isinstance(body["tags"], list):
            raise InvalidOperationError("Time-entry updates require a tags array")
        if not body.keys() <= allowed:
            raise InvalidOperationError("Time-entry update contains unsupported fields")
        if ("start" in body) != ("end" in body):
            raise InvalidOperationError("Time-entry updates must provide start and end together")
        response = self._request(
            "PUT", f"/team/{workspace_id}/time_entries/{entry_id}", json_body=body
        )
        response.close()

    def delete_time_entry(self, workspace_id: str, entry_id: str) -> None:
        workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        entry_id = validate_native_id(entry_id, label="ENTRY_ID")
        response = self._request(
            "DELETE",
            f"/team/{workspace_id}/time_entries/{entry_id}",
            json_content_type=True,
        )
        response.close()
