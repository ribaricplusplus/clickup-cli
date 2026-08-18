"""Deterministic ClickUp hierarchy discovery and bounded task queries."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from clickup_cli.client import ClickUpClient
from clickup_cli.domain import (
    DueDateInput,
    TaskService,
    normalize_tags,
    summarize_task,
    task_assignee_ids,
    task_due_date,
    task_status,
    task_tag_names,
)
from clickup_cli.errors import (
    AmbiguousMatchError,
    APIError,
    InvalidOperationError,
    ResourceNotFoundError,
)
from clickup_cli.errors import ReferenceError as ClickUpReferenceError
from clickup_cli.refs import validate_native_id
from clickup_cli.types import JsonObject, JsonValue

DEFAULT_TASK_LIMIT = 100
MAX_TASK_RESULTS = 10_000
MAX_TASK_PAGES = 1_000
_MAX_DUE_DAYS = 3_650
_NEXT_DUE = re.compile(r"next:([1-9]\d*)d\Z", re.IGNORECASE)
_TERMINAL_STATUS_TYPES = {"closed", "done"}


@dataclass(frozen=True)
class TaskScope:
    kind: str
    resource_id: str

    @classmethod
    def from_options(
        cls,
        *,
        workspace_id: str | None,
        space_id: str | None,
        folder_id: str | None,
        list_id: str | None,
    ) -> TaskScope:
        supplied = [
            ("workspace", workspace_id, "WORKSPACE_ID"),
            ("space", space_id, "SPACE_ID"),
            ("folder", folder_id, "FOLDER_ID"),
            ("list", list_id, "LIST_ID"),
        ]
        selected = [(kind, value, label) for kind, value, label in supplied if value is not None]
        if len(selected) != 1:
            raise InvalidOperationError(
                "Exactly one task scope is required: --workspace-id, --space-id, "
                "--folder-id, or --list-id"
            )
        kind, value, label = selected[0]
        assert value is not None
        return cls(kind=kind, resource_id=validate_native_id(value, label=label))


@dataclass(frozen=True)
class DueFilter:
    kind: str
    days: int | None = None

    @classmethod
    def parse(cls, value: str | None) -> DueFilter | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if normalized in {"today", "overdue", "none"}:
            return cls(kind=normalized)
        match = _NEXT_DUE.fullmatch(normalized)
        if match is None:
            raise InvalidOperationError(
                "--due must be today, overdue, none, or next:Nd with a positive day count"
            )
        days = int(match.group(1))
        if days > _MAX_DUE_DAYS:
            raise InvalidOperationError(f"--due next range cannot exceed {_MAX_DUE_DAYS} days")
        return cls(kind="next", days=days)


@dataclass(frozen=True)
class TaskQuery:
    scope: TaskScope
    assignees: tuple[str, ...]
    statuses: tuple[str, ...]
    tags: tuple[str, ...]
    exclude_tags: tuple[str, ...]
    due: DueFilter | None
    include_closed: bool
    include_subtasks: bool
    include_archived: bool
    limit: int | None

    @classmethod
    def from_options(
        cls,
        *,
        workspace_id: str | None,
        space_id: str | None,
        folder_id: str | None,
        list_id: str | None,
        assignees: list[str] | None,
        statuses: list[str] | None,
        tags: list[str] | None,
        exclude_tags: list[str] | None,
        due: str | None,
        include_closed: bool,
        include_subtasks: bool,
        include_archived: bool,
        limit: int,
        all_results: bool,
    ) -> TaskQuery:
        scope = TaskScope.from_options(
            workspace_id=workspace_id,
            space_id=space_id,
            folder_id=folder_id,
            list_id=list_id,
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise InvalidOperationError("--limit must be a positive integer")
        if limit > MAX_TASK_RESULTS:
            raise InvalidOperationError(
                f"--limit cannot exceed the safety ceiling of {MAX_TASK_RESULTS}"
            )

        normalized_assignees = _normalize_assignee_options(assignees)
        normalized_statuses = tuple(_normalize_strings(statuses, label="Statuses"))
        normalized_tags = tuple(normalize_tags(tags))
        normalized_excluded = tuple(normalize_tags(exclude_tags))
        overlap = {tag.casefold() for tag in normalized_tags} & {
            tag.casefold() for tag in normalized_excluded
        }
        if overlap:
            raise InvalidOperationError("The same tag cannot be both included and excluded")
        return cls(
            scope=scope,
            assignees=normalized_assignees,
            statuses=normalized_statuses,
            tags=normalized_tags,
            exclude_tags=normalized_excluded,
            due=DueFilter.parse(due),
            include_closed=include_closed,
            include_subtasks=include_subtasks,
            include_archived=include_archived,
            limit=None if all_results else limit,
        )


@dataclass(frozen=True)
class EnsureResult:
    created: bool
    task: JsonObject


def _normalize_strings(values: list[str] | None, *, label: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values or []:
        value = raw_value.strip()
        if not value:
            raise InvalidOperationError(f"{label} must be non-empty strings")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(value)
    return normalized


def _normalize_assignee_options(values: list[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values or []:
        value = raw_value.strip()
        if value.casefold() == "me":
            assignee = "me"
        else:
            try:
                user_id = int(value)
            except ValueError as exc:
                raise InvalidOperationError("--assignee must be me or a positive USER_ID") from exc
            if user_id <= 0 or str(user_id) != value:
                raise InvalidOperationError("--assignee must be me or a positive USER_ID")
            assignee = value
        key = assignee.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(assignee)
    return tuple(normalized)


def _required_string(value: JsonValue | None, *, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
        raise APIError(f"ClickUp response is missing {label}")
    return str(value)


def _optional_string(value: JsonValue | None) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value)


def _resource_id(value: JsonValue | None, *, label: str) -> str:
    resource_id = _required_string(value, label=label)
    try:
        return validate_native_id(resource_id, label=label.upper().replace(" ", "_"))
    except ClickUpReferenceError as exc:
        raise APIError(f"ClickUp response contains an invalid {label}") from exc


def _user_id(value: JsonValue | None, *, label: str) -> str:
    user_id = _resource_id(value, label=label)
    try:
        parsed = int(user_id)
    except ValueError as exc:
        raise APIError(f"ClickUp response contains an invalid {label}") from exc
    if parsed <= 0 or str(parsed) != user_id:
        raise APIError(f"ClickUp response contains an invalid {label}")
    return user_id


def _objects(payload: JsonObject, key: str, *, label: str) -> list[JsonObject]:
    values = payload.get(key)
    if not isinstance(values, list):
        raise APIError(f"ClickUp response is missing {label}")
    objects: list[JsonObject] = []
    for value in values:
        if not isinstance(value, dict):
            raise APIError(f"ClickUp response contains an invalid {label.removesuffix('s')}")
        objects.append(cast(JsonObject, value))
    return objects


def _archived(resource: JsonObject) -> bool:
    value = resource.get("archived")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise APIError("ClickUp response contains an invalid archived flag")
    return value


def _catalog_summary(resource: JsonObject, *, label: str) -> JsonObject:
    return {
        "archived": _archived(resource),
        "id": _resource_id(resource.get("id"), label=f"{label} ID"),
        "name": _required_string(resource.get("name"), label=f"{label} name"),
    }


def _sort_catalog(resources: list[JsonObject]) -> list[JsonObject]:
    return sorted(
        resources,
        key=lambda resource: (
            str(resource.get("name") or "").casefold(),
            str(resource.get("id") or ""),
        ),
    )


def summarize_statuses(list_payload: JsonObject) -> list[JsonObject]:
    statuses: list[JsonObject] = []
    for status in _objects(list_payload, "statuses", label="list statuses"):
        label = _required_string(status.get("status"), label="list status label")
        status_type = _optional_string(status.get("type"))
        statuses.append({"status": label, "type": status_type})
    return sorted(
        statuses,
        key=lambda status: (
            str(status.get("status") or "").casefold(),
            str(status.get("type") or "").casefold(),
        ),
    )


def summarize_list(list_payload: JsonObject) -> JsonObject:
    folder = list_payload.get("folder")
    space = list_payload.get("space")
    folder_payload = cast(JsonObject, folder) if isinstance(folder, dict) else {}
    space_payload = cast(JsonObject, space) if isinstance(space, dict) else {}
    return {
        "archived": _archived(list_payload),
        "folder_id": _optional_string(folder_payload.get("id")),
        "folder_name": _optional_string(folder_payload.get("name")),
        "id": _resource_id(list_payload.get("id"), label="list ID"),
        "name": _required_string(list_payload.get("name"), label="list name"),
        "space_id": _optional_string(space_payload.get("id")),
        "space_name": _optional_string(space_payload.get("name")),
        "statuses": cast(list[JsonValue], summarize_statuses(list_payload)),
    }


class DiscoveryService:
    """Read-only catalog and task traversal with deterministic normalization."""

    def __init__(
        self,
        client: ClickUpClient,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._now = now

    def list_workspaces(self) -> list[JsonObject]:
        workspaces: list[JsonObject] = [
            {
                "id": _resource_id(workspace.get("id"), label="workspace ID"),
                "name": _required_string(workspace.get("name"), label="workspace name"),
            }
            for workspace in _objects(self._client.get_workspaces(), "teams", label="workspaces")
        ]
        return _sort_catalog(workspaces)

    def _workspace_payload(self, workspace_id: str) -> JsonObject:
        native_id = validate_native_id(workspace_id, label="WORKSPACE_ID")
        workspaces = _objects(self._client.get_workspaces(), "teams", label="workspaces")
        match = next(
            (
                workspace
                for workspace in workspaces
                if _resource_id(workspace.get("id"), label="workspace ID") == native_id
            ),
            None,
        )
        if match is None:
            raise ResourceNotFoundError(
                f"Workspace {native_id} is not available to the authorized user",
                details={"workspace_id": native_id},
            )
        return match

    def list_members(self, workspace_id: str) -> list[JsonObject]:
        workspace = self._workspace_payload(workspace_id)
        members: list[JsonObject] = []
        for member in _objects(workspace, "members", label="workspace members"):
            user = member.get("user")
            if not isinstance(user, dict):
                raise APIError("ClickUp response is missing workspace member user data")
            user_payload = cast(JsonObject, user)
            members.append(
                {
                    "email": (
                        str(user_payload["email"])
                        if isinstance(user_payload.get("email"), str)
                        else None
                    ),
                    "id": _user_id(user_payload.get("id"), label="member ID"),
                    "username": (
                        str(user_payload["username"])
                        if isinstance(user_payload.get("username"), str)
                        else None
                    ),
                }
            )
        return sorted(
            members,
            key=lambda member: (
                str(member.get("username") or "").casefold(),
                str(member.get("id") or ""),
            ),
        )

    def _catalog(
        self,
        fetch: Callable[[bool], JsonObject],
        *,
        key: str,
        label: str,
        include_archived: bool,
    ) -> list[JsonObject]:
        resources: dict[str, JsonObject] = {}
        archive_states = (False, True) if include_archived else (False,)
        for archived in archive_states:
            for resource in _objects(fetch(archived), key, label=key):
                summary = _catalog_summary(resource, label=label)
                resources.setdefault(cast(str, summary["id"]), summary)
        return _sort_catalog(list(resources.values()))

    def _spaces(self, workspace_id: str, *, include_archived: bool) -> list[JsonObject]:
        return self._catalog(
            lambda archived: self._client.get_spaces(workspace_id, archived=archived),
            key="spaces",
            label="space",
            include_archived=include_archived,
        )

    def _folders(self, space_id: str, *, include_archived: bool) -> list[JsonObject]:
        return self._catalog(
            lambda archived: self._client.get_folders(space_id, archived=archived),
            key="folders",
            label="folder",
            include_archived=include_archived,
        )

    def _space_lists(self, space_id: str, *, include_archived: bool) -> list[JsonObject]:
        return self._catalog(
            lambda archived: self._client.get_space_lists(space_id, archived=archived),
            key="lists",
            label="list",
            include_archived=include_archived,
        )

    def _folder_lists(self, folder_id: str, *, include_archived: bool) -> list[JsonObject]:
        return self._catalog(
            lambda archived: self._client.get_folder_lists(folder_id, archived=archived),
            key="lists",
            label="list",
            include_archived=include_archived,
        )

    def workspace_tree(self, workspace_id: str, *, include_archived: bool) -> JsonObject:
        workspace = self._workspace_payload(workspace_id)
        native_id = _resource_id(workspace.get("id"), label="workspace ID")
        spaces: list[JsonValue] = []
        for space in self._spaces(native_id, include_archived=include_archived):
            space_id = cast(str, space["id"])
            folders: list[JsonValue] = []
            for folder in self._folders(space_id, include_archived=include_archived):
                folder_id = cast(str, folder["id"])
                folders.append(
                    {
                        **folder,
                        "lists": cast(
                            list[JsonValue],
                            self._folder_lists(folder_id, include_archived=include_archived),
                        ),
                    }
                )
            spaces.append(
                {
                    **space,
                    "folders": folders,
                    "lists": cast(
                        list[JsonValue],
                        self._space_lists(space_id, include_archived=include_archived),
                    ),
                }
            )
        return {
            "id": native_id,
            "name": _required_string(workspace.get("name"), label="workspace name"),
            "spaces": spaces,
        }

    def show_list(self, list_id: str) -> JsonObject:
        return summarize_list(self._client.get_list(validate_native_id(list_id, label="LIST_ID")))

    def list_statuses(self, list_id: str) -> list[JsonObject]:
        payload = self._client.get_list(validate_native_id(list_id, label="LIST_ID"))
        return summarize_statuses(payload)

    def _candidate_list_ids(self, scope: TaskScope, *, include_archived: bool) -> list[str]:
        if scope.kind == "list":
            return [scope.resource_id]
        if scope.kind == "folder":
            lists = self._folder_lists(scope.resource_id, include_archived=include_archived)
        elif scope.kind == "space":
            lists = self._lists_in_space(scope.resource_id, include_archived=include_archived)
        else:
            lists = []
            for space in self._spaces(scope.resource_id, include_archived=include_archived):
                lists.extend(
                    self._lists_in_space(cast(str, space["id"]), include_archived=include_archived)
                )
        return sorted({cast(str, item["id"]) for item in lists})

    def _lists_in_space(self, space_id: str, *, include_archived: bool) -> list[JsonObject]:
        lists = self._space_lists(space_id, include_archived=include_archived)
        for folder in self._folders(space_id, include_archived=include_archived):
            lists.extend(
                self._folder_lists(cast(str, folder["id"]), include_archived=include_archived)
            )
        return lists

    def _resolve_assignees(self, assignees: tuple[str, ...]) -> tuple[str, ...]:
        resolved: list[str] = []
        if "me" in assignees:
            user = self._client.get_user().get("user")
            if not isinstance(user, dict):
                raise APIError("ClickUp response is missing authorized user data")
            raw_user_id = _user_id(cast(JsonObject, user).get("id"), label="user ID")
        else:
            raw_user_id = ""
        for assignee in assignees:
            resolved.append(raw_user_id if assignee == "me" else assignee)
        return tuple(sorted(set(resolved), key=lambda value: int(value)))

    def _due_bounds(self, due: DueFilter | None) -> tuple[int | None, int | None]:
        if due is None or due.kind == "none":
            return None, None
        now = self._now().astimezone(UTC)
        start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
        start_ms = int(start.timestamp() * 1_000)
        if due.kind == "overdue":
            return None, start_ms
        days = 1 if due.kind == "today" else cast(int, due.days)
        end_ms = int((start + timedelta(days=days)).timestamp() * 1_000)
        return start_ms, end_ms

    def _server_parameters(
        self,
        query: TaskQuery,
        assignees: tuple[str, ...],
        *,
        list_endpoint: bool,
        include_markdown: bool,
        archived_tasks: bool = False,
    ) -> list[tuple[str, str | int]]:
        parameters: list[tuple[str, str | int]] = []
        if list_endpoint and archived_tasks:
            parameters.append(("archived", "true"))
        if include_markdown:
            parameters.append(("include_markdown_description", "true"))
        if query.include_subtasks:
            parameters.append(("subtasks", "true"))
        parameters.extend(("statuses[]", status) for status in query.statuses)
        if query.include_closed:
            parameters.append(("include_closed", "true"))
        parameters.extend(("assignees[]", assignee) for assignee in assignees)
        parameters.extend(("tags[]", tag) for tag in query.tags)
        due_start, due_end = self._due_bounds(query.due)
        if due_start is not None:
            parameters.append(("due_date_gt", due_start - 1))
        if due_end is not None:
            parameters.append(("due_date_lt", due_end))
        return parameters

    def _task_page(self, payload: JsonObject) -> tuple[list[JsonObject], bool | None]:
        tasks = _objects(payload, "tasks", label="tasks")
        last_page = payload.get("last_page")
        if last_page is not None and not isinstance(last_page, bool):
            raise APIError("ClickUp task response contains an invalid last_page marker")
        return tasks, last_page

    def _paginate_tasks(
        self,
        fetch: Callable[[int], JsonObject],
        *,
        scope_label: str,
    ) -> list[JsonObject]:
        collected: dict[str, JsonObject] = {}
        seen_pages: set[tuple[str, ...]] = set()
        for page in range(MAX_TASK_PAGES):
            tasks, last_page = self._task_page(fetch(page))
            if not tasks:
                return list(collected.values())
            identifiers = tuple(sorted(_task_id(task) for task in tasks))
            if identifiers in seen_pages:
                raise APIError(f"ClickUp task pagination did not advance for {scope_label}")
            seen_pages.add(identifiers)
            previous_count = len(collected)
            for task in tasks:
                collected.setdefault(_task_id(task), task)
            if len(collected) == previous_count:
                raise APIError(f"ClickUp task pagination did not advance for {scope_label}")
            if len(collected) > MAX_TASK_RESULTS:
                raise APIError(
                    "ClickUp task traversal exceeded the safety ceiling of "
                    f"{MAX_TASK_RESULTS} tasks"
                )
            if last_page is True:
                return list(collected.values())
        raise APIError(f"ClickUp task pagination exceeded {MAX_TASK_PAGES} pages for {scope_label}")

    def _collect_tasks(
        self,
        query: TaskQuery,
        *,
        deep: bool,
        include_markdown: bool,
    ) -> list[JsonObject]:
        assignees = self._resolve_assignees(query.assignees)
        use_workspace_endpoint = (
            query.scope.kind == "workspace" and not deep and not query.include_archived
        )
        if use_workspace_endpoint:
            parameters = self._server_parameters(
                query,
                assignees,
                list_endpoint=False,
                include_markdown=include_markdown,
            )
            tasks = self._paginate_tasks(
                lambda page: self._client.get_workspace_tasks(
                    query.scope.resource_id,
                    page=page,
                    parameters=parameters,
                ),
                scope_label=f"Workspace {query.scope.resource_id}",
            )
        else:
            tasks_by_id: dict[str, JsonObject] = {}
            archive_states = (False, True) if query.include_archived else (False,)
            for list_id in self._candidate_list_ids(
                query.scope, include_archived=query.include_archived
            ):
                for archived_tasks in archive_states:
                    parameters = self._server_parameters(
                        query,
                        assignees,
                        list_endpoint=True,
                        include_markdown=include_markdown,
                        archived_tasks=archived_tasks,
                    )

                    def fetch_page(
                        page: int,
                        current_list_id: str = list_id,
                        current_parameters: tuple[tuple[str, str | int], ...] = tuple(parameters),
                    ) -> JsonObject:
                        return self._client.get_list_tasks(
                            current_list_id,
                            page=page,
                            parameters=current_parameters,
                        )

                    page_tasks = self._paginate_tasks(
                        fetch_page,
                        scope_label=f"List {list_id}",
                    )
                    for task in page_tasks:
                        tasks_by_id.setdefault(_task_id(task), task)
                    if len(tasks_by_id) > MAX_TASK_RESULTS:
                        raise APIError(
                            "ClickUp task traversal exceeded the safety ceiling of "
                            f"{MAX_TASK_RESULTS} tasks"
                        )
            tasks = list(tasks_by_id.values())
        return sorted(
            (task for task in tasks if self._matches_filters(task, query, assignees)),
            key=_task_id,
        )

    def _matches_filters(
        self,
        task: JsonObject,
        query: TaskQuery,
        assignees: tuple[str, ...],
    ) -> bool:
        if not query.include_archived and _archived(task):
            return False
        if not query.include_subtasks and _is_subtask(task):
            return False
        if not query.include_closed and _is_closed(task):
            return False
        if query.statuses and task_status(task).casefold() not in {
            status.casefold() for status in query.statuses
        }:
            return False
        if assignees and not {str(user_id) for user_id in task_assignee_ids(task)} & set(assignees):
            return False
        if query.tags or query.exclude_tags:
            task_tags = {tag.casefold() for tag in task_tag_names(task)}
            if query.tags and not task_tags & {tag.casefold() for tag in query.tags}:
                return False
            if task_tags & {tag.casefold() for tag in query.exclude_tags}:
                return False
        return self._matches_due(task, query.due)

    def _matches_due(self, task: JsonObject, due: DueFilter | None) -> bool:
        if due is None:
            return True
        milliseconds = task_due_date(task).milliseconds
        if due.kind == "none":
            return milliseconds is None
        if milliseconds is None:
            return False
        due_start, due_end = self._due_bounds(due)
        if due.kind == "overdue":
            return due_end is not None and milliseconds < due_end
        return due_start is not None and due_end is not None and due_start <= milliseconds < due_end

    def list_tasks(self, query: TaskQuery) -> list[JsonObject]:
        tasks = [
            summarize_task(task)
            for task in self._collect_tasks(query, deep=False, include_markdown=False)
        ]
        return tasks if query.limit is None else tasks[: query.limit]

    def search_tasks(
        self,
        query: TaskQuery,
        search_query: str,
        *,
        exact_name: bool,
        deep: bool,
    ) -> list[JsonObject]:
        needle = search_query.strip().casefold()
        if not needle:
            raise InvalidOperationError("Search query cannot be empty")
        matches = [
            task
            for task in self._collect_tasks(query, deep=deep, include_markdown=not exact_name)
            if _matches_search(task, needle, exact_name=exact_name)
        ]
        summaries = [summarize_task(task) for task in matches]
        return summaries if query.limit is None else summaries[: query.limit]

    def ensure_task(
        self,
        name: str,
        list_id: str,
        *,
        description: str | None,
        status: str | None,
        assignees: list[int] | None,
        due_date: DueDateInput | None,
        tags: list[str] | None,
    ) -> EnsureResult:
        native_list_id = validate_native_id(list_id, label="LIST_ID")
        normalized_name = name.strip()
        if not normalized_name:
            raise InvalidOperationError("Task name cannot be empty")
        normalized_status = status.strip() if status is not None else None
        if normalized_status == "":
            raise InvalidOperationError("Task status cannot be empty")
        normalized_assignees = sorted(set(assignees or []))
        if any(
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
            for user_id in normalized_assignees
        ):
            raise InvalidOperationError("Assignee IDs must be positive integers")
        normalized_tags = normalize_tags(tags)

        query = TaskQuery.from_options(
            workspace_id=None,
            space_id=None,
            folder_id=None,
            list_id=native_list_id,
            assignees=None,
            statuses=None,
            tags=None,
            exclude_tags=None,
            due=None,
            include_closed=True,
            include_subtasks=True,
            include_archived=False,
            limit=DEFAULT_TASK_LIMIT,
            all_results=True,
        )
        matches = self.search_tasks(query, normalized_name, exact_name=True, deep=False)
        if len(matches) > 1:
            candidate_ids = [cast(str, task["id"]) for task in matches]
            raise AmbiguousMatchError(
                f"Multiple tasks named {normalized_name!r} exist in List {native_list_id}",
                details={
                    "candidate_ids": cast(list[JsonValue], candidate_ids),
                    "candidates": cast(list[JsonValue], matches),
                    "list_id": native_list_id,
                },
            )
        if matches:
            return EnsureResult(created=False, task=matches[0])
        created = TaskService(self._client).create_task(
            native_list_id,
            normalized_name,
            description=description,
            status=normalized_status,
            assignees=normalized_assignees,
            due_date=due_date,
            tags=normalized_tags,
        )
        return EnsureResult(created=True, task=summarize_task(created))


def _task_id(task: JsonObject) -> str:
    return _resource_id(task.get("id"), label="task ID")


def _is_closed(task: JsonObject) -> bool:
    status = task.get("status")
    if isinstance(status, dict):
        status_type = status.get("type")
        if isinstance(status_type, str) and status_type.casefold() in _TERMINAL_STATUS_TYPES:
            return True
    date_closed = task.get("date_closed")
    return date_closed is not None and date_closed != ""


def _is_subtask(task: JsonObject) -> bool:
    parent = task.get("parent")
    if parent is None or parent == "":
        return False
    if isinstance(parent, bool) or not isinstance(parent, (str, int)):
        raise APIError("ClickUp response contains an invalid task parent")
    return True


def _matches_search(task: JsonObject, needle: str, *, exact_name: bool) -> bool:
    name = task.get("name")
    if exact_name:
        return isinstance(name, str) and name.strip().casefold() == needle
    for field in ("name", "description", "text_content", "markdown_description"):
        value = task.get(field)
        if isinstance(value, str) and needle in value.casefold():
            return True
    return False
