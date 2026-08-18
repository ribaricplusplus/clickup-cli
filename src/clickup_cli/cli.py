"""Typer adapter for the reusable ClickUp client and domain library."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

import typer
from typer._click.exceptions import ClickException
from typer.main import get_command

from clickup_cli import __version__
from clickup_cli.attachments import (
    AttachmentDownloadResult,
    AttachmentService,
    AttachmentUploadResult,
    validate_attachment_file,
)
from clickup_cli.batch import (
    BatchService,
    batch_apply_text,
    batch_plan_text,
    load_manifest,
)
from clickup_cli.client import ClickUpClient
from clickup_cli.config import DEFAULT_ENV_FILE, resolve_base_url, resolve_token
from clickup_cli.discovery import (
    DEFAULT_TASK_LIMIT,
    MAX_TASK_RESULTS,
    DiscoveryService,
    EnsureResult,
    TaskQuery,
)
from clickup_cli.domain import (
    AssignmentMutationResult,
    CommentMutationResult,
    DueDateMutationResult,
    MutationResult,
    TaskService,
    parse_due_date,
    summarize_task,
    task_status,
)
from clickup_cli.errors import (
    APIError,
    BatchPartialFailureError,
    ClickUpCLIError,
    ConfirmationError,
    CreatedButAttachmentFailedError,
    CreatedButUnverifiedError,
    InvalidOperationError,
)
from clickup_cli.refs import (
    parse_comment_ref,
    parse_task_ref,
    validate_native_id,
    validate_numeric_id,
)
from clickup_cli.task_mutations import (
    TagMutationResult,
    TaskMutationService,
    TaskUpdateRequest,
    TaskUpdateResult,
    parse_priority,
    parse_start_date,
    read_description_file,
)
from clickup_cli.time_tracking import (
    StopTimeResult,
    TimeListResult,
    TimeMutationResult,
    TimeTrackingService,
    parse_duration,
    parse_time_boundary,
    parse_time_range,
)
from clickup_cli.types import JsonObject, JsonValue

app = typer.Typer(no_args_is_help=True, help="Deterministic ClickUp operations.")
auth_app = typer.Typer(no_args_is_help=True, help="Authentication inspection.")
task_app = typer.Typer(no_args_is_help=True, help="Read and mutate ClickUp tasks.")
comment_app = typer.Typer(no_args_is_help=True, help="Show, list, and add task comments.")
due_date_app = typer.Typer(no_args_is_help=True, help="Set and clear task due dates.")
workspace_app = typer.Typer(no_args_is_help=True, help="Discover ClickUp Workspaces.")
member_app = typer.Typer(no_args_is_help=True, help="Discover Workspace members.")
list_app = typer.Typer(no_args_is_help=True, help="Inspect ClickUp Lists.")
attachment_app = typer.Typer(no_args_is_help=True, help="List, upload, and download attachments.")
priority_app = typer.Typer(no_args_is_help=True, help="Manage task priority.")
start_date_app = typer.Typer(no_args_is_help=True, help="Manage task start dates.")
tag_app = typer.Typer(no_args_is_help=True, help="Add and remove task tags.")
time_app = typer.Typer(no_args_is_help=True, help="Read and safely mutate time entries.")
batch_app = typer.Typer(no_args_is_help=True, help="Plan and apply strict task manifests.")
app.add_typer(auth_app, name="auth")
app.add_typer(task_app, name="task")
app.add_typer(workspace_app, name="workspace")
app.add_typer(member_app, name="member")
app.add_typer(list_app, name="list")
app.add_typer(time_app, name="time")
task_app.add_typer(comment_app, name="comment")
task_app.add_typer(due_date_app, name="due-date")
task_app.add_typer(attachment_app, name="attachment")
task_app.add_typer(priority_app, name="priority")
task_app.add_typer(start_date_app, name="start-date")
task_app.add_typer(tag_app, name="tag")
task_app.add_typer(batch_app, name="batch")

T = TypeVar("T")


@dataclass(frozen=True)
class AppState:
    base_url: str
    env_file: Path
    json_output: bool

    def client(self) -> ClickUpClient:
        return ClickUpClient(token=resolve_token(self.env_file), base_url=self.base_url)


def _state(context: typer.Context) -> AppState:
    state = context.find_root().obj
    if not isinstance(state, AppState):
        raise RuntimeError("CLI context was not initialized")
    return state


def _emit_json(payload: JsonObject, *, error: bool = False) -> None:
    typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")), err=error)


def _fail(state: AppState, error: ClickUpCLIError) -> None:
    if state.json_output:
        error_payload: JsonObject = {"message": str(error), "type": error.error_type}
        error_payload.update(error.details)
        _emit_json(
            {"error": error_payload, "ok": False},
            error=True,
        )
    else:
        typer.echo(f"Error: {error}", err=True)
        if isinstance(error, BatchPartialFailureError):
            typer.echo(
                "Partial outcome: "
                + json.dumps(
                    error.details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                err=True,
            )
    raise typer.Exit(code=1)


def _execute(
    state: AppState,
    operation: Callable[[], T],
    *,
    json_result: Callable[[T], JsonObject],
    text_result: Callable[[T], str],
) -> None:
    try:
        result = operation()
    except ClickUpCLIError as exc:
        _fail(state, exc)
    if state.json_output:
        _emit_json({"ok": True, "result": json_result(result)})
    else:
        typer.echo(text_result(result))


def _with_client(state: AppState, operation: Callable[[ClickUpClient], T]) -> T:
    with state.client() as client:
        return operation(client)


def _mutation_json(result: MutationResult) -> JsonObject:
    return {
        "changed": result.changed,
        "previous_status": result.previous_status,
        "status": result.status,
        "task_id": result.task_id,
    }


def _mutation_text(result: MutationResult) -> str:
    if result.changed:
        return f"{result.task_id}: {result.previous_status} -> {result.status}"
    return f"{result.task_id}: already {result.status} (no change)"


def _due_date_json(result: DueDateMutationResult) -> JsonObject:
    return {
        "changed": result.changed,
        "due_date": result.due_date,
        "due_date_ms": result.due_date_ms,
        "due_date_time": result.due_date_time,
        "previous_due_date_ms": result.previous_due_date_ms,
        "task_id": result.task_id,
    }


def _due_date_text(result: DueDateMutationResult) -> str:
    if result.due_date is None:
        return (
            f"{result.task_id}: due date cleared"
            if result.changed
            else f"{result.task_id}: already has no due date (no change)"
        )
    return (
        f"{result.task_id}: due date set to {result.due_date}"
        if result.changed
        else f"{result.task_id}: already due {result.due_date} (no change)"
    )


def _assignment_json(result: AssignmentMutationResult) -> JsonObject:
    return {
        "assigned": result.assigned,
        "assignee_ids": cast(list[JsonValue], result.assignee_ids),
        "changed": result.changed,
        "task_id": result.task_id,
        "user_id": result.user_id,
    }


def _assignment_text(result: AssignmentMutationResult) -> str:
    action = "assigned" if result.assigned else "unassigned"
    if result.changed:
        return f"{result.task_id}: {action} user {result.user_id}"
    return f"{result.task_id}: user {result.user_id} already {action} (no change)"


def _comment_json(result: CommentMutationResult) -> JsonObject:
    return {"comment": result.comment, "task_id": result.task_id}


def _task_update_json(result: TaskUpdateResult) -> JsonObject:
    return {
        "changed": result.changed,
        "fields": cast(list[JsonValue], result.fields),
        "task": summarize_task(result.task),
        "task_id": result.task_id,
    }


def _task_update_text(result: TaskUpdateResult) -> str:
    if result.changed:
        return f"{result.task_id}: updated {', '.join(result.fields)}"
    return f"{result.task_id}: requested state already present (no change)"


def _tag_json(result: TagMutationResult) -> JsonObject:
    return {
        "added": result.added,
        "changed": result.changed,
        "tag": result.tag,
        "tags": cast(list[JsonValue], result.tags),
        "task_id": result.task_id,
    }


def _attachment_upload_json(result: AttachmentUploadResult) -> JsonObject:
    return {"attachment": result.attachment, "task_id": result.task_id}


def _attachment_download_json(result: AttachmentDownloadResult) -> JsonObject:
    return {
        "attachment_id": result.attachment_id,
        "output": result.output,
        "size": result.size,
        "task_id": result.task_id,
    }


def _time_mutation_json(result: TimeMutationResult) -> JsonObject:
    return {"changed": result.changed, "entry": result.entry}


def _time_entry_text(entry: JsonObject) -> str:
    duration = entry.get("duration_ms")
    state = (
        "running"
        if entry.get("running")
        else f"{duration}ms"
        if isinstance(duration, int) and not isinstance(duration, bool)
        else "duration=unknown"
    )
    task = f" task={entry['task_id']}" if entry.get("task_id") else ""
    description = f" {entry['description']}" if entry.get("description") else ""
    return f"{entry.get('id')} {state}{task}{description}"


def _billable_value(*, billable: bool, non_billable: bool) -> bool | None:
    if billable and non_billable:
        raise InvalidOperationError("--billable and --non-billable cannot be used together")
    if billable:
        return True
    if non_billable:
        return False
    return None


def _positive_assignee(user_id: int | None) -> int | None:
    if user_id is not None and (isinstance(user_id, bool) or user_id <= 0):
        raise InvalidOperationError("ASSIGNEE must be a positive integer")
    return user_id


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"clickup {__version__}")
        raise typer.Exit()


def _json_requested(arguments: list[str]) -> bool:
    for argument in arguments:
        if argument == "--":
            return False
        if argument == "--json":
            return True
    return False


@app.callback()
def configure(
    context: typer.Context,
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON output."),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Override CLICKUP_API_BASE_URL.",
        metavar="URL",
    ),
    env_file: Path = typer.Option(
        DEFAULT_ENV_FILE,
        "--env-file",
        help="Dotenv file used after CLICKUP_API_TOKEN.",
        metavar="PATH",
    ),
) -> None:
    """Configure output and direct API access."""

    try:
        resolved_base_url = resolve_base_url(base_url)
    except ClickUpCLIError as exc:
        provisional = AppState(base_url="", env_file=env_file, json_output=json_output)
        _fail(provisional, exc)
    context.obj = AppState(
        base_url=resolved_base_url,
        env_file=env_file,
        json_output=json_output,
    )


@auth_app.command("whoami")
def whoami(context: typer.Context) -> None:
    """Show the user associated with the configured personal token."""

    state = _state(context)

    def operation() -> JsonObject:
        payload = _with_client(state, lambda client: client.get_user())
        user = payload.get("user")
        if not isinstance(user, dict):
            raise APIError("ClickUp response is missing user data")
        return {
            "email": str(user["email"]) if isinstance(user.get("email"), str) else None,
            "id": str(user["id"]) if isinstance(user.get("id"), (str, int)) else None,
            "username": (str(user["username"]) if isinstance(user.get("username"), str) else None),
        }

    def text(user: JsonObject) -> str:
        username = user.get("username") or "unknown user"
        email = f" <{user['email']}>" if user.get("email") else ""
        identifier = f" [{user['id']}]" if user.get("id") else ""
        return f"{username}{email}{identifier}"

    _execute(
        state,
        operation,
        json_result=lambda user: {"user": user},
        text_result=text,
    )


def _catalog_text(resources: list[JsonObject], *, empty: str) -> str:
    if not resources:
        return empty
    return "\n".join(f"{item.get('id')} {item.get('name') or ''}".rstrip() for item in resources)


@workspace_app.command("list")
def list_workspaces(context: typer.Context) -> None:
    """List authorized Workspaces without exposing embedded member data."""

    state = _state(context)
    _execute(
        state,
        lambda: _with_client(state, lambda client: DiscoveryService(client).list_workspaces()),
        json_result=lambda workspaces: {"workspaces": cast(list[JsonValue], workspaces)},
        text_result=lambda workspaces: _catalog_text(workspaces, empty="No workspaces"),
    )


def _tree_text(workspace: JsonObject) -> str:
    lines = [f"{workspace.get('id')} {workspace.get('name') or ''}".rstrip()]
    spaces = workspace.get("spaces")
    if isinstance(spaces, list):
        for space in spaces:
            if not isinstance(space, dict):
                continue
            lines.append(f"  {space.get('id')} {space.get('name') or ''}".rstrip())
            folders = space.get("folders")
            if isinstance(folders, list):
                for folder in folders:
                    if not isinstance(folder, dict):
                        continue
                    lines.append(f"    {folder.get('id')} {folder.get('name') or ''}".rstrip())
                    folder_lists = folder.get("lists")
                    if isinstance(folder_lists, list):
                        for item in folder_lists:
                            if isinstance(item, dict):
                                lines.append(
                                    f"      {item.get('id')} {item.get('name') or ''}".rstrip()
                                )
            folderless_lists = space.get("lists")
            if isinstance(folderless_lists, list):
                for item in folderless_lists:
                    if isinstance(item, dict):
                        lines.append(f"    {item.get('id')} {item.get('name') or ''}".rstrip())
    return "\n".join(lines)


@workspace_app.command("tree")
def workspace_tree(
    context: typer.Context,
    workspace_id: str = typer.Argument(..., metavar="WORKSPACE_ID"),
    include_archived: bool = typer.Option(False, "--include-archived"),
) -> None:
    """Show a normalized Workspace, Space, Folder, and List tree."""

    state = _state(context)
    _execute(
        state,
        lambda: _with_client(
            state,
            lambda client: DiscoveryService(client).workspace_tree(
                workspace_id, include_archived=include_archived
            ),
        ),
        json_result=lambda workspace: {"workspace": workspace},
        text_result=_tree_text,
    )


@member_app.command("list")
def list_members(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
) -> None:
    """List the minimal identities of members in one Workspace."""

    state = _state(context)

    def text(members: list[JsonObject]) -> str:
        if not members:
            return "No members"
        return "\n".join(
            (
                f"{member.get('id')} {member.get('username') or ''}"
                + (f" <{member['email']}>" if member.get("email") else "")
            ).rstrip()
            for member in members
        )

    _execute(
        state,
        lambda: _with_client(
            state, lambda client: DiscoveryService(client).list_members(workspace_id)
        ),
        json_result=lambda members: {
            "members": cast(list[JsonValue], members),
            "workspace_id": workspace_id,
        },
        text_result=text,
    )


@list_app.command("show")
def show_list(
    context: typer.Context,
    list_id: str = typer.Argument(..., metavar="LIST_ID"),
) -> None:
    """Show a List in a stable normalized shape."""

    state = _state(context)

    def text(item: JsonObject) -> str:
        return "\n".join(
            (
                f"ID: {item.get('id') or ''}",
                f"Name: {item.get('name') or ''}",
                f"Space: {item.get('space_name') or item.get('space_id') or ''}",
                f"Folder: {item.get('folder_name') or item.get('folder_id') or ''}",
                f"Archived: {str(bool(item.get('archived'))).lower()}",
            )
        )

    _execute(
        state,
        lambda: _with_client(state, lambda client: DiscoveryService(client).show_list(list_id)),
        json_result=lambda item: {"list": item},
        text_result=text,
    )


@list_app.command("statuses")
def list_list_statuses(
    context: typer.Context,
    list_id: str = typer.Argument(..., metavar="LIST_ID"),
) -> None:
    """List normalized status labels and types for a List."""

    state = _state(context)

    def text(statuses: list[JsonObject]) -> str:
        if not statuses:
            return "No statuses"
        return "\n".join(
            f"{status.get('status')} ({status.get('type') or 'unknown'})" for status in statuses
        )

    _execute(
        state,
        lambda: _with_client(state, lambda client: DiscoveryService(client).list_statuses(list_id)),
        json_result=lambda statuses: {
            "list_id": list_id,
            "statuses": cast(list[JsonValue], statuses),
        },
        text_result=text,
    )


def _task_query(
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
    return TaskQuery.from_options(
        workspace_id=workspace_id,
        space_id=space_id,
        folder_id=folder_id,
        list_id=list_id,
        assignees=assignees,
        statuses=statuses,
        tags=tags,
        exclude_tags=exclude_tags,
        due=due,
        include_closed=include_closed,
        include_subtasks=include_subtasks,
        include_archived=include_archived,
        limit=limit,
        all_results=all_results,
    )


def _tasks_text(tasks: list[JsonObject]) -> str:
    if not tasks:
        return "No tasks"
    return "\n".join(
        f"{task.get('id')} [{task.get('status') or 'unknown'}] {task.get('name') or ''}".rstrip()
        for task in tasks
    )


@task_app.command("list")
def list_tasks(
    context: typer.Context,
    workspace_id: str | None = typer.Option(None, "--workspace-id", metavar="WORKSPACE_ID"),
    space_id: str | None = typer.Option(None, "--space-id", metavar="SPACE_ID"),
    folder_id: str | None = typer.Option(None, "--folder-id", metavar="FOLDER_ID"),
    list_id: str | None = typer.Option(None, "--list-id", metavar="LIST_ID"),
    assignees: list[str] | None = typer.Option(None, "--assignee", metavar="me|USER_ID"),
    statuses: list[str] | None = typer.Option(None, "--status", metavar="STATUS"),
    tags: list[str] | None = typer.Option(None, "--tag", metavar="TAG"),
    exclude_tags: list[str] | None = typer.Option(None, "--exclude-tag", metavar="TAG"),
    due: str | None = typer.Option(None, "--due", metavar="DUE_FILTER"),
    include_closed: bool = typer.Option(False, "--include-closed"),
    include_subtasks: bool = typer.Option(False, "--include-subtasks"),
    include_archived: bool = typer.Option(False, "--include-archived"),
    limit: int = typer.Option(
        DEFAULT_TASK_LIMIT,
        "--limit",
        min=1,
        max=MAX_TASK_RESULTS,
        metavar="N",
    ),
    all_results: bool = typer.Option(
        False, "--all", help="Return all results within safety limits."
    ),
) -> None:
    """List tasks from exactly one scope with consistent local filtering."""

    state = _state(context)

    def operation() -> list[JsonObject]:
        query = _task_query(
            workspace_id=workspace_id,
            space_id=space_id,
            folder_id=folder_id,
            list_id=list_id,
            assignees=assignees,
            statuses=statuses,
            tags=tags,
            exclude_tags=exclude_tags,
            due=due,
            include_closed=include_closed,
            include_subtasks=include_subtasks,
            include_archived=include_archived,
            limit=limit,
            all_results=all_results,
        )
        return _with_client(state, lambda client: DiscoveryService(client).list_tasks(query))

    _execute(
        state,
        operation,
        json_result=lambda tasks: {"tasks": cast(list[JsonValue], tasks)},
        text_result=_tasks_text,
    )


@task_app.command("search")
def search_tasks(
    context: typer.Context,
    query_text: str = typer.Argument(..., metavar="QUERY"),
    workspace_id: str | None = typer.Option(None, "--workspace-id", metavar="WORKSPACE_ID"),
    space_id: str | None = typer.Option(None, "--space-id", metavar="SPACE_ID"),
    folder_id: str | None = typer.Option(None, "--folder-id", metavar="FOLDER_ID"),
    list_id: str | None = typer.Option(None, "--list-id", metavar="LIST_ID"),
    assignees: list[str] | None = typer.Option(None, "--assignee", metavar="me|USER_ID"),
    statuses: list[str] | None = typer.Option(None, "--status", metavar="STATUS"),
    tags: list[str] | None = typer.Option(None, "--tag", metavar="TAG"),
    exclude_tags: list[str] | None = typer.Option(None, "--exclude-tag", metavar="TAG"),
    due: str | None = typer.Option(None, "--due", metavar="DUE_FILTER"),
    include_closed: bool = typer.Option(False, "--include-closed"),
    include_subtasks: bool = typer.Option(False, "--include-subtasks"),
    include_archived: bool = typer.Option(False, "--include-archived"),
    limit: int = typer.Option(
        DEFAULT_TASK_LIMIT,
        "--limit",
        min=1,
        max=MAX_TASK_RESULTS,
        metavar="N",
    ),
    all_results: bool = typer.Option(
        False, "--all", help="Return all results within safety limits."
    ),
    exact_name: bool = typer.Option(False, "--exact-name"),
    deep: bool = typer.Option(False, "--deep"),
) -> None:
    """Search task names and descriptions case-insensitively."""

    state = _state(context)

    def operation() -> list[JsonObject]:
        task_query = _task_query(
            workspace_id=workspace_id,
            space_id=space_id,
            folder_id=folder_id,
            list_id=list_id,
            assignees=assignees,
            statuses=statuses,
            tags=tags,
            exclude_tags=exclude_tags,
            due=due,
            include_closed=include_closed,
            include_subtasks=include_subtasks,
            include_archived=include_archived,
            limit=limit,
            all_results=all_results,
        )
        return _with_client(
            state,
            lambda client: DiscoveryService(client).search_tasks(
                task_query,
                query_text,
                exact_name=exact_name,
                deep=deep,
            ),
        )

    _execute(
        state,
        operation,
        json_result=lambda tasks: {
            "query": query_text,
            "tasks": cast(list[JsonValue], tasks),
        },
        text_result=_tasks_text,
    )


def _ensure_json(result: EnsureResult) -> JsonObject:
    return {"created": result.created, "task": result.task}


@task_app.command("ensure")
def ensure_task(
    context: typer.Context,
    name: str = typer.Argument(..., metavar="NAME"),
    list_id: str = typer.Option(..., "--list-id", metavar="LIST_ID"),
    description: str | None = typer.Option(None, "--description"),
    status: str | None = typer.Option(None, "--status"),
    assignees: list[int] | None = typer.Option(None, "--assignee", metavar="USER_ID"),
    due_at: str | None = typer.Option(
        None,
        "--due-date",
        metavar="DUE_AT",
        help="YYYY-MM-DD or timezone-aware ISO 8601 timestamp.",
    ),
    tags: list[str] | None = typer.Option(None, "--tag", metavar="TAG"),
) -> None:
    """Return one exact-name List task or create it through verified creation."""

    state = _state(context)

    def operation() -> EnsureResult:
        native_list_id = validate_native_id(list_id, label="LIST_ID")
        requested_due_date = parse_due_date(due_at) if due_at is not None else None
        return _with_client(
            state,
            lambda client: DiscoveryService(client).ensure_task(
                name,
                native_list_id,
                description=description,
                status=status,
                assignees=assignees,
                due_date=requested_due_date,
                tags=tags,
            ),
        )

    _execute(
        state,
        operation,
        json_result=_ensure_json,
        text_result=lambda result: (
            f"Created {result.task.get('id')}: {result.task.get('name')}"
            if result.created
            else f"Found {result.task.get('id')}: {result.task.get('name')} (no change)"
        ),
    )


@task_app.command("show")
def show_task(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """Show a task in a stable normalized shape."""

    state = _state(context)

    def operation() -> JsonObject:
        task_id = parse_task_ref(task_ref)
        task = _with_client(state, lambda client: client.get_task(task_id))
        return summarize_task(task)

    def text(task: JsonObject) -> str:
        raw_assignees = task.get("assignees")
        assignee_labels: list[str] = []
        if isinstance(raw_assignees, list):
            for raw_assignee in raw_assignees:
                if isinstance(raw_assignee, dict):
                    label = raw_assignee.get("username") or raw_assignee.get("id")
                    if label is not None:
                        assignee_labels.append(str(label))
        raw_tags = task.get("tags")
        tag_labels = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
        return "\n".join(
            (
                f"ID: {task.get('id') or ''}",
                f"Name: {task.get('name') or ''}",
                f"Status: {task.get('status') or ''}",
                f"Archived: {task.get('archived') if task.get('archived') is not None else ''}",
                f"Priority: {task.get('priority') or ''}",
                f"List: {task.get('list_name') or task.get('list_id') or ''}",
                f"Due: {task.get('due_date') or ''}",
                f"Start: {task.get('start_date') or ''}",
                f"Assignees: {', '.join(assignee_labels)}",
                f"Tags: {', '.join(tag_labels)}",
                f"Attachments: {len(cast(list[JsonValue], task.get('attachments') or []))}",
                f"URL: {task.get('url') or ''}",
            )
        )

    _execute(
        state,
        operation,
        json_result=lambda task: {"task": task},
        text_result=text,
    )


@batch_app.command("plan")
def plan_task_batch(
    context: typer.Context,
    manifest_path: Path = typer.Argument(..., metavar="MANIFEST.jsonl"),
) -> None:
    """Validate and plan a bounded JSONL manifest without performing writes."""

    state = _state(context)

    def operation() -> JsonObject:
        manifest = load_manifest(manifest_path)
        return _with_client(state, lambda client: BatchService(client).plan(manifest))

    _execute(
        state,
        operation,
        json_result=lambda result: result,
        text_result=batch_plan_text,
    )


@batch_app.command("apply")
def apply_task_batch(
    context: typer.Context,
    manifest_path: Path = typer.Argument(..., metavar="MANIFEST.jsonl"),
    yes: bool = typer.Option(False, "--yes", help="Confirm batch task mutations."),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error",
        help="Continue with later tasks after an operation failure.",
    ),
) -> None:
    """Preflight the complete manifest, then apply verified operations serially."""

    state = _state(context)

    def operation() -> JsonObject:
        if not yes:
            raise ConfirmationError("Batch apply requires --yes")
        manifest = load_manifest(manifest_path)
        return _with_client(
            state,
            lambda client: BatchService(client).apply(
                manifest,
                continue_on_error=continue_on_error,
            ),
        )

    _execute(
        state,
        operation,
        json_result=lambda result: result,
        text_result=batch_apply_text,
    )


@task_app.command("status")
def show_status(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """Show only a task's current status label."""

    state = _state(context)

    def operation() -> tuple[str, str]:
        task_id = parse_task_ref(task_ref)
        task = _with_client(state, lambda client: client.get_task(task_id))
        return task_id, task_status(task)

    _execute(
        state,
        operation,
        json_result=lambda result: {"status": result[1], "task_id": result[0]},
        text_result=lambda result: result[1],
    )


@task_app.command("set-status")
def set_status(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    status: str = typer.Argument(..., metavar="STATUS"),
) -> None:
    """Set a valid list status with a minimal write and verified readback."""

    state = _state(context)

    def operation() -> MutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).set_status(task_id, status))

    _execute(state, operation, json_result=_mutation_json, text_result=_mutation_text)


@task_app.command("complete")
def complete_task(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """Select the highest-priority semantic completion status."""

    state = _state(context)

    def operation() -> MutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).complete(task_id))

    _execute(state, operation, json_result=_mutation_json, text_result=_mutation_text)


@comment_app.command("show")
def show_comment(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    comment_id: str | None = typer.Argument(None, metavar="COMMENT_ID"),
) -> None:
    """Show one comment by explicit ID or directly from a ClickUp comment URL."""

    state = _state(context)

    def operation() -> CommentMutationResult:
        task_id, native_comment_id = parse_comment_ref(task_ref, comment_id)
        return _with_client(
            state,
            lambda client: TaskService(client).get_comment(task_id, native_comment_id),
        )

    def text(result: CommentMutationResult) -> str:
        author = result.comment.get("username") or result.comment.get("user_id") or "unknown"
        return f"{result.comment.get('id')} {author}: {result.comment.get('text') or ''}"

    _execute(state, operation, json_result=_comment_json, text_result=text)


@comment_app.command("list")
def list_comments(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """List the latest task comments in a stable normalized shape."""

    state = _state(context)

    def operation() -> tuple[str, list[JsonObject]]:
        task_id = parse_task_ref(task_ref)
        comments = _with_client(state, lambda client: TaskService(client).list_comments(task_id))
        return task_id, comments

    def text(result: tuple[str, list[JsonObject]]) -> str:
        comments = result[1]
        if not comments:
            return "No comments"
        lines: list[str] = []
        for comment in comments:
            author = comment.get("username") or comment.get("user_id") or "unknown"
            lines.append(f"{comment.get('id')} {author}: {comment.get('text') or ''}")
        return "\n".join(lines)

    _execute(
        state,
        operation,
        json_result=lambda result: {
            "comments": cast(list[JsonValue], result[1]),
            "task_id": result[0],
        },
        text_result=text,
    )


@comment_app.command("add")
def add_comment(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    text: str = typer.Argument(..., metavar="TEXT"),
) -> None:
    """Add a plain-text task comment and verify it by readback."""

    state = _state(context)

    def operation() -> CommentMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).add_comment(task_id, text))

    _execute(
        state,
        operation,
        json_result=_comment_json,
        text_result=lambda result: f"Added comment {result.comment.get('id')} to {result.task_id}",
    )


@due_date_app.command("set")
def set_due_date(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    due_at: str = typer.Argument(..., metavar="DUE_AT"),
) -> None:
    """Set YYYY-MM-DD or a timezone-aware ISO timestamp and verify readback."""

    state = _state(context)

    def operation() -> DueDateMutationResult:
        task_id = parse_task_ref(task_ref)
        requested = parse_due_date(due_at)
        return _with_client(
            state, lambda client: TaskService(client).set_due_date(task_id, requested)
        )

    _execute(state, operation, json_result=_due_date_json, text_result=_due_date_text)


@due_date_app.command("clear")
def clear_due_date(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """Clear a task due date with a minimal update and verified readback."""

    state = _state(context)

    def operation() -> DueDateMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).clear_due_date(task_id))

    _execute(state, operation, json_result=_due_date_json, text_result=_due_date_text)


@task_app.command("assign")
def assign_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    user_id: int = typer.Argument(..., metavar="USER_ID"),
) -> None:
    """Assign one user with an idempotent minimal update and verified readback."""

    state = _state(context)

    def operation() -> AssignmentMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).assign(task_id, user_id))

    _execute(state, operation, json_result=_assignment_json, text_result=_assignment_text)


@task_app.command("unassign")
def unassign_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    user_id: int = typer.Argument(..., metavar="USER_ID"),
) -> None:
    """Unassign one user with an idempotent minimal update and verified readback."""

    state = _state(context)

    def operation() -> AssignmentMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).unassign(task_id, user_id))

    _execute(state, operation, json_result=_assignment_json, text_result=_assignment_text)


@task_app.command("update")
def update_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    name: str | None = typer.Option(None, "--name", metavar="TEXT"),
    description: str | None = typer.Option(None, "--description", metavar="TEXT"),
    description_file: Path | None = typer.Option(None, "--description-file", metavar="PATH"),
    priority: str | None = typer.Option(None, "--priority", metavar="PRIORITY"),
    start_date: str | None = typer.Option(None, "--start-date", metavar="START_AT"),
    clear_start_date: bool = typer.Option(False, "--clear-start-date"),
) -> None:
    """Update explicitly supplied fields with one minimal write and one readback."""

    state = _state(context)

    def operation() -> TaskUpdateResult:
        if description is not None and description_file is not None:
            raise InvalidOperationError("Use exactly one of --description and --description-file")
        if start_date is not None and clear_start_date:
            raise InvalidOperationError("Cannot use --start-date and --clear-start-date together")
        resolved_description = (
            read_description_file(description_file) if description_file is not None else description
        )
        resolved_priority = parse_priority(priority) if priority is not None else None
        resolved_start_date = parse_start_date(start_date) if start_date is not None else None
        request = TaskUpdateRequest(
            name=name,
            description=resolved_description,
            description_supplied=description is not None or description_file is not None,
            priority=resolved_priority,
            priority_supplied=priority is not None,
            start_date=resolved_start_date,
            clear_start_date=clear_start_date,
        )
        task_id = parse_task_ref(task_ref)
        return _with_client(
            state,
            lambda client: TaskMutationService(client).update(task_id, request),
        )

    _execute(state, operation, json_result=_task_update_json, text_result=_task_update_text)


@priority_app.command("clear")
def clear_priority(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
) -> None:
    """Clear task priority idempotently and verify the readback."""

    state = _state(context)

    def operation() -> TaskUpdateResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(
            state,
            lambda client: TaskMutationService(client).clear_priority(task_id),
        )

    _execute(state, operation, json_result=_task_update_json, text_result=_task_update_text)


@start_date_app.command("clear")
def clear_start_date(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
) -> None:
    """Clear a task start date idempotently and verify the readback."""

    state = _state(context)

    def operation() -> TaskUpdateResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(
            state,
            lambda client: TaskMutationService(client).clear_start_date(task_id),
        )

    _execute(state, operation, json_result=_task_update_json, text_result=_task_update_text)


def _set_archived(context: typer.Context, task_ref: str, *, archived: bool) -> None:
    state = _state(context)

    def operation() -> TaskUpdateResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(
            state,
            lambda client: TaskMutationService(client).set_archived(task_id, archived=archived),
        )

    _execute(state, operation, json_result=_task_update_json, text_result=_task_update_text)


@task_app.command("archive")
def archive_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
) -> None:
    """Archive a task through a reversible verified lifecycle update."""

    _set_archived(context, task_ref, archived=True)


@task_app.command("unarchive")
def unarchive_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
) -> None:
    """Unarchive a task through a reversible verified lifecycle update."""

    _set_archived(context, task_ref, archived=False)


def _set_tag(context: typer.Context, task_ref: str, tag: str, *, add: bool) -> None:
    state = _state(context)

    def operation() -> TagMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(
            state,
            lambda client: TaskMutationService(client).set_tag(task_id, tag, add=add),
        )

    action = "Added" if add else "Removed"
    _execute(
        state,
        operation,
        json_result=_tag_json,
        text_result=lambda result: (
            f"{action} tag {result.tag!r} on {result.task_id}"
            if result.changed
            else f"{result.task_id}: tag {result.tag!r} already in requested state (no change)"
        ),
    )


@tag_app.command("add")
def add_tag(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    tag: str = typer.Argument(..., metavar="TAG"),
) -> None:
    """Add a tag idempotently with safe path encoding and verified readback."""

    _set_tag(context, task_ref, tag, add=True)


@tag_app.command("remove")
def remove_tag(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    tag: str = typer.Argument(..., metavar="TAG"),
) -> None:
    """Remove a tag idempotently with safe path encoding and verified readback."""

    _set_tag(context, task_ref, tag, add=False)


@attachment_app.command("list")
def list_attachments(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
) -> None:
    """List task attachments in a stable normalized shape."""

    state = _state(context)

    def operation() -> tuple[str, list[JsonObject]]:
        task_id = parse_task_ref(task_ref)
        attachments = _with_client(
            state,
            lambda client: AttachmentService(client).list(task_id),
        )
        return task_id, attachments

    def text(result: tuple[str, list[JsonObject]]) -> str:
        if not result[1]:
            return "No attachments"
        return "\n".join(
            f"{attachment.get('id') or ''} {attachment.get('title') or ''}"
            for attachment in result[1]
        )

    _execute(
        state,
        operation,
        json_result=lambda result: {
            "attachments": cast(list[JsonValue], result[1]),
            "task_id": result[0],
        },
        text_result=text,
    )


@attachment_app.command("upload")
def upload_attachment(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    path: Path = typer.Argument(..., metavar="PATH"),
    name: str | None = typer.Option(None, "--name", metavar="NAME"),
) -> None:
    """Upload one regular readable file and verify it on the task."""

    state = _state(context)

    def operation() -> AttachmentUploadResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(
            state,
            lambda client: AttachmentService(client).upload(task_id, path, name=name),
        )

    _execute(
        state,
        operation,
        json_result=_attachment_upload_json,
        text_result=lambda result: f"Uploaded {result.attachment.get('id')} to {result.task_id}",
    )


@attachment_app.command("download")
def download_attachment(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    attachment_id: str = typer.Argument(..., metavar="ATTACHMENT_ID"),
    output: Path = typer.Option(..., "--output", metavar="PATH"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Download a fetched-task attachment atomically without forwarding authorization."""

    state = _state(context)

    def operation() -> AttachmentDownloadResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(
            state,
            lambda client: AttachmentService(client).download(
                task_id,
                attachment_id,
                output,
                force=force,
            ),
        )

    _execute(
        state,
        operation,
        json_result=_attachment_download_json,
        text_result=lambda result: (
            f"Downloaded {result.attachment_id} to {result.output} ({result.size} bytes)"
        ),
    )


@task_app.command("create")
def create_task(
    context: typer.Context,
    name: str = typer.Argument(..., metavar="NAME"),
    list_id: str = typer.Option(..., "--list-id", metavar="LIST_ID"),
    description: str | None = typer.Option(None, "--description"),
    status: str | None = typer.Option(None, "--status"),
    assignees: list[int] | None = typer.Option(None, "--assignee", metavar="USER_ID"),
    due_at: str | None = typer.Option(
        None,
        "--due-date",
        metavar="DUE_AT",
        help="YYYY-MM-DD or timezone-aware ISO 8601 timestamp.",
    ),
    tags: list[str] | None = typer.Option(None, "--tag", metavar="TAG"),
    attachments: list[Path] | None = typer.Option(None, "--attach", metavar="PATH"),
) -> None:
    """Create and read back a task with only explicitly supplied supported fields."""

    state = _state(context)

    def operation() -> JsonObject:
        native_list_id = validate_native_id(list_id, label="LIST_ID")
        requested_due_date = parse_due_date(due_at) if due_at is not None else None
        requested_attachments = list(attachments or [])
        for path in requested_attachments:
            validate_attachment_file(path)

        def create_and_upload(client: ClickUpClient) -> JsonObject:
            try:
                task = TaskService(client).create_task(
                    native_list_id,
                    name,
                    description=description,
                    status=status,
                    assignees=assignees,
                    due_date=requested_due_date,
                    tags=tags,
                )
            except CreatedButUnverifiedError as exc:
                task_id = exc.details.get("task_id")
                if requested_attachments and isinstance(task_id, str):
                    raise CreatedButAttachmentFailedError(
                        "Task was created, but attachment processing could not begin safely",
                        details={
                            "failed_path": str(requested_attachments[0]),
                            "task_id": task_id,
                            "uploaded_attachment_ids": [],
                        },
                    ) from exc
                raise

            task_id_value = task.get("id")
            if not isinstance(task_id_value, (str, int)) or isinstance(task_id_value, bool):
                raise APIError("Verified task is missing its ID")
            task_id = str(task_id_value)
            uploaded_ids: list[str] = []
            attachment_service = AttachmentService(client)
            for path in requested_attachments:
                try:
                    upload = attachment_service.upload(task_id, path)
                except ClickUpCLIError as exc:
                    raise CreatedButAttachmentFailedError(
                        "Task was created, but a requested attachment failed; "
                        "inspect the task before retrying the attachment",
                        details={
                            "failed_path": str(path),
                            "task_id": task_id,
                            "uploaded_attachment_ids": cast(list[JsonValue], uploaded_ids),
                        },
                    ) from exc
                attachment_id = upload.attachment.get("id")
                if not isinstance(attachment_id, str):
                    raise RuntimeError("Verified attachment is missing its ID")
                uploaded_ids.append(attachment_id)
                task = upload.task
            return task

        task = _with_client(state, create_and_upload)
        return summarize_task(task)

    _execute(
        state,
        operation,
        json_result=lambda task: {"task": task},
        text_result=lambda task: f"Created {task.get('id')}: {task.get('name')}",
    )


@task_app.command("delete")
def delete_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    yes: bool = typer.Option(False, "--yes", help="Confirm permanent deletion."),
) -> None:
    """Permanently delete a task after explicit confirmation."""

    state = _state(context)
    if not yes:
        _fail(state, ConfirmationError("Refusing to delete without --yes"))

    def operation() -> str:
        task_id = parse_task_ref(task_ref)

        def remove(client: ClickUpClient) -> str:
            client.delete_task(task_id)
            return task_id

        return _with_client(state, remove)

    _execute(
        state,
        operation,
        json_result=lambda task_id: {"deleted": True, "task_id": task_id},
        text_result=lambda task_id: f"Deleted {task_id}",
    )


@time_app.command("current")
def current_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    assignee: int | None = typer.Option(None, "--assignee", metavar="USER_ID"),
) -> None:
    """Show the authenticated user's current timer, or another user's when authorized."""

    state = _state(context)

    def operation() -> JsonObject | None:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        user_id = _positive_assignee(assignee)
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).current(
                native_workspace_id, assignee=user_id
            ),
        )

    _execute(
        state,
        operation,
        json_result=lambda entry: {"entry": entry},
        text_result=lambda entry: (
            _time_entry_text(entry) if entry is not None else "No running timer"
        ),
    )


@time_app.command("list")
def list_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    from_value: str = typer.Option(
        ..., "--from", metavar="FROM", help="Inclusive date or timezone-aware timestamp."
    ),
    to_value: str = typer.Option(
        ..., "--to", metavar="TO", help="Exclusive date or timezone-aware timestamp."
    ),
    assignee: int | None = typer.Option(None, "--assignee", metavar="USER_ID"),
    task: str | None = typer.Option(None, "--task", metavar="TASK_REF"),
    space_id: str | None = typer.Option(None, "--space-id", metavar="SPACE_ID"),
    folder_id: str | None = typer.Option(None, "--folder-id", metavar="FOLDER_ID"),
    list_id: str | None = typer.Option(None, "--list-id", metavar="LIST_ID"),
    billable: bool = typer.Option(False, "--billable", help="Only billable entries."),
    non_billable: bool = typer.Option(False, "--non-billable", help="Only non-billable entries."),
) -> None:
    """List entries in a bounded start-inclusive, end-exclusive interval."""

    state = _state(context)

    def operation() -> TimeListResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        locations = [task, space_id, folder_id, list_id]
        if sum(value is not None for value in locations) > 1:
            raise InvalidOperationError(
                "Only one of --task, --space-id, --folder-id, or --list-id may be used"
            )
        requested_range = parse_time_range(from_value, to_value)
        task_id = parse_task_ref(task) if task is not None else None
        native_space_id = (
            validate_numeric_id(space_id, label="SPACE_ID") if space_id is not None else None
        )
        native_folder_id = (
            validate_numeric_id(folder_id, label="FOLDER_ID") if folder_id is not None else None
        )
        native_list_id = (
            validate_numeric_id(list_id, label="LIST_ID") if list_id is not None else None
        )
        user_id = _positive_assignee(assignee)
        billable_state = _billable_value(billable=billable, non_billable=non_billable)
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).list_entries(
                native_workspace_id,
                requested_range,
                assignee=user_id,
                task_id=task_id,
                space_id=native_space_id,
                folder_id=native_folder_id,
                list_id=native_list_id,
                billable=billable_state,
            ),
        )

    def json_result(result: TimeListResult) -> JsonObject:
        return {
            "entries": cast(list[JsonValue], result.entries),
            "from_ms": result.start_ms,
            "range_semantics": "start-inclusive,end-exclusive",
            "to_ms": result.end_ms,
            "workspace_id": result.workspace_id,
        }

    def text_result(result: TimeListResult) -> str:
        if not result.entries:
            return "No time entries"
        return "\n".join(_time_entry_text(entry) for entry in result.entries)

    _execute(state, operation, json_result=json_result, text_result=text_result)


@time_app.command("start")
def start_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    task: str | None = typer.Option(None, "--task", metavar="TASK_REF"),
    description: str | None = typer.Option(None, "--description", metavar="TEXT"),
    billable: bool = typer.Option(False, "--billable"),
    non_billable: bool = typer.Option(False, "--non-billable"),
) -> None:
    """Start one timer after proving that no timer is already running."""

    state = _state(context)

    def operation() -> TimeMutationResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        task_id = parse_task_ref(task) if task is not None else None
        billable_state = _billable_value(billable=billable, non_billable=non_billable)
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).start(
                native_workspace_id,
                task_id=task_id,
                description=description,
                billable=billable_state,
            ),
        )

    _execute(
        state,
        operation,
        json_result=_time_mutation_json,
        text_result=lambda result: f"Started {result.entry.get('id')}",
    )


@time_app.command("stop")
def stop_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
) -> None:
    """Stop and verify the current timer, or return an idempotent no-op."""

    state = _state(context)

    def operation() -> StopTimeResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        return _with_client(
            state, lambda client: TimeTrackingService(client).stop(native_workspace_id)
        )

    _execute(
        state,
        operation,
        json_result=lambda result: {
            "entry": result.entry,
            "entry_id": result.entry_id,
            "stopped": result.stopped,
        },
        text_result=lambda result: (
            f"Stopped {result.entry_id}" if result.stopped else "No running timer (no change)"
        ),
    )


@time_app.command("add")
def add_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    start_at: str = typer.Option(..., "--start", metavar="DATETIME"),
    duration_value: str = typer.Option(..., "--duration", metavar="DURATION"),
    task: str | None = typer.Option(None, "--task", metavar="TASK_REF"),
    description: str | None = typer.Option(None, "--description", metavar="TEXT"),
    billable: bool = typer.Option(False, "--billable"),
    non_billable: bool = typer.Option(False, "--non-billable"),
) -> None:
    """Create one completed entry and verify every explicitly requested field."""

    state = _state(context)

    def operation() -> TimeMutationResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        requested_start = parse_time_boundary(start_at, label="START", allow_date=False)
        requested_duration = parse_duration(duration_value)
        task_id = parse_task_ref(task) if task is not None else None
        billable_state = _billable_value(billable=billable, non_billable=non_billable)
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).add(
                native_workspace_id,
                start=requested_start,
                duration=requested_duration,
                task_id=task_id,
                description=description,
                billable=billable_state,
            ),
        )

    _execute(
        state,
        operation,
        json_result=_time_mutation_json,
        text_result=lambda result: f"Added {result.entry.get('id')}",
    )


@time_app.command("update")
def update_time(
    context: typer.Context,
    entry_id: str = typer.Argument(..., metavar="ENTRY_ID"),
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    description: str | None = typer.Option(None, "--description", metavar="TEXT"),
    task: str | None = typer.Option(None, "--task", metavar="TASK_REF"),
    start_at: str | None = typer.Option(None, "--start", metavar="DATETIME"),
    duration_value: str | None = typer.Option(None, "--duration", metavar="DURATION"),
    billable: bool = typer.Option(False, "--billable"),
    non_billable: bool = typer.Option(False, "--non-billable"),
) -> None:
    """Update supported fields with the smallest valid body and verified readback."""

    state = _state(context)

    def operation() -> TimeMutationResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        native_entry_id = validate_native_id(entry_id, label="ENTRY_ID")
        billable_state = _billable_value(billable=billable, non_billable=non_billable)
        if all(
            value is None for value in (description, task, start_at, duration_value, billable_state)
        ):
            raise InvalidOperationError("At least one time-entry field must be provided")
        requested_start = (
            parse_time_boundary(start_at, label="START", allow_date=False)
            if start_at is not None
            else None
        )
        requested_duration = parse_duration(duration_value) if duration_value is not None else None
        task_id = parse_task_ref(task) if task is not None else None
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).update(
                native_workspace_id,
                native_entry_id,
                description=description,
                task_id=task_id,
                start=requested_start,
                duration=requested_duration,
                billable=billable_state,
            ),
        )

    _execute(
        state,
        operation,
        json_result=_time_mutation_json,
        text_result=lambda result: (
            f"Updated {result.entry.get('id')}"
            if result.changed
            else f"{result.entry.get('id')}: no change"
        ),
    )


@time_app.command("delete")
def delete_time(
    context: typer.Context,
    entry_id: str = typer.Argument(..., metavar="ENTRY_ID"),
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    yes: bool = typer.Option(False, "--yes", help="Confirm permanent deletion."),
) -> None:
    """Permanently delete a time entry after explicit confirmation."""

    state = _state(context)
    if not yes:
        _fail(state, ConfirmationError("Refusing to delete without --yes"))

    def operation() -> str:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        native_entry_id = validate_native_id(entry_id, label="ENTRY_ID")

        def remove(client: ClickUpClient) -> str:
            TimeTrackingService(client).delete(native_workspace_id, native_entry_id)
            return native_entry_id

        return _with_client(state, remove)

    _execute(
        state,
        operation,
        json_result=lambda deleted_id: {"deleted": True, "entry_id": deleted_id},
        text_result=lambda deleted_id: f"Deleted {deleted_id}",
    )


def main(args: list[str] | None = None, *, prog_name: str | None = None) -> int:
    """Console-script entry point."""

    arguments = list(sys.argv[1:] if args is None else args)
    resolved_prog_name = prog_name or Path(sys.argv[0]).name or "clickup"
    command = get_command(app)
    try:
        result = command.main(
            args=arguments,
            prog_name=resolved_prog_name,
            standalone_mode=False,
        )
    except ClickException as exc:
        if _json_requested(arguments):
            _emit_json(
                {
                    "error": {"message": exc.format_message(), "type": "usage_error"},
                    "ok": False,
                },
                error=True,
            )
        else:
            exc.show()
        return exc.exit_code
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
