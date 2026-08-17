"""Small synchronous ClickUp HTTP client with v2 details kept private."""

from __future__ import annotations

import email.utils
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

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
    ) -> httpx.Response:
        headers = {"Content-Type": "application/json"} if json_body is not None else None
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
            raise APIError(f"ClickUp API returned HTTP {status_code}: {safe_message}")
        return response

    def _object_response(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
    ) -> JsonObject:
        response = self._request(method, path, json_body=json_body)
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise APIError("ClickUp API returned invalid JSON") from exc
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise APIError("ClickUp API returned an unexpected JSON shape")
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

    def get_task_comments(self, task_id: str) -> JsonObject:
        task_id = validate_native_id(task_id, label="TASK_ID")
        return self._object_response("GET", f"/task/{task_id}/comment")

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
    ) -> JsonObject:
        list_id = validate_native_id(list_id, label="LIST_ID")
        body: JsonObject = {"name": name}
        if description is not None:
            body["description"] = description
        if status is not None:
            body["status"] = status
        if assignees:
            body["assignees"] = list(assignees)
        return self._object_response("POST", f"/list/{list_id}/task", json_body=body)

    def delete_task(self, task_id: str) -> None:
        task_id = validate_native_id(task_id, label="TASK_ID")
        response = self._request("DELETE", f"/task/{task_id}")
        response.close()
