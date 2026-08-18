"""Focused task field, tag, and reversible lifecycle mutations."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from clickup_cli.client import ClickUpClient
from clickup_cli.errors import (
    APIError,
    InvalidOperationError,
    InvalidPriorityError,
    InvalidStartDateError,
    VerificationError,
)
from clickup_cli.types import JsonObject, JsonValue

_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIMED_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PRIORITIES = {"urgent": 1, "high": 2, "normal": 3, "low": 4}
_PRIORITY_NAMES = {value: key for key, value in _PRIORITIES.items()}
_MAX_DESCRIPTION_BYTES = 1024 * 1024


@dataclass(frozen=True)
class StartDateInput:
    milliseconds: int
    display: str
    has_time: bool


@dataclass(frozen=True)
class StartDateState:
    milliseconds: int | None
    has_time: bool | None


@dataclass(frozen=True)
class TaskUpdateRequest:
    name: str | None = None
    description: str | None = None
    description_supplied: bool = False
    priority: int | None = None
    priority_supplied: bool = False
    start_date: StartDateInput | None = None
    clear_start_date: bool = False
    archived: bool | None = None


@dataclass(frozen=True)
class TaskUpdateResult:
    task_id: str
    changed: bool
    fields: list[str]
    task: JsonObject


@dataclass(frozen=True)
class TagMutationResult:
    task_id: str
    tag: str
    added: bool
    changed: bool
    tags: list[str]
    task: JsonObject


def _normalized_timestamp_ms(value: datetime) -> tuple[datetime, int]:
    try:
        normalized = value.astimezone(UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidStartDateError("Start date falls outside the supported UTC range") from exc
    delta = normalized - _EPOCH
    milliseconds = delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000
    return normalized, milliseconds


def parse_start_date(value: str) -> StartDateInput:
    """Parse a date-only value or timezone-aware ISO timestamp as a start date."""

    requested = value.strip()
    if _DATE_ONLY.fullmatch(requested):
        try:
            parsed_date = date.fromisoformat(requested)
        except ValueError as exc:
            raise InvalidStartDateError(
                "Start date must be YYYY-MM-DD or an ISO 8601 timestamp with Z or an offset"
            ) from exc
        parsed = datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
        _, milliseconds = _normalized_timestamp_ms(parsed)
        if milliseconds < 0:
            raise InvalidStartDateError("Start date must not be before 1970-01-01")
        return StartDateInput(milliseconds, parsed_date.isoformat(), False)

    if _TIMED_DATE.fullmatch(requested) is None:
        raise InvalidStartDateError(
            "Start date must be YYYY-MM-DD or an ISO 8601 timestamp with Z or an offset"
        )
    iso_value = f"{requested[:-1]}+00:00" if requested.endswith("Z") else requested
    try:
        parsed_datetime = datetime.fromisoformat(iso_value)
        offset = parsed_datetime.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise InvalidStartDateError("Start date contains an invalid ISO 8601 timestamp") from exc
    if offset is None:
        raise InvalidStartDateError("Timed start dates require Z or an explicit UTC offset")
    if parsed_datetime.microsecond % 1_000:
        raise InvalidStartDateError("Timed start dates support at most millisecond precision")
    normalized, milliseconds = _normalized_timestamp_ms(parsed_datetime)
    if milliseconds < 0:
        raise InvalidStartDateError("Start date must not be before 1970-01-01")
    timespec = "milliseconds" if normalized.microsecond else "seconds"
    display = normalized.isoformat(timespec=timespec).replace("+00:00", "Z")
    return StartDateInput(milliseconds, display, True)


def _timestamp_from_ms(milliseconds: int, *, has_time: bool | None) -> str:
    try:
        parsed = _EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError as exc:
        raise APIError("ClickUp response contains an out-of-range start date") from exc
    if has_time is False:
        return parsed.date().isoformat()
    timespec = "milliseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def task_start_date(task: JsonObject) -> StartDateState:
    raw_start_date = task.get("start_date")
    raw_has_time = task.get("start_date_time")
    if raw_has_time is not None and not isinstance(raw_has_time, bool):
        raise APIError("ClickUp response contains an invalid start-date time flag")
    has_time = raw_has_time if isinstance(raw_has_time, bool) else None
    if raw_start_date is None:
        return StartDateState(None, has_time)
    if isinstance(raw_start_date, bool) or not isinstance(raw_start_date, (str, int)):
        raise APIError("ClickUp response contains an invalid start date")
    try:
        milliseconds = int(raw_start_date)
    except ValueError as exc:
        raise APIError("ClickUp response contains an invalid start date") from exc
    if milliseconds < 0 or str(milliseconds) != str(raw_start_date):
        raise APIError("ClickUp response contains an invalid start date")
    return StartDateState(milliseconds, has_time)


def start_date_display(state: StartDateState) -> str | None:
    if state.milliseconds is None:
        return None
    return _timestamp_from_ms(state.milliseconds, has_time=state.has_time)


def parse_priority(value: str) -> int | None:
    requested = value.strip().casefold()
    if requested == "clear":
        return None
    priority = _PRIORITIES.get(requested)
    if priority is None:
        raise InvalidPriorityError("Priority must be urgent, high, normal, low, or clear")
    return priority


def task_priority(task: JsonObject) -> int | None:
    raw_priority = task.get("priority")
    if raw_priority is None:
        return None
    if not isinstance(raw_priority, dict):
        raise APIError("ClickUp response contains an invalid task priority")
    raw_id = raw_priority.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        raise APIError("ClickUp response contains an invalid task priority ID")
    try:
        priority = int(raw_id)
    except ValueError as exc:
        raise APIError("ClickUp response contains an invalid task priority ID") from exc
    if priority not in _PRIORITY_NAMES or str(priority) != str(raw_id):
        raise APIError("ClickUp response contains an invalid task priority ID")
    return priority


def priority_display(task: JsonObject) -> str | None:
    priority = task_priority(task)
    if priority is None:
        return None
    return _PRIORITY_NAMES[priority]


def task_archived(task: JsonObject) -> bool:
    value = task.get("archived")
    if not isinstance(value, bool):
        raise APIError("ClickUp response is missing a valid archived state")
    return value


def read_description_file(path: Path) -> str:
    """Read a bounded UTF-8 description from a regular local file."""

    try:
        metadata = path.stat()
    except OSError as exc:
        raise InvalidOperationError(f"Could not access description file: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise InvalidOperationError(f"Description path is not a regular file: {path}")
    if metadata.st_size > _MAX_DESCRIPTION_BYTES:
        raise InvalidOperationError("Description file exceeds the 1 MiB safety limit")
    try:
        with path.open("rb") as handle:
            raw_content = handle.read(_MAX_DESCRIPTION_BYTES + 1)
    except OSError as exc:
        raise InvalidOperationError(f"Could not read description file: {path}") from exc
    if len(raw_content) > _MAX_DESCRIPTION_BYTES:
        raise InvalidOperationError("Description file exceeds the 1 MiB safety limit")
    try:
        return raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidOperationError("Description file must contain valid UTF-8") from exc


def logical_task_description(value: JsonValue | None) -> str | None:
    """Normalize ClickUp's single-space clear representation to logical empty text."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise APIError("ClickUp response contains an invalid task description")
    return "" if value in {"", " "} else value


def _task_tags(task: JsonObject) -> list[str]:
    raw_tags = task.get("tags")
    if not isinstance(raw_tags, list):
        raise APIError("ClickUp response is missing task tags")
    tags: list[str] = []
    for raw_tag in raw_tags:
        if isinstance(raw_tag, str):
            tag = raw_tag
        elif isinstance(raw_tag, dict) and isinstance(raw_tag.get("name"), str):
            tag = str(raw_tag["name"])
        else:
            raise APIError("ClickUp response contains an invalid task tag")
        if not tag:
            raise APIError("ClickUp response contains an empty task tag")
        tags.append(tag)
    return tags


def _date_value_matches(state: StartDateState, requested: StartDateInput) -> bool:
    same_value = state.milliseconds == requested.milliseconds
    if not requested.has_time and state.milliseconds is not None:
        same_value = _timestamp_from_ms(state.milliseconds, has_time=False) == requested.display
    return same_value


def _same_date(state: StartDateState, requested: StartDateInput) -> bool:
    return _date_value_matches(state, requested) and state.has_time == requested.has_time


class TaskMutationService:
    """Apply one minimal task update and verify it through one separate readback."""

    def __init__(self, client: ClickUpClient) -> None:
        self._client = client

    @staticmethod
    def _validate_request(request: TaskUpdateRequest) -> None:
        if request.name is not None and not request.name.strip():
            raise InvalidOperationError("Task name cannot be empty")
        if request.description_supplied and request.description is None:
            raise InvalidOperationError("Task description must be text")
        if request.start_date is not None and request.clear_start_date:
            raise InvalidOperationError("Cannot set and clear the start date together")
        if request.priority_supplied and request.priority not in {None, 1, 2, 3, 4}:
            raise InvalidPriorityError("Priority must be urgent, high, normal, low, or clear")
        if not any(
            (
                request.name is not None,
                request.description_supplied,
                request.priority_supplied,
                request.start_date is not None,
                request.clear_start_date,
                request.archived is not None,
            )
        ):
            raise InvalidOperationError("Task update requires at least one field")

    @staticmethod
    def _changed_fields(task: JsonObject, request: TaskUpdateRequest) -> JsonObject:
        fields: JsonObject = {}
        if request.name is not None:
            current_name = task.get("name")
            if not isinstance(current_name, str):
                raise APIError("ClickUp response is missing task name")
            if current_name != request.name:
                fields["name"] = request.name
        if request.description_supplied:
            current_description = logical_task_description(task.get("description"))
            if current_description != request.description:
                fields["description"] = request.description
        if request.priority_supplied and task_priority(task) != request.priority:
            fields["priority"] = request.priority
        if request.start_date is not None:
            if not _same_date(task_start_date(task), request.start_date):
                fields["start_date"] = request.start_date.milliseconds
                fields["start_date_time"] = request.start_date.has_time
        elif request.clear_start_date and task_start_date(task).milliseconds is not None:
            fields["start_date"] = None
        if request.archived is not None and task_archived(task) != request.archived:
            fields["archived"] = request.archived
        return fields

    @staticmethod
    def _verify(readback: JsonObject, request: TaskUpdateRequest, fields: JsonObject) -> None:
        if "name" in fields and readback.get("name") != request.name:
            raise VerificationError("Task name verification failed")
        if (
            "description" in fields
            and logical_task_description(readback.get("description")) != request.description
        ):
            raise VerificationError("Task description verification failed")
        if "priority" in fields and task_priority(readback) != request.priority:
            raise VerificationError("Task priority verification failed")
        if "start_date" in fields:
            observed = task_start_date(readback)
            if request.clear_start_date:
                if observed.milliseconds is not None:
                    raise VerificationError("Start date verification failed: date was not cleared")
            elif request.start_date is not None and not _same_date(observed, request.start_date):
                if not _date_value_matches(observed, request.start_date):
                    received = start_date_display(observed)
                    raise VerificationError(
                        "Start date verification failed: expected "
                        f"{request.start_date.display}, received {received}"
                    )
                if (
                    observed.has_time is not None
                    and observed.has_time != request.start_date.has_time
                ):
                    raise VerificationError(
                        "Start date verification failed: expected start_date_time="
                        f"{request.start_date.has_time}, received {observed.has_time}"
                    )
        if "archived" in fields and task_archived(readback) != request.archived:
            expected = "archived" if request.archived else "unarchived"
            raise VerificationError(f"Task lifecycle verification failed: task was not {expected}")

    def update(self, task_id: str, request: TaskUpdateRequest) -> TaskUpdateResult:
        self._validate_request(request)
        task = self._client.get_task(task_id)
        fields = self._changed_fields(task, request)
        if not fields:
            return TaskUpdateResult(task_id, False, [], task)
        self._client.update_task(task_id, fields)
        readback = self._client.get_task(task_id)
        self._verify(readback, request, fields)
        return TaskUpdateResult(task_id, True, sorted(fields), readback)

    def set_archived(self, task_id: str, *, archived: bool) -> TaskUpdateResult:
        return self.update(task_id, TaskUpdateRequest(archived=archived))

    def clear_priority(self, task_id: str) -> TaskUpdateResult:
        return self.update(
            task_id,
            TaskUpdateRequest(priority=None, priority_supplied=True),
        )

    def clear_start_date(self, task_id: str) -> TaskUpdateResult:
        return self.update(task_id, TaskUpdateRequest(clear_start_date=True))

    def set_tag(self, task_id: str, tag: str, *, add: bool) -> TagMutationResult:
        requested = tag.strip()
        if not requested:
            raise InvalidOperationError("Tag name cannot be empty")
        task = self._client.get_task(task_id)
        previous = _task_tags(task)
        match = next(
            (existing for existing in previous if existing.casefold() == requested.casefold()),
            None,
        )
        if (match is not None) == add:
            return TagMutationResult(task_id, match or requested, add, False, previous, task)

        wire_tag = requested if add else match
        if wire_tag is None:
            raise RuntimeError("Tag mutation state was inconsistent")
        self._client.update_task_tag(task_id, wire_tag, add=add)
        readback = self._client.get_task(task_id)
        observed = _task_tags(readback)
        observed_match = next(
            (existing for existing in observed if existing.casefold() == requested.casefold()),
            None,
        )
        if (observed_match is not None) != add:
            action = "added" if add else "removed"
            raise VerificationError(f"Tag verification failed: {requested!r} was not {action}")
        return TagMutationResult(
            task_id,
            observed_match or wire_tag,
            add,
            True,
            observed,
            readback,
        )
