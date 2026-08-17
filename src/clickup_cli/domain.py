"""Deterministic task operations independent of the CLI adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

from clickup_cli.client import ClickUpClient
from clickup_cli.errors import (
    APIError,
    CompletionStatusError,
    InvalidDueDateError,
    InvalidOperationError,
    InvalidStatusError,
    VerificationError,
)
from clickup_cli.types import JsonObject, JsonValue

_COMPLETION_LABELS = ("completed", "complete", "done", "closed")
_COMPLETION_TYPES = {"done", "closed"}
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIMED_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class StatusDefinition:
    label: str
    status_type: str | None


@dataclass(frozen=True)
class MutationResult:
    task_id: str
    previous_status: str
    status: str
    changed: bool
    task: JsonObject


@dataclass(frozen=True)
class DueDateInput:
    milliseconds: int
    display: str
    has_time: bool


@dataclass(frozen=True)
class DueDateState:
    milliseconds: int | None
    has_time: bool | None


@dataclass(frozen=True)
class DueDateMutationResult:
    task_id: str
    previous_due_date_ms: int | None
    due_date_ms: int | None
    due_date: str | None
    due_date_time: bool | None
    changed: bool
    task: JsonObject


@dataclass(frozen=True)
class AssignmentMutationResult:
    task_id: str
    user_id: int
    assigned: bool
    changed: bool
    assignee_ids: list[int]
    task: JsonObject


@dataclass(frozen=True)
class CommentMutationResult:
    task_id: str
    comment: JsonObject


def _mapping(value: JsonValue | None, *, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise APIError(f"ClickUp response is missing {label}")
    return cast(JsonObject, value)


def _required_string(value: JsonValue | None, *, label: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value):
        raise APIError(f"ClickUp response is missing {label}")
    return str(value)


def _optional_string(value: JsonValue | None) -> str | None:
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _timestamp_ms(value: datetime) -> int:
    normalized = value.astimezone(UTC)
    delta = normalized - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _utc_date_from_ms(milliseconds: int) -> str:
    try:
        return (_EPOCH + timedelta(milliseconds=milliseconds)).date().isoformat()
    except OverflowError as exc:
        raise APIError("ClickUp response contains an out-of-range due date") from exc


def parse_due_date(value: str) -> DueDateInput:
    """Parse a date-only value or a timezone-aware ISO 8601 timestamp."""

    requested = value.strip()
    if _DATE_ONLY.fullmatch(requested):
        try:
            parsed_date = date.fromisoformat(requested)
        except ValueError as exc:
            raise InvalidDueDateError(
                "Due date must be YYYY-MM-DD or an ISO 8601 timestamp with Z or an offset"
            ) from exc
        parsed = datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
        milliseconds = _timestamp_ms(parsed)
        if milliseconds < 0:
            raise InvalidDueDateError("Due date must not be before 1970-01-01")
        return DueDateInput(
            milliseconds=milliseconds,
            display=parsed_date.isoformat(),
            has_time=False,
        )

    if _TIMED_DATE.fullmatch(requested) is None:
        raise InvalidDueDateError(
            "Due date must be YYYY-MM-DD or an ISO 8601 timestamp with Z or an offset"
        )
    iso_value = f"{requested[:-1]}+00:00" if requested.endswith("Z") else requested
    try:
        parsed_datetime = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise InvalidDueDateError("Due date contains an invalid ISO 8601 timestamp") from exc
    if parsed_datetime.utcoffset() is None:
        raise InvalidDueDateError("Timed due dates require Z or an explicit UTC offset")
    if parsed_datetime.microsecond % 1_000:
        raise InvalidDueDateError("Timed due dates support at most millisecond precision")
    milliseconds = _timestamp_ms(parsed_datetime)
    if milliseconds < 0:
        raise InvalidDueDateError("Due date must not be before 1970-01-01")
    normalized = parsed_datetime.astimezone(UTC)
    timespec = "milliseconds" if normalized.microsecond else "seconds"
    display = normalized.isoformat(timespec=timespec).replace("+00:00", "Z")
    return DueDateInput(milliseconds=milliseconds, display=display, has_time=True)


def task_status(task: JsonObject) -> str:
    status = _mapping(task.get("status"), label="task status")
    return _required_string(status.get("status"), label="task status label")


def task_list_id(task: JsonObject) -> str:
    home_list = _mapping(task.get("list"), label="task home list")
    return _required_string(home_list.get("id"), label="task home list ID")


def task_due_date(task: JsonObject) -> DueDateState:
    raw_due_date = task.get("due_date")
    raw_has_time = task.get("due_date_time")
    if raw_has_time is not None and not isinstance(raw_has_time, bool):
        raise APIError("ClickUp response contains an invalid due-date time flag")
    has_time = raw_has_time if isinstance(raw_has_time, bool) else None
    if raw_due_date is None:
        return DueDateState(milliseconds=None, has_time=has_time)
    if isinstance(raw_due_date, bool) or not isinstance(raw_due_date, (str, int)):
        raise APIError("ClickUp response contains an invalid due date")
    try:
        milliseconds = int(raw_due_date)
    except ValueError as exc:
        raise APIError("ClickUp response contains an invalid due date") from exc
    if milliseconds < 0 or str(milliseconds) != str(raw_due_date):
        raise APIError("ClickUp response contains an invalid due date")
    return DueDateState(milliseconds=milliseconds, has_time=has_time)


def task_assignee_ids(task: JsonObject) -> list[int]:
    raw_assignees = task.get("assignees")
    if not isinstance(raw_assignees, list):
        raise APIError("ClickUp response is missing task assignees")
    identifiers: set[int] = set()
    for raw_assignee in raw_assignees:
        if not isinstance(raw_assignee, dict):
            raise APIError("ClickUp response contains an invalid task assignee")
        raw_id = raw_assignee.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise APIError("ClickUp response contains an invalid task assignee ID")
        try:
            user_id = int(raw_id)
        except ValueError as exc:
            raise APIError("ClickUp response contains an invalid task assignee ID") from exc
        if user_id <= 0 or str(user_id) != str(raw_id):
            raise APIError("ClickUp response contains an invalid task assignee ID")
        identifiers.add(user_id)
    return sorted(identifiers)


def list_statuses(list_payload: JsonObject) -> list[StatusDefinition]:
    raw_statuses = list_payload.get("statuses")
    if not isinstance(raw_statuses, list):
        raise APIError("ClickUp response is missing list statuses")
    statuses: list[StatusDefinition] = []
    for raw_status in raw_statuses:
        if not isinstance(raw_status, dict):
            raise APIError("ClickUp response contains an invalid list status")
        label = _required_string(raw_status.get("status"), label="list status label")
        raw_type = raw_status.get("type")
        status_type = str(raw_type) if isinstance(raw_type, (str, int)) else None
        statuses.append(StatusDefinition(label=label, status_type=status_type))
    if not statuses:
        raise APIError("ClickUp list has no statuses")
    return statuses


def summarize_task(task: JsonObject) -> JsonObject:
    """Produce the stable task shape used by machine-readable CLI output."""

    status_payload = task.get("status")
    status_label: JsonValue = None
    status_type: JsonValue = None
    if isinstance(status_payload, dict):
        raw_label = status_payload.get("status")
        raw_type = status_payload.get("type")
        status_label = str(raw_label) if isinstance(raw_label, (str, int)) else None
        status_type = str(raw_type) if isinstance(raw_type, (str, int)) else None
    list_payload = task.get("list")
    list_id: JsonValue = None
    if isinstance(list_payload, dict):
        raw_list_id = list_payload.get("id")
        list_id = str(raw_list_id) if isinstance(raw_list_id, (str, int)) else None

    raw_id = task.get("id")
    raw_name = task.get("name")
    raw_description = task.get("description")
    raw_url = task.get("url")
    return {
        "description": str(raw_description) if isinstance(raw_description, str) else None,
        "id": str(raw_id) if isinstance(raw_id, (str, int)) else None,
        "list_id": list_id,
        "name": str(raw_name) if isinstance(raw_name, str) else None,
        "status": status_label,
        "status_type": status_type,
        "url": str(raw_url) if isinstance(raw_url, str) else None,
    }


def summarize_comment(comment: JsonObject) -> JsonObject:
    comment_id = _required_string(comment.get("id"), label="comment ID")
    raw_text = comment.get("comment_text")
    if isinstance(raw_text, str):
        text = raw_text
    else:
        raw_segments = comment.get("comment")
        if not isinstance(raw_segments, list):
            raise APIError("ClickUp response is missing comment text")
        text_segments: list[str] = []
        for segment in raw_segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                raise APIError("ClickUp response contains invalid comment text")
            text_segments.append(str(segment["text"]))
        text = "".join(text_segments)

    user = comment.get("user")
    user_id: str | None = None
    username: str | None = None
    if isinstance(user, dict):
        user_id = _optional_string(user.get("id"))
        username = str(user["username"]) if isinstance(user.get("username"), str) else None
    resolved = comment.get("resolved")
    return {
        "date": _optional_string(comment.get("date")),
        "id": comment_id,
        "resolved": resolved if isinstance(resolved, bool) else None,
        "text": text,
        "user_id": user_id,
        "username": username,
    }


def summarize_comments(payload: JsonObject) -> list[JsonObject]:
    raw_comments = payload.get("comments")
    if not isinstance(raw_comments, list):
        raise APIError("ClickUp response is missing task comments")
    comments: list[JsonObject] = []
    for raw_comment in raw_comments:
        if not isinstance(raw_comment, dict):
            raise APIError("ClickUp response contains an invalid task comment")
        comments.append(summarize_comment(cast(JsonObject, raw_comment)))
    return comments


class TaskService:
    """Orchestrates minimal task writes and verified readbacks."""

    def __init__(self, client: ClickUpClient) -> None:
        self._client = client

    def _context(self, task_id: str) -> tuple[JsonObject, str, list[StatusDefinition]]:
        task = self._client.get_task(task_id)
        current_status = task_status(task)
        statuses = list_statuses(self._client.get_list(task_list_id(task)))
        return task, current_status, statuses

    @staticmethod
    def _labels(statuses: list[StatusDefinition]) -> str:
        return ", ".join(status.label for status in statuses)

    def _apply_status(
        self,
        task_id: str,
        task: JsonObject,
        current_status: str,
        canonical_status: str,
    ) -> MutationResult:
        if current_status.casefold() == canonical_status.casefold():
            return MutationResult(
                task_id=task_id,
                previous_status=current_status,
                status=canonical_status,
                changed=False,
                task=task,
            )

        self._client.update_task_status(task_id, canonical_status)
        readback = self._client.get_task(task_id)
        readback_status = task_status(readback)
        if readback_status != canonical_status:
            raise VerificationError(
                "Task status verification failed: "
                f"expected '{canonical_status}', received '{readback_status}'"
            )
        return MutationResult(
            task_id=task_id,
            previous_status=current_status,
            status=readback_status,
            changed=True,
            task=readback,
        )

    def set_status(self, task_id: str, requested_status: str) -> MutationResult:
        task, current_status, statuses = self._context(task_id)
        requested = requested_status.strip()
        match = next(
            (status for status in statuses if status.label.casefold() == requested.casefold()),
            None,
        )
        if match is None:
            raise InvalidStatusError(
                f"Invalid status '{requested_status}'. Valid statuses: {self._labels(statuses)}"
            )
        return self._apply_status(task_id, task, current_status, match.label)

    def complete(self, task_id: str) -> MutationResult:
        task, current_status, statuses = self._context(task_id)
        candidates = {
            status.label.casefold(): status
            for status in statuses
            if (status.status_type or "").casefold() in _COMPLETION_TYPES
            and status.label.casefold() in _COMPLETION_LABELS
        }
        match = next(
            (candidates[label] for label in _COMPLETION_LABELS if label in candidates), None
        )
        if match is None:
            raise CompletionStatusError(
                "No semantic completion status is available. "
                f"Valid statuses: {self._labels(statuses)}"
            )
        return self._apply_status(task_id, task, current_status, match.label)

    def list_comments(self, task_id: str) -> list[JsonObject]:
        return summarize_comments(self._client.get_task_comments(task_id))

    def add_comment(self, task_id: str, comment_text: str) -> CommentMutationResult:
        if not comment_text.strip():
            raise InvalidOperationError("Comment text cannot be empty")
        created = self._client.create_task_comment(task_id, comment_text)
        comment_id = _required_string(created.get("id"), label="created comment ID")
        comments = self.list_comments(task_id)
        readback = next((comment for comment in comments if comment.get("id") == comment_id), None)
        if readback is None:
            raise VerificationError(
                f"Comment verification failed: created comment {comment_id} was not returned"
            )
        if readback.get("text") != comment_text:
            raise VerificationError(
                f"Comment verification failed: created comment {comment_id} text did not match"
            )
        return CommentMutationResult(task_id=task_id, comment=readback)

    def set_due_date(self, task_id: str, requested: DueDateInput) -> DueDateMutationResult:
        task = self._client.get_task(task_id)
        previous = task_due_date(task)
        same_value = previous.milliseconds == requested.milliseconds
        if not requested.has_time and previous.milliseconds is not None:
            same_value = _utc_date_from_ms(previous.milliseconds) == requested.display
        if same_value and previous.has_time == requested.has_time:
            return DueDateMutationResult(
                task_id=task_id,
                previous_due_date_ms=previous.milliseconds,
                due_date_ms=previous.milliseconds,
                due_date=requested.display,
                due_date_time=requested.has_time,
                changed=False,
                task=task,
            )

        self._client.update_task_due_date(
            task_id,
            requested.milliseconds,
            due_date_time=requested.has_time,
        )
        readback = self._client.get_task(task_id)
        observed = task_due_date(readback)
        value_matches = observed.milliseconds == requested.milliseconds
        if not requested.has_time and observed.milliseconds is not None:
            value_matches = _utc_date_from_ms(observed.milliseconds) == requested.display
        if not value_matches:
            received = (
                _utc_date_from_ms(observed.milliseconds)
                if not requested.has_time and observed.milliseconds is not None
                else observed.milliseconds
            )
            raise VerificationError(
                f"Due date verification failed: expected {requested.display}, received {received}"
            )
        if observed.has_time is not None and observed.has_time != requested.has_time:
            raise VerificationError(
                "Due date verification failed: "
                f"expected due_date_time={requested.has_time}, received {observed.has_time}"
            )
        return DueDateMutationResult(
            task_id=task_id,
            previous_due_date_ms=previous.milliseconds,
            due_date_ms=observed.milliseconds,
            due_date=requested.display,
            due_date_time=requested.has_time,
            changed=True,
            task=readback,
        )

    def clear_due_date(self, task_id: str) -> DueDateMutationResult:
        task = self._client.get_task(task_id)
        previous = task_due_date(task)
        if previous.milliseconds is None:
            return DueDateMutationResult(
                task_id=task_id,
                previous_due_date_ms=None,
                due_date_ms=None,
                due_date=None,
                due_date_time=None,
                changed=False,
                task=task,
            )

        self._client.update_task_due_date(task_id, None)
        readback = self._client.get_task(task_id)
        observed = task_due_date(readback)
        if observed.milliseconds is not None:
            raise VerificationError(
                "Due date verification failed: "
                f"expected no due date, received {observed.milliseconds}"
            )
        return DueDateMutationResult(
            task_id=task_id,
            previous_due_date_ms=previous.milliseconds,
            due_date_ms=None,
            due_date=None,
            due_date_time=None,
            changed=True,
            task=readback,
        )

    def _set_assigned(
        self, task_id: str, user_id: int, *, assigned: bool
    ) -> AssignmentMutationResult:
        if isinstance(user_id, bool) or user_id <= 0:
            raise InvalidOperationError("USER_ID must be a positive integer")
        task = self._client.get_task(task_id)
        previous = task_assignee_ids(task)
        if (user_id in previous) == assigned:
            return AssignmentMutationResult(
                task_id=task_id,
                user_id=user_id,
                assigned=assigned,
                changed=False,
                assignee_ids=previous,
                task=task,
            )

        self._client.update_task_assignees(
            task_id,
            add=[user_id] if assigned else [],
            remove=[] if assigned else [user_id],
        )
        readback = self._client.get_task(task_id)
        assignee_ids = task_assignee_ids(readback)
        if (user_id in assignee_ids) != assigned:
            expected = "assigned" if assigned else "unassigned"
            raise VerificationError(
                f"Assignment verification failed: user {user_id} was not {expected}"
            )
        return AssignmentMutationResult(
            task_id=task_id,
            user_id=user_id,
            assigned=assigned,
            changed=True,
            assignee_ids=assignee_ids,
            task=readback,
        )

    def assign(self, task_id: str, user_id: int) -> AssignmentMutationResult:
        return self._set_assigned(task_id, user_id, assigned=True)

    def unassign(self, task_id: str, user_id: int) -> AssignmentMutationResult:
        return self._set_assigned(task_id, user_id, assigned=False)
