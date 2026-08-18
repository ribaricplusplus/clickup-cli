"""Strict, preflighted task batch planning and application."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypeAlias, cast

from clickup_cli.client import ClickUpClient
from clickup_cli.domain import (
    DueDateInput,
    TaskService,
    list_statuses,
    parse_due_date,
    summarize_task,
    task_assignee_ids,
    task_list_id,
    task_status,
    task_tag_names,
)
from clickup_cli.errors import (
    APIError,
    BatchManifestError,
    BatchPartialFailureError,
    ClickUpCLIError,
    InvalidStatusError,
)
from clickup_cli.refs import parse_task_ref
from clickup_cli.task_mutations import (
    StartDateInput,
    TaskMutationService,
    TaskUpdateRequest,
    parse_priority,
    parse_start_date,
    task_archived,
)
from clickup_cli.types import JsonObject, JsonValue

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MANIFEST_LINES = 10_000
MAX_MANIFEST_TASKS = 1_000
MAX_MANIFEST_LINE_BYTES = 64 * 1024

_TOP_LEVEL_KEYS = {
    "task",
    "set",
    "add_tags",
    "remove_tags",
    "add_assignees",
    "remove_assignees",
}
_SET_KEYS = {
    "name",
    "description",
    "status",
    "due_date",
    "priority",
    "start_date",
    "archived",
}
_SET_ORDER = ("name", "description", "status", "due_date", "priority", "start_date")
_PRIORITY_NAMES = {1: "urgent", 2: "high", 3: "normal", 4: "low"}

OperationKind: TypeAlias = Literal[
    "set_name",
    "set_description",
    "set_status",
    "set_due_date",
    "set_priority",
    "set_start_date",
    "set_archived",
    "add_tag",
    "remove_tag",
    "add_assignee",
    "remove_assignee",
]
OperationValue: TypeAlias = str | int | bool | DueDateInput | StartDateInput | None


@dataclass(frozen=True)
class BatchOperation:
    kind: OperationKind
    value: OperationValue


@dataclass(frozen=True)
class BatchTask:
    line: int
    task_id: str
    operations: tuple[BatchOperation, ...]


@dataclass(frozen=True)
class BatchManifest:
    manifest_sha256: str
    tasks: tuple[BatchTask, ...]


@dataclass(frozen=True)
class _PreflightTask:
    entry: BatchTask
    task: JsonObject
    task_name: str
    changes: tuple[JsonObject, ...]


@dataclass(frozen=True)
class _Preflight:
    manifest_sha256: str
    tasks: tuple[_PreflightTask, ...]


class _DuplicateJSONKey(ValueError):
    pass


def _manifest_error(message: str, *, line: int | None = None) -> BatchManifestError:
    details: dict[str, JsonValue] = {}
    if line is not None:
        details["line"] = line
        message = f"Manifest line {line}: {message}"
    return BatchManifestError(message, details=details)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON value {value}")


def _unknown_keys(payload: dict[str, object], allowed: set[str]) -> list[str]:
    return sorted(set(payload) - allowed)


def _string(value: object, *, label: str, line: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "must be text" if allow_empty else "must be a non-empty string"
        raise _manifest_error(f"{label} {suffix}", line=line)
    return value


def _tags(value: object, *, label: str, line: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _manifest_error(f"{label} must be an array", line=line)
    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw_tag in enumerate(value):
        tag = _string(raw_tag, label=f"{label}[{index}]", line=line).strip()
        if any(character in tag for character in ("\0", "\r", "\n")):
            raise _manifest_error(
                f"{label}[{index}] contains unsupported control characters",
                line=line,
            )
        key = tag.casefold()
        if key in seen:
            raise _manifest_error(f"{label} contains duplicate value {tag!r}", line=line)
        seen.add(key)
        normalized.append(tag)
    return tuple(normalized)


def _assignees(value: object, *, label: str, line: int) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise _manifest_error(f"{label} must be an array", line=line)
    normalized: list[int] = []
    seen: set[int] = set()
    for index, raw_id in enumerate(value):
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            raise _manifest_error(
                f"{label}[{index}] must be a positive integer",
                line=line,
            )
        if raw_id in seen:
            raise _manifest_error(f"{label} contains duplicate value {raw_id}", line=line)
        seen.add(raw_id)
        normalized.append(raw_id)
    return tuple(normalized)


def _set_operation(field: str, value: object, *, line: int) -> BatchOperation:
    if field == "name":
        return BatchOperation("set_name", _string(value, label="set.name", line=line))
    if field == "description":
        return BatchOperation(
            "set_description",
            _string(value, label="set.description", line=line, allow_empty=True),
        )
    if field == "status":
        return BatchOperation("set_status", _string(value, label="set.status", line=line).strip())
    if field == "due_date":
        if value is None:
            return BatchOperation("set_due_date", None)
        raw_due_date = _string(value, label="set.due_date", line=line)
        try:
            return BatchOperation("set_due_date", parse_due_date(raw_due_date))
        except ClickUpCLIError as exc:
            raise _manifest_error(str(exc), line=line) from exc
    if field == "priority":
        if value is None:
            return BatchOperation("set_priority", None)
        raw_priority = _string(value, label="set.priority", line=line)
        if raw_priority.strip().casefold() == "clear":
            raise _manifest_error(
                "set.priority must be urgent, high, normal, low, or null",
                line=line,
            )
        try:
            return BatchOperation("set_priority", parse_priority(raw_priority))
        except ClickUpCLIError as exc:
            raise _manifest_error(str(exc), line=line) from exc
    if field == "start_date":
        if value is None:
            return BatchOperation("set_start_date", None)
        raw_start_date = _string(value, label="set.start_date", line=line)
        try:
            return BatchOperation("set_start_date", parse_start_date(raw_start_date))
        except ClickUpCLIError as exc:
            raise _manifest_error(str(exc), line=line) from exc
    if field == "archived":
        if not isinstance(value, bool):
            raise _manifest_error("set.archived must be boolean", line=line)
        return BatchOperation("set_archived", value)
    raise RuntimeError(f"Unhandled batch field: {field}")


def _parse_task(payload: object, *, line: int) -> BatchTask:
    if not isinstance(payload, dict):
        raise _manifest_error("each nonblank line must be a JSON object", line=line)
    typed_payload = cast(dict[str, object], payload)
    unknown = _unknown_keys(typed_payload, _TOP_LEVEL_KEYS)
    if unknown:
        raise _manifest_error("unknown keys: " + ", ".join(unknown), line=line)
    if "task" not in typed_payload:
        raise _manifest_error("task is required", line=line)
    raw_ref = _string(typed_payload["task"], label="task", line=line)
    try:
        task_id = parse_task_ref(raw_ref)
    except ClickUpCLIError as exc:
        raise _manifest_error(str(exc), line=line) from exc

    set_payload: dict[str, object] = {}
    if "set" in typed_payload:
        raw_set = typed_payload["set"]
        if not isinstance(raw_set, dict):
            raise _manifest_error("set must be an object", line=line)
        set_payload = cast(dict[str, object], raw_set)
        unknown_set = _unknown_keys(set_payload, _SET_KEYS)
        if unknown_set:
            raise _manifest_error("unknown set keys: " + ", ".join(unknown_set), line=line)

    add_tags = _tags(typed_payload.get("add_tags", []), label="add_tags", line=line)
    remove_tags = _tags(typed_payload.get("remove_tags", []), label="remove_tags", line=line)
    add_assignees = _assignees(
        typed_payload.get("add_assignees", []), label="add_assignees", line=line
    )
    remove_assignees = _assignees(
        typed_payload.get("remove_assignees", []), label="remove_assignees", line=line
    )

    tag_conflicts = sorted(
        {tag.casefold() for tag in add_tags} & {tag.casefold() for tag in remove_tags}
    )
    if tag_conflicts:
        raise _manifest_error(
            "tag values cannot appear in both add_tags and remove_tags: "
            + ", ".join(tag_conflicts),
            line=line,
        )
    assignee_conflicts = sorted(set(add_assignees) & set(remove_assignees))
    if assignee_conflicts:
        raise _manifest_error(
            "assignee IDs cannot appear in both add_assignees and remove_assignees: "
            + ", ".join(str(value) for value in assignee_conflicts),
            line=line,
        )

    operations: list[BatchOperation] = []
    if set_payload.get("archived") is False:
        operations.append(_set_operation("archived", False, line=line))
    for field in _SET_ORDER:
        if field in set_payload:
            operations.append(_set_operation(field, set_payload[field], line=line))
    operations.extend(BatchOperation("remove_tag", tag) for tag in remove_tags)
    operations.extend(BatchOperation("add_tag", tag) for tag in add_tags)
    operations.extend(BatchOperation("remove_assignee", user_id) for user_id in remove_assignees)
    operations.extend(BatchOperation("add_assignee", user_id) for user_id in add_assignees)
    if set_payload.get("archived") is True:
        operations.append(_set_operation("archived", True, line=line))
    if not operations:
        raise _manifest_error("task has no operations", line=line)
    return BatchTask(line=line, task_id=task_id, operations=tuple(operations))


def load_manifest(path: Path) -> BatchManifest:
    """Read, hash, parse, and strictly validate one bounded UTF-8 JSONL manifest."""

    try:
        metadata = path.stat()
    except OSError as exc:
        raise _manifest_error(f"could not access manifest: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise _manifest_error(f"manifest is not a regular file: {path}")
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise _manifest_error("manifest exceeds the 1 MiB safety limit")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise _manifest_error(f"could not read manifest: {path}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise _manifest_error("manifest exceeds the 1 MiB safety limit")
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    raw_lines = raw.splitlines()
    if len(raw_lines) > MAX_MANIFEST_LINES:
        raise _manifest_error(f"manifest exceeds the {MAX_MANIFEST_LINES} line safety limit")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        invalid_line = raw[: exc.start].count(b"\n") + 1
        raise _manifest_error("manifest must contain valid UTF-8", line=invalid_line) from exc
    lines = [line.decode("utf-8") for line in raw_lines]

    tasks: list[BatchTask] = []
    task_lines: dict[str, int] = {}
    for line_number, (raw_line, line) in enumerate(zip(raw_lines, lines, strict=True), start=1):
        if len(raw_line) > MAX_MANIFEST_LINE_BYTES:
            raise _manifest_error(
                f"line exceeds the {MAX_MANIFEST_LINE_BYTES}-byte safety limit",
                line=line_number,
            )
        if not line.strip():
            continue
        try:
            payload = json.loads(
                line,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, _DuplicateJSONKey, ValueError, RecursionError) as exc:
            raise _manifest_error(f"malformed JSON ({exc})", line=line_number) from exc
        task = _parse_task(payload, line=line_number)
        previous_line = task_lines.get(task.task_id)
        if previous_line is not None:
            raise _manifest_error(
                f"duplicate task {task.task_id!r} (first declared on line {previous_line})",
                line=line_number,
            )
        task_lines[task.task_id] = line_number
        tasks.append(task)
        if len(tasks) > MAX_MANIFEST_TASKS:
            raise _manifest_error(
                f"manifest exceeds the {MAX_MANIFEST_TASKS} task safety limit",
                line=line_number,
            )
    if not tasks:
        raise _manifest_error("manifest must contain at least one task")
    return BatchManifest(manifest_sha256=manifest_sha256, tasks=tuple(tasks))


def _operation_label(operation: BatchOperation) -> str:
    return operation.kind


def _operation_field(operation: BatchOperation) -> str:
    return {
        "set_name": "name",
        "set_description": "description",
        "set_status": "status",
        "set_due_date": "due_date",
        "set_priority": "priority",
        "set_start_date": "start_date",
        "set_archived": "archived",
        "add_tag": "tags",
        "remove_tag": "tags",
        "add_assignee": "assignees",
        "remove_assignee": "assignees",
    }[operation.kind]


def _display_value(operation: BatchOperation) -> JsonValue:
    value = operation.value
    if isinstance(value, DueDateInput | StartDateInput):
        return value.display
    if operation.kind == "set_priority" and isinstance(value, int) and not isinstance(value, bool):
        return _PRIORITY_NAMES[value]
    return cast(JsonValue, value)


def _initial_state(task: JsonObject, operations: list[BatchOperation]) -> dict[str, JsonValue]:
    summary = summarize_task(task)
    kinds = {operation.kind for operation in operations}
    if "set_description" in kinds:
        description = task.get("description")
        if description is not None and not isinstance(description, str):
            raise APIError("ClickUp response contains an invalid task description")
    status = task_status(task) if "set_status" in kinds else summary["status"]
    archived = task_archived(task) if "set_archived" in kinds else summary["archived"]
    tags = task_tag_names(task) if kinds & {"add_tag", "remove_tag"} else []
    assignees = task_assignee_ids(task) if kinds & {"add_assignee", "remove_assignee"} else []
    return {
        "name": summary["name"],
        "description": summary["description"],
        "status": status,
        "due_date": summary["due_date"],
        "due_date_time": summary["due_date_time"],
        "priority": summary["priority"],
        "start_date": summary["start_date"],
        "start_date_time": summary["start_date_time"],
        "archived": archived,
        "tags": cast(list[JsonValue], tags),
        "assignees": cast(list[JsonValue], assignees),
    }


def _planned_change(state: dict[str, JsonValue], operation: BatchOperation) -> JsonObject:
    field = _operation_field(operation)
    before = state[field]
    changed: bool
    value = _display_value(operation)
    if operation.kind in {"set_name", "set_description", "set_priority"}:
        after = value
        changed = before != after
        state[field] = after
    elif operation.kind == "set_status":
        after = value
        changed = not (
            isinstance(before, str)
            and isinstance(after, str)
            and before.casefold() == after.casefold()
        )
        state[field] = after
    elif operation.kind == "set_archived":
        after = value
        changed = before != after
        state[field] = after
    elif operation.kind == "set_due_date":
        due_value = cast(DueDateInput | None, operation.value)
        after = value
        requested_time: JsonValue = due_value.has_time if due_value is not None else None
        changed = (
            before is not None
            if due_value is None
            else (before != after or state["due_date_time"] != requested_time)
        )
        state[field] = after
        state["due_date_time"] = requested_time
    elif operation.kind == "set_start_date":
        start_value = cast(StartDateInput | None, operation.value)
        after = value
        requested_time = start_value.has_time if start_value is not None else None
        changed = (
            before is not None
            if start_value is None
            else (before != after or state["start_date_time"] != requested_time)
        )
        state[field] = after
        state["start_date_time"] = requested_time
    elif operation.kind in {"add_tag", "remove_tag"}:
        tags = [str(item) for item in cast(list[JsonValue], before)]
        requested = cast(str, operation.value)
        match = next((tag for tag in tags if tag.casefold() == requested.casefold()), None)
        if operation.kind == "add_tag":
            changed = match is None
            after_tags = [*tags, requested] if changed else tags
        else:
            changed = match is not None
            after_tags = [tag for tag in tags if tag.casefold() != requested.casefold()]
        after = cast(JsonValue, after_tags)
        state[field] = after
    else:
        assignees = list(cast(list[int], before))
        requested_id = cast(int, operation.value)
        present = requested_id in assignees
        adding = operation.kind == "add_assignee"
        changed = present != adding
        after_ids = sorted([*assignees, requested_id]) if adding and not present else assignees
        if not adding and present:
            after_ids = [user_id for user_id in assignees if user_id != requested_id]
        after = cast(JsonValue, after_ids)
        state[field] = after
    result: JsonObject = {
        "after": after,
        "before": before,
        "changed": changed,
        "field": field,
        "operation": _operation_label(operation),
    }
    if operation.kind.startswith("add_") or operation.kind.startswith("remove_"):
        result["value"] = value
    return result


def _counts(changes: tuple[JsonObject, ...] | list[JsonObject]) -> tuple[int, int, int]:
    operation_count = len(changes)
    change_count = sum(change.get("changed") is True for change in changes)
    return operation_count, change_count, operation_count - change_count


def _task_plan_payload(task: _PreflightTask) -> JsonObject:
    operation_count, change_count, no_op_count = _counts(task.changes)
    return {
        "change_count": change_count,
        "changes": cast(list[JsonValue], list(task.changes)),
        "line": task.entry.line,
        "no_op_count": no_op_count,
        "operation_count": operation_count,
        "task_id": task.entry.task_id,
        "task_name": task.task_name,
    }


def _preflight_payload(preflight: _Preflight) -> JsonObject:
    tasks = [_task_plan_payload(task) for task in preflight.tasks]
    operation_count = sum(cast(int, task["operation_count"]) for task in tasks)
    change_count = sum(cast(int, task["change_count"]) for task in tasks)
    return {
        "change_count": change_count,
        "manifest_sha256": preflight.manifest_sha256,
        "no_op_count": operation_count - change_count,
        "operation_count": operation_count,
        "task_count": len(tasks),
        "tasks": cast(list[JsonValue], tasks),
    }


class BatchService:
    """Perform a complete read-only preflight before optional verified writes."""

    def __init__(self, client: ClickUpClient) -> None:
        self._client = client

    def _preflight(self, manifest: BatchManifest) -> _Preflight:
        fetched: list[tuple[BatchTask, JsonObject, str]] = []
        for entry in manifest.tasks:
            task = self._client.get_task(entry.task_id)
            summary = summarize_task(task)
            observed_id = summary.get("id")
            if observed_id != entry.task_id:
                raise APIError(
                    f"Manifest line {entry.line}: task readback ID did not match {entry.task_id}",
                    details={"line": entry.line, "task_id": entry.task_id},
                )
            task_name = summary.get("name")
            if not isinstance(task_name, str):
                raise APIError(
                    f"Manifest line {entry.line}: ClickUp response is missing task name",
                    details={"line": entry.line, "task_id": entry.task_id},
                )
            fetched.append((entry, task, task_name))

        list_cache: dict[str, list[str]] = {}
        preflight_tasks: list[_PreflightTask] = []
        for entry, task, task_name in fetched:
            operations = list(entry.operations)
            if any(operation.kind == "set_status" for operation in operations):
                list_id = task_list_id(task)
                labels = list_cache.get(list_id)
                if labels is None:
                    labels = [
                        status.label for status in list_statuses(self._client.get_list(list_id))
                    ]
                    list_cache[list_id] = labels
                for index, operation in enumerate(operations):
                    if operation.kind != "set_status":
                        continue
                    requested = cast(str, operation.value)
                    matches = [
                        label for label in labels if label.casefold() == requested.casefold()
                    ]
                    if len(matches) != 1:
                        valid = ", ".join(labels)
                        raise InvalidStatusError(
                            f"Manifest line {entry.line}: invalid status {requested!r}. "
                            f"Valid statuses: {valid}",
                            details={
                                "line": entry.line,
                                "list_id": list_id,
                                "task_id": entry.task_id,
                            },
                        )
                    operations[index] = replace(operation, value=matches[0])
            resolved_entry = replace(entry, operations=tuple(operations))
            state = _initial_state(task, operations)
            changes = tuple(_planned_change(state, operation) for operation in operations)
            preflight_tasks.append(
                _PreflightTask(
                    entry=resolved_entry,
                    task=task,
                    task_name=task_name,
                    changes=changes,
                )
            )
        return _Preflight(manifest.manifest_sha256, tuple(preflight_tasks))

    def plan(self, manifest: BatchManifest) -> JsonObject:
        """Return a deterministic, strictly read-only plan."""

        try:
            preflight = self._preflight(manifest)
        except ClickUpCLIError as exc:
            exc.details.setdefault("manifest_sha256", manifest.manifest_sha256)
            raise
        return _preflight_payload(preflight)

    def _apply_operation(self, task_id: str, operation: BatchOperation) -> tuple[bool, JsonObject]:
        task_service = TaskService(self._client)
        mutation_service = TaskMutationService(self._client)
        value = operation.value
        if operation.kind == "set_name":
            result = mutation_service.update(task_id, TaskUpdateRequest(name=cast(str, value)))
        elif operation.kind == "set_description":
            result = mutation_service.update(
                task_id,
                TaskUpdateRequest(
                    description=cast(str, value),
                    description_supplied=True,
                ),
            )
        elif operation.kind == "set_status":
            status_result = task_service.set_prevalidated_status(task_id, cast(str, value))
            return status_result.changed, status_result.task
        elif operation.kind == "set_due_date":
            due_result = (
                task_service.clear_due_date(task_id)
                if value is None
                else task_service.set_due_date(task_id, cast(DueDateInput, value))
            )
            return due_result.changed, due_result.task
        elif operation.kind == "set_priority":
            result = mutation_service.update(
                task_id,
                TaskUpdateRequest(
                    priority=cast(int | None, value),
                    priority_supplied=True,
                ),
            )
        elif operation.kind == "set_start_date":
            result = mutation_service.update(
                task_id,
                TaskUpdateRequest(
                    start_date=cast(StartDateInput | None, value),
                    clear_start_date=value is None,
                ),
            )
        elif operation.kind == "set_archived":
            result = mutation_service.set_archived(task_id, archived=cast(bool, value))
        elif operation.kind in {"add_tag", "remove_tag"}:
            tag_result = mutation_service.set_tag(
                task_id,
                cast(str, value),
                add=operation.kind == "add_tag",
            )
            return tag_result.changed, tag_result.task
        else:
            assignment_result = (
                task_service.assign(task_id, cast(int, value))
                if operation.kind == "add_assignee"
                else task_service.unassign(task_id, cast(int, value))
            )
            return assignment_result.changed, assignment_result.task
        return result.changed, result.task

    @staticmethod
    def _operation_result(operation: BatchOperation, *, changed: bool) -> JsonObject:
        result: JsonObject = {
            "changed": changed,
            "field": _operation_field(operation),
            "operation": _operation_label(operation),
        }
        if operation.kind.startswith("add_") or operation.kind.startswith("remove_"):
            result["value"] = _display_value(operation)
        return result

    @staticmethod
    def _failure(
        task: _PreflightTask,
        operation: BatchOperation,
        error: ClickUpCLIError,
    ) -> JsonObject:
        error_payload: JsonObject = {"message": str(error), "type": error.error_type}
        error_payload.update(error.details)
        return {
            "error": error_payload,
            "line": task.entry.line,
            "operation": _operation_label(operation),
            "task_id": task.entry.task_id,
        }

    @staticmethod
    def _partial_task_result(
        task: _PreflightTask,
        operations: list[JsonObject],
        last_verified: JsonObject,
    ) -> JsonObject:
        change_count = sum(operation.get("changed") is True for operation in operations)
        return {
            "change_count": change_count,
            "completed_operation_count": len(operations),
            "last_verified_task": summarize_task(last_verified),
            "line": task.entry.line,
            "no_op_count": len(operations) - change_count,
            "operation_count": len(task.entry.operations),
            "operations": cast(list[JsonValue], operations),
            "status": "failed",
            "task_id": task.entry.task_id,
            "task_name": task.task_name,
        }

    def apply(self, manifest: BatchManifest, *, continue_on_error: bool) -> JsonObject:
        """Preflight every task, then apply verified operations in manifest order."""

        try:
            preflight = self._preflight(manifest)
        except ClickUpCLIError as exc:
            exc.details.setdefault("manifest_sha256", manifest.manifest_sha256)
            raise
        results: list[JsonObject] = []
        failures: list[JsonObject] = []
        completed_task_ids: list[str] = []
        for task in preflight.tasks:
            operation_results: list[JsonObject] = []
            last_verified = task.task
            failed = False
            for operation in task.entry.operations:
                try:
                    changed, last_verified = self._apply_operation(task.entry.task_id, operation)
                except ClickUpCLIError as exc:
                    failure = self._failure(task, operation, exc)
                    failures.append(failure)
                    partial = self._partial_task_result(
                        task,
                        operation_results,
                        last_verified,
                    )
                    results.append(partial)
                    failed = True
                    if not continue_on_error:
                        details: dict[str, JsonValue] = {
                            "completed_task_ids": cast(list[JsonValue], completed_task_ids),
                            "failed_line": task.entry.line,
                            "failed_operation": _operation_label(operation),
                            "failed_task_id": task.entry.task_id,
                            "failure": failure,
                            "manifest_sha256": preflight.manifest_sha256,
                            "results": cast(list[JsonValue], results),
                        }
                        raise BatchPartialFailureError(
                            "Batch apply failed at line "
                            f"{task.entry.line}, task {task.entry.task_id}, "
                            f"operation {_operation_label(operation)}",
                            details=details,
                        ) from exc
                    break
                operation_results.append(self._operation_result(operation, changed=changed))
            if failed:
                continue
            change_count = sum(operation.get("changed") is True for operation in operation_results)
            results.append(
                {
                    "change_count": change_count,
                    "final_task": summarize_task(last_verified),
                    "line": task.entry.line,
                    "no_op_count": len(operation_results) - change_count,
                    "operation_count": len(operation_results),
                    "operations": cast(list[JsonValue], operation_results),
                    "status": "completed",
                    "task_id": task.entry.task_id,
                    "task_name": task.task_name,
                }
            )
            completed_task_ids.append(task.entry.task_id)

        if failures:
            details = {
                "completed_task_ids": cast(list[JsonValue], completed_task_ids),
                "failures": cast(list[JsonValue], failures),
                "manifest_sha256": preflight.manifest_sha256,
                "results": cast(list[JsonValue], results),
            }
            raise BatchPartialFailureError(
                f"Batch apply completed with {len(failures)} failed task(s)",
                details=details,
            )
        change_count = sum(cast(int, result["change_count"]) for result in results)
        operation_count = sum(cast(int, result["operation_count"]) for result in results)
        return {
            "change_count": change_count,
            "manifest_sha256": preflight.manifest_sha256,
            "no_op_count": operation_count - change_count,
            "operation_count": operation_count,
            "task_count": len(results),
            "tasks": cast(list[JsonValue], results),
        }


def batch_plan_text(plan: JsonObject) -> str:
    """Render stable human-readable plan output."""

    lines = [
        f"Manifest SHA-256: {plan['manifest_sha256']}",
        "Tasks: "
        f"{plan['task_count']}; operations: {plan['operation_count']}; "
        f"changes: {plan['change_count']}; no-ops: {plan['no_op_count']}",
    ]
    for raw_task in cast(list[JsonValue], plan["tasks"]):
        task = cast(JsonObject, raw_task)
        lines.append(f"{task['task_id']} {task['task_name']} (line {task['line']})")
        for raw_change in cast(list[JsonValue], task["changes"]):
            change = cast(JsonObject, raw_change)
            marker = "change" if change["changed"] else "no-op"
            lines.append(
                f"  {marker} {change['operation']}: "
                f"{json.dumps(change['before'], ensure_ascii=False, sort_keys=True)} -> "
                f"{json.dumps(change['after'], ensure_ascii=False, sort_keys=True)}"
            )
    return "\n".join(lines)


def batch_apply_text(result: JsonObject) -> str:
    """Render stable human-readable successful apply output."""

    lines = [
        f"Manifest SHA-256: {result['manifest_sha256']}",
        "Applied tasks: "
        f"{result['task_count']}; operations: {result['operation_count']}; "
        f"changes: {result['change_count']}; no-ops: {result['no_op_count']}",
    ]
    for raw_task in cast(list[JsonValue], result["tasks"]):
        task = cast(JsonObject, raw_task)
        lines.append(f"{task['task_id']} {task['status']} (line {task['line']})")
        for raw_operation in cast(list[JsonValue], task["operations"]):
            operation = cast(JsonObject, raw_operation)
            marker = "changed" if operation["changed"] else "no-op"
            lines.append(f"  {marker} {operation['operation']}")
    return "\n".join(lines)
