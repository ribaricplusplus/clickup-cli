"""Containment checks shared by the opt-in live lifecycle and offline tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from clickup_cli.client import ClickUpClient
from clickup_cli.errors import APIError
from clickup_cli.time_tracking import normalize_time_entry
from clickup_cli.types import JsonObject, JsonValue

SANDBOX_LIST_NAME = "ClickUp CLI Test Sandbox"


class LiveContainmentError(RuntimeError):
    """A destructive live-test action could not prove sandbox ownership."""


@dataclass(frozen=True)
class OwnedTask:
    """The marker that must remain in both mutable identity fields of a run task."""

    marker: str


@dataclass(frozen=True)
class OwnedTimeEntry:
    """The run task and marker that must remain on a manual time entry."""

    task_id: str
    marker: str


def _objects(value: JsonValue | None, *, label: str) -> list[JsonObject]:
    if not isinstance(value, list):
        raise LiveContainmentError(f"Containment failed: {label} is not an array")
    objects: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            raise LiveContainmentError(f"Containment failed: {label} contains a non-object")
        objects.append(cast(JsonObject, item))
    return objects


def prove_sandbox_destination(
    list_summary: JsonObject,
    workspace_tree: JsonObject,
    *,
    workspace_id: str,
    space_id: str,
    list_id: str,
) -> None:
    """Require exact List identity plus Workspace -> Space -> List membership."""

    if list_summary.get("id") != list_id:
        raise LiveContainmentError("Containment failed: returned List ID is not configured ID")
    if list_summary.get("name") != SANDBOX_LIST_NAME:
        raise LiveContainmentError(
            f"Containment failed: List name must be exactly {SANDBOX_LIST_NAME!r}"
        )
    if list_summary.get("space_id") != space_id:
        raise LiveContainmentError("Containment failed: returned Space ID is not configured ID")
    if workspace_tree.get("id") != workspace_id:
        raise LiveContainmentError("Containment failed: returned Workspace ID is not configured ID")

    configured_space = next(
        (
            space
            for space in _objects(workspace_tree.get("spaces"), label="workspace spaces")
            if space.get("id") == space_id
        ),
        None,
    )
    if configured_space is None:
        raise LiveContainmentError(
            "Containment failed: configured Space is absent from configured Workspace tree"
        )

    tree_list_ids = {
        item.get("id") for item in _objects(configured_space.get("lists"), label="folderless Lists")
    }
    for folder in _objects(configured_space.get("folders"), label="Space folders"):
        tree_list_ids.update(
            item.get("id") for item in _objects(folder.get("lists"), label="folder Lists")
        )
    if list_id not in tree_list_ids:
        raise LiveContainmentError(
            "Containment failed: configured List is absent from configured Space tree"
        )


def require_owned_task(
    task: JsonObject,
    task_id: str,
    ownership: Mapping[str, OwnedTask],
    *,
    sandbox_list_id: str,
) -> None:
    """Prove that an exact fetched task is run-created and still in the sandbox."""

    owner = ownership.get(task_id)
    if owner is None:
        raise LiveContainmentError(f"Refusing task cleanup for unowned ID {task_id}")
    if task.get("id") is None or str(task.get("id")) != task_id:
        raise LiveContainmentError(f"Refusing task cleanup: readback ID mismatch for {task_id}")
    raw_list = task.get("list")
    if not isinstance(raw_list, dict) or str(raw_list.get("id")) != sandbox_list_id:
        raise LiveContainmentError(
            f"Refusing task cleanup outside sandbox List; surviving ID {task_id}"
        )
    name = task.get("name")
    description = task.get("description")
    if not isinstance(name, str) or owner.marker not in name:
        raise LiveContainmentError(
            f"Refusing task cleanup without marker in name; surviving ID {task_id}"
        )
    if not isinstance(description, str) or owner.marker not in description:
        raise LiveContainmentError(
            f"Refusing task cleanup without marker in description; surviving ID {task_id}"
        )


def delete_owned_task(
    client: ClickUpClient,
    task_id: str,
    ownership: Mapping[str, OwnedTask],
    *,
    sandbox_list_id: str,
    delete: Callable[[str], object] | None = None,
) -> None:
    """Fetch, prove, delete, and require HTTP 404 for exactly one run task."""

    require_owned_task(
        client.get_task(task_id),
        task_id,
        ownership,
        sandbox_list_id=sandbox_list_id,
    )
    (delete or client.delete_task)(task_id)
    try:
        client.get_task(task_id)
    except APIError as exc:
        if exc.status_code == 404:
            return
        raise
    raise LiveContainmentError(f"Task cleanup did not produce HTTP 404 for {task_id}")


def _time_entry_data(payload: JsonObject) -> JsonObject:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LiveContainmentError("Containment failed: time-entry readback is missing data")
    return cast(JsonObject, data)


def require_owned_time_entry(
    client: ClickUpClient,
    workspace_id: str,
    entry_id: str,
    ownership: Mapping[str, OwnedTimeEntry],
    task_ownership: Mapping[str, OwnedTask],
    *,
    sandbox_list_id: str,
) -> None:
    """Prove a captured manual entry and its attached task belong to this run."""

    owner = ownership.get(entry_id)
    if owner is None:
        raise LiveContainmentError(f"Refusing time-entry cleanup for unowned ID {entry_id}")
    entry = normalize_time_entry(_time_entry_data(client.get_time_entry(workspace_id, entry_id)))
    if entry.get("id") != entry_id:
        raise LiveContainmentError(
            f"Refusing time-entry cleanup: readback ID mismatch for {entry_id}"
        )
    if entry.get("task_id") != owner.task_id or owner.task_id not in task_ownership:
        raise LiveContainmentError(
            f"Refusing time-entry cleanup outside a run-owned task; surviving ID {entry_id}"
        )
    description = entry.get("description")
    if not isinstance(description, str) or owner.marker not in description:
        raise LiveContainmentError(
            f"Refusing time-entry cleanup without marker; surviving ID {entry_id}"
        )
    require_owned_task(
        client.get_task(owner.task_id),
        owner.task_id,
        task_ownership,
        sandbox_list_id=sandbox_list_id,
    )


def delete_owned_time_entry(
    client: ClickUpClient,
    workspace_id: str,
    entry_id: str,
    ownership: Mapping[str, OwnedTimeEntry],
    task_ownership: Mapping[str, OwnedTask],
    *,
    sandbox_list_id: str,
    delete: Callable[[str], object] | None = None,
) -> None:
    """Fetch, prove, and delete exactly one captured manual entry."""

    require_owned_time_entry(
        client,
        workspace_id,
        entry_id,
        ownership,
        task_ownership,
        sandbox_list_id=sandbox_list_id,
    )
    (delete or (lambda target: client.delete_time_entry(workspace_id, target)))(entry_id)
