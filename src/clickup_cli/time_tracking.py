"""Strict parsing, normalization, and verified ClickUp time-entry operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

from clickup_cli.client import ClickUpClient
from clickup_cli.errors import (
    APIError,
    ClickUpCLIError,
    CreatedButUnidentifiedError,
    CreatedButUnverifiedError,
    InvalidDurationError,
    InvalidOperationError,
    InvalidTimeRangeError,
    OutcomeUnknownError,
    TransportError,
    VerificationError,
)
from clickup_cli.types import JsonObject, JsonValue

_DATE_ONLY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
_DURATION = re.compile(r"(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_RANGE_MS = 366 * 86_400_000
_MAX_DURATION_MS = 2_147_483_647  # OpenAPI declares signed int32 milliseconds.
_MAX_TIMESTAMP_MS = (datetime.max.replace(tzinfo=UTC) - _EPOCH).days * 86_400_000 + 86_399_999
_CREATE_SEARCH_PADDING_MS = 1_000


@dataclass(frozen=True)
class TimeBoundary:
    milliseconds: int
    display: str
    date_only: bool


@dataclass(frozen=True)
class TimeRange:
    """A start-inclusive, end-exclusive interval sent directly to ClickUp."""

    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class DurationInput:
    milliseconds: int
    display: str


@dataclass(frozen=True)
class TimeListResult:
    workspace_id: str
    start_ms: int
    end_ms: int
    entries: list[JsonObject]


@dataclass(frozen=True)
class TimeMutationResult:
    changed: bool
    entry: JsonObject


@dataclass(frozen=True)
class StopTimeResult:
    stopped: bool
    entry_id: str | None
    entry: JsonObject | None


@dataclass(frozen=True)
class TimeDeleteResult:
    changed: bool
    deleted: bool
    entry_id: str
    entry: JsonObject | None


def _timestamp_ms(value: datetime, *, label: str) -> tuple[datetime, int]:
    try:
        normalized = value.astimezone(UTC)
        delta = normalized - _EPOCH
    except (OverflowError, OSError, ValueError) as exc:
        raise InvalidTimeRangeError(f"{label} is outside the supported UTC range") from exc
    milliseconds = delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000
    if milliseconds < 0:
        raise InvalidTimeRangeError(f"{label} must not be before 1970-01-01")
    return normalized, milliseconds


def parse_time_boundary(value: str, *, label: str, allow_date: bool = True) -> TimeBoundary:
    """Parse a strict date or timezone-aware ISO timestamp to Unix milliseconds."""

    requested = value.strip()
    if allow_date and _DATE_ONLY.fullmatch(requested):
        try:
            parsed_date = date.fromisoformat(requested)
        except ValueError as exc:
            raise InvalidTimeRangeError(f"{label} contains an invalid calendar date") from exc
        parsed = datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
        _, milliseconds = _timestamp_ms(parsed, label=label)
        return TimeBoundary(milliseconds, parsed_date.isoformat(), True)

    if _TIMESTAMP.fullmatch(requested) is None:
        accepted = (
            "YYYY-MM-DD or a timezone-aware ISO 8601 timestamp"
            if allow_date
            else "a timezone-aware ISO 8601 timestamp"
        )
        raise InvalidTimeRangeError(f"{label} must be {accepted}")
    iso_value = f"{requested[:-1]}+00:00" if requested.endswith("Z") else requested
    try:
        parsed_datetime = datetime.fromisoformat(iso_value)
        offset = parsed_datetime.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise InvalidTimeRangeError(f"{label} contains an invalid ISO 8601 timestamp") from exc
    if offset is None:
        raise InvalidTimeRangeError(f"{label} requires Z or an explicit UTC offset")
    if parsed_datetime.microsecond % 1_000:
        raise InvalidTimeRangeError(f"{label} supports at most millisecond precision")
    normalized, milliseconds = _timestamp_ms(parsed_datetime, label=label)
    timespec = "milliseconds" if normalized.microsecond else "seconds"
    display = normalized.isoformat(timespec=timespec).replace("+00:00", "Z")
    return TimeBoundary(milliseconds, display, False)


def parse_time_range(from_value: str, to_value: str) -> TimeRange:
    """Return a bounded [from, to) range; date values mean midnight UTC boundaries."""

    start = parse_time_boundary(from_value, label="FROM")
    end = parse_time_boundary(to_value, label="TO")
    if end.milliseconds <= start.milliseconds:
        raise InvalidTimeRangeError("TO must be later than FROM")
    if end.milliseconds - start.milliseconds > _MAX_RANGE_MS:
        raise InvalidTimeRangeError("Time-entry ranges cannot exceed 366 days")
    return TimeRange(start_ms=start.milliseconds, end_ms=end.milliseconds)


def parse_duration(value: str) -> DurationInput:
    """Parse ordered whole hours/minutes/seconds and return exact milliseconds."""

    requested = value.strip()
    match = _DURATION.fullmatch(requested)
    if match is None or all(component is None for component in match.groups()):
        raise InvalidDurationError("DURATION must use whole units such as 45m, 1h30m, or 90s")
    hours, minutes, seconds = (int(component or 0) for component in match.groups())
    milliseconds = ((hours * 60 + minutes) * 60 + seconds) * 1_000
    if milliseconds <= 0:
        raise InvalidDurationError("DURATION must be greater than zero")
    if milliseconds > _MAX_DURATION_MS:
        raise InvalidDurationError("DURATION exceeds ClickUp's signed int32 millisecond limit")
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    return DurationInput(milliseconds=milliseconds, display="".join(parts))


def _optional_string(value: JsonValue | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise APIError(f"ClickUp response contains an invalid {label}")
    return str(value)


def _required_string(value: JsonValue | None, *, label: str) -> str:
    normalized = _optional_string(value, label=label)
    if normalized is None or not normalized:
        raise APIError(f"ClickUp response is missing {label}")
    return normalized


def _optional_text(value: JsonValue | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise APIError(f"ClickUp response contains an invalid {label}")
    return value


def _milliseconds(
    value: JsonValue | None, *, label: str, allow_negative: bool = False
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise APIError(f"ClickUp response contains an invalid {label}")
    raw = str(value)
    if not re.fullmatch(r"-?\d+", raw):
        raise APIError(f"ClickUp response contains an invalid {label}")
    parsed = int(raw)
    if str(parsed) != raw or (parsed < 0 and not allow_negative):
        raise APIError(f"ClickUp response contains an invalid {label}")
    return parsed


def _iso_from_ms(milliseconds: int, *, label: str) -> str:
    try:
        parsed = _EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError as exc:
        raise APIError(f"ClickUp response contains an out-of-range {label}") from exc
    timespec = "milliseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _normalized_tags(value: JsonValue | None) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise APIError("ClickUp response contains invalid time-entry tags")
    tags: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            name = str(item["name"])
        else:
            raise APIError("ClickUp response contains an invalid time-entry tag")
        if not name:
            raise APIError("ClickUp response contains an empty time-entry tag")
        tags.append(name)
    return tags


def normalize_time_entry(entry: JsonObject, *, force_running: bool = False) -> JsonObject:
    """Normalize API variants into one stable machine-readable time-entry shape."""

    entry_id = _required_string(entry.get("id"), label="time-entry ID")

    task_id: str | None = None
    task_name: str | None = None
    raw_task = entry.get("task")
    if raw_task is not None:
        if not isinstance(raw_task, dict):
            raise APIError("ClickUp response contains invalid time-entry task data")
        task_id = _optional_string(raw_task.get("id"), label="time-entry task ID")
        task_name = _optional_text(raw_task.get("name"), label="time-entry task name")
    if task_id is None:
        task_id = _optional_string(entry.get("tid"), label="time-entry task ID")

    raw_user = entry.get("user")
    user: JsonValue = None
    if raw_user is not None:
        if not isinstance(raw_user, dict):
            raise APIError("ClickUp response contains invalid time-entry user data")
        user = {
            "email": _optional_text(raw_user.get("email"), label="time-entry user email"),
            "id": _optional_string(raw_user.get("id"), label="time-entry user ID"),
            "username": _optional_text(raw_user.get("username"), label="time-entry username"),
        }

    raw_description = entry.get("description")
    if raw_description is not None and not isinstance(raw_description, str):
        raise APIError("ClickUp response contains an invalid time-entry description")
    raw_billable = entry.get("billable")
    if raw_billable is not None and not isinstance(raw_billable, bool):
        raise APIError("ClickUp response contains an invalid time-entry billable state")
    raw_source = entry.get("source")
    if raw_source is not None and not isinstance(raw_source, str):
        raise APIError("ClickUp response contains an invalid time-entry source")

    start_ms = _milliseconds(entry.get("start"), label="time-entry start")
    end_ms = _milliseconds(entry.get("end"), label="time-entry end")
    duration_ms = _milliseconds(
        entry.get("duration"), label="time-entry duration", allow_negative=True
    )
    running = force_running or (duration_ms is not None and duration_ms < 0)
    return {
        "billable": raw_billable if isinstance(raw_billable, bool) else None,
        "description": raw_description if isinstance(raw_description, str) else None,
        "duration_ms": duration_ms,
        "end": _iso_from_ms(end_ms, label="time-entry end") if end_ms is not None else None,
        "end_ms": end_ms,
        "id": entry_id,
        "running": running,
        "source": raw_source if isinstance(raw_source, str) else None,
        "start": (
            _iso_from_ms(start_ms, label="time-entry start") if start_ms is not None else None
        ),
        "start_ms": start_ms,
        "tags": cast(JsonValue, _normalized_tags(entry.get("tags"))),
        "task_id": task_id,
        "task_name": task_name,
        "user": user,
        "workspace_id": _optional_string(entry.get("wid"), label="time-entry workspace ID"),
    }


def _data_object(payload: JsonObject, *, label: str) -> JsonObject:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise APIError(f"ClickUp response is missing {label}")
    return cast(JsonObject, data)


def _current_data(payload: JsonObject) -> JsonObject | None:
    data = payload.get("data")
    if data is None or data == {}:
        return None
    if not isinstance(data, dict):
        raise APIError("ClickUp response contains invalid current timer data")
    return cast(JsonObject, data)


def _created_id(payload: JsonObject) -> str | None:
    direct = payload.get("id")
    if isinstance(direct, (str, int)) and not isinstance(direct, bool) and str(direct):
        return str(direct)
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("id")
        if isinstance(nested, (str, int)) and not isinstance(nested, bool) and str(nested):
            return str(nested)
    return None


def _ambiguous_api_error(error: APIError) -> bool:
    status = error.status_code
    return status is None or status == 408 or status >= 500 or 200 <= status < 300


def _complete_update_tag(value: JsonValue) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    foreground = value.get("tag_fg")
    background = value.get("tag_bg")
    if not all(isinstance(item, str) and item for item in (name, foreground, background)):
        return None
    return {"name": str(name), "tag_bg": str(background), "tag_fg": str(foreground)}


def _update_tag_names(entry: JsonObject) -> tuple[list[str], list[JsonValue]]:
    raw_tags = entry.get("tags")
    if not isinstance(raw_tags, list):
        raise InvalidOperationError(
            "Cannot update this entry because its required tags array was not returned"
        )
    names: list[str] = []
    for raw_tag in raw_tags:
        if isinstance(raw_tag, str):
            name = raw_tag
        elif isinstance(raw_tag, dict) and isinstance(raw_tag.get("name"), str):
            name = str(raw_tag["name"])
        else:
            raise InvalidOperationError(
                "Cannot safely preserve this entry's tags because a tag name is invalid"
            )
        if not name:
            raise InvalidOperationError(
                "Cannot safely preserve this entry's tags because a tag name is empty"
            )
        names.append(name)
    return names, cast(list[JsonValue], raw_tags)


def _catalog_update_tags(
    payload: JsonObject,
    names: list[str],
    *,
    workspace_id: str,
) -> list[JsonValue]:
    raw_catalog = payload.get("data")
    if not isinstance(raw_catalog, list):
        raise InvalidOperationError(
            "Cannot update this entry because the Workspace time-tag catalog is invalid",
            details={"workspace_id": workspace_id},
        )
    catalog: list[JsonObject] = []
    for raw_tag in raw_catalog:
        complete = _complete_update_tag(raw_tag)
        if complete is None:
            raise InvalidOperationError(
                "Cannot update this entry because the Workspace time-tag catalog contains "
                "invalid metadata",
                details={"workspace_id": workspace_id},
            )
        catalog.append(complete)

    tags: list[JsonValue] = []
    for name in names:
        matches = [
            tag
            for tag in catalog
            if isinstance(tag.get("name"), str) and str(tag["name"]).casefold() == name.casefold()
        ]
        if len(matches) != 1:
            qualifier = "missing" if not matches else "ambiguous"
            raise InvalidOperationError(
                f"Cannot update this entry because time-tag metadata for {name!r} is {qualifier}",
                details={"tag": name, "workspace_id": workspace_id},
            )
        tags.append(matches[0])
    return tags


def _creation_response_fields(payload: JsonObject) -> JsonObject:
    data = payload.get("data")
    if isinstance(data, dict) and any(
        field in data
        for field in ("assignee", "billable", "description", "duration", "start", "tid")
    ):
        return cast(JsonObject, data)
    return payload


def _created_entry_matches(
    entry: JsonObject,
    response: JsonObject,
    *,
    start_ms: int,
    duration_ms: int,
    task_id: str | None,
    description: str | None,
    billable: bool | None,
) -> bool:
    if entry.get("start_ms") != start_ms or entry.get("duration_ms") != duration_ms:
        return False

    fields = _creation_response_fields(response)
    response_task = fields.get("tid")
    evidence_task = (
        str(response_task)
        if isinstance(response_task, (str, int))
        and not isinstance(response_task, bool)
        and str(response_task)
        else None
    )
    expected_task = task_id if task_id is not None else evidence_task
    if entry.get("task_id") != expected_task:
        return False

    response_description = fields.get("description")
    expected_descriptions: set[str | None]
    if description is not None:
        expected_descriptions = {description}
    elif isinstance(response_description, str):
        expected_descriptions = {response_description}
    else:
        expected_descriptions = {None, ""}
    if entry.get("description") not in expected_descriptions:
        return False

    response_billable = fields.get("billable")
    expected_billable = billable
    if expected_billable is None and isinstance(response_billable, bool):
        expected_billable = response_billable
    if expected_billable is not None and entry.get("billable") is not expected_billable:
        return False

    response_assignee = fields.get("assignee")
    if (
        isinstance(response_assignee, (str, int))
        and not isinstance(response_assignee, bool)
        and str(response_assignee)
    ):
        user = entry.get("user")
        if not isinstance(user, dict) or user.get("id") != str(response_assignee):
            return False
    return True


class TimeTrackingService:
    """Orchestrate exact time-entry requests and verify every supported mutation."""

    def __init__(self, client: ClickUpClient) -> None:
        self._client = client

    def _created_entry_id(
        self,
        workspace_id: str,
        response: JsonObject,
        *,
        start: TimeBoundary,
        duration: DurationInput,
        task_id: str | None,
        description: str | None,
        billable: bool | None,
    ) -> str:
        direct_id = _created_id(response)
        if direct_id is not None:
            return direct_id

        search_start = max(0, start.milliseconds - _CREATE_SEARCH_PADDING_MS)
        search_end = min(
            _MAX_TIMESTAMP_MS,
            start.milliseconds + duration.milliseconds + _CREATE_SEARCH_PADDING_MS,
        )
        details: dict[str, JsonValue] = {
            "candidate_ids": [],
            "retry_safe": False,
            "start_ms": start.milliseconds,
            "workspace_id": workspace_id,
        }
        try:
            result = self.list_entries(
                workspace_id,
                TimeRange(start_ms=search_start, end_ms=search_end),
                task_id=task_id,
            )
            matches = [
                entry
                for entry in result.entries
                if _created_entry_matches(
                    entry,
                    response,
                    start_ms=start.milliseconds,
                    duration_ms=duration.milliseconds,
                    task_id=task_id,
                    description=description,
                    billable=billable,
                )
            ]
        except ClickUpCLIError as exc:
            raise CreatedButUnidentifiedError(
                "ClickUp confirmed time-entry creation, but the native ID search failed. "
                "DO NOT RETRY this creation because retrying can create a duplicate; inspect "
                f"Workspace {workspace_id} around start {start.milliseconds}: {exc}",
                details=details,
            ) from exc

        candidate_ids = [str(entry["id"]) for entry in matches]
        details["candidate_ids"] = cast(list[JsonValue], candidate_ids)
        details["match_count"] = len(candidate_ids)
        if len(candidate_ids) != 1:
            state = "could not be identified" if not candidate_ids else "is ambiguous"
            raise CreatedButUnidentifiedError(
                f"ClickUp confirmed time-entry creation, but the native ID {state}. "
                "DO NOT RETRY this creation because retrying can create a duplicate; inspect "
                f"Workspace {workspace_id} around start {start.milliseconds}",
                details=details,
            )
        return candidate_ids[0]

    def _update_tags(self, workspace_id: str, entry: JsonObject) -> list[JsonValue]:
        names, raw_tags = _update_tag_names(entry)
        if not raw_tags:
            return []
        complete = [_complete_update_tag(raw_tag) for raw_tag in raw_tags]
        if all(tag is not None for tag in complete):
            return cast(list[JsonValue], complete)
        catalog = self._client.get_time_entry_tags(workspace_id)
        return _catalog_update_tags(catalog, names, workspace_id=workspace_id)

    def current(self, workspace_id: str, *, assignee: int | None = None) -> JsonObject | None:
        raw = _current_data(self._client.get_current_time_entry(workspace_id, assignee=assignee))
        return normalize_time_entry(raw, force_running=True) if raw is not None else None

    def list_entries(
        self,
        workspace_id: str,
        requested_range: TimeRange,
        *,
        assignee: int | None = None,
        task_id: str | None = None,
        space_id: str | None = None,
        folder_id: str | None = None,
        list_id: str | None = None,
        billable: bool | None = None,
    ) -> TimeListResult:
        payload = self._client.get_time_entries(
            workspace_id,
            start_ms=requested_range.start_ms,
            end_ms=requested_range.end_ms,
            assignee=assignee,
            task_id=task_id,
            space_id=space_id,
            folder_id=folder_id,
            list_id=list_id,
            billable=billable,
        )
        raw_entries = payload.get("data")
        if not isinstance(raw_entries, list):
            raise APIError("ClickUp response is missing time-entry list data")
        entries: list[JsonObject] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise APIError("ClickUp response contains an invalid time-entry list item")
            entries.append(normalize_time_entry(cast(JsonObject, raw_entry)))
        return TimeListResult(
            workspace_id=workspace_id,
            start_ms=requested_range.start_ms,
            end_ms=requested_range.end_ms,
            entries=entries,
        )

    def start(
        self,
        workspace_id: str,
        *,
        task_id: str | None = None,
        description: str | None = None,
        billable: bool | None = None,
    ) -> TimeMutationResult:
        current = self.current(workspace_id)
        if current is not None:
            raise InvalidOperationError(
                f"Refusing to start another timer while {current['id']} is running",
                details={"entry_id": str(current["id"])},
            )
        try:
            response = self._client.start_time_entry(
                workspace_id,
                task_id=task_id,
                description=description,
                billable=billable,
            )
            entry_id = _required_string(
                _data_object(response, label="started time-entry data").get("id"),
                label="started time-entry ID",
            )
        except TransportError as exc:
            raise OutcomeUnknownError(
                "Timer start outcome is unknown because ClickUp did not return a usable response; "
                "check the current timer before retrying",
                details={"workspace_id": workspace_id},
            ) from exc
        except APIError as exc:
            if _ambiguous_api_error(exc):
                raise OutcomeUnknownError(
                    "Timer start outcome is unknown because ClickUp did not return a usable "
                    "entry ID; check the current timer before retrying",
                    details={"workspace_id": workspace_id},
                ) from exc
            raise

        try:
            verified = self.current(workspace_id)
            if verified is None:
                raise VerificationError("ClickUp did not return the started timer as current")
            if verified.get("id") != entry_id:
                raise VerificationError(
                    f"Started timer verification expected ID {entry_id}, received "
                    f"{verified.get('id')}"
                )
            if task_id is not None and verified.get("task_id") != task_id:
                raise VerificationError("Started timer task verification failed")
            if description is not None and verified.get("description") != description:
                raise VerificationError("Started timer description verification failed")
            if billable is not None and verified.get("billable") is not billable:
                raise VerificationError("Started timer billable verification failed")
        except ClickUpCLIError as exc:
            raise CreatedButUnverifiedError(
                "Timer was started but final verification failed; inspect the current timer "
                "before retrying: " + str(exc),
                details={"entry_id": entry_id},
            ) from exc
        return TimeMutationResult(changed=True, entry=verified)

    def stop(self, workspace_id: str) -> StopTimeResult:
        current = self.current(workspace_id)
        if current is None:
            return StopTimeResult(stopped=False, entry_id=None, entry=None)
        entry_id = str(current["id"])

        try:
            response = self._client.stop_time_entry(workspace_id)
        except TransportError as exc:
            raise OutcomeUnknownError(
                "Timer stop outcome is unknown; inspect the current timer before retrying",
                details={"stopped_entry_id": entry_id},
            ) from exc
        except APIError as exc:
            exc.details.setdefault("stopped_entry_id", entry_id)
            if _ambiguous_api_error(exc):
                raise OutcomeUnknownError(
                    "Timer stop outcome is unknown; inspect the current timer before retrying",
                    details={"stopped_entry_id": entry_id},
                ) from exc
            raise

        response_error: ClickUpCLIError | None = None
        stopped_entry: JsonObject | None = None
        try:
            stopped_entry = normalize_time_entry(
                _data_object(response, label="stopped time-entry data")
            )
            if stopped_entry.get("id") != entry_id:
                raise VerificationError(
                    f"Stop response expected entry {entry_id}, received {stopped_entry.get('id')}"
                )
        except ClickUpCLIError as exc:
            response_error = exc

        try:
            after = self.current(workspace_id)
        except ClickUpCLIError as exc:
            raise VerificationError(
                "Timer stop readback failed: " + str(exc),
                details={"stopped_entry_id": entry_id},
            ) from exc
        if after is not None and after.get("id") == entry_id:
            raise VerificationError(
                f"Timer stop verification failed: entry {entry_id} is still current",
                details={"stopped_entry_id": entry_id},
            )
        if response_error is not None:
            raise VerificationError(
                f"Timer {entry_id} stopped, but the stop response was invalid: {response_error}",
                details={"stopped_entry_id": entry_id},
            ) from response_error
        return StopTimeResult(stopped=True, entry_id=entry_id, entry=stopped_entry)

    def add(
        self,
        workspace_id: str,
        *,
        start: TimeBoundary,
        duration: DurationInput,
        task_id: str | None = None,
        description: str | None = None,
        billable: bool | None = None,
    ) -> TimeMutationResult:
        try:
            response = self._client.create_time_entry(
                workspace_id,
                start_ms=start.milliseconds,
                duration_ms=duration.milliseconds,
                task_id=task_id,
                description=description,
                billable=billable,
            )
            entry_id = self._created_entry_id(
                workspace_id,
                response,
                start=start,
                duration=duration,
                task_id=task_id,
                description=description,
                billable=billable,
            )
        except TransportError as exc:
            raise OutcomeUnknownError(
                "Time-entry creation outcome is unknown because ClickUp did not return an ID; "
                "inspect the requested interval before retrying",
                details={"start_ms": start.milliseconds, "workspace_id": workspace_id},
            ) from exc
        except APIError as exc:
            if _ambiguous_api_error(exc):
                raise OutcomeUnknownError(
                    "Time-entry creation outcome is unknown because ClickUp did not return a "
                    "usable ID; inspect the requested interval before retrying",
                    details={"start_ms": start.milliseconds, "workspace_id": workspace_id},
                ) from exc
            raise

        try:
            raw = _data_object(
                self._client.get_time_entry(workspace_id, entry_id),
                label="time-entry readback data",
            )
            entry = normalize_time_entry(raw)
            if entry.get("id") != entry_id:
                raise VerificationError("Created time-entry ID verification failed")
            if entry.get("start_ms") != start.milliseconds:
                raise VerificationError("Created time-entry start verification failed")
            if entry.get("duration_ms") != duration.milliseconds:
                raise VerificationError("Created time-entry duration verification failed")
            if task_id is not None and entry.get("task_id") != task_id:
                raise VerificationError("Created time-entry task verification failed")
            if description is not None and entry.get("description") != description:
                raise VerificationError("Created time-entry description verification failed")
            if billable is not None and entry.get("billable") is not billable:
                raise VerificationError("Created time-entry billable verification failed")
        except ClickUpCLIError as exc:
            raise CreatedButUnverifiedError(
                "Time entry was created but final verification failed; inspect it before "
                "retrying: " + str(exc),
                details={"entry_id": entry_id},
            ) from exc
        return TimeMutationResult(changed=True, entry=entry)

    def update(
        self,
        workspace_id: str,
        entry_id: str,
        *,
        description: str | None = None,
        task_id: str | None = None,
        start: TimeBoundary | None = None,
        duration: DurationInput | None = None,
        billable: bool | None = None,
    ) -> TimeMutationResult:
        if all(value is None for value in (description, task_id, start, duration, billable)):
            raise InvalidOperationError("At least one time-entry field must be provided")

        raw = _data_object(
            self._client.get_time_entry(workspace_id, entry_id),
            label="time-entry readback data",
        )
        current = normalize_time_entry(raw)
        if current.get("id") != entry_id:
            raise APIError(
                f"Time-entry readback expected ID {entry_id}, received {current.get('id')}"
            )
        changes = {
            "description": description is not None and current.get("description") != description,
            "task": task_id is not None and current.get("task_id") != task_id,
            "start": start is not None and current.get("start_ms") != start.milliseconds,
            "duration": (
                duration is not None and current.get("duration_ms") != duration.milliseconds
            ),
            "billable": billable is not None and current.get("billable") is not billable,
        }
        if not any(changes.values()):
            return TimeMutationResult(changed=False, entry=current)

        if current.get("running") and (changes["start"] or changes["duration"]):
            raise InvalidOperationError("Cannot safely change timing fields on a running entry")

        body: JsonObject = {"tags": self._update_tags(workspace_id, raw)}
        if changes["description"]:
            body["description"] = description
        if changes["task"]:
            body["tid"] = task_id
        if changes["billable"]:
            body["billable"] = billable

        expected_end_ms: int | None = None
        expected_duration_ms: int | None = None
        if changes["start"]:
            assert start is not None
            desired_duration = (
                duration.milliseconds if duration is not None else current.get("duration_ms")
            )
            if (
                not isinstance(desired_duration, int)
                or isinstance(desired_duration, bool)
                or desired_duration <= 0
            ):
                raise InvalidOperationError(
                    "Cannot change start because the existing positive duration is unavailable"
                )
            end_ms = start.milliseconds + desired_duration
            if end_ms > _MAX_TIMESTAMP_MS:
                raise InvalidOperationError("Updated time entry would exceed the supported range")
            body["start"] = start.milliseconds
            body["end"] = end_ms
            expected_end_ms = end_ms
            expected_duration_ms = desired_duration
        elif changes["duration"]:
            assert duration is not None
            body["duration"] = duration.milliseconds

        try:
            self._client.update_time_entry(workspace_id, entry_id, body=body)
        except TransportError as exc:
            raise OutcomeUnknownError(
                "Time-entry update outcome is unknown; inspect the entry before retrying",
                details={"entry_id": entry_id},
            ) from exc
        except APIError as exc:
            exc.details.setdefault("entry_id", entry_id)
            if _ambiguous_api_error(exc):
                raise OutcomeUnknownError(
                    "Time-entry update outcome is unknown; inspect the entry before retrying",
                    details={"entry_id": entry_id},
                ) from exc
            raise

        try:
            updated = normalize_time_entry(
                _data_object(
                    self._client.get_time_entry(workspace_id, entry_id),
                    label="updated time-entry readback data",
                )
            )
        except ClickUpCLIError as exc:
            raise VerificationError(
                "Time entry was updated but readback failed: " + str(exc),
                details={"entry_id": entry_id},
            ) from exc
        if updated.get("id") != entry_id:
            raise VerificationError("Updated time-entry ID verification failed")
        if description is not None and updated.get("description") != description:
            raise VerificationError("Updated time-entry description verification failed")
        if task_id is not None and updated.get("task_id") != task_id:
            raise VerificationError("Updated time-entry task verification failed")
        if start is not None and updated.get("start_ms") != start.milliseconds:
            raise VerificationError("Updated time-entry start verification failed")
        if expected_end_ms is not None and updated.get("end_ms") != expected_end_ms:
            raise VerificationError("Updated time-entry end verification failed")
        if expected_duration_ms is not None and updated.get("duration_ms") != expected_duration_ms:
            raise VerificationError("Updated time-entry preserved-duration verification failed")
        if duration is not None and updated.get("duration_ms") != duration.milliseconds:
            raise VerificationError("Updated time-entry duration verification failed")
        if billable is not None and updated.get("billable") is not billable:
            raise VerificationError("Updated time-entry billable verification failed")
        return TimeMutationResult(changed=True, entry=updated)

    def delete(self, workspace_id: str, entry_id: str) -> TimeDeleteResult:
        try:
            current_payload = self._client.get_time_entry(workspace_id, entry_id)
        except APIError as exc:
            if exc.status_code == 404:
                return TimeDeleteResult(
                    changed=False,
                    deleted=False,
                    entry_id=entry_id,
                    entry=None,
                )
            raise
        current = normalize_time_entry(
            _data_object(current_payload, label="time-entry delete pre-read data")
        )
        if current.get("id") != entry_id:
            raise APIError(
                f"Time-entry delete pre-read expected ID {entry_id}, received {current.get('id')}",
                details={"entry_id": entry_id},
            )

        try:
            response = self._client.delete_time_entry(workspace_id, entry_id)
        except TransportError as exc:
            raise OutcomeUnknownError(
                "Time-entry deletion outcome is unknown because the response was lost; inspect "
                "the exact entry before retrying",
                details={"entry_id": entry_id},
            ) from exc
        except APIError as exc:
            exc.details.setdefault("entry_id", entry_id)
            if _ambiguous_api_error(exc):
                raise OutcomeUnknownError(
                    "Time-entry deletion outcome is unknown because ClickUp did not return a "
                    "usable response; inspect the exact entry before retrying",
                    details={"entry_id": entry_id},
                ) from exc
            raise

        try:
            deleted_id = _required_string(
                _data_object(response, label="deleted time-entry data").get("id"),
                label="deleted time-entry ID",
            )
        except ClickUpCLIError as exc:
            raise OutcomeUnknownError(
                "Time-entry deletion outcome is unknown because the successful response did not "
                "identify the deleted entry; inspect the exact entry before retrying",
                details={"entry_id": entry_id},
            ) from exc
        if deleted_id != entry_id:
            raise OutcomeUnknownError(
                f"Time-entry deletion outcome is unknown because ClickUp returned ID {deleted_id} "
                f"instead of {entry_id}; inspect both IDs before retrying",
                details={"entry_id": entry_id, "response_entry_id": deleted_id},
            )

        try:
            present_payload = self._client.get_time_entry(workspace_id, entry_id)
        except APIError as exc:
            if exc.status_code == 404:
                return TimeDeleteResult(
                    changed=True,
                    deleted=True,
                    entry_id=entry_id,
                    entry=current,
                )
            raise OutcomeUnknownError(
                "Time-entry deletion returned the expected ID, but the absence check failed; "
                "inspect the exact entry before retrying",
                details={"entry_id": entry_id},
            ) from exc
        except TransportError as exc:
            raise OutcomeUnknownError(
                "Time-entry deletion returned the expected ID, but the absence check was lost; "
                "inspect the exact entry before retrying",
                details={"entry_id": entry_id},
            ) from exc

        try:
            present = normalize_time_entry(
                _data_object(present_payload, label="time-entry delete absence-check data")
            )
        except ClickUpCLIError as exc:
            raise OutcomeUnknownError(
                "Time-entry deletion returned the expected ID, but the absence check was invalid; "
                "inspect the exact entry before retrying",
                details={"entry_id": entry_id},
            ) from exc
        raise VerificationError(
            f"Time-entry deletion verification failed: entry {present.get('id')} is still present",
            details={"entry_id": entry_id},
        )
