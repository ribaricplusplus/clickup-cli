"""Status-aware deterministic task operations independent of the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from clickup_cli.client import ClickUpClient
from clickup_cli.errors import (
    APIError,
    CompletionStatusError,
    InvalidStatusError,
    VerificationError,
)
from clickup_cli.types import JsonObject, JsonValue

_COMPLETION_LABELS = ("completed", "complete", "done", "closed")
_COMPLETION_TYPES = {"done", "closed"}


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


def _mapping(value: JsonValue | None, *, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise APIError(f"ClickUp response is missing {label}")
    return cast(JsonObject, value)


def _required_string(value: JsonValue | None, *, label: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value):
        raise APIError(f"ClickUp response is missing {label}")
    return str(value)


def task_status(task: JsonObject) -> str:
    status = _mapping(task.get("status"), label="task status")
    return _required_string(status.get("status"), label="task status label")


def task_list_id(task: JsonObject) -> str:
    home_list = _mapping(task.get("list"), label="task home list")
    return _required_string(home_list.get("id"), label="task home list ID")


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


class TaskService:
    """Orchestrates status validation, minimal writes, and verified readbacks."""

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

    def _apply(
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
        return self._apply(task_id, task, current_status, match.label)

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
        return self._apply(task_id, task, current_status, match.label)
